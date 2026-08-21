"""
dsh.settings.service —— SettingsService（ctx.settings）：用户设置缝（settings-file 等价）。

对应 TS 版 settings + settings-file：

- 后端 = `~/.dsh/settings.json`（config.path 可换）；
- 每次写入同步落盘并广播 ``settings/updated`` 与 ``settings/document-updated``；
- agent-default-model 的 current_selection 会实时读这里的 `agent_default_model`。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

from ..kernel import Service


class SettingsService(Service):
    """用户设置服务（ctx.settings）。"""

    provides = "settings"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/settings.json"))
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    def apply(self, ctx) -> None:
        ctx.set("settings", self)

    # ---- 读写 ----

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

    def get(self, key: str, default: Any = None) -> Any:
        """读取一个设置项。"""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入一个设置项（同步落盘 + 广播）。"""
        self._data[key] = value
        self._save()
        try:
            self.ctx.events.emit("settings/updated", {"key": key, "value": value})
            self.ctx.events.emit("settings/document-updated",
                                 {"key": key, "value": value})
        except Exception:
            pass

    def delete(self, key: str) -> None:
        """删除一个设置项。"""
        if key in self._data:
            del self._data[key]
            self._save()
            self.ctx.events.emit("settings/updated", {"key": key, "value": None})

    def all(self) -> Dict[str, Any]:
        """全部设置快照。"""
        return dict(self._data)
