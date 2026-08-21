"""
dsh.llm.stream —— 原始 StreamChunk 协议与组装器。

对应 TS 版 StreamChunk 判别联合。适配器产出块，组装器把块折叠成
assistant 消息（blocks + finish_reason + usage），循环把原始块照录日志（回放保真）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StreamChunk:
    """
    一个原始流式块。``type`` ∈ text-delta / reasoning-delta / tool-call / usage / finish。
    """

    type: str
    text: Optional[str] = None
    tool_call: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None

    @staticmethod
    def text_delta(text: str) -> "StreamChunk":
        return StreamChunk(type="text-delta", text=text)

    @staticmethod
    def reasoning_delta(text: str) -> "StreamChunk":
        return StreamChunk(type="reasoning-delta", text=text)

    @staticmethod
    def tool_call_chunk(index: int, call_id: str, name: str,
                        arguments: str) -> "StreamChunk":
        """便捷构造：工具调用块。"""
        return StreamChunk(type="tool-call",
                           tool_call={"index": index, "call_id": call_id,
                                      "name": name, "arguments": arguments})

    @staticmethod
    def usage_chunk(usage: Dict[str, Any]) -> "StreamChunk":
        return StreamChunk(type="usage", usage=usage)

    @staticmethod
    def finish(reason: str) -> "StreamChunk":
        return StreamChunk(type="finish", finish_reason=reason)


class AssistantAssembler:
    """
    把块流折叠为组装好的助手消息（含工具调用、usage、finish_reason）。
    """

    def __init__(self) -> None:
        self.text_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self.usage: Optional[Dict[str, Any]] = None
        self.finish_reason: Optional[str] = None

    def feed(self, chunk: StreamChunk) -> None:
        """喂入一个块。"""
        if chunk.type == "text-delta" and chunk.text:
            self.text_parts.append(chunk.text)
        elif chunk.type == "reasoning-delta" and chunk.text:
            self.reasoning_parts.append(chunk.text)
        elif chunk.type == "tool-call" and chunk.tool_call:
            call = chunk.tool_call
            index = int(call.get("index", 0))
            slot = self.tool_calls.setdefault(
                index, {"call_id": call.get("call_id", ""),
                        "name": call.get("name", ""), "arguments": ""})
            if call.get("name"):
                slot["name"] = call["name"]
            if call.get("arguments"):
                slot["arguments"] += call["arguments"]
        elif chunk.type == "usage" and chunk.usage:
            self.usage = dict(chunk.usage)
        elif chunk.type == "finish":
            self.finish_reason = chunk.finish_reason

    def blocks(self) -> List[Any]:
        """组装内容块（text/reasoning 合并 + tool-call 块）。"""
        from .messages import ContentBlock
        blocks: List[ContentBlock] = []
        text = "".join(self.text_parts)
        if text:
            blocks.append(ContentBlock.text_block(text))
        reasoning = "".join(self.reasoning_parts)
        if reasoning:
            blocks.append(ContentBlock(kind="reasoning", text=reasoning))
        for index in sorted(self.tool_calls.keys()):
            call = self.tool_calls[index]
            blocks.append(ContentBlock.tool_call_block(
                call["call_id"] or f"call-{index}", call["name"],
                call["arguments"] or "{}"))
        return blocks

    def finish(self) -> Dict[str, Any]:
        """最终结果: {blocks, finish_reason, usage}。"""
        return {"blocks": self.blocks(), "finish_reason": self.finish_reason,
                "usage": self.usage}
