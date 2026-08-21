"""
dsh.goal —— 目标域（ctx.goals + goal 轮驱动 + goal_* 工具 + /goal 命令）。

对应 TS 版 goal：``ctx.goals`` 拥有持久状态，``goal-round-driver`` 经公共 Agent
调度同会话续轮（每个 turn 结束后，若目标未完成且轮次未耗尽，followup 一条续轮消息）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..ids import new_goal_id
from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.goal")


@dataclass
class Goal:
    """一个同会话完成目标。"""

    id: str
    objective: str
    max_rounds: int = 5
    completed_rounds: int = 0
    status: str = "active"  # active|completed|blocked
    blocker_reason: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "objective": self.objective,
                "max_rounds": self.max_rounds,
                "completed_rounds": self.completed_rounds,
                "status": self.status,
                "blocker_reason": self.blocker_reason}


class GoalService(Service):
    """目标领域服务（ctx.goals）。每个 agent 作用域可持有一个目标。"""

    provides = "goals"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._goals: Dict[Any, Goal] = {}  # agent ctx_name -> Goal

    def apply(self, ctx) -> None:
        ctx.set("goals", self)

    def create(self, scope: Any, objective: str,
               max_rounds: int = 5) -> Goal:
        """创建目标（同作用域已有活跃目标则替换）。"""
        goal = Goal(id=new_goal_id(), objective=objective,
                    max_rounds=max_rounds)
        self._goals[scope] = goal
        self.ctx.events.emit("goal/changed", {"scope": scope, "goal": goal})
        return goal

    def get(self, scope: Any) -> Optional[Goal]:
        return self._goals.get(scope)

    def update(self, scope: Any, *, status: Optional[str] = None,
               blocker_reason: Optional[str] = None,
               objective: Optional[str] = None,
               max_rounds: Optional[int] = None) -> Optional[Goal]:
        """更新目标（status ∈ completed|blocked|active）。"""
        goal = self._goals.get(scope)
        if goal is None:
            return None
        if status is not None:
            goal.status = status
        if blocker_reason is not None:
            goal.blocker_reason = blocker_reason
        if objective is not None:
            goal.objective = objective
        if max_rounds is not None:
            goal.max_rounds = max_rounds
        self.ctx.events.emit("goal/changed", {"scope": scope, "goal": goal})
        return goal

    def finish_round(self, scope: Any) -> Optional[Goal]:
        """一轮结束：completed_rounds+1，超过上限仍未完成则置 blocked。"""
        goal = self._goals.get(scope)
        if goal is None or goal.status != "active":
            return goal
        goal.completed_rounds += 1
        if goal.completed_rounds >= goal.max_rounds:
            goal.status = "blocked"
            goal.blocker_reason = "goal round limit reached"
        self.ctx.events.emit("goal/changed", {"scope": scope, "goal": goal})
        return goal


class GoalRoundDriverPlugin(Service):
    """目标轮驱动：turn/end 后调度续轮（对应 goal-round-driver）。"""

    inject = ("goals", "agents")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        self._disposer = ctx.on("session/event", self._on_event)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
        return cleanup

    def _on_event(self, session: Any, event: Any) -> None:
        if event.type != "turn/end":
            return
        agent = self.ctx.agents.get(session.id)
        if agent is None:
            return
        scope = agent.ctx_name
        goal = self.ctx.goals.finish_round(scope)
        if goal is None or goal.status != "active":
            return
        # 续轮：注入下一条面向模型的用户消息
        agent.followup(
            f"[目标续轮 {goal.completed_rounds}/{goal.max_rounds}] "
            f"继续完成目标：{goal.objective}",
            source={"kind": "goal", "goal_id": goal.id})


def build_goal_tools() -> List[Any]:
    """构造 goal_* 工具族。"""

    def _scope_of(run_ctx):
        agent = run_ctx.execution.agent
        return agent.ctx_name if agent is not None else None

    @define_tool(
        name="goal_create",
        description="创建同会话完成目标（后续轮次自动续跑）。",
        parameters={"objective": {"type": "string", "required": True},
                    "max_rounds": {"type": "integer"}},
        output={"type": "string"})
    async def goal_create(args, run_ctx):
        scope = _scope_of(run_ctx)
        if scope is None:
            from ..errors import ToolError
            raise ToolError("goal requires an agent", code="NO_AGENT")
        ctx = run_ctx.root_ctx if run_ctx.execution.agent is None \
            else run_ctx.execution.agent.ctx
        goal = ctx.goals.create(scope, args["objective"],
                                max_rounds=int(args.get("max_rounds") or 5))
        return f"goal created: {goal.id}"

    @define_tool(
        name="goal_status",
        description="读取当前目标状态。",
        parameters={}, output={"type": "object"})
    async def goal_status(args, run_ctx):
        scope = _scope_of(run_ctx)
        if scope is None:
            return None
        ctx = run_ctx.root_ctx if run_ctx.execution.agent is None \
            else run_ctx.execution.agent.ctx
        goal = ctx.goals.get(scope)
        return goal.to_json() if goal else None

    @define_tool(
        name="goal_complete",
        description="标记目标完成（停止续轮）。",
        parameters={}, output={"type": "string"})
    async def goal_complete(args, run_ctx):
        scope = _scope_of(run_ctx)
        ctx = run_ctx.root_ctx if run_ctx.execution.agent is None \
            else run_ctx.execution.agent.ctx
        ctx.goals.update(scope, status="completed")
        return "goal completed"

    return [goal_create, goal_status, goal_complete]


class ToolGoalPlugin(Service):
    """注册 goal_* 工具的插件。"""

    inject = ("tools", "goals")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx):
        for tool in build_goal_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
