"""
dsh.feedback.feedback —— MessageFeedbackService（ctx.messageFeedback）：消息反馈。

对应 TS 版 message-feedback：

- 反馈记录 = {session_id, seq, kind('up'|'down'), note?, time}；
- 存储经 ctx.storage（domain "message-feedback"；未挂载则仅内存）；
- 每次写入广播 ``message-feedback/updated`` {session_id}；
- Web UI 在助手消息上提供点赞/点踩（POST /api/sessions/{id}/feedback）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..kernel import Service

STORAGE_DOMAIN = "message-feedback"


class MessageFeedbackService(Service):
    """消息反馈服务（ctx.messageFeedback）。"""

    provides = "messageFeedback"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._memory: Dict[str, List[Dict[str, Any]]] = {}

    def apply(self, ctx) -> None:
        ctx.set("messageFeedback", self)
        self._restore()

    def _restore(self) -> None:
        if self.ctx.has("storage"):
            self._memory = self.ctx.storage.domain(STORAGE_DOMAIN)

    def _persist(self) -> None:
        if self.ctx.has("storage"):
            for session_id, records in self._memory.items():
                self.ctx.storage.put(STORAGE_DOMAIN, session_id,
                                     list(records))

    def put(self, session_id: str, seq: int, kind: str,
            note: Optional[str] = None) -> Dict[str, Any]:
        """
        记录一条反馈（kind ∈ up/down）。

        :return: 完整反馈记录。
        """
        if kind not in ("up", "down"):
            raise ValueError(f"invalid feedback kind: {kind!r}")
        record = {"session_id": session_id, "seq": int(seq), "kind": kind,
                  "note": note, "time": int(time.time() * 1000)}
        self._memory.setdefault(session_id, []).append(record)
        self._persist()
        try:
            self.ctx.events.emit("message-feedback/updated",
                                 {"session_id": session_id})
        except Exception:
            pass
        return dict(record)

    def get(self, session_id: str) -> List[Dict[str, Any]]:
        """某会话的全部反馈。"""
        return list(self._memory.get(session_id, []))

    def close(self) -> None:
        self._memory.clear()
