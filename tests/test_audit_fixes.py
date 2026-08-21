"""审计修复回归测试：surface replace 顺序 / fork 边界 / schema 强制 / temperature=0 /
flush 契约 / approval None 配置 / cancel cause 重置 / wanter goal_complete 位置。"""
import asyncio

import pytest

from dsh.boot import boot
from dsh.errors import ToolArgsError, ToolError
from dsh.kernel import Context
from dsh.session import SessionStore


# ① surface replace：新节点插入被替换区间位置（摘要在前）
def test_surface_replace_keeps_order():
    ctx = Context("r1")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    session = store.create()
    session.append("user/message", {"content": "旧1", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("user/message", {"content": "旧2", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("user/message", {"content": "旧3", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("user/message", {"content": "新消息", "source": {"kind": "user"}},
                   surface_op="append")
    # 压缩最旧两条 → 摘要应位于剩余消息之前
    shadowed = [session._events[i].seq for i in range(2)]
    session.append("compaction/summary", {"summary": "已压缩旧对话"},
                   surface_op={"op": "replace", "start": shadowed[0],
                               "end": shadowed[-1]},
                   source_event_seqs=shadowed)
    contents = [m.plain_text() for m in session.derive_messages()]
    assert contents[0] == "已压缩旧对话"
    assert contents[1] == "旧3"


# ② fork 边界：前缀结束于 turn 中段必须拒绝
def test_fork_rejects_mid_turn_boundary():
    ctx = Context("r2")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    session = store.create()
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "x", "source": {"kind": "user"}},
                   surface_op="append")
    with pytest.raises(ValueError):
        store.fork(session)  # 前缀结束于 open turn 内


# ③ schema：properties 缺 type:"object" → fail loud
def test_schema_requires_object_type():
    from dsh.tools.schema import assert_supported_schema
    with pytest.raises(ToolArgsError):
        assert_supported_schema({"properties": {"a": {"type": "string"}}})
    with pytest.raises(ToolArgsError):
        assert_supported_schema({"type": "string",
                                 "properties": {"a": {"type": "string"}}})
    # 合法形态仍通过
    assert_supported_schema({"type": "object",
                             "properties": {"a": {"type": "string"}}})


# ④ temperature=0 不得被默认值覆盖
async def test_temperature_zero_preserved(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(
            options={"provider": "mock", "model": "mock", "temperature": 0})
        captured = {}

        async def grab(payload, next):
            config = await next()
            captured["temperature"] = config.temperature
            return config
        dispose = ctx.events.on("agent/request", grab)
        agent.followup("你好")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        dispose()
        assert captured["temperature"] == 0
    finally:
        await tree.dispose()


# ⑤ flush 契约：无持久化监听器 → False；有 → True
async def test_flush_contract(tmp_path):
    ctx = Context("r5")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    session = store.create()
    session.append("user/message", {"content": "x", "source": {"kind": "user"}},
                   surface_op="append")
    assert await store.flush(session) is False  # 无持久化插件

    ctx2, tree = await boot(profile="headless", workspace=str(tmp_path),
                            mock_llm=True)
    try:
        session2 = ctx2.sessions.create()
        session2.append("user/message",
                        {"content": "y", "source": {"kind": "user"}},
                        surface_op="append")
        assert await ctx2.sessions.flush(session2) is True
    finally:
        await tree.dispose()


# ⑥ ApprovalService 直构（config=None）不崩
def test_approval_service_none_config():
    from dsh.agent.approval import ApprovalService
    ctx = Context("r6")
    service = ApprovalService(ctx)
    service.apply(ctx)
    assert service._default is False


# ⑦ cancel cause 每 turn 重置（首个 cause 胜出不跨 turn 泄漏）
async def test_cancel_cause_reset_per_turn(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("第一轮")
        agent.cancel({"kind": "user", "note": "第一次"})
        await agent.when_idle()
        await asyncio.sleep(0.05)
        assert agent._cancel_cause is None  # turn 结束后已重置
        agent.followup("第二轮")
        agent.cancel({"kind": "hook", "note": "第二次"})
        await agent.when_idle()
        await asyncio.sleep(0.05)
        assert agent._cancel_cause is None
    finally:
        await tree.dispose()


# ⑧ wanter goal_complete 用当前水滴位置（而非会话哈希初始坐标）
async def test_wanter_goal_complete_uses_current_position(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        engine = ctx.wanter
        engine.add_goal((5.0, 5.0), 1.0)
        engine.add_goal((-5.0, -5.0), 1.0)
        # 当前水滴位置贴近 B 目标
        engine.set_position(agent.id, (-4.9, -4.9))
        result = await ctx.tools.execute(
            "c1", "wanter_goal_complete", {}, agent=agent,
            scope=agent.ctx_name)
        assert not result.is_error, result.content
        remaining = [g for g, _s in engine.terrain.goals]
        assert (-5.0, -5.0) not in remaining   # 移除的是当前位置最近的目标
        assert (5.0, 5.0) in remaining         # 另一个保留
        assert (3.0, 0.0) in remaining         # base.yml 默认目标保留
    finally:
        await tree.dispose()
