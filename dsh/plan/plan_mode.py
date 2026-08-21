"""
dsh.plan —— 计划模式（对应 TS 版 plan-mode）。

- ``ctx.planMode`` 持有逐作用域开关状态；
- 进入时注册 ``plan:policy`` 提示分节 + ``tools/pre-execute`` 只读守卫
  （非只读工具一律 deny）；
- ``exit_plan_mode`` 工具与 ``/plan off`` 命令退出。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..prompt import PromptSection
from ..tools import define_tool

log = logging.getLogger("dsh.plan")

READ_ONLY_TOOL_PREFIXES = ("fs_read", "fs_glob", "fs_grep", "job_list",
                           "job_output", "goal_status", "todo_write")


class PlanModeService(Service):
    """计划模式协作状态（ctx.planMode）。"""

    provides = "planMode"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._on: set = set()  # agent ctx_name 集合

    def apply(self, ctx) -> None:
        ctx.set("planMode", self)

    def enter(self, agent: Any) -> None:
        """进入计划模式。"""
        self._on.add(agent.ctx_name)

    def exit(self, agent: Any) -> None:
        """退出计划模式。"""
        self._on.discard(agent.ctx_name)

    def is_on(self, agent: Any) -> bool:
        return agent.ctx_name in self._on


class PlanModePlugin(Service):
    """计划模式插件：提示分节 + 只读守卫 + exit_plan_mode 工具。"""

    inject = ("planMode", "tools", "systemPrompt")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        mode = ctx.planMode

        # 提示分节（生效时给出策略指引）
        section = PromptSection(
            name="plan:policy", order=90,
            text=lambda ac: "计划模式已开启：只允许只读工具。向用户呈现计划，"
                            "经 exit_plan_mode 工具或 /plan off 退出。"
                            if (ac and ac.get("scope") in mode._on) else "")

        # 只读守卫
        async def read_only_guard(execution, next):
            agent = execution.agent
            if agent is not None and ctx.planMode.is_on(agent):
                if not any(execution.name.startswith(prefix)
                           for prefix in READ_ONLY_TOOL_PREFIXES):
                    from ..tools.pipeline import DenyDecision
                    return DenyDecision("plan mode: write tools are disabled")
            return await next()

        # exit_plan_mode 工具
        @define_tool(
            name="exit_plan_mode",
            description="向用户呈现计划并退出计划模式。",
            parameters={"plan": {"type": "string", "required": True}},
            output={"type": "string"})
        async def exit_plan_mode(args, run_ctx):
            agent = run_ctx.execution.agent
            if agent is None:
                return "no agent"
            ctx.planMode.exit(agent)
            run_ctx.conclude_turn()
            return f"已退出计划模式。计划如下：\n{args['plan']}"

        self._disposers.append(ctx.systemPrompt.section(section))
        self._disposers.append(ctx.on("tools/pre-execute", read_only_guard))
        self._disposers.append(ctx.tools.register(exit_plan_mode))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
