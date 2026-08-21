"""
dsh.llm.mock —— Mock 适配器：无密钥的确定性回复（headless 演示与测试用）。

两种模式:

1. 默认回显: 把最后一条用户消息文本原样回显；
2. 脚本模式: 提供 ``script``（dict 列表），每个 dict 描述一个「回合」的回复:
   ``{"text": "..."}``、``{"tool": {"name": ..., "arguments": {...}}}``、
   ``{"chunks": [StreamChunk, ...]}``。回合按调用次数推进，脚本耗尽则回显。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from .adapters import LlmAdapter, LlmRequest
from .stream import StreamChunk

log = __import__("logging").getLogger("dsh.llm")


class MockAdapter(LlmAdapter):
    """确定性 Mock 适配器（provider 名 ``mock``）。"""

    name = "mock"
    context_window = 8192

    def __init__(self, script: Optional[List[Dict[str, Any]]] = None,
                 text: Optional[str] = None) -> None:
        """
        :param script: 脚本回合列表（按调用次序消费）。
        :param text: 固定回复文本（脚本耗尽后的回退）。
        """
        self.script = list(script or [])
        self.fallback_text = text
        self.calls: List[LlmRequest] = []

    def _last_user_text(self, request: LlmRequest) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.plain_text()
        return ""

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
        """按脚本/回显产出块流。"""
        self.calls.append(request)
        if self.script:
            turn = self.script.pop(0)
            if "chunks" in turn:
                for chunk in turn["chunks"]:
                    yield chunk
                yield StreamChunk.finish("stop")
                return
            if "tool" in turn:
                tool = turn["tool"]
                yield StreamChunk.tool_call_chunk(
                    index=0, call_id=tool.get("call_id", "mock-call-0"),
                    name=tool["name"],
                    arguments=json.dumps(tool.get("arguments", {}),
                                         ensure_ascii=False))
                yield StreamChunk.finish("tool_calls")
                return
            text = turn.get("text", "")
        elif self.fallback_text is not None:
            text = self.fallback_text
        else:
            text = f"[mock] 回显: {self._last_user_text(request)}"
        for token in text:  # 逐字符产出，模拟流式
            yield StreamChunk.text_delta(token)
        yield StreamChunk.finish("stop")
