"""架构硬化批次测试：异常保真 / approval 事件化 / cron L / 幂等关闭。"""
import asyncio
from datetime import datetime

import pytest

from dsh.agent import ApprovalService
from dsh.kernel import Context
from dsh.schedule import CronError, next_fire_time, parse_cron


# ---- ① 子代理创建失败：原始异常不被 NameError 掩盖 ----

async def test_subagent_create_failure_preserves_error():
    from types import SimpleNamespace
    from dsh.subagent import (InProcessProviderPlugin, InProcessSubagent,
                              SubagentRegistry)
    ctx = Context("sub-fail")
    registry = SubagentRegistry(ctx, {})
    registry.apply(ctx)
    InProcessProviderPlugin(ctx, {}).apply(ctx)
    provider = InProcessSubagent()

    parent = SimpleNamespace(
        id="p1",
        options={"provider": "mock", "model": "mock"},
        ctx=ctx,
        session=SimpleNamespace(header=SimpleNamespace(cwd=None)),
        _factory=SimpleNamespace(ctx=ctx),  # 无 agent factory → create 失败
    )
    with pytest.raises(Exception) as excinfo:
        await provider.run(parent, "d", "p")
    # 守卫生效：原始失败（agents 服务缺失）保真传播，而非被 NameError 掩盖
    assert type(excinfo.value).__name__ != "NameError"
    assert "agents" in str(excinfo.value)


# ---- ② approval/request 事件化 ----

async def test_approval_request_waterfall_event():
    ctx = Context("approval-event")
    service = ApprovalService(ctx, {})
    service.apply(ctx)
    # 无监听者、无通道 → 默认拒绝
    assert await service.request("q") is False
    # 监听者短路允许
    async def allow(payload, next):
        return True
    off = ctx.on("approval/request", allow)
    assert await service.request("q") is True
    off()
    # 监听者短路拒绝
    async def deny(payload, next):
        return False
    off = ctx.on("approval/request", deny)
    assert await service.request("q") is False
    off()
    # 监听者委派（next）→ 回退默认
    async def delegate(payload, next):
        return await next()
    off = ctx.on("approval/request", delegate)
    assert await service.request("q") is False
    off()


# ---- ③ cron L（当月最后一天） ----

def test_cron_last_day_of_month():
    spec = parse_cron("0 9 L * *")
    # 2026-01 有 31 天
    assert spec.matches(datetime(2026, 1, 31, 9, 0, 0))
    assert not spec.matches(datetime(2026, 1, 30, 9, 0, 0))
    # 闰年 2 月最后一天 = 29
    assert spec.matches(datetime(2028, 2, 29, 9, 0, 0))
    assert not spec.matches(datetime(2028, 2, 28, 9, 0, 0))
    # next_after：从 1 月 30 日起，下一次 = 1 月 31 日 9:00
    nxt = next_fire_time("0 9 L * *", datetime(2026, 1, 30, 9, 0, 1))
    assert nxt == datetime(2026, 1, 31, 9, 0, 0)
    # L 只允许在 day 字段
    with pytest.raises(CronError):
        parse_cron("L * * * *")


# ---- ④ 幂等关闭 ----

async def test_loop_close_idempotent():
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.session import SessionStore
    from dsh.tools import ToolRuntime
    ctx = Context("loop-close")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    prompt = SystemPromptService(ctx, {})
    prompt.apply(ctx)
    prompt.section(PromptSection(name="p", order=0, text="x"))
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    registry = AgentRegistry(ctx, {})
    registry.apply(ctx)
    loop = AgentLoopService(ctx, {})
    loop.apply(ctx)
    ApprovalService(ctx, {}).apply(ctx)
    agent = await ctx.agents.create(options={"provider": "mock",
                                             "model": "mock"})
    assert loop._drivers
    loop.close()
    loop.close()  # 幂等：第二次无副作用、不抛异常
    assert loop._drivers == {}
    await asyncio.sleep(0.05)


async def test_engine_close_idempotent(tmp_path):
    from dsh.storage.service import StorageService
    from dsh.wanter import WanterEngine
    ctx = Context("engine-close")
    storage = StorageService(ctx, {"path": str(tmp_path / "s.json")})
    storage.apply(ctx)
    engine = WanterEngine(ctx, {"evaporate_interval": 999})
    engine.apply(ctx)
    engine.deposit((1.0, 0.0))
    engine.close()
    engine.close()  # 幂等
    assert storage.get("wanter", "trace") is not None


async def test_dispose_cancels_maintenance():
    from dsh.agent import AgentLoopService, AgentRegistry, AgentHandle, \
        ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.session import SessionStore
    from dsh.tools import ToolRuntime
    ctx = Context("maint-cancel")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    prompt = SystemPromptService(ctx, {})
    prompt.apply(ctx)
    prompt.section(PromptSection(name="p", order=0, text="x"))
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    registry = AgentRegistry(ctx, {})
    registry.apply(ctx)
    loop = AgentLoopService(ctx, {})
    loop.apply(ctx)
    ApprovalService(ctx, {}).apply(ctx)
    agent = await ctx.agents.create(options={"provider": "mock",
                                             "model": "mock"})

    async def long_task(signal):
        await signal.wait()  # 永远挂起，直到被取消

    task = agent.run_maintenance(long_task)
    assert not task.done()
    await AgentHandle(agent).dispose()
    await asyncio.sleep(0.05)
    assert task.cancelled() or task.done()  # dispose 中止维护任务
