"""redteam.adapters.mcp_adapter —— MCP（Model Context Protocol）目标适配器。

适用于通过 stdio 暴露 MCP 工具面的 agent 业务系统（复用 dsh.mcp 最小客户端）：

- 侦察：启动服务器 → tools/list 发现工具清单（写入 CapabilityProbe）；
- 攻击：API 型样本 path=工具名、body=工具参数 → tools/call 注入恶意参数，
  工具返回文本作为判定证据；
- 对话型样本 → UnsupportedSurface（跳过，不误报 error）；
- 无 reset/副作用探测（MCP 目标状态由目标自身管理）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from dsh.errors import ToolError
from dsh.mcp.mcp import McpClient

from ..errors import AdapterError, UnsupportedSurface
from ..models import CapabilityProbe, ConcreteSample, TargetResponse
from .base import SideEffectSnapshot, TargetAdapter

log = logging.getLogger("redteam.adapters.mcp")


class McpAdapter(TargetAdapter):
    """stdio MCP 目标适配器。"""

    kind = "mcp"

    def __init__(self, base_url: str, command: List[str],
                 headers: Optional[Dict[str, str]] = None,
                 timeout_s: float = 60.0) -> None:
        super().__init__(base_url, headers, timeout_s)
        self.command = list(command)
        self._client = McpClient(self.command, timeout=timeout_s)
        self._tools: List[Dict[str, Any]] = []
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return
        await self._client.start()
        self._tools = await self._client.list_tools()
        self._started = True
        log.info("MCP 目标已连接：发现 %d 个工具（%s）", len(self._tools),
                 ", ".join(t.get("name", "?") for t in self._tools[:8]))

    # ---- 攻击发送 ----

    async def send(self, sample: ConcreteSample) -> TargetResponse:
        await self._ensure_started()
        if sample.surface == "chat":
            raise UnsupportedSurface(
                "MCP 适配器不支持对话样本（对话型目标请使用 http 适配器）")
        tool_name = sample.path.strip("/") or sample.sample.id
        arguments = {str(k): v for k, v in (sample.body or {}).items()}
        try:
            text = await self._client.call_tool(tool_name, arguments)
        except ToolError as exc:
            # 工具报错 = 目标拒绝了攻击：作为正常响应参与判定（一般判 failed）
            return TargetResponse(status=200, text=f"工具调用失败: {exc}",
                                  meta={"tool": tool_name})
        return TargetResponse(status=200, text=text or "",
                              meta={"tool": tool_name})

    async def send_text(self, text: str, role: str = "customer",
                        session_id: Optional[str] = None) -> TargetResponse:
        raise UnsupportedSurface("MCP 适配器不支持自由文本对话")

    # ---- 侦察 / 生命周期 ----

    async def probe(self) -> CapabilityProbe:
        probe = CapabilityProbe()
        try:
            await self._ensure_started()
            probe.reachable = True
            probe.notes = [f"MCP 工具: {t.get('name', '?')}"
                           for t in self._tools]
            probe.banner = "mcp-stdio"
        except Exception as exc:
            probe.notes = [f"MCP 连接失败: {exc}"]
        return probe

    async def check_side_effect(self) -> SideEffectSnapshot:
        return SideEffectSnapshot(available=False)

    async def reload_guards(self) -> bool:
        return False

    async def close(self) -> None:
        if self._started:
            try:
                await self._client.stop()
            except Exception as exc:
                log.debug("MCP 关闭异常（忽略）: %s", exc)
            self._started = False
