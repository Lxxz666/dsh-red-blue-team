"""
dsh.persistence.jsonl —— JSONL 后端：每会话一个 `<id>.jsonl` 文件。

格式::

    第 1 行  {"header": {...}}            # SessionHeader
    之后每行  SessionEvent.to_json()      # 事件（seq 连续）

- 写批在独立线程执行（asyncio.to_thread），不阻塞事件循环；
- 加载时校验 header 版本与事件连续性（Session.from_seed 复用同一套校验）；
- ``locate`` 返回会话文件绝对路径（位置提示，非授权）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..errors import SessionError, SessionFormatError
from ..session import Session, SessionEvent, SessionHeader
from .service import SessionPersistence

log = logging.getLogger("dsh.persistence")


class JsonlPersistence(SessionPersistence):
    """JSONL 文件后端（ctx.sessionPersistence 的默认实现）。"""

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        # 注意 expanduser：base.yml 里写的是 "~/.dsh/sessions"，必须展开
        self.dir = os.path.expanduser(
            (config or {}).get("dir", "~/.dsh/sessions"))
        self._io_lock = threading.Lock()

    def apply(self, ctx) -> None:
        super().apply(ctx)
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self.dir, f"{session_id}.jsonl")

    def locate(self, session: Session) -> Optional[str]:
        """返回会话文件绝对路径（可能尚不存在 = 位置提示）。"""
        return self._path(session.id)

    # ---- 写 ----

    async def _write_batch(self, session: Session,
                           events: List[SessionEvent]) -> None:
        path = self._path(session.id)
        header_line = json.dumps({"header": {
            "version": session.header.version, "id": session.header.id,
            "created_at": session.header.created_at,
            "cwd": session.header.cwd,
            "parent_session": session.header.parent_session,
            "seed_length": session.header.seed_length,
            "origin": session.header.origin,
            "delegation_depth": session.header.delegation_depth,
            "agent_preset": session.header.agent_preset,
        }}, ensure_ascii=False)
        lines = [json.dumps(e.to_json(), ensure_ascii=False) for e in events]

        def _write() -> None:
            with self._io_lock:
                existed = os.path.exists(path)
                with open(path, "a", encoding="utf-8") as fh:
                    if not existed:
                        fh.write(header_line + "\n")
                    for event in events:
                        fh.write(json.dumps(event.to_json(),
                                            ensure_ascii=False) + "\n")
        await asyncio.to_thread(_write)

    # ---- 读 ----

    async def _load_raw(self, session_id: str) -> Tuple[SessionHeader, List[dict]]:
        path = self._path(session_id)

        def _read() -> Tuple[Optional[SessionHeader], List[dict]]:
            if not os.path.exists(path):
                return None, []
            with self._io_lock:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            header: Optional[SessionHeader] = None
            rows: List[dict] = []
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SessionFormatError(
                        f"corrupt JSONL line in {path}: {exc}") from exc
                header_obj = obj.get("header")
                if header_obj is not None:
                    header = self._header_from_json(header_obj)
                else:
                    rows.append(obj)
            return header, rows

        header, rows = await asyncio.to_thread(_read)
        if header is None:
            raise SessionError(f"session {session_id} not found in {self.dir}")
        # 版本闸门（比 TS 版早退：直读首行）
        if header.version != __import__("dsh.session.events",
                                        fromlist=["SESSION_FORMAT_VERSION"]).SESSION_FORMAT_VERSION:
            direction = ("newer" if header.version > 1 else "older")
            raise SessionFormatError(
                f"session {session_id} format v{header.version} ({direction} "
                f"than supported)", direction)
        return header, rows

    @staticmethod
    def _header_from_json(raw: Dict[str, Any]) -> SessionHeader:
        return SessionHeader(
            version=int(raw.get("version", 1)), id=str(raw.get("id", "")),
            created_at=int(raw.get("created_at", 0)), cwd=raw.get("cwd"),
            parent_session=raw.get("parent_session"),
            seed_length=raw.get("seed_length"), origin=raw.get("origin"),
            delegation_depth=int(raw.get("delegation_depth", 0)),
            agent_preset=raw.get("agent_preset"))

    async def list_ids(self) -> List[str]:
        def _list() -> List[str]:
            if not os.path.isdir(self.dir):
                return []
            return [name[:-len(".jsonl")]
                    for name in os.listdir(self.dir)
                    if name.endswith(".jsonl")]
        return await asyncio.to_thread(_list)
