"""
dsh.credentials.credentials —— 凭据缝（ctx.credentials）：.credentials.yaml 文件后端。

对应 TS 版 credentials + credentials-local：

- 凭据存 ``~/.dsh/.credentials.yaml``（config.path 可换），YAML 格式；
- ``get``/``set``/``delete``/``names``（names 只列名不泄值）；
- ``set``/``delete`` 落盘并广播 ``credentials/updated`` {name}；
- DeepSeekAdapterPlugin 在环境变量未提供密钥时回退到这里（key ``deepseek``）。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

import yaml

from ..kernel import Service

log = logging.getLogger("dsh.credentials")


class CredentialsService(Service):
    """凭据服务（ctx.credentials）。"""

    provides = "credentials"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/.credentials.yaml"))
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    def apply(self, ctx) -> None:
        ctx.set("credentials", self)

    # ---- 读写 ----

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            self._data = {}

    def _save(self) -> None:
        with self._lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(self._data, fh, allow_unicode=True)

    def get(self, name: str) -> Any:
        """读取凭据（不存在返回 None）。"""
        return self._data.get(name)

    def set(self, name: str, value: Any) -> None:
        """写入凭据（落盘 + credentials/updated）。"""
        self._data[name] = value
        self._save()
        try:
            self.ctx.events.emit("credentials/updated", {"name": name})
        except Exception:
            pass

    def delete(self, name: str) -> None:
        """删除凭据。"""
        if name in self._data:
            del self._data[name]
            self._save()
            self.ctx.events.emit("credentials/updated", {"name": name})

    def names(self) -> List[str]:
        """凭据名列表（不含值）。"""
        return list(self._data.keys())
