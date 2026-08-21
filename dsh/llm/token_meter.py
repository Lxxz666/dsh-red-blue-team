"""
dsh.llm.token_meter —— TokenMeterService（ctx.tokenMeter）：回放 token 计量。

对应 TS 版 token-meter：粗粒度估算（4 字符 ≈ 1 token）。
compaction 的压力检测经此估算（未挂载时回退本地 estimate_tokens）。
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..kernel import Service

TOKENS_PER_CHAR = 4


class TokenMeterService(Service):
    """Token 计量服务（ctx.tokenMeter）。"""

    provides = "tokenMeter"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)

    def apply(self, ctx) -> None:
        ctx.set("tokenMeter", self)

    def estimate(self, messages: List[Any]) -> int:
        """
        粗估一组派生消息的 token 数（文本块长度 // TOKENS_PER_CHAR）。

        :param messages: Message 列表（含 content 块）。
        :return: 估算 token 数。
        """
        total = 0
        for message in messages:
            for block in getattr(message, "content", []):
                text = getattr(block, "text", None)
                if text:
                    total += len(text) // TOKENS_PER_CHAR
        return total

    def count_text(self, text: str) -> int:
        """粗估一段文本的 token 数。"""
        return len(text) // TOKENS_PER_CHAR
