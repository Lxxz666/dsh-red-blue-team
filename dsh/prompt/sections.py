"""
dsh.prompt.sections —— 内置提示分节：harness 身份 + persona（可被用户 patch 覆盖）。

对应 dsh 的约定：-100 = harness 身份（在所有其它分节之前），0 = persona。
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..kernel import Service
from .system_prompt import PromptSection


class PersonaPlugin(Service):
    """注册 harness 身份与 persona 分节。"""

    inject = ("systemPrompt",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        self._register_sections()

        def cleanup() -> None:
            self._unregister_sections()
        return cleanup

    def _register_sections(self) -> None:
        config = self.config or {}
        identity = PromptSection(
            name="harness:identity", order=-100,
            text=config.get("identity",
                            "你是 dsh-python（DeepSeek Harness 的 Python 实现）"
                            "驱动的编码智能体。当前工作目录用 pwd 获取，"
                            "不要从其它路径推断。"))
        persona = PromptSection(
            name="persona", order=0,
            text=config.get("persona", "你是一个严谨、可靠的智能体助手。"))
        self._disposers.append(self.ctx.systemPrompt.section(identity))
        self._disposers.append(self.ctx.systemPrompt.section(persona))

    def _unregister_sections(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()

    def reconfigure(self, config: Optional[dict]) -> bool:
        """
        HMR 消费者：热更新 persona/身份分节（先卸旧、后挂新；失败回滚）。
        """
        old = dict(self.config)
        self._unregister_sections()
        try:
            self.config = config or {}
            self._register_sections()
            return True
        except Exception:
            self._unregister_sections()
            self.config = old
            self._register_sections()
            return False
