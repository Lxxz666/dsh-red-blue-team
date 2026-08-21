"""高级能力测试：commands / plan-mode / compaction / goal 续轮 / subagent。"""
import asyncio
from types import SimpleNamespace

import pytest

from dsh.agent import (AgentLoopService, AgentRegistry, ApprovalService,
                       Inbox, make_message)
from dsh.commands import BuiltinCommandsPlugin, CommandRegistry
from dsh.compaction import CompactionService
from dsh.goal import GoalRoundDriverPlugin, GoalService
from dsh.kernel import Context
from dsh.llm.adapters import LlmRuntime
from dsh.llm.mock import MockAdapter
from dsh.plan import PlanModePlugin, PlanModeService
from dsh.prompt import PromptSection, SystemPromptService
from dsh.session import SessionStore
from dsh.subagent import (InProcessProviderPlugin, SubagentRegistry,
                          ToolSubagentPlugin)
from dsh.tools import ToolRuntime, define_tool


async def build_ctx(with_agents: bool = True):
    """装配脊柱（与 base bundle 相同的行）。"""
    ctx = Context("advanced")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    prompt = SystemPromptService(ctx, {})
    prompt.apply(ctx)
    prompt.section(PromptSection(name="persona", order=0, text="测试助手"))
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    if with_agents:
        registry = AgentRegistry(ctx, {})
        registry.apply(ctx)
        loop = AgentLoopService(ctx, {})
        loop.apply(ctx)
        approval = ApprovalService(ctx, {})
        approval.apply(ctx)
    return ctx


# ---- commands ----

async def test_command_dispatch_help():
    ctx = Context("cmd")
    registry = CommandRegistry(ctx, {})
    registry.apply(ctx)
    plugin = BuiltinCommandsPlugin(ctx, {})
    plugin.apply(ctx)
    agent = SimpleNamespace(ctx_name="agent:x")
    result = registry.dispatch(agent, "/help")
    assert result["handled"] and "help" in result["reply"]
    result = registry.dispatch(agent, "/nope")
    assert result["reply"] and "unknown" in result["reply"]
    result = registry.dispatch(agent, "普通消息")
    assert result["handled"] is False


# ---- plan-mode ----

async def test_plan_mode_read_only_guard():
    ctx = await build_ctx(with_agents=False)
    mode = PlanModeService(ctx, {})
    mode.apply(ctx)
    plugin = PlanModePlugin(ctx, {})
    plugin.apply(ctx)

    @define_tool(name="writer", description="写工具", parameters={},
                 output={"type": "string"})
    async def writer(args, run_ctx):
        return "wrote"

    ctx.tools.register(writer)
    agent = SimpleNamespace(ctx_name="agent:x")
    # 计划模式下写工具被拒
    mode.enter(agent)
    result = await ctx.tools.execute("c1", "writer", {}, agent=agent,
                                     scope=agent.ctx_name)
    assert result.is_error and result.error.code == "DENIED"
    # 退出后放行
    mode.exit(agent)
    result = await ctx.tools.execute("c2", "writer", {}, agent=agent,
                                     scope=agent.ctx_name)
    assert not result.is_error


# ---- compaction ----

async def test_compaction_service_replaces_surface():
    ctx = Context("compact")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    service = CompactionService(ctx, {"keep_last_messages": 2})
    service.apply(ctx)

    session = store.create()
    for i in range(6):
        session.append("user/message", {"content": f"msg {i}",
                                        "source": {"kind": "user"}},
                       surface_op="append")
    assert len(session.surface.nodes) == 6
    agent = SimpleNamespace(
        session=session, options={"provider": "mock", "model": "mock"},
        _factory=SimpleNamespace(ctx=ctx))
    result = await service.compact(agent)
    assert result["compacted"]
    assert len(session.surface.nodes) == 3  # 保留 2 + 摘要 1
    assert session.events[-1].type == "compaction/summary"


# ---- goal 续轮 ----

async def test_goal_round_continuation():
    ctx = await build_ctx()
    goals = GoalService(ctx, {})
    goals.apply(ctx)
    driver = GoalRoundDriverPlugin(ctx, {})
    driver.apply(ctx)

    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    goals.create(agent.ctx_name, "测试目标", max_rounds=2)
    agent.followup("开始执行")
    # 轮询直到目标不再 active（续轮消息是同步排队的，最多 2 轮）
    goal = goals.get(agent.ctx_name)
    for _ in range(60):
        if goal.status != "active":
            break
        await asyncio.sleep(0.05)
        goal = goals.get(agent.ctx_name)
    assert goal.status == "blocked"
    assert goal.completed_rounds == 2
    turns = [e for e in agent.session.events if e.type == "turn/start"]
    assert len(turns) == 2  # 首轮 + 1 次续轮


# ---- subagent ----

async def test_subagent_tool_round_trip():
    ctx = await build_ctx()
    registry = SubagentRegistry(ctx, {})
    registry.apply(ctx)
    provider_plugin = InProcessProviderPlugin(ctx, {})
    provider_plugin.apply(ctx)
    tool_plugin = ToolSubagentPlugin(ctx, {})
    tool_plugin.apply(ctx)
    # 父 agent 用脚本：第一回合委托给子代理（子代理无脚本 → 走 echo 回退）
    ctx.llm.register_adapter(MockAdapter(script=[
        {"tool": {"name": "subagent",
                  "arguments": {"description": "测试委托",
                                "prompt": "请回复：子代理已完成"}}},
    ]))
    parent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    parent.followup("把任务委托给子代理")
    await parent.when_idle()
    await asyncio.sleep(0.05)
    results = [e for e in parent.session.events if e.type == "tool/result"]
    assert results and not results[0].data["is_error"]
    # 子代理 echo 了委托 prompt
    assert "子代理已完成" in results[0].data["content"]
    # 子代理会话已结束并被清理（只留父 agent）
    assert len(ctx.agents.list()) == 1


# ---- request-error retry 上限 ----

async def test_request_error_retry_cap():
    from dsh.errors import LlmFailure
    from dsh.llm.adapters import LlmAdapter

    ctx = await build_ctx()

    class AlwaysFail(LlmAdapter):
        name = "mock"

        async def stream(self, request):
            raise LlmFailure("模拟失败", code="TEST_FAIL")
            yield  # pragma: no cover

    ctx.llm.register_adapter(AlwaysFail())

    async def retry_handler(payload, next):
        return {"kind": "retry"}  # 永远要求重试

    agent_errors = []
    ctx.on("agent/error", lambda p: agent_errors.append(p))
    ctx.on("agent/request-error", retry_handler)
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    agent.followup("hi")
    await agent.when_idle()
    await asyncio.sleep(0.05)
    turn_end = [e for e in agent.session.events if e.type == "turn/end"][-1]
    reason = turn_end.data["reason"]
    assert reason["kind"] == "error"
    assert reason["error"]["code"] == "TEST_FAIL"
    # 重试了 3 次（3 个 step/end 前没有 assistant/message）
    steps = [e for e in agent.session.events if e.type == "step/start"]
    assert len(steps) == 3
    # agent/error 已派发（补齐批次的扩展点）
    assert agent_errors and agent_errors[-1]["error"]["code"] == "TEST_FAIL"
