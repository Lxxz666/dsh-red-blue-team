"""
dsh.interaction.user_questions —— UserQuestionsService（ctx.userQuestions）+ ask_user 工具。

对应 TS 版 user-questions 缝 + tool-ask-user：

- ``set_channel(callback)``：注册文本问答通道（Web UI 弹窗带输入框）；
  ``ask(question)`` 经通道返回用户文本；无通道（headless）→ ToolError(NO_CHANNEL)。
- ``ask_user`` 工具让模型向人提问（区别于权限审批的 bool 通道）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.interaction")


class UserQuestionsService(Service):
    """文本问答通道（ctx.userQuestions）。"""

    provides = "userQuestions"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._channel = None

    def apply(self, ctx) -> None:
        ctx.set("userQuestions", self)

    def set_channel(self, callback) -> None:
        """
        注册问答通道：``async def callback(question, detail) -> str|None``。

        返回 None = 用户未作答（提问失败）。
        """
        self._channel = callback

    async def ask(self, question: str, detail: str = "") -> str:
        """
        向人提问并等待文本回答。

        :raises ToolError: 无通道（headless 模式）。
        """
        if self._channel is None:
            raise ToolError("no user-question channel (headless mode)",
                            code="NO_CHANNEL")
        try:
            answer = self._channel(question, detail)
            if asyncio.iscoroutine(answer):
                answer = await answer
        except Exception as exc:
            log.exception("user-question channel failed")
            raise ToolError(f"question failed: {exc}", code="CHANNEL_ERROR")
        if answer is None:
            raise ToolError("question was not answered", code="UNANSWERED")
        return str(answer)


def build_ask_user_tool() -> Any:
    """构造 ask_user 工具。"""

    @define_tool(
        name="ask_user",
        description="向用户提出一个文本问题并等待回答。",
        parameters={"question": {"type": "string", "required": True},
                    "detail": {"type": "string",
                               "description": "补充上下文（可选）"}},
        output={"type": "string"},
        timeout_ms=300_000)
    async def ask_user(args, run_ctx):
        agent = run_ctx.execution.agent
        ctx = agent.ctx if agent is not None else run_ctx.root_ctx
        if not ctx.has("userQuestions"):
            raise ToolError("userQuestions service not mounted", code="NO_CHANNEL")
        return await ctx.userQuestions.ask(args["question"],
                                           args.get("detail", ""))

    return ask_user


class ToolAskUserPlugin(Service):
    """注册 ask_user 工具的插件。"""

    inject = ("tools", "userQuestions")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        self._disposer = ctx.tools.register(build_ask_user_tool())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
