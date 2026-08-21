"""
dsh.cordis.tools —— 七个 cordis_* 工具 + ToolCordisPlugin。

对应 TS 版 tool-cordis：模型可定义/运行/停止/删除动态插件，并经只读检查
目录自省。全部工具要求父 agent（会话所有权）；无 agent → NO_AGENT。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service
from ..tools import define_tool


def _require_runner(run_ctx):
    runner = run_ctx.root_ctx.dynamicCordisRunner
    if runner is None:
        raise ToolError("dynamicCordisRunner not mounted", code="NO_RUNNER")
    return runner


def _require_agent(run_ctx):
    agent = run_ctx.execution.agent
    if agent is None:
        raise ToolError("cordis tools require a parent agent",
                        code="NO_AGENT")
    return agent


def _render_json(args: Any, value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


# ---- 工具构造 ----

def _cordis_define_tool() -> Any:
    @define_tool(
        name="cordis_define",
        description="定义动态 Cordis 插件的一个不可变 Package 版本（只定义不运行）。"
                    "kind=new 创建新插件（idPrefix 3-6 个小写字母），"
                    "kind=existing 给已有插件追加版本。code.host 为宿主体"
                    "（async 函数体，return 一个 (ctx)->disposer 函数或 Service 类），"
                    "code.client 仅存储（本宿主无浏览器运行时）。",
        parameters={
            "name": {"type": "string", "required": True,
                     "description": "包标签"},
            "purpose": {"type": "string", "required": True,
                        "description": "用户可见的目的说明"},
            "plugin": {
                "type": "object", "required": True,
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["new", "existing"]},
                    "idPrefix": {"type": "string"},
                    "pluginId": {"type": "string"},
                },
                "required": ["kind"],
            },
            "code": {
                "type": "object", "required": True,
                "properties": {
                    "host": {"type": "string"},
                    "client": {"type": "string"},
                },
            },
        },
        output={"type": "object"}, render=_render_json)
    async def cordis_define(args, run_ctx):
        runner = _require_runner(run_ctx)
        agent = _require_agent(run_ctx)
        return runner.define(args, agent.id)

    return cordis_define


def _cordis_run_tool() -> Any:
    @define_tool(
        name="cordis_run",
        description="启动或切换动态插件的一个 Package。mode=run 启动（无成功版本或"
                    "重跑当前版本），mode=update 切换到新版本。返回 ok/状态或可操作拒绝。",
        parameters={
            "pluginId": {"type": "string", "required": True},
            "packageId": {"type": "string", "required": True},
            "mode": {"type": "string", "enum": ["run", "update"],
                     "required": True},
        },
        output={"type": "object"}, render=_render_json)
    async def cordis_run(args, run_ctx):
        runner = _require_runner(run_ctx)
        agent = _require_agent(run_ctx)
        return await runner.run(agent, args["pluginId"], args["packageId"],
                                args["mode"], signal=run_ctx.signal)

    return cordis_run


def _cordis_stop_tool() -> Any:
    @define_tool(
        name="cordis_stop",
        description="停止动态插件的活动运行（保留全部 Package 版本）。",
        parameters={"pluginId": {"type": "string", "required": True}},
        output={"type": "object"}, render=_render_json)
    async def cordis_stop(args, run_ctx):
        runner = _require_runner(run_ctx)
        agent = _require_agent(run_ctx)
        return await runner.stop(agent, args["pluginId"])

    return cordis_stop


def _cordis_undefine_tool() -> Any:
    @define_tool(
        name="cordis_undefine",
        description="删除动态插件及其全部 Package 版本（若在运行先停止）。",
        parameters={"pluginId": {"type": "string", "required": True}},
        output={"type": "object"}, render=_render_json)
    async def cordis_undefine(args, run_ctx):
        runner = _require_runner(run_ctx)
        agent = _require_agent(run_ctx)
        return await runner.undefine(agent, args["pluginId"])

    return cordis_undefine


def _cordis_inspect_list_tool() -> Any:
    @define_tool(
        name="cordis_inspect_list",
        description="列出只读检查提供者目录（host 平台；含内建 harness 提供者）。",
        parameters={},
        output={"type": "array"}, render=_render_json)
    async def cordis_inspect_list(args, run_ctx):
        runner = _require_runner(run_ctx)
        return runner.inspect_registry.list()

    return cordis_inspect_list


def _cordis_inspect_query_tool() -> Any:
    @define_tool(
        name="cordis_inspect_query",
        description="解析一个只读检查查询（provider/method 从 cordis_inspect_list 取；"
                    "输入输出都按 schema 校验）。",
        parameters={
            "provider": {"type": "string", "required": True},
            "method": {"type": "string", "required": True},
            "input": {"type": "object"},
        },
        output={"type": "object"}, render=_render_json)
    async def cordis_inspect_query(args, run_ctx):
        runner = _require_runner(run_ctx)
        return await runner.inspect_registry.query(
            args["provider"], args["method"], args.get("input"))

    return cordis_inspect_query


def _cordis_inspect_self_tool() -> Any:
    @define_tool(
        name="cordis_inspect_self",
        description="读取本会话拥有的动态插件快照（版本/活动运行/最新尝试，不含源码）。",
        parameters={},
        output={"type": "array"}, render=_render_json)
    async def cordis_inspect_self(args, run_ctx):
        runner = _require_runner(run_ctx)
        agent = _require_agent(run_ctx)
        return runner.snapshot(agent)

    return cordis_inspect_self


class ToolCordisPlugin(Service):
    """注册七个 cordis_* 工具的插件。"""

    inject = ("tools", "dynamicCordisRunner")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        factories = (_cordis_define_tool, _cordis_run_tool, _cordis_stop_tool,
                     _cordis_undefine_tool, _cordis_inspect_list_tool,
                     _cordis_inspect_query_tool, _cordis_inspect_self_tool)
        for factory in factories:
            self._disposers.append(ctx.tools.register(factory()))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
