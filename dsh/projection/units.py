"""
dsh.projection.units —— 内建投影单元（todos / session_stats）+ 注册插件。

对应 TS 版 tool-todo 与 session-stats 的投影单元：

- ``todos``（state_version=2）：init None；每个 ``todo/write`` 取整表
  （最新写入胜出），每个 ``turn/start`` 清为 None（当前有效计划；turn/end
  保留刚完成的清单）；其余事件返回同一状态引用。
- ``session_stats``（state_version=1）：全日志计数与墙钟——
  {messages, user_messages, assistant_messages, tool_results, started_at,
  last_activity_at}；仅相关事件构造新状态（身份比较驱动变更流）。

单元是纯数学：注册表拥有驱动，载体经 snapshot / on_changed 取值。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..session.events import SessionEvent
from .projection import ProjectionDefinition


def _todos_definition() -> ProjectionDefinition:
    def apply(state: Any, event: SessionEvent) -> Any:
        if event.type == "todo/write":
            return list(event.data.get("todos") or [])
        if event.type == "turn/start":
            return None  # 新轮次：上一轮的清单不再是「当前计划」
        return state
    return ProjectionDefinition(key="todos", init=None, apply=apply,
                                state_version=2)


def _session_stats_definition() -> ProjectionDefinition:
    def _init() -> Dict[str, Any]:
        return {"messages": 0, "user_messages": 0, "assistant_messages": 0,
                "tool_results": 0, "started_at": None,
                "last_activity_at": None}

    def apply(state: Any, event: SessionEvent) -> Any:
        if event.type in ("user/message", "assistant/message",
                          "tool/result"):
            new_state = dict(state)
            new_state["messages"] += 1
            if event.type == "user/message":
                new_state["user_messages"] += 1
            elif event.type == "assistant/message":
                new_state["assistant_messages"] += 1
            else:
                new_state["tool_results"] += 1
            if new_state["started_at"] is None:
                new_state["started_at"] = event.time
            new_state["last_activity_at"] = event.time
            return new_state
        return state
    return ProjectionDefinition(key="session_stats", init=_init(),
                                apply=apply, state_version=1)


class ProjectionUnitsPlugin(Service):
    """注册内建投影单元的插件（todos + session_stats）。"""

    inject = ("sessionProjections",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        self._disposers.append(
            ctx.sessionProjections.register(_todos_definition()))
        self._disposers.append(
            ctx.sessionProjections.register(_session_stats_definition()))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
