"""
dsh.compaction.pruner —— ToolResultPrunerService（ctx.toolResultPruner）：
模型无关的工具结果修剪。

对应 TS 版 compaction-tool-result-pruner：压缩前的可选修剪步骤——
把超长 tool-result 块截断到 max_chars（带截断标记），降低摘要输入体积。
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..kernel import Service
from ..llm.messages import ContentBlock, Message


class ToolResultPrunerService(Service):
    """工具结果修剪服务（ctx.toolResultPruner）。"""

    provides = "toolResultPruner"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.max_chars = int((config or {}).get("max_chars", 4000))

    def apply(self, ctx) -> None:
        ctx.set("toolResultPruner", self)

    def prune(self, messages: List[Message]) -> List[Message]:
        """
        修剪消息列表中的超长 tool-result 块（重建新消息，不改原对象）。

        :return: 修剪后的消息列表。
        """
        out: List[Message] = []
        for message in messages:
            blocks = []
            changed = False
            for block in message.content:
                if block.kind == "tool-result":
                    text = str(block.content or "")
                    if len(text) > self.max_chars:
                        truncated = text[:self.max_chars] + \
                            f"\n...[截断 {len(text) - self.max_chars} 字符]"
                        block = ContentBlock(
                            kind="tool-result",
                            tool_call_id=block.tool_call_id,
                            content=truncated, is_error=block.is_error)
                        changed = True
                blocks.append(block)
            if changed:
                out.append(Message(id=message.id, role=message.role,
                                   content=blocks, source=dict(message.source)))
            else:
                out.append(message)
        return out

    def prune_text(self, text: str) -> str:
        """单段文本修剪。"""
        if len(text) <= self.max_chars:
            return text
        return text[:self.max_chars] + f"\n...[截断 {len(text) - self.max_chars} 字符]"
