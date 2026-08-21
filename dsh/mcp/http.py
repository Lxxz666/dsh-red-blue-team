"""
dsh.mcp.http —— MCP Streamable HTTP 客户端（对应 TS 版 MCP SSE/HTTP 传输）。

- 单端点 POST JSON-RPC 2.0：``initialize`` → ``notifications/initialized`` →
  ``tools/list`` → ``tools/call``；
- 响应两种形态：``application/json``（单信封）与 ``text/event-stream``
  （SSE 流：按 id 匹配响应，其余为通知只记日志）；
- ``Mcp-Session-Id`` 会话头：initialize 响应带回后自动回传；
- 可选 Bearer 令牌；与 stdio 客户端同接口（list_tools/call_tool），
  插件复用同一套工具包装（safe_schema 降级）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from ..errors import ToolError
from ..kernel import Service
from .mcp import (MAX_TOOL_NAME, PROTOCOL_VERSION, build_mcp_tool_definition)

log = logging.getLogger("dsh.mcp")


class McpHttpClient:
    """Streamable HTTP JSON-RPC 2.0 客户端（JSON / SSE 双形态响应）。"""

    def __init__(self, url: str, token: Optional[str] = None,
                 timeout: float = 60.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._next_id = 0
        self._closed = False

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    # ---- 生命周期 ----

    async def start(self) -> None:
        """initialize 握手（协议版本协商 + 会话 id 捕获）。"""
        init = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dsh-python", "version": "0.1.0"},
        }, timeout=self.timeout)
        version = (init or {}).get("protocolVersion", "")
        if not version.startswith("2024-") and not version.startswith("2025-"):
            log.warning("MCP HTTP server %s protocol %r", self.url, version)
        await self.notify("notifications/initialized")

    async def stop(self) -> None:
        self._closed = True

    # ---- JSON-RPC ----

    async def notify(self, method: str,
                     params: Optional[Dict[str, Any]] = None) -> None:
        """发送通知（无 id；不解析响应体）。"""
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await client.post(self.url, json=payload, headers=self._headers())

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                      timeout: Optional[float] = None) -> Any:
        """
        发送请求并等待响应（JSON 或 SSE 形态）。

        :return: JSON-RPC 信封的 ``result`` 字段（已解包）。
        :raises ToolError: 端点缺失 / 响应非法 / 错误信封 / 流无响应。
        """
        if self._closed:
            raise ToolError("MCP HTTP client closed", code="MCP_CLOSED")
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params or {}}
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            async with client.stream("POST", self.url, json=payload,
                                     headers=self._headers()) as response:
                if response.status_code == 404:
                    raise ToolError(f"MCP endpoint not found: {self.url}",
                                    code="MCP_NOT_FOUND")
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise ToolError(
                        f"MCP HTTP {response.status_code}: {body[:200]}",
                        code="MCP_HTTP_ERROR")
                session_header = response.headers.get("Mcp-Session-Id")
                if session_header:
                    self.session_id = session_header
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    envelope = await self._parse_sse(response, request_id)
                else:
                    body = await response.aread()
                    try:
                        envelope = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise ToolError(
                            f"MCP invalid JSON response: "
                            f"{body[:200]!r}", code="MCP_BAD_RESPONSE") from exc
                if isinstance(envelope, dict) and "error" in envelope:
                    raise ToolError(f"MCP error: {envelope['error']}",
                                    code="MCP_ERROR")
                return (envelope or {}).get("result")

    async def _parse_sse(self, response: Any, request_id: int) -> Any:
        """解析 SSE 流：按 id 匹配响应；其余事件 = 通知（记日志）。"""
        found: Optional[Dict[str, Any]] = None
        async for line in response.aiter_lines():
            if not line or line.startswith(":"):
                continue
            data = line[len("data:"):].strip() if line.startswith("data:") \
                else line.strip()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:
                found = message
            elif "method" in message:
                log.debug("MCP HTTP notification: %s", message.get("method"))
        if found is None:
            raise ToolError(
                "MCP SSE stream ended without a matching response",
                code="MCP_SSE_EOF")
        return found

    # ---- 工具发现与调用 ----

    async def list_tools(self) -> List[Dict[str, Any]]:
        response = await self.request("tools/list")
        return (response or {}).get("tools") or []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        response = await self.request("tools/call",
                                      {"name": name, "arguments": arguments})
        if isinstance(response, dict) and response.get("isError"):
            raise ToolError(f"MCP tool {name} failed", code="MCP_TOOL_ERROR")
        parts: List[str] = []
        for block in (response or {}).get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)


class McpHttpServerPlugin(Service):
    """HTTP MCP 服务器插件：发现工具并注册（与 stdio 插件同构）。"""

    inject = ("tools",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._client: Optional[McpHttpClient] = None
        self._disposers: List[Any] = []
        self._start_hook = None

    def apply(self, ctx) -> None:
        url = (self.config or {}).get("url")
        if not url:
            raise ToolError("MCP HTTP plugin needs config.url",
                            code="MCP_BAD_CONFIG")
        prefix = str((self.config or {}).get("prefix", ""))
        token = (self.config or {}).get("token")
        timeout = float((self.config or {}).get("timeout", 60))

        async def mount() -> None:
            self._client = McpHttpClient(url, token=token, timeout=timeout)
            await self._client.start()
            for tool in await self._client.list_tools():
                definition = build_mcp_tool_definition(
                    tool.get("name", "tool"), tool.get("description", ""),
                    tool.get("inputSchema"), self._client, prefix)
                self._disposers.append(ctx.tools.register(definition))

        async def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
            if self._client is not None:
                await self._client.stop()
                self._client = None
        self._start_hook = mount
        return cleanup

    async def start(self) -> None:
        if self._start_hook is not None:
            await self._start_hook()

    def close(self) -> None:
        pass  # 无进程可停；工具注销由 disposer 承担
