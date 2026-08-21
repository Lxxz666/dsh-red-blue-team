"""
dsh.subagent —— 子代理：provider 注册表（ctx.subagents）+ 进程内 provider + 工具。

对应 TS 版 subagent 缝：同一接口背后可换「全新子 agent」或「另一个产品的委托 turn」。
本实现提供 in-process provider：子 agent 用父 agent 的作用域 ctx（工具继承 +
restrict 过滤），跑完一个 followup 后等待静默，返回最终助手文本。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.subagent")


class SubagentRegistry(Service):
    """子代理 provider 注册表（ctx.subagents）。"""

    provides = "subagents"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._providers: Dict[str, Any] = {}

    def apply(self, ctx) -> None:
        ctx.set("subagents", self)

    def register(self, name: str, provider):
        """注册 provider（provider.run(agent, description, prompt) -> 文本）。"""
        self._providers[name] = provider
        self.ctx.events.emit("subagent/provider-added",
                             {"name": name, "provider": provider})

        def unregister() -> None:
            if self._providers.pop(name, None) is provider:
                self.ctx.events.emit("subagent/provider-removed", {"name": name})
        return self.ctx.effect(unregister)

    def get(self, name: str) -> Optional[Any]:
        return self._providers.get(name)

    def names(self) -> List[str]:
        return list(self._providers.keys())


class InProcessSubagent:
    """进程内子代理 provider：继承父作用域（同一插件实例、同一工具注册）。"""

    def __init__(self) -> None:
        self.name = "in-process"

    async def run(self, parent: Any, description: str, prompt: str,
                  max_tokens: Optional[int] = None) -> str:
        """
        在父 agent 的作用域内创建子会话 agent，跑一个 followup，返回最终助手文本。

        :param parent: 父 Agent。
        :return: 子 agent 最后一条 assistant 消息的纯文本。
        """
        registry = parent._factory.ctx.agents
        sessions = parent._factory.ctx.sessions
        bus = parent._factory.ctx.events
        # 子会话（fork 血缘 + 委托深度）
        meta = {"cwd": parent.session.header.cwd,
                "parent_session": parent.id,
                "delegation_depth": parent.session.header.delegation_depth + 1,
                "origin": "subagent"}
        options = dict(parent.options)
        if max_tokens:
            options["max_tokens"] = max_tokens
        bus.emit("subagent/start",
                 {"parent": parent.id, "description": description})
        child = None
        try:
            child = await registry.create(options=options, meta=meta,
                                          scope_parent=parent.ctx)
            child.followup(prompt, source={"kind": "plugin",
                                           "plugin": "subagent"})
            await child.when_idle()
            await asyncio.sleep(0.01)
            assistant = [m for m in child.session.derive_messages()
                         if m.role == "assistant"]
            result = assistant[-1].plain_text() if assistant else ""
            bus.emit("subagent/end",
                     {"parent": parent.id, "child": child.id,
                      "description": description, "ok": True})
            return result
        except Exception:
            bus.emit("subagent/end",
                     {"parent": parent.id,
                      "child": child.id if child is not None else None,
                      "description": description, "ok": False})
            raise
        finally:
            # 创建失败时 child 为 None：只清理已创建的子代理，
            # 不再让 NameError 掩盖原始异常
            if child is not None:
                handle = __import__("dsh.agent", fromlist=["AgentHandle"]) \
                    .AgentHandle(child)
                await handle.dispose()


class InProcessProviderPlugin(Service):
    """把 in-process provider 注册进 registry 的插件（base bundle 行）。"""

    inject = ("subagents",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        self._disposer = ctx.subagents.register("in-process",
                                                InProcessSubagent())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup


def build_subagent_tool(default_provider: str = "in-process") -> Any:
    """构造 model-facing subagent 工具。"""

    @define_tool(
        name="subagent",
        description="把自包含子任务委托给一个全新子代理，返回其最终结果。",
        parameters={"description": {"type": "string", "required": True,
                                     "description": "3-5 词任务概述"},
                    "prompt": {"type": "string", "required": True,
                               "description": "完整自包含任务说明"}},
        output={"type": "string"})
    async def subagent_tool(args, run_ctx):
        agent = run_ctx.execution.agent
        if agent is None:
            from ..errors import ToolError
            raise ToolError("subagent requires a parent agent", code="NO_AGENT")
        ctx = agent.ctx if hasattr(agent, "ctx") else run_ctx.root_ctx
        registry = ctx.subagents
        provider = registry.get(default_provider)
        if provider is None:
            from ..errors import ToolError
            raise ToolError(f"subagent provider {default_provider!r} not registered",
                            code="NO_PROVIDER")
        try:
            return await provider.run(agent, args["description"], args["prompt"])
        except Exception as exc:
            from ..errors import ToolError
            raise ToolError(f"subagent failed: {exc}") from exc

    return subagent_tool


class ToolSubagentPlugin(Service):
    """注册 subagent 工具的插件。"""

    inject = ("tools", "subagents")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx):
        self._disposer = ctx.tools.register(build_subagent_tool())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
