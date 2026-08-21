"""
dsh.session.query —— SessionQueryService（ctx.sessionQuery）：会话读取、搜索、过滤。

对应 TS 版 session-query（+ session-query-sqlite 的轻量实现）：

- ``list(origin, limit)``：按 origin（'subagent' 等）过滤会话头；
- ``search(text, limit)``：全量事件文本子串搜索（返回会话 + 命中位置）；
- ``trace(session_id)``：重建会话（header + 事件，走 persistence.inspect）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..errors import SessionError
from ..kernel import Service

log = logging.getLogger("dsh.session")


class SessionQueryService(Service):
    """会话查询服务（ctx.sessionQuery）。"""

    provides = "sessionQuery"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._default_limit = int((config or {}).get("limit", 50))

    def apply(self, ctx) -> None:
        ctx.set("sessionQuery", self)

    def _persistence(self):
        if not self.ctx.has("sessionPersistence"):
            raise SessionError("sessionPersistence not mounted")
        return self.ctx.sessionPersistence

    async def list(self, origin: Optional[str] = None,
                   limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        列出已持久化会话（header 摘要）。

        :param origin: 过滤 SessionHeader.origin（如 'subagent'）。
        :param limit: 上限（默认配置 50）。
        """
        persistence = self._persistence()
        limit = limit or self._default_limit
        out: List[Dict[str, Any]] = []
        for session_id in await persistence.list_ids():
            if len(out) >= limit:
                break
            try:
                header, events = await persistence.inspect(session_id)
            except Exception:
                log.exception("inspect failed for %s", session_id)
                continue
            if origin is not None and header.origin != origin:
                continue
            out.append({"id": header.id, "created_at": header.created_at,
                        "cwd": header.cwd, "origin": header.origin,
                        "parent_session": header.parent_session,
                        "delegation_depth": header.delegation_depth,
                        "events": len(events)})
        return out

    async def search(self, text: str, limit: Optional[int] = None
                     ) -> List[Dict[str, Any]]:
        """
        全文搜索：user/message 与 assistant/message 的文本子串命中。

        :return: [{"session_id", "matches": [{"seq", "time", "text"}]}]。
        """
        persistence = self._persistence()
        limit = limit or self._default_limit
        results: List[Dict[str, Any]] = []
        needle = text.lower()
        for session_id in await persistence.list_ids():
            if len(results) >= limit:
                break
            try:
                _header, events = await persistence.inspect(session_id)
            except Exception:
                continue
            matches: List[Dict[str, Any]] = []
            for event in events:
                if event["type"] in ("user/message", "assistant/message",
                                     "compaction/summary"):
                    data = event.get("data") or {}
                    haystack = str(data.get("content", ""))
                    if not haystack and data.get("summary"):
                        haystack = str(data["summary"])
                    if needle in haystack.lower():
                        matches.append({"seq": event["seq"],
                                        "time": event["time"],
                                        "text": haystack[:200]})
            if matches:
                results.append({"session_id": session_id,
                                "matches": matches})
        return results

    async def trace(self, session_id: str) -> Dict[str, Any]:
        """
        重建一个会话的完整轨迹。

        :return: {"header": {...}, "events": [事件行]}。
        :raises SessionError: 会话不存在。
        """
        persistence = self._persistence()
        header, events = await persistence.inspect(session_id)
        return {"header": {
            "id": header.id, "created_at": header.created_at,
            "cwd": header.cwd, "origin": header.origin,
            "parent_session": header.parent_session,
            "delegation_depth": header.delegation_depth,
            "agent_preset": header.agent_preset,
        }, "events": events}
