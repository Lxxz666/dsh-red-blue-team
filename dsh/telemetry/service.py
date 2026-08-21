"""
dsh.telemetry.service —— 会话遥测缝（ctx.sessionTelemetry + session-telemetry/record）。

对应 TS 版 session-telemetry + session-telemetry-otel：

- 服务订阅 ``session/event``，把每个事件经 ``session-telemetry/record``
  waterfall 派发（后端监听者包装/替换记录）；
- ``session/flush`` 时后端的 flush 钩子被调用（批写落盘）；
- ConsoleTelemetry 后端 = 每事件一行 JSON 追加到文件（replayable trace），
  默认关闭（config.enabled）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from ..kernel import Service

log = logging.getLogger("dsh.telemetry")


class SessionTelemetryService(Service):
    """会话遥测分发（ctx.sessionTelemetry）。"""

    provides = "sessionTelemetry"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        ctx.set("sessionTelemetry", self)
        self._disposers.append(ctx.on("session/event", self._on_event))
        self._disposers.append(ctx.on("session/flush", self._on_flush))

    def _on_event(self, session: Any, event: Any) -> None:
        """session/event → session-telemetry/record waterfall（后端处理）。"""
        record = {"session": session.id, "event": event.to_json()}
        try:
            self.ctx.events.emit("session-telemetry/record", record)
        except Exception:
            log.exception("telemetry dispatch failed for %s", session.id)

    async def _on_flush(self, session: Any) -> bool:
        """flush 钩子：后端在 session-telemetry/flush 上落盘。"""
        await self.ctx.events.parallel("session-telemetry/flush", session.id)
        return True

    def close(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()


class ConsoleTelemetryPlugin(Service):
    """JSONL 遥测后端（replayable trace，对应 session-telemetry-otel 的简化）。"""

    inject = ("sessionTelemetry",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []
        self._io_lock = threading.Lock()
        self.enabled = bool((config or {}).get("enabled", False))
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/telemetry.jsonl"))

    def apply(self, ctx) -> None:
        if not self.enabled:
            return
        self._disposers.append(ctx.on("session-telemetry/record",
                                      self._record))
        self._disposers.append(ctx.on("session-telemetry/flush",
                                      self._flush))

    def _record(self, record: Dict[str, Any]) -> None:
        try:
            with self._io_lock:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("telemetry write failed")

    async def _flush(self, session_id: str) -> None:
        return None  # 记录是同步追加的，flush 无额外工作

    def close(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()
