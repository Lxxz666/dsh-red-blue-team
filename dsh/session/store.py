"""
dsh.session.store —— SessionStore（ctx.sessions）：会话生命周期 + 发布钩子。

与 TS 版 SessionStore 对齐：

- ``create`` 由调用 fiber 拥有；``prepare/enter/announce`` 是高级有序生命周期原语；
- 发布钩子在 append 时同步触发，本服务把 ``session/event`` 以 fire-and-forget task 广播
  （热路径不阻塞 I/O，与「持久化插件异步缓冲」的契约一致）；
- ``flush`` 派发 awaited 的 ``session/flush``（parallel）持久化 checkpoint；
- ``fork`` 从稳定前缀创建子会话。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ids import new_session_id
from ..kernel import Service
from .events import SessionEvent
from .session import Session, SessionHeader

log = logging.getLogger("dsh.session")


class SessionStore(Service):
    """内存会话存储 + 发布枢纽。持久化由订阅 session/event 的插件负责。"""

    provides = "sessions"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._live: Dict[str, Session] = {}
        self._pending_tasks: List[asyncio.Task] = []
        self._flush_listeners = 0

    def apply(self, ctx) -> None:
        ctx.set("sessions", self)
        ctx.on("session/flush", self._on_flush)

    def _publish(self, session: Session, event: SessionEvent) -> None:
        """
        append 后的同步通知（TS 契约: session/event 是同步通知）。

        emit 会立即调用同步监听器（持久化插件因此同步入缓冲）；
        异步监听器被调度为后台 task，由 :meth:`flush` 前统一排空。
        """
        try:
            tasks = self.ctx.events.emit("session/event", session, event)
            self._pending_tasks.extend(tasks)
            for task in tasks:
                task.add_done_callback(
                    lambda t: self._pending_tasks.remove(t)
                    if t in self._pending_tasks else None)
        except Exception:
            log.exception("session/event broadcast failed for %s", session.id)

    async def _drain_pending(self) -> None:
        """排空尚未完成的异步 session/event 广播。"""
        tasks = list(self._pending_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_flush(self, session: Session) -> Optional[bool]:
        """store 自身的汇聚监听器：不声称持久化参与（返回 None）。

        契约：「是否有持久化监听器参与」由真实持久化插件（返回 True）决定。
        """
        return None

    # ---- 生命周期 ----

    def create(self, session_id: Optional[str] = None,
               meta: Optional[Dict[str, Any]] = None,
               seed: Optional[Sequence[SessionEvent]] = None) -> Session:
        """
        创建并发布一个活跃会话。

        :param session_id: 省略则铸 ``session-<n>``。
        :param meta: 创建元数据（cwd/parent_session/seed_length/origin/delegation_depth/agent_preset）。
        :param seed: 回放/fork 的前缀事件。
        :return: 已进入存储并公告的会话。
        :raises ValueError: id 已存在。
        """
        session = self.prepare(session_id, meta, seed)
        self.enter(session)
        self.announce(session)
        return session

    def prepare(self, session_id: Optional[str] = None,
                meta: Optional[Dict[str, Any]] = None,
                seed: Optional[Sequence[SessionEvent]] = None) -> Session:
        """构造但不进入存储的会话（与 enter + announce 配对）。"""
        meta = meta or {}
        session_id = session_id or new_session_id()
        if session_id in self._live:
            raise ValueError(f"session {session_id} already exists")
        cwd = meta.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("meta.cwd must be an absolute path string")
        header = SessionHeader(
            id=session_id,
            created_at=int(meta.get("created_at") or time.time() * 1000),
            cwd=cwd,
            parent_session=meta.get("parent_session"),
            seed_length=meta.get("seed_length"),
            origin=meta.get("origin"),
            delegation_depth=int(meta.get("delegation_depth") or 0),
            agent_preset=meta.get("agent_preset"),
        )
        return Session(session_id, header=header, publish=self._publish, seed=seed)

    def enter(self, session: Session) -> None:
        """把 prepared 会话加入存储（发布钩子已随 Session 注入）。"""
        if session.id in self._live:
            raise ValueError(f"session {session.id} already in store")
        self._live[session.id] = session

    def announce(self, session: Session) -> None:
        """公告 session/created（同步监听器 throw 会回滚）。"""
        try:
            self.ctx.events.emit("session/created", session)
        except Exception:
            self._live.pop(session.id, None)
            raise

    def get(self, session_id: str) -> Optional[Session]:
        """按 id 取活跃会话。"""
        return self._live.get(session_id)

    def list(self) -> List[Session]:
        """全部活跃会话（创建序）。"""
        return list(self._live.values())

    async def flush(self, session: Session) -> bool:
        """
        派发 awaited 的 ``session/flush`` 持久化 checkpoint。

        先排空异步广播（保证持久化插件已看到全部事件），再并行派发。

        :return: 是否有持久化监听器参与。
        """
        await self._drain_pending()
        results = await self.ctx.events.parallel("session/flush", session)
        return any(result for result in results)

    def remove(self, session: Session) -> None:
        """让会话离开存储并公告 session/disposed。"""
        if self._live.pop(session.id, None) is None:
            return
        self.ctx.events.emit("session/disposed", session)

    def fork(self, source: Session, boundary: Optional[int] = None,
             child_session_id: Optional[str] = None) -> Session:
        """
        从活跃源会话的稳定前缀创建子会话。

        :param boundary: 含端点的源 seq；省略 = 当前最后一条。
        :raises ValueError: 前缀结束于未关闭的 turn 内。
        """
        boundary = len(source._events) - 1 if boundary is None else boundary
        if boundary < 0:
            boundary = -1
        prefix = source._events[:boundary + 1]
        # turn 深度平衡：前缀不得结束于开放 turn 内（不止最后一条是 turn/start）
        depth = 0
        for event in prefix:
            if event.type == "turn/start":
                depth += 1
            elif event.type == "turn/end":
                depth = max(0, depth - 1)
        if depth > 0:
            raise ValueError("fork boundary must not end inside an open turn")
        child = self.create(
            session_id=child_session_id,
            meta={"parent_session": source.id, "seed_length": len(prefix),
                  "cwd": source.header.cwd,
                  "delegation_depth": source.header.delegation_depth + 1},
            seed=prefix)
        return child

    # ---- 卸载 ----

    def close(self) -> None:
        for session in list(self._live.values()):
            self.remove(session)
        for task in self._pending_tasks:
            task.cancel()
