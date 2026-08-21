"""
dsh.projection.projection —— SessionProjectionRegistry（ctx.sessionProjections）。

对应 TS 版 session-projection：框架驱动、领域计算。

- ``register(definition) -> disposer``：注册一个投影单元（key + init + apply
  + view + state_version）。同 key 重复注册 = 引用计数（同一工具包挂 N 个
  agent preset 注册 N 次，最后一个卸载才消失）；不同定义同 key 抛错。
- 驱动：注册表只订阅一次 ``session/event``（同步监听器），把每个已提交事件
  折叠进每个单元；``apply`` 返回**同一状态引用**表示不变（身份比较），引用
  变化 → 更新水位 cell + 通知变更流（key, session, 视图值, 致因 seq）。
- cell 惰性构建：注册晚于事件流、或会话早于注册表时，首次触达（事件或
  读取）从 ``init`` 在内存日志上折叠。
- ``snapshot(session)``：一次一致的同步切面 ``{as_of_seq, values}``
  （as_of_seq = 所有值共同反映到的最后事件 seq，空日志 -1）。
- ``on_changed(listener) -> disposer``：变更流订阅（effect 绑定）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..kernel import Service
from ..session.events import SessionEvent

log = logging.getLogger("dsh.projection")


@dataclass
class ProjectionDefinition:
    """一个领域的投影单元（纯数学：init/apply/view；驱动权归框架）。"""

    key: str
    init: Any
    apply: Callable[[Any, SessionEvent], Any]
    view: Optional[Callable[[Any], Any]] = None
    state_version: int = 1


ChangeListener = Callable[[str, Any, Any, int], None]
"""变更流签名：listener(key, session, view_value, causing_seq)。"""


class SessionProjectionRegistry(Service):
    """会话投影单元表与驱动（ctx.sessionProjections）。"""

    provides = "sessionProjections"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._units: Dict[str, Tuple[ProjectionDefinition, int]] = {}
        self._cells: Dict[Tuple[str, str], Tuple[Any, int]] = {}
        self._changed: List[ChangeListener] = []

    def apply(self, ctx) -> None:
        ctx.set("sessionProjections", self)
        ctx.on("session/event", self._on_event)
        ctx.on("session/disposed", self._on_disposed)

    # ---- 注册 ----

    def register(self, definition: ProjectionDefinition):
        """
        注册一个投影单元（effect 绑定；同 key 计数共享，最后一个卸载才消失）。

        :raises ValueError: key 非法 / state_version 非法 / 同 key 不同定义。
        """
        key = definition.key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("projection key must be a non-empty string")
        if (not isinstance(definition.state_version, int)
                or definition.state_version < 1):
            raise ValueError("state_version must be a positive integer")
        if not callable(definition.apply):
            raise ValueError("projection apply must be callable")
        existing = self._units.get(key)
        if existing is not None and existing[0] is not definition:
            raise ValueError(f"duplicate projection key: {key!r}")
        if existing is None:
            self._units[key] = (definition, 1)
        else:
            self._units[key] = (definition, existing[1] + 1)

        def unregister() -> None:
            record = self._units.get(key)
            if record is None:
                return
            if record[1] <= 1:
                self._units.pop(key, None)
                for cell_key in [k for k in self._cells if k[1] == key]:
                    self._cells.pop(cell_key, None)
            else:
                self._units[key] = (definition, record[1] - 1)
        return self.ctx.effect(unregister)

    def on_changed(self, listener: ChangeListener):
        """订阅变更流（每单元每次状态引用变化一次）。"""
        self._changed.append(listener)

        def remove() -> None:
            try:
                self._changed.remove(listener)
            except ValueError:
                pass
        return self.ctx.effect(remove)

    # ---- 驱动 ----

    def _on_event(self, session: Any, event: SessionEvent) -> None:
        for key, (definition, _count) in list(self._units.items()):
            cell_key = (session.id, key)
            cell = self._cells.get(cell_key)
            if cell is None:
                # 惰性构建：注册晚于事件流 → 折叠 init over 整个日志
                state = definition.init
                for committed in session._events:
                    state = definition.apply(state, committed)
                self._cells[cell_key] = (state, event.seq)
                self._notify(key, session, definition, state, event.seq)
                continue
            state, _watermark = cell
            new_state = definition.apply(state, event)
            if new_state is not state:
                self._cells[cell_key] = (new_state, event.seq)
                self._notify(key, session, definition, new_state, event.seq)

    def _notify(self, key: str, session: Any,
                definition: ProjectionDefinition, state: Any,
                seq: int) -> None:
        value = definition.view(state) if definition.view is not None else state
        for listener in list(self._changed):
            try:
                listener(key, session, value, seq)
            except Exception:
                log.exception("projection change listener failed for %s", key)

    def _on_disposed(self, session: Any) -> None:
        for cell_key in [k for k in self._cells if k[0] == session.id]:
            self._cells.pop(cell_key, None)

    # ---- 读取 ----

    def snapshot(self, session: Any) -> Dict[str, Any]:
        """一次一致的同步切面：{as_of_seq, values}。"""
        last = session._events[-1].seq if session._events else -1
        values: Dict[str, Any] = {}
        for key, (definition, _count) in self._units.items():
            cell_key = (session.id, key)
            cell = self._cells.get(cell_key)
            if cell is None:
                # 惰性构建：从 init 折叠全日志
                state = definition.init
                start = 0
            else:
                # 增量重折：从 cell 状态折叠未覆盖的后缀（绝不丢弃累积状态）
                state = cell[0]
                start = cell[1] + 1
            for event in session._events[start:]:
                state = definition.apply(state, event)
            self._cells[cell_key] = (state, last)
            values[key] = (definition.view(state)
                           if definition.view is not None else state)
        return {"as_of_seq": last, "values": values}

    def unit_keys(self) -> List[str]:
        return list(self._units.keys())

    def close(self) -> None:
        self._units.clear()
        self._cells.clear()
        self._changed.clear()
