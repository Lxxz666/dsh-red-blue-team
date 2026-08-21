"""
dsh.mcp.mcp —— MCP 最小客户端（stdio JSON-RPC 2.0）。

对应 TS 版「MCP：一个插件对应一个服务器：discover tools → ctx.tools.register()」：

- 传输：stdio，newline-delimited JSON-RPC 2.0；
- 流程：initialize（协议版本协商）→ initialized 通知 → tools/list 发现工具
  → 每个工具包装为 ToolDefinition 注册进 ctx.tools（schema 超出本框架子集时
  安全降级为开放对象，参数原样透传）→ tools/call 转发调用；
- 插件卸载时停止服务器进程并注销全部工具。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service
from ..tools import ToolDefinition
from ..tools.definition import ToolOutputDefinition, _default_render
from ..tools.schema import assert_supported_schema

log = logging.getLogger("dsh.mcp")

PROTOCOL_VERSION = "2024-11-05"
MAX_TOOL_NAME = 128


def safe_schema(schema: Any) -> Dict[str, Any]:
    """
    把远端输入 schema 转换为本框架可校验的 schema。

    超出强制子集的关键字（如 anyOf）→ 降级为开放对象（校验交由远端服务器）。
    """
    if schema is None:
        return {"type": "object"}
    try:
        assert_supported_schema(schema)
        return dict(schema)
    except ToolError:
        return {"type": "object"}


class McpClient:
    """stdio JSON-RPC 客户端。"""

    def __init__(self, command: List[str], env: Optional[Dict[str, str]] = None,
                 timeout: float = 60.0) -> None:
        self.command = list(command)
        self.env = env
        self.timeout = timeout
        self.process: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动服务器进程并完成 initialize 握手。"""
        env = {**os.environ, **(self.env or {})}
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
        except OSError as exc:
            raise ToolError(f"cannot start MCP server {self.command[0]!r}: {exc}",
                            code="MCP_SPAWN_FAILED") from exc
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop())
        init = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dsh-python", "version": "0.1.0"},
        }, timeout=self.timeout)
        version = (init or {}).get("protocolVersion", "")
        if not version.startswith("2024-") and not version.startswith("2025-"):
            log.warning("MCP server %s protocol %r", self.command[0], version)
        await self.notify("notifications/initialized")

    async def stop(self) -> None:
        """停止读取循环并终止服务器进程。"""
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self.process.kill()
                await self.process.wait()
        self.process = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ToolError("MCP server stopped",
                                               code="MCP_CLOSED"))
        self._pending.clear()

    # ---- JSON-RPC ----

    async def _write(self, message: Dict[str, Any]) -> None:
        """写一行 JSON-RPC 并 flush（asyncio stdin 必须 drain 才到达对端）。"""
        line = json.dumps(message, ensure_ascii=False)
        self.process.stdin.write((line + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def notify(self, method: str,
                     params: Optional[Dict[str, Any]] = None) -> None:
        """发送通知（无 id，不等待响应）。"""
        await self._write({"jsonrpc": "2.0", "method": method,
                           "params": params or {}})

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                      timeout: Optional[float] = None) -> Any:
        """
        发送请求并等待响应。

        :return: JSON-RPC 信封的 ``result`` 字段（已解包）。
        :raises ToolError: 响应含 error / 超时。
        """
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "method": method, "params": params or {}})
            try:
                envelope = await asyncio.wait_for(
                    asyncio.shield(future), timeout=timeout or self.timeout)
            except asyncio.TimeoutError as exc:
                raise ToolError(f"MCP request {method} timed out",
                                code="MCP_TIMEOUT") from exc
        finally:
            # 写入失败/超时/异常一律清理 pending，绝不滞留 future
            self._pending.pop(request_id, None)
        if isinstance(envelope, dict) and "error" in envelope:
            error = envelope["error"]
            raise ToolError(f"MCP error: {error}", code="MCP_ERROR")
        return (envelope or {}).get("result")

    async def _read_loop(self) -> None:
        """读取 stdout 行，分发响应/通知（通知暂只记录日志）。"""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    log.warning("MCP non-JSON line ignored")
                    continue
                if "id" in message and message["id"] is not None:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    log.debug("MCP notification: %s", message.get("method"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MCP reader crashed for %s", self.command[0])

    # ---- 工具发现与调用 ----

    async def list_tools(self) -> List[Dict[str, Any]]:
        """tools/list → 工具定义列表。"""
        response = await self.request("tools/list")
        return (response or {}).get("tools") or []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """
        tools/call → 拼接 content 文本。

        :raises ToolError: 服务器报错/协议错误。
        """
        response = await self.request("tools/call",
                                      {"name": name, "arguments": arguments})
        if isinstance(response, dict) and response.get("isError"):
            raise ToolError(f"MCP tool {name} failed", code="MCP_TOOL_ERROR")
        content = (response or {}).get("content") or []
        parts: List[str] = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)


def build_mcp_tool_definition(name: str, description: str,
                              input_schema: Any, client: McpClient,
                              prefix: str = "") -> ToolDefinition:
    """把 MCP 工具包装为本框架 ToolDefinition。"""
    full_name = (prefix + name)[:MAX_TOOL_NAME]

    async def execute(args: Dict[str, Any], run_ctx) -> str:
        return await client.call_tool(name, dict(args or {}))

    return ToolDefinition(
        name=full_name,
        description=f"[MCP:{name}] {description or ''}".strip(),
        parameters=safe_schema(input_schema),
        output=ToolOutputDefinition(schema=None, render=_default_render),
        execute=execute)


class McpServerPlugin(Service):
    """MCP 服务器插件：发现工具并注册（base bundle 示例行，默认禁用）。"""

    inject = ("tools",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._client: Optional[McpClient] = None
        self._disposers: List[Any] = []
        self._mounted = False
        self._start_hook = None

    def apply(self, ctx) -> None:
        command = (self.config or {}).get("command")
        if not command:
            raise ToolError("MCP plugin needs config.command (argv list)",
                            code="MCP_BAD_CONFIG")
        prefix = str((self.config or {}).get("prefix", ""))
        timeout = float((self.config or {}).get("timeout", 60))

        async def mount() -> None:
            self._client = McpClient(list(command),
                                     env=(self.config or {}).get("env"),
                                     timeout=timeout)
            await self._client.start()
            for tool in await self._client.list_tools():
                definition = build_mcp_tool_definition(
                    tool.get("name", "tool"), tool.get("description", ""),
                    tool.get("inputSchema"), self._client, prefix)
                self._disposers.append(ctx.tools.register(definition))
            self._mounted = True

        async def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
            if self._client is not None:
                await self._client.stop()
                self._client = None
            self._mounted = False
        # 挂载在 start 钩子（PluginTree.mount 会 await start）
        self._start_hook = mount
        return cleanup

    async def start(self) -> None:
        if self._start_hook is not None:
            await self._start_hook()

    def close(self) -> None:
        if self._client is not None:
            import asyncio
            try:
                asyncio.get_running_loop().create_task(self._client.stop())
            except RuntimeError:
                pass
