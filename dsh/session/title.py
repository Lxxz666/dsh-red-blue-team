"""
dsh.session.title —— SessionTitleService（ctx.sessionTitle）：会话标题 provider。

对应 TS 版 session-title 缝：注册表里唯一的 provider 决定标题；
默认策略 = 首条 user 消息截断 60 字符（session-title-first-prompt 的简化）。
Web UI 侧栏与恢复列表经此取标题。
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..kernel import Service


class SessionTitleService(Service):
    """会话标题 provider 注册表（ctx.sessionTitle）。"""

    provides = "sessionTitle"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._provider = None
        self._max_len = int((config or {}).get("max_len", 60))

    def apply(self, ctx) -> None:
        ctx.set("sessionTitle", self)

    def set_provider(self, provider) -> None:
        """
        注册唯一标题 provider（签名 ``(session, messages) -> str|None``）。

        返回 None 表示该 provider 放弃（回退默认策略）。
        """
        self._provider = provider

    def title_for(self, session: Any, messages: Optional[List[Any]] = None) -> str:
        """计算标题：provider → 首条用户消息截断 → 会话 id 前缀。"""
        if messages is None:
            messages = session.derive_messages()
        if self._provider is not None:
            try:
                title = self._provider(session, messages)
                if title:
                    return title
            except Exception:
                pass
        for message in messages:
            if message.role == "user":
                text = message.plain_text().strip()
                if text:
                    return text[:self._max_len]
        return session.id[:16]
