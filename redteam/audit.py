"""redteam.audit —— 事件溯源审计日志（审计证据链）。

扫描/攻击/修复全过程经 dsh EventBus 派发事件，本服务订阅并把每条
事件追加写入 ``<audit_dir>/<scan_id>.jsonl``，形成完整可回放的
审计证据（每次攻击的载荷、原始响应、判定依据都在里面）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dsh.kernel import Service

log = logging.getLogger("redteam.audit")

#: 审计事件名（与引擎 emit 对齐）
EVENTS = (
    "scan/started", "recon/finished", "agent/dispatched", "agent/report",
    "attack/executed", "attack/verdict",
    "finding/detected", "fix/planned", "fix/applied", "fix/rolled_back",
    "regression/verified", "scan/finished", "scan/failed",
)


class AuditSink(Service):
    """事件溯源落盘服务（ctx.audit）。"""

    provides = "audit"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.dir = os.path.abspath((config or {}).get("audit_dir", "./audit"))
        self._current: Optional[str] = None
        self._path: Optional[str] = None
        self._count = 0

    def apply(self, ctx) -> None:
        ctx.set("audit", self)
        os.makedirs(self.dir, exist_ok=True)
        for name in EVENTS:
            # 事件名经闭包绑定（EventBus 只把 emit 参数传给 handler）
            ctx.on(name, lambda payload=None, _name=name: self._record(
                _name, payload))

    def open_scan(self, scan_id: str) -> str:
        """为一次扫描打开审计文件，返回文件路径。"""
        self._current = scan_id
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in scan_id)
        self._path = os.path.join(self.dir, f"{safe}.jsonl")
        self._count = 0
        return self._path

    def _record(self, event_name: str, payload: Any = None) -> None:
        """事件处理：任何事件载荷（dict）落一行 JSON。"""
        if not self._path:
            return
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "event": event_name,
               "payload": _jsonable(payload)}
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._count += 1
        except OSError as exc:
            log.warning("audit write failed: %s", exc)

    @property
    def count(self) -> int:
        return self._count

    def read(self, scan_id: str) -> List[Dict[str, Any]]:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in scan_id)
        path = os.path.join(self.dir, f"{safe}.jsonl")
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "to_json"):
        return _jsonable(obj.to_json())
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items()
                if not k.startswith("_")}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
