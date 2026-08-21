"""
dsh.persistence.service —— SessionPersistence（ctx.sessionPersistence）：持久化缝。

契约（与 TS 版 persistence.md 对齐）:

- 订阅 ``session/event`` 把事件拷贝进每会话缓冲（不阻塞生产者）；
- ``session/flush`` 取消等待并排空到 quiescence（循环的排序/错误观察 checkpoint）；
- 拒绝的背景写保留事件并暂停自动重试（显式 flush 立即重试并报告失败）；
- ``load`` 做崩溃修复：孤儿 turn 以合成 ``turn/end {kind:'interrupted'}`` 关闭（不截断）；
- 格式不符（版本过新/过旧、未知必填事件）→ SessionFormatError 拒绝，绝不静默跳过。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from ..errors import SessionError
from ..kernel import Service
from ..session import Session, SessionEvent, SessionHeader
from ..session.events import EVENT_CATALOG

log = logging.getLogger("dsh.persistence")


class SessionPersistence(Service, ABC):
    """会话持久化抽象服务。"""

    provides = "sessionPersistence"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._buffers: Dict[str, List[SessionEvent]] = {}
        self._buffer_lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._failed: set = set()
        self._closed = False

    def apply(self, ctx) -> None:
        ctx.set("sessionPersistence", self)
        ctx.on("session/event", self._on_event)
        ctx.on("session/flush", self._on_flush)

    # ---- 缓冲 ----

    def _on_event(self, session: Session, event: SessionEvent) -> None:
        """session/event → 入缓冲（同步、不阻塞）。"""
        with self._buffer_lock:
            if session.id in self._failed:
                return  # 暂停自动重试，等显式 flush
            self._buffers.setdefault(session.id, []).append(event)

    async def _on_flush(self, session: Session) -> bool:
        """session/flush → 排空该会话缓冲（失败向上报告）。"""
        await self.flush(session)
        return True

    async def flush(self, session: Session) -> None:
        """把缓冲事件写成一批持久化（幂等）。"""
        with self._buffer_lock:
            batch = self._buffers.pop(session.id, [])
        if not batch:
            return
        async with self._write_lock:
            try:
                await self._write_batch(session, batch)
                self._failed.discard(session.id)
            except Exception as exc:
                log.exception("durable write failed for %s", session.id)
                self._failed.add(session.id)
                with self._buffer_lock:
                    self._buffers.setdefault(session.id, batch)
                raise

    # ---- 后端契约 ----

    @abstractmethod
    async def _write_batch(self, session: Session,
                           events: List[SessionEvent]) -> None:
        """写一批事件（后端实现）。"""

    @abstractmethod
    async def _load_raw(self, session_id: str) -> Tuple[SessionHeader, List[dict]]:
        """读原始存储（后端实现）：(header, 事件行)。"""

    @abstractmethod
    async def list_ids(self) -> List[str]:
        """列出已持久化会话 id。"""

    def locate(self, session: Session) -> Optional[str]:
        """每会话工件位置提示（JSONL 返回绝对路径；SQLite 返回 None）。"""
        return None

    # ---- 加载 + 崩溃修复 ----

    async def load(self, session_id: str) -> Tuple[SessionHeader, List[dict]]:
        """
        加载并修复一个会话。

        :return: (SessionHeader, 修复后的事件行列表)。
        :raises SessionError: 未知会话 / 格式损坏。
        """
        header, rows = await self._load_raw(session_id)
        repaired = self.repair_open_turn(rows)
        return header, repaired

    async def inspect(self, session_id: str) -> Tuple[SessionHeader, List[dict]]:
        """
        轻量检视（默认 = load；后端可覆写为不修复/只读头部的实现）。

        会话查询（ctx.sessionQuery）用此取 header 与事件。
        """
        return await self.load(session_id)

    @staticmethod
    def repair_open_turn(rows: List[dict]) -> List[dict]:
        """
        崩溃修复：为无 turn/end 的开放 turn 追加合成 interrupted 结束（不截断）。

        :return: 新列表（末尾可能多一条合成 turn/end）。
        """
        depth = 0
        open_turn: Optional[int] = None
        last_seq = -1
        for row in rows:
            event_type = row.get("type")
            seq = int(row.get("seq", last_seq + 1))
            last_seq = seq
            if event_type == "turn/start":
                depth += 1
                open_turn = row.get("data", {}).get("turn")
            elif event_type == "turn/end":
                depth = max(0, depth - 1)
                if depth == 0:
                    open_turn = None
        if depth > 0 and open_turn is not None:
            import time
            rows = list(rows)
            rows.append({"type": "turn/end", "seq": last_seq + 1,
                         "time": int(time.time() * 1000),
                         "data": {"turn": open_turn,
                                  "reason": {"kind": "interrupted"}}})
        return rows

    def known_event_types(self) -> set:
        return set(EVENT_CATALOG.keys())

    def close(self) -> None:
        self._closed = True
        self._buffers.clear()
