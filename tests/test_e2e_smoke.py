"""端到端冒烟测试：完整脊柱（kernel→session→tools→llm→prompt→agent-loop）。"""
import asyncio

import pytest

from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
from dsh.kernel import Context
from dsh.llm.adapters import LlmRuntime
from dsh.llm.mock import MockAdapter
from dsh.prompt import PromptSection, SystemPromptService
from dsh.session import SessionStore
from dsh.tools import ToolRuntime, define_tool


@define_tool(name="echo", description="回显输入文本",
             parameters={"text": {"type": "string", "required": True}},
             output={"type": "string"})
async def echo_tool(args, run_ctx):
    return f"echoed: {args['text']}"


async def build_context():
    ctx = Context("smoke")
    # 手动装配脊柱（等价于 base bundle）
    store = SessionStore(ctx, {})
    store.apply(ctx)
    prompt_svc = SystemPromptService(ctx, {})
    prompt_svc.apply(ctx)
    prompt_svc.section(PromptSection(name="persona", order=0,
                                     text="你是测试助手。"))
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    tools.register(echo_tool)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    registry = AgentRegistry(ctx, {})
    registry.apply(ctx)
    loop = AgentLoopService(ctx, {})
    loop.apply(ctx)
    approval = ApprovalService(ctx, {})
    approval.apply(ctx)
    return ctx


async def test_simple_turn_echo():
    ctx = await build_context()
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    agent.followup("hello")
    await agent.when_idle()
    await asyncio.sleep(0.05)

    events = agent.session.events
    types = [e.type for e in events]
    assert types[0] == "turn/start"
    assert "user/message" in types
    assert "assistant/chunk" in types
    assert "assistant/message" in types
    assert types[-1] == "turn/end"
    messages = agent.session.derive_messages()
    assert messages[-1].role == "assistant"
    assert "hello" in messages[-1].plain_text()


async def test_tool_round_trip():
    ctx = await build_context()
    script = [
        {"tool": {"name": "echo", "arguments": {"text": "abc"}}},
        {"text": "收到。"},
    ]
    ctx.llm.register_adapter(MockAdapter(script=script))
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    agent.followup("请回显 abc")
    await agent.when_idle()
    await asyncio.sleep(0.05)

    types = [e.type for e in agent.session.events]
    assert "tool/call" in types
    assert "tool/result" in types
    result = [e for e in agent.session.events if e.type == "tool/result"][0]
    assert "echoed: abc" in result.data["content"]
    # 模型对工具结果给出回复 → 第二个 assistant/message
    assistants = [e for e in agent.session.events if e.type == "assistant/message"]
    assert len(assistants) == 2


async def test_pre_step_reject_closes_turn_without_step():
    ctx = await build_context()

    async def reject(payload, next):
        return {"kind": "reject"}

    ctx.on("agent/pre-step", reject)
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    agent.followup("hello")
    await agent.when_idle()
    await asyncio.sleep(0.05)
    types = [e.type for e in agent.session.events]
    assert "step/start" not in types
    assert types[0] == "turn/start" and types[-1] == "turn/end"


async def test_cancel_turn():
    from dsh.llm.adapters import LlmAdapter
    from dsh.llm.stream import StreamChunk

    class WaitForCancel(LlmAdapter):
        name = "mock"

        async def stream(self, request):
            await request.signal.wait()  # 挂起直到取消
            yield StreamChunk.finish("stop")

    ctx = await build_context()
    ctx.llm.register_adapter(WaitForCancel())
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    agent.followup("hello")
    await asyncio.sleep(0.05)  # 让驱动进入模型请求
    agent.cancel({"kind": "user"})
    await agent.when_idle()
    await asyncio.sleep(0.05)
    turn_end = [e for e in agent.session.events if e.type == "turn/end"][-1]
    assert turn_end.data["reason"]["kind"] == "aborted"
