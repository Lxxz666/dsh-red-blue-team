"""
dsh.context.time_context —— 时间上下文注入（对应 TS 版 time-context）。

注册动态 PromptContext（order 10，persona 之后、工具指引之前），
每次组装把当前日期/时间注入模型上下文。
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

from ..kernel import Service
from ..prompt import PromptContext


class TimeContextPlugin(Service):
    """当前时间注入插件。"""

    inject = ("systemPrompt",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        context = PromptContext(
            name="time", order=10,
            text=lambda _ac: "当前时间：" + time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        self._disposer = ctx.systemPrompt.context(context)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
