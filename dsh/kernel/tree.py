"""
dsh.kernel.tree —— PluginTree：插件树组装与挂载（boot 核心）。

流程:
    compose(bundles, patches) → 拓扑排序 → mount → 失败则逆序卸载（fail loud）。

对应 dsh 的 composeEntries / boot：组合、禁用推导与 dump 用同一份代码，保证不会漂移。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .context import Context
from .loader import Entry, apply_patch, resolve_target
from .service import (PluginTarget, Service, is_service_class,
                      plugin_inject, plugin_name, plugin_provides)
from ..errors import LoaderError

log = logging.getLogger("dsh.kernel")


@dataclass
class MountedPlugin:
    """一个已挂载插件。"""

    entry: Entry
    instance: Any
    disposer: Optional[Any] = None
    started: bool = False


def _topo_sort(entries: List[Entry]) -> List[Entry]:
    """
    按 ``provides/inject`` 做 Kahn 拓扑排序。

    每个插件的 ``provides``（若有）即其服务名；``inject`` 里的名字必须由别的插件提供。
    无 provides 的插件不参与图（视作叶子，最后挂载）。

    :raises LoaderError: 依赖缺失或存在环时。
    """
    by_id = {e.id: e for e in entries}
    provides_map: Dict[str, Entry] = {}
    for e in entries:
        key = plugin_provides(e.target)
        if key:
            provides_map[key] = e

    # 依赖边: entry -> 它依赖的 entry
    deps: Dict[str, List[str]] = {}
    for e in entries:
        need: List[str] = []
        for service_key in plugin_inject(e.target):
            dep = provides_map.get(service_key)
            if dep is None:
                # 允许依赖由主程序手工 set 的服务（如测试/外部宿主）
                continue
            need.append(dep.id)
        deps[e.id] = need

    indegree = {e.id: len(deps[e.id]) for e in entries}
    dependents: Dict[str, List[str]] = {e.id: [] for e in entries}
    for e in entries:
        for dep_id in deps[e.id]:
            dependents.setdefault(dep_id, []).append(e.id)

    queue = [e.id for e in entries if indegree[e.id] == 0]
    order: List[Entry] = []
    while queue:
        node_id = queue.pop(0)
        order.append(by_id[node_id])
        for dependent_id in dependents.get(node_id, []):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                queue.append(dependent_id)

    if len(order) != len(entries):
        cycle = [e.id for e in entries if e.id not in {o.id for o in order}]
        raise LoaderError(f"dependency cycle among plugins: {cycle}")
    return order


class PluginTree:
    """一次 boot 的插件树：组合 → 挂载 → 查询 → 卸载。"""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._entries: Dict[str, Entry] = {}
        self._mounted: List[MountedPlugin] = []
        self._mounted_by_id: Dict[str, MountedPlugin] = {}

    # ---- 组合 ----

    def add_bundle_rows(self, rows: Sequence[dict], layer_name: str = "bundle") -> None:
        """应用一个 bundle 的行列表（bundle 的 patch 文件内容）。"""
        for row in rows:
            entry = _row_entry(row)
            self._entries[entry.id] = entry

    def apply_patch_rows(self, rows: Sequence[dict], layer_name: str) -> None:
        """应用一个用户 patch 层。"""
        apply_patch(self._entries, rows, layer_name)

    def enabled_entries(self) -> List[Entry]:
        """按插入序返回启用的条目。"""
        return [e for e in self._entries.values() if not e.disabled]

    def entries(self) -> List[Entry]:
        """按插入序返回全部条目（含禁用，供 dump 用）。"""
        return list(self._entries.values())

    def get_entry(self, entry_id: str) -> Optional[Entry]:
        """按 id 取条目（含禁用）。"""
        return self._entries.get(entry_id)

    def set_disabled(self, entry_id: str, disabled: bool) -> bool:
        """标记条目禁用状态（HMR disable 用）。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.disabled = disabled
        return True

    # ---- 挂载 ----

    async def mount(self) -> List[MountedPlugin]:
        """拓扑排序后逐个挂载；失败则逆序卸载已挂载部分并抛出（fail loud）。"""
        ordered = _topo_sort(self.enabled_entries())
        try:
            for entry in ordered:
                await self._mount_one(entry)
        except Exception:
            await self._rollback()
            raise
        return self._mounted

    async def _mount_one(self, entry: Entry) -> MountedPlugin:
        """挂载单条目（复用同一段挂载逻辑，boot 与 HMR 不漂移）。"""
        mounted = MountedPlugin(entry=entry, instance=None)
        try:
            if is_service_class(entry.target):
                instance = entry.target(self.ctx, entry.config)
                mounted.instance = instance
                mounted.disposer = instance.apply(self.ctx)
                await instance.start()
                mounted.started = True
            elif isinstance(entry.target, Service):
                instance = entry.target
                mounted.instance = instance
                mounted.disposer = instance.apply(self.ctx)
            else:
                # 函数插件: apply(ctx) -> disposer
                mounted.disposer = entry.target(self.ctx)
                mounted.instance = entry.target
            self._mounted.append(mounted)
            self._mounted_by_id[entry.id] = mounted
            log.debug("mounted plugin %s", entry.id)
            return mounted
        except Exception as exc:
            raise LoaderError(f"plugin failed: {exc}", entry_id=entry.id) from exc

    # ---- HMR：运行期增量操作 ----

    def mounted(self, entry_id: str) -> Optional[MountedPlugin]:
        """取已挂载条目（无则 None）。"""
        return self._mounted_by_id.get(entry_id)

    def is_mounted(self, entry_id: str) -> bool:
        return entry_id in self._mounted_by_id

    async def unmount_entry(self, entry_id: str) -> bool:
        """
        卸载一条已挂载插件（HMR disable 用）。

        :return: 是否确实卸载了。
        """
        import inspect
        mounted = self._mounted_by_id.pop(entry_id, None)
        if mounted is None:
            return False
        try:
            if mounted.instance is not None:
                mounted.instance.close()
            if mounted.disposer is not None:
                result = mounted.disposer()
                if inspect.isawaitable(result):
                    await result
        except Exception:
            log.exception("hmr unmount failed for %s", entry_id)
        if mounted in self._mounted:
            self._mounted.remove(mounted)
        return True

    async def mount_additional(self, entries: Sequence[Entry]) -> List[MountedPlugin]:
        """
        运行期增量挂载新条目（HMR insert 用）。

        拓扑排序时把「已挂载条目的 provides」当作可用依赖：
        新条目的 inject 可由已有插件满足，不必重新挂载整棵树。
        任一失败 → 逆序卸载本次新增部分并抛 LoaderError（last good tree）。

        :return: 本次挂载的条目列表。
        """
        ordered = _topo_sort(list(entries))
        mounted_now: List[MountedPlugin] = []
        try:
            for entry in ordered:
                if entry.id in self._mounted_by_id:
                    continue
                mounted_now.append(await self._mount_one(entry))
        except Exception:
            for mounted in reversed(mounted_now):
                await self.unmount_entry(mounted.entry.id)
            raise
        return mounted_now

    async def _rollback(self) -> None:
        """逆序卸载已挂载插件（fail loud 前的部分树清理）。"""
        import inspect
        for mounted in reversed(self._mounted):
            try:
                if mounted.instance is not None:
                    mounted.instance.close()
                # apply 返回的 disposer 也必须执行（如 MCP 服务器进程停止）
                if mounted.disposer is not None:
                    result = mounted.disposer()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                log.exception("rollback close failed for %s", mounted.entry.id)
        self._mounted.clear()
        self._mounted_by_id.clear()

    async def dispose(self, dispose_ctx: bool = True) -> None:
        """
        卸载整棵树（逆序）。

        :param dispose_ctx: False = 只卸载插件、不销毁 Context
            （preset 树挂在 agent 作用域上时，作用域生命周期归 agent 所有）。
        """
        await self._rollback()
        if dispose_ctx:
            await self.ctx.dispose()


def _row_entry(row: dict) -> Entry:
    """把一行 YAML 配置转成 Entry（复用 loader 的同一入口，保证语义一致）。"""
    from .loader import entry_from_row
    return entry_from_row(row)
