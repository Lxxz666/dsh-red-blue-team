"""
dsh.persistence.sqlite —— SQLite 后端（对应 session-persistence-sqlite）。

- 单文件 `~/.dsh/sessions.db`：表 `sessions(session_id PRIMARY KEY, header)`
  与 `events(session_id, seq, event, PRIMARY KEY(session_id, seq))`；
- 所有 IO 在 asyncio.to_thread 中执行；`locate()` 返回 None（会话共享一库）；
- 加载走与 JSONL 相同的校验/崩溃修复路径（SessionPersistence.load）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..errors import SessionError
from ..session import Session, SessionEvent, SessionHeader
from .service import SessionPersistence

SCHEMA_VERSION = 1


class SqlitePersistence(SessionPersistence):
    """SQLite 文件后端（base bundle 中默认禁用，patch 开启）。"""

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/sessions.db"))
        self._io_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def apply(self, ctx) -> None:
        super().apply(ctx)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False + _io_lock：跨 to_thread 线程共享单连接
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "session_id TEXT PRIMARY KEY, header TEXT NOT NULL)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "session_id TEXT NOT NULL, seq INTEGER NOT NULL, "
                "event TEXT NOT NULL, PRIMARY KEY (session_id, seq))")
            self._conn.commit()
        return self._conn

    def locate(self, session: Session) -> Optional[str]:
        return None  # 会话共享一个数据库

    # ---- 写 ----

    async def _write_batch(self, session: Session,
                           events: List[SessionEvent]) -> None:
        header_json = json.dumps(_header_to_json(session.header),
                                 ensure_ascii=False)

        def _write() -> None:
            conn = self._connect()
            with self._io_lock:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions (session_id, header) "
                    "VALUES (?, ?)", (session.id, header_json))
                conn.executemany(
                    "INSERT OR REPLACE INTO events (session_id, seq, event) "
                    "VALUES (?, ?, ?)",
                    [(session.id, e.seq,
                      json.dumps(e.to_json(), ensure_ascii=False))
                     for e in events])
                conn.commit()
        await asyncio.to_thread(_write)

    # ---- 读 ----

    async def _load_raw(self, session_id: str) -> Tuple[SessionHeader, List[dict]]:
        def _read() -> Tuple[Optional[SessionHeader], List[dict]]:
            conn = self._connect()
            with self._io_lock:
                row = conn.execute(
                    "SELECT header FROM sessions WHERE session_id = ?",
                    (session_id,)).fetchone()
                if row is None:
                    return None, []
                header = _header_from_json(json.loads(row[0]))
                event_rows = conn.execute(
                    "SELECT seq, event FROM events WHERE session_id = ? "
                    "ORDER BY seq", (session_id,)).fetchall()
                return header, [json.loads(event) for _seq, event in event_rows]

        header, rows = await asyncio.to_thread(_read)
        if header is None:
            raise SessionError(f"session {session_id} not found in {self.path}")
        return header, rows

    async def list_ids(self) -> List[str]:
        def _list() -> List[str]:
            conn = self._connect()
            with self._io_lock:
                return [row[0] for row in
                        conn.execute("SELECT session_id FROM sessions").fetchall()]
        return await asyncio.to_thread(_list)

    def close(self) -> None:
        super().close()
        if self._conn is not None:
            with self._io_lock:
                self._conn.close()
                self._conn = None


def _header_to_json(header: SessionHeader) -> Dict[str, Any]:
    return {"version": header.version, "id": header.id,
            "created_at": header.created_at, "cwd": header.cwd,
            "parent_session": header.parent_session,
            "seed_length": header.seed_length, "origin": header.origin,
            "delegation_depth": header.delegation_depth,
            "agent_preset": header.agent_preset}


def _header_from_json(raw: Dict[str, Any]) -> SessionHeader:
    return SessionHeader(
        version=int(raw.get("version", 1)), id=str(raw.get("id", "")),
        created_at=int(raw.get("created_at", 0)), cwd=raw.get("cwd"),
        parent_session=raw.get("parent_session"),
        seed_length=raw.get("seed_length"), origin=raw.get("origin"),
        delegation_depth=int(raw.get("delegation_depth", 0)),
        agent_preset=raw.get("agent_preset"))
