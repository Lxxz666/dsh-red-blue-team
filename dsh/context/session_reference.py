"""
dsh.context.session_reference —— SessionReferenceResolver
（ctx.sessionReferenceResolver）。

对应 TS 版 session-reference：把其他会话准备为**有界、只读**快照，作为带
来源信息、面向模型的上下文。消费 ctx.sessionQuery / ctx.sessionPersistence
（后端无关的 compact 检查点标记 = 最后一个 compaction/summary 事件），
不需要 SQLite FTS。支持跨会话 mention 的宿主可主动启用本服务。

- ``resolve(reference)``：reference = {"session_id", "before_seq"?,
  "max_blocks"?, "max_chars"?} → {session_id, header, blocks, truncated}；
- 快照起点 = 最后一个 compact 检查点（compaction/summary）之后；只投影
  会话表面（user/message、assistant/message、tool/result、compaction/summary）；
- 边界：从**尾部**取最多 max_blocks 个块（每块内容有上限），累计字符 ≤
  max_chars；越界截断并置 truncated；
- 只读：返回全新 dict/字符串，绝不触碰源会话。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..errors import SessionError
from ..kernel import Service
from ..session.events import SessionEvent

log = logging.getLogger("dsh.context")

MAX_BLOCK_CHARS = 400
"""单块内容上限（超长工具结果截断）。"""


class SessionReferenceResolver(Service):
    """跨会话有界快照准备（ctx.sessionReferenceResolver）。"""

    provides = "sessionReferenceResolver"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.max_blocks = int((config or {}).get("max_blocks", 20))
        self.max_chars = int((config or {}).get("max_chars", 8000))

    def apply(self, ctx) -> None:
        ctx.set("sessionReferenceResolver", self)

    # ---- 内部 ----

    async def _load_events(self, session_id: str):
        """(header, [SessionEvent])：活跃会话优先，否则持久化检视。"""
        if self.ctx.has("sessions"):
            live = self.ctx.sessions.get(session_id)
            if live is not None:
                return live.header, list(live._events)
        if not self.ctx.has("sessionPersistence"):
            raise SessionError("sessionPersistence not mounted")
        header, rows = await self.ctx.sessionPersistence.inspect(session_id)
        events = [SessionEvent.from_json(row) for row in rows]
        return header, events

    def _compact_checkpoint(self, events: List[SessionEvent]) -> int:
        """最后一个 compaction/summary 的 seq（无则 -1）。"""
        checkpoint = -1
        for event in events:
            if event.type == "compaction/summary":
                checkpoint = event.seq
        return checkpoint

    def _project_block(self, event: SessionEvent) -> Optional[Dict[str, Any]]:
        data = event.data or {}
        if event.type == "user/message":
            content = str(data.get("content") or "")
            if not content:
                return None
            return {"source": data.get("source") or {"kind": "user"},
                    "content": content[:MAX_BLOCK_CHARS]}
        if event.type == "assistant/message":
            parts: List[str] = []
            for block in data.get("blocks") or []:
                kind = block.get("kind")
                if kind in ("text", "reasoning") and block.get("text"):
                    parts.append(block["text"])
                elif kind == "tool-call":
                    parts.append(f"[调用工具 {block.get('name', '?')}]")
            content = "\n".join(parts).strip()
            if not content:
                return None
            return {"source": {"kind": "model",
                               "provider": data.get("provider"),
                               "model": data.get("model")},
                    "content": content[:MAX_BLOCK_CHARS]}
        if event.type == "tool/result":
            content = str(data.get("content") or "")
            return {"source": {"kind": "tool", "name": data.get("name", "?")},
                    "content": content[:MAX_BLOCK_CHARS]}
        if event.type == "compaction/summary":
            content = str(data.get("summary") or "")
            return {"source": {"kind": "compaction"},
                    "content": "（此前上下文已压缩）\n"
                    + content[:MAX_BLOCK_CHARS]}
        return None

    # ---- 对外 API ----

    async def resolve(self, reference: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备一个有界只读快照。

        :param reference: {"session_id", "before_seq"?, "max_blocks"?,
            "max_chars"?}。
        :return: {"session_id", "header", "blocks", "truncated"}。
        :raises SessionError: 会话不存在 / 无持久化后端。
        """
        session_id = str(reference.get("session_id") or "")
        if not session_id:
            raise SessionError("session reference needs a session_id")
        header, events = await self._load_events(session_id)
        start = self._compact_checkpoint(events) + 1
        end = (int(reference["before_seq"]) + 1
               if reference.get("before_seq") is not None else len(events))
        window = events[start:max(start, end)]
        blocks: List[Dict[str, Any]] = []
        for event in window:
            block = self._project_block(event)
            if block is not None:
                blocks.append(block)

        max_blocks = int(reference.get("max_blocks") or self.max_blocks)
        max_chars = int(reference.get("max_chars") or self.max_chars)
        kept: List[Dict[str, Any]] = []
        total = 0
        truncated = False
        for block in reversed(blocks):
            if len(kept) >= max_blocks:
                truncated = True
                break
            size = len(block["content"])
            if total + size > max_chars:
                truncated = True
                break
            kept.append(block)
            total += size
        kept.reverse()
        return {"session_id": session_id, "header": {
                    "id": header.id, "created_at": header.created_at,
                    "cwd": header.cwd, "origin": header.origin,
                    "parent_session": header.parent_session,
                    "delegation_depth": header.delegation_depth},
                "blocks": kept, "truncated": truncated}

    def close(self) -> None:
        pass
