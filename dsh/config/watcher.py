"""
dsh.config.watcher —— ConfigWatcherPlugin：配置热重载（对应 TS 版 watchUserPatches）。

- 轮询 profile/home 两级 ``cordis.patch.yml``（mtime+hash），变更即应用；
- 解析失败 → 发 ``hmr/config-update-failed`` 并保持旧树（last good tree）；
- 三类变更：
  - ``disable`` → 卸载该行（unmount_entry）并标记禁用；
  - ``insert`` → 增量挂载新行（mount_additional，失败回滚本次新增）；
  - ``id + config`` → 已挂载且实现 ``reconfigure`` 的实例热更新（失败回滚旧配置）；
    其余仅更新存储配置（下次 boot 生效，记入 pending）；
- 成功发 ``hmr/config-updated`` 摘要。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import yaml

from ..kernel import Service

log = logging.getLogger("dsh.config")


class ConfigWatcherPlugin(Service):
    """配置热重载监视器。"""

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.paths = list((config or {}).get("paths") or [])
        self.interval = float((config or {}).get("interval", 2.0))
        self._hashes: Dict[str, str] = {}
        self._task: Optional[asyncio.Task] = None

    def apply(self, ctx) -> None:
        if not self.paths:
            log.warning("config watcher has no paths; disabled")
            return
        # 初始基线：只记录不应用（boot 已应用）
        for path in self.paths:
            digest = self._hash(path)
            if digest is not None:
                self._hashes[path] = digest
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._loop())

    # ---- 轮询 ----

    @staticmethod
    def _hash(path: str) -> Optional[str]:
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                for path in self.paths:
                    digest = self._hash(path)
                    if digest is None:
                        continue
                    if self._hashes.get(path) != digest:
                        self._hashes[path] = digest
                        await self._apply(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("config watcher loop crashed")

    # ---- 应用 ----

    async def _apply(self, path: str) -> None:
        """应用一个 patch 文件（解析失败 → 保持旧树）。"""
        try:
            text = open(path, "r", encoding="utf-8").read()
            rows = yaml.safe_load(text) or []
            if not isinstance(rows, list):
                raise ValueError("patch file must be a YAML list")
        except Exception as exc:
            self._emit("hmr/config-update-failed",
                       {"path": path, "error": str(exc)})
            log.warning("hmr config parse failed for %s: %s", path, exc)
            return

        tree = self.ctx.pluginTree
        summary: Dict[str, List[str]] = {"disabled": [], "inserted": [],
                                         "updated": [], "pending": [],
                                         "failed": []}
        for row in rows:
            if not isinstance(row, dict):
                summary["failed"].append(str(row))
                continue
            if "disable" in row:
                await self._apply_disable(row, tree, summary)
            elif "insert" in row:
                await self._apply_insert(row, tree, summary)
            elif "id" in row:
                self._apply_update(row, tree, summary)
        self._emit("hmr/config-updated", {"path": path, "summary": summary})
        log.info("hmr applied %s: %s", path, summary)

    async def _apply_disable(self, row: dict, tree, summary) -> None:
        for entry_id in row["disable"]:
            if await tree.unmount_entry(entry_id):
                tree.set_disabled(entry_id, True)
                summary["disabled"].append(entry_id)
            else:
                summary["failed"].append(f"disable:{entry_id}")

    async def _apply_insert(self, row: dict, tree, summary) -> None:
        try:
            tree.apply_patch_rows([{"insert": row["insert"]}], "hmr")
            new_entries = [tree.get_entry(r.get("id"))
                           for r in row["insert"]]
            new_entries = [e for e in new_entries if e is not None]
            mounted = await tree.mount_additional(new_entries)
            summary["inserted"].extend(m.entry.id for m in mounted)
        except Exception as exc:
            summary["failed"].append(f"insert: {exc}")

    def _apply_update(self, row: dict, tree, summary) -> None:
        row_id = row["id"]
        entry = tree.get_entry(row_id)
        if entry is None:
            summary["failed"].append(f"update:{row_id}(unknown id)")
            return
        new_config = dict(row.get("config") or {})
        mounted = tree.mounted(row_id)
        instance = mounted.instance if mounted else None
        if instance is not None and hasattr(instance, "reconfigure"):
            old = entry.config
            try:
                if not instance.reconfigure(new_config):
                    raise ValueError("reconfigure refused")
                entry.config = new_config
                summary["updated"].append(row_id)
            except Exception as exc:
                try:
                    instance.reconfigure(old)
                except Exception:
                    pass
                entry.config = old
                summary["failed"].append(f"update:{row_id}({exc})")
        else:
            entry.config = new_config
            summary["pending"].append(row_id)

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        try:
            self.ctx.events.emit(name, payload)
        except Exception:
            pass

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
