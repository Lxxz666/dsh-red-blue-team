"""
dsh.storage.service —— StorageService（ctx.storage）：非会话存储枢纽（storage-json 等价）。

对应 TS 版 storage + storage-json + storage-domain：

- 后端 = `~/.dsh/storage.json`，键空间按 domain 分区；
- 写入广播 ``domain/changed`` {domain}；
- 供目标/计划等域存元数据（域数据设施的最小形态）。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

from ..kernel import Service


class StorageService(Service):
    """非会话 JSON 存储（ctx.storage）。"""

    provides = "storage"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/storage.json"))
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def apply(self, ctx) -> None:
        ctx.set("storage", self)

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        with self._lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)

    def get(self, domain: str, key: str, default: Any = None) -> Any:
        """读取域内键值。"""
        return self._data.get(domain, {}).get(key, default)

    def put(self, domain: str, key: str, value: Any) -> None:
        """写入域内键值（落盘 + domain/changed）。"""
        self._data.setdefault(domain, {})[key] = value
        self._save()
        try:
            self.ctx.events.emit("domain/changed", {"domain": domain})
        except Exception:
            pass

    def delete(self, domain: str, key: str) -> None:
        """删除域内键值。"""
        table = self._data.get(domain, {})
        if key in table:
            del table[key]
            self._save()
            self.ctx.events.emit("domain/changed", {"domain": domain})

    def domain(self, domain: str) -> Dict[str, Any]:
        """域内全部键值快照。"""
        return dict(self._data.get(domain, {}))

    def domains(self) -> List[str]:
        """已存在的域列表。"""
        return list(self._data.keys())
