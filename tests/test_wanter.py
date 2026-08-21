"""wanter 物理性质测试（四法则逐一验证）。

重点实验：
- ② 蒸发：指数衰减的数值精确性 + 遗忘阈值；
- ③ 梯度流：无洼地时纯梯度到达目标；
- ④ 双势阱逃逸对照：构造「浅局部阱 + 势垒 + 深全局阱」，无侵蚀被困 /
  有侵蚀（环形挖低 rim）逃逸到全局阱——法则 ④ 的直接证明；
- ② 路径复用偏好：迹沉积后 softmin 选择旧路径概率单调上升；
- 引擎持久化：storage 往返后地形/迹/侵蚀计数一致。
"""
import math
import random

from dsh.kernel import Context
from dsh.storage.service import StorageService
from dsh.wanter import (Eroder, StagnationDetector, Terrain, TraceField,
                        WanterEngine, gradient_step, langevin_step,
                        softmin_weights)


# ---- ② 蒸发：指数衰减精确性 ----

def test_evaporation_exact_decay():
    field = TraceField(decay_rate=0.5, epsilon=1e-9)
    field.deposit((0.0, 0.0), weight=4.0, at=0.0)
    field.deposit((1.0, 0.0), weight=2.0, at=0.0)
    weights = field.weights_at(now=2.0)  # Δt=2, λ=0.5 → e^-1
    expected = [4.0 * math.exp(-1.0), 2.0 * math.exp(-1.0)]
    for actual, want in zip(sorted(weights), sorted(expected)):
        assert abs(actual - want) < 1e-9


def test_evaporation_forgets_below_epsilon():
    field = TraceField(decay_rate=1.0, epsilon=0.1)
    field.deposit((0.0,), weight=1.0, at=0.0)
    # Δt=3, λ=1 → 权重 e^-3 ≈ 0.0498 < 0.1 → 遗忘
    removed = field.evaporate_to(now=3.0)
    assert removed == 1
    assert field.event_count() == 0


# ---- ③ 梯度流达目标 ----

def test_gradient_descent_reaches_goal():
    terrain = Terrain(dim=2, goal=(0.0, 0.0), sigma=1.0)
    x = (5.0, 5.0)
    for _ in range(500):
        x = gradient_step(x, terrain, lr=0.1)
    assert (x[0] ** 2 + x[1] ** 2) < 0.05 ** 2


# ---- ④ 双势阱逃逸对照 ----

def _double_well_terrain(with_trace=False):
    """1D：局部阱@1（深2）+ 势垒@2（高1.5）+ 全局阱@3（谐波目标阱）。"""
    terrain = Terrain(dim=1, goal=(3.0,), sigma=0.8)
    terrain.add_bump((1.0,), height=-2.0)   # 局部洼地
    terrain.add_bump((2.0,), height=+1.5)   # 势垒
    if with_trace:
        terrain.trace_field = TraceField(decay_rate=0.0)
    return terrain


def _simulate(terrain, erode: bool, steps: int = 12000):
    """Langevin 流动；erode=True 时每 20 步检测停滞并环形侵蚀。"""
    rng = random.Random(42)
    detector = StagnationDetector(window=30, delta=0.2)
    eroder = Eroder(depth=0.4, anneal=0.995, radius=0.8)
    x = (0.5,)
    escaped = False
    for step in range(steps):
        x = langevin_step(x, terrain, lr=0.05, D=0.005, rng=rng)
        detector.observe(x)
        if erode and step % 20 == 0 and detector.stagnated():
            eroder.erode(terrain, x)
            detector.reset()
        if x[0] > 2.5:
            escaped = True
        if abs(x[0] - 3.0) < 0.4:
            break
    return x[0], eroder.erosion_count, escaped


def test_double_well_control_group_stays_trapped():
    terrain = _double_well_terrain()
    final_x, _, escaped = _simulate(terrain, erode=False)
    assert escaped is False  # 无侵蚀 → 从未越过势垒
    assert final_x < 2.5


def test_double_well_erosion_escapes():
    terrain = _double_well_terrain()
    final_x, erosion_count, escaped = _simulate(terrain, erode=True)
    assert escaped is True           # 越过势垒
    assert erosion_count >= 1        # 靠侵蚀（而非随机噪声）开辟通道
    assert abs(final_x - 3.0) < 0.4  # 落入全局势阱（任务完成）


# ---- ② 路径复用偏好 ----

def test_path_reuse_preference_grows_with_trace():
    terrain = Terrain(dim=2, goal=(10.0, 0.0), sigma=1.0)
    trace = TraceField(decay_rate=0.0)
    terrain.trace_field = trace
    candidates = [(5.0, 1.0), (5.0, -1.0)]  # 两条等势路径
    before = softmin_weights(candidates, terrain, tau=1.0)
    assert abs(before[0] - 0.5) < 1e-9 and abs(before[1] - 0.5) < 1e-9
    # 在路径 A 附近沉积水迹 → A 侧地形被刻蚀降低 → 新水流偏好 A
    trace.deposit((4.7, 1.0), weight=6.0, at=0.0)
    trace.deposit((5.3, 1.0), weight=6.0, at=0.0)
    after = softmin_weights(candidates, terrain, tau=1.0)
    assert after[0] > before[0] + 0.2  # 路径 A 概率单调上升


# ---- 停滞检测 ----

def test_stagnation_detector():
    detector = StagnationDetector(window=10, delta=0.1)
    for _ in range(10):
        detector.observe((1.0, 1.0))  # 位置不动 → 困
    assert detector.stagnated() is True
    detector.reset()
    for i in range(10):
        detector.observe((1.0 + i * 0.5, 0.0))  # 持续前进 → 不困
    assert detector.stagnated() is False


# ---- 引擎持久化往返 ----

async def test_engine_persistence_round_trip(tmp_path):
    ctx = Context("wanter-persist")
    storage = StorageService(ctx, {"path": str(tmp_path / "storage.json")})
    storage.apply(ctx)
    engine = WanterEngine(ctx, {"goal": [3.0, 0.0], "evaporate_interval": 999})
    engine.apply(ctx)
    engine.deposit((1.0, 0.0), weight=2.0)
    engine.report_and_maybe_erode((1.0, 0.0))  # 能量观测（未达窗口）
    engine.terrain.erode((2.0, 0.0), 0.3)
    engine._persist()
    engine.close()

    # 恢复
    ctx2 = Context("wanter-restore")
    storage2 = StorageService(ctx2, {"path": str(tmp_path / "storage.json")})
    storage2.apply(ctx2)
    engine2 = WanterEngine(ctx2, {"goal": [3.0, 0.0],
                                  "evaporate_interval": 999})
    engine2.apply(ctx2)
    assert engine2.trace.event_count() == 1
    assert len(engine2.terrain.erosion_bumps) == 1
    engine2.close()


# ---- harness 集成（WanterPlugin） ----

async def test_wanter_plugin_deposit_and_context(tmp_path):
    import asyncio
    from dsh.boot import boot
    from dsh.llm.mock import MockAdapter
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        ctx.llm.register_adapter(MockAdapter(script=[
            {"tool": {"name": "fs_write",
                      "arguments": {"path": "wanter.txt", "content": "x"}}},
            {"text": "完成。"},
        ]))
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("写个文件")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        # ② 成功工具结果 → 水迹沉积
        assert ctx.wanter.trace.event_count() >= 1
        # ③ 地形状态进入模型可见上下文
        assembly = ctx.systemPrompt._build(
            agent.ctx_name, {"scope": agent.ctx_name})
        assert "wanter 地形状态" in assembly["text"]
    finally:
        await tree.dispose()


async def test_wanter_plugin_stagnation_steers():
    import asyncio
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.session import SessionStore
    from dsh.tools import ToolRuntime
    from dsh.wanter import WanterEngine
    from dsh.wanter.plugin import WanterPlugin
    ctx = Context("wanter-int")
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
    # 无目标、无山丘 → 梯度为零 → 水滴静止 → 停滞 → 侵蚀 + steer
    engine = WanterEngine(ctx, {"goal": None, "stagnation_window": 10,
                                "stagnation_delta": 0.05})
    engine.apply(ctx)
    plugin = WanterPlugin(ctx, {})
    cleanup = plugin.apply(ctx)

    agent = await ctx.agents.create(options={"provider": "mock",
                                             "model": "mock"})
    # 直接驱动 turn-stopping 监听器 12 次（每次观测同一静止位置）
    for _ in range(12):
        await ctx.events.serial("agent/turn-stopping", {"agent": agent})
    assert engine.eroder.erosion_count >= 1
    snapshot = agent.inbox.snapshot()
    assert any("wanter" in m["content"] for m in snapshot["next_step"])
    cleanup()
    engine.close()


# ---- 深化 ①：淤积（负沉积 = 反向刻蚀） ----

def test_silting_raises_terrain_and_reduces_preference():
    terrain = Terrain(dim=2, goal=(10.0, 0.0), sigma=1.0)
    trace = TraceField(decay_rate=0.0)
    terrain.trace_field = trace
    candidates = [(5.0, 1.0), (5.0, -1.0)]
    before_phi_a = terrain.phi(candidates[0], now=0.0)
    before = softmin_weights(candidates, terrain, tau=1.0, now=0.0)
    # 淤积在 A 侧（负沉积 → 地形被抬高 → 不再偏好 A）
    trace.deposit((4.8, 1.0), weight=-3.0, at=0.0)
    trace.deposit((5.2, 1.0), weight=-3.0, at=0.0)
    after_phi_a = terrain.phi(candidates[0], now=0.0)
    after = softmin_weights(candidates, terrain, tau=1.0, now=0.0)
    assert after_phi_a > before_phi_a      # 淤积抬高地形
    assert after[0] < before[0] - 0.2      # A 路径被避开（反向刻蚀）


# ---- 深化 ③：多目标势阱（子任务分解） ----

def test_multi_goal_sequential_descent():
    terrain = Terrain(dim=1, goal=(1.0,))    # 主目标（近、弱）
    terrain.add_goal((5.0,), strength=2.0)   # 子目标（远、强）
    # 两阱叠加的平衡点：∇Φ=(x−1)+2(x−5)=0 → x=11/3≈3.667
    x = (0.0,)
    for _ in range(600):
        x = gradient_step(x, terrain, lr=0.05)
    assert abs(x[0] - 11.0 / 3.0) < 0.1
    # 移除近目标 → 继续向远目标下坡
    terrain.remove_goal((1.0,))
    for _ in range(600):
        x = gradient_step(x, terrain, lr=0.05)
    assert abs(x[0] - 5.0) < 0.3
    assert terrain.nearest_goal(x) == (5.0,)


async def test_engine_multi_goal_completed_any():
    from dsh.kernel import Context
    ctx = Context("wanter-multi")
    engine = WanterEngine(ctx, {"goals": [[1.0], [5.0]],
                                "complete_radius": 0.2})
    assert engine.completed((1.05,)) is True   # 任一阱内即完成
    assert engine.completed((2.5,)) is False
    engine.remove_goal((1.0,))
    assert engine.completed((5.1,)) is True


# ---- 深化 ②：Coordinator 缝 ----

def test_coordinator_hash_deterministic():
    from dsh.wanter import HashCoordinator, MockEmbeddingCoordinator
    coord = HashCoordinator(dim=2)
    a1, a2 = coord.embed("写一个函数"), coord.embed("写一个函数")
    b = coord.embed("完全不同的任务")
    assert a1 == a2 and a1 != b
    mock = MockEmbeddingCoordinator(dim=3)
    v = mock.embed("测试 任务")
    assert len(v) == 3 and all(abs(vi) <= 2.0 + 1e-9 for vi in v)
    v2 = mock.embed("测试 任务")
    assert v == v2


async def test_http_coordinator_falls_back_to_hash():
    from dsh.wanter import HttpEmbeddingCoordinator
    coord = HttpEmbeddingCoordinator(base_url="http://127.0.0.1:1",
                                     model="m", dim=2, timeout=2.0)
    vector = await coord.embed("hello")
    assert len(vector) == 2  # 端点不可达 → 回退 Hash，不抛异常


# ---- 深化 ①：feedback 连续奖励 + 失败淤积（插件集成） ----

async def test_plugin_feedback_deposit_and_silt(tmp_path):
    import asyncio
    from dsh.boot import boot
    from dsh.llm.mock import MockAdapter
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        ctx.llm.register_adapter(MockAdapter(script=[{"text": "回复内容"}]))
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("你好")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        assistant = [e for e in agent.session.events
                     if e.type == "assistant/message"][-1]
        # 用户 up → 强化沉积（+2）；同点 down → 聚合抵消为 +0.5
        ctx.messageFeedback.put(agent.id, assistant.seq, "up")
        await asyncio.sleep(0.05)
        weights = ctx.wanter.trace.weights_at()
        assert any(w > 1.5 for w in weights)      # up → +2 沉积
        ctx.messageFeedback.put(agent.id, assistant.seq, "down")
        await asyncio.sleep(0.05)
        weights = ctx.wanter.trace.weights_at()
        # 同点聚合：+2 + (−1.5) ≈ +0.5（减去两次之间微小的蒸发衰减）
        assert any(0.4 < w < 0.6 for w in weights)
        assert not any(w < -1.0 for w in weights)
    finally:
        await tree.dispose()


async def test_plugin_tool_failure_silts(tmp_path):
    import asyncio
    from dsh.boot import boot
    from dsh.llm.mock import MockAdapter
    from dsh.tools import define_tool
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        @define_tool(name="failing", description="必失败",
                     parameters={}, output={"type": "string"})
        async def failing(args, run_ctx):
            from dsh.errors import ToolError
            raise ToolError("boom", code="TEST")
        ctx.tools.register(failing)
        ctx.llm.register_adapter(MockAdapter(script=[
            {"tool": {"name": "failing", "arguments": {}}},
            {"text": "ok"}]))
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("试试")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        weights = ctx.wanter.trace.weights_at()
        assert any(w < 0 for w in weights)  # 失败 → 淤积（负沉积）
    finally:
        await tree.dispose()


# ---- 打磨批次：聚合 / 上限 / 回拨 / 偏置 / 清理 / 脏写 ----

def test_trace_same_point_aggregation():
    field = TraceField(decay_rate=0.0)
    field.deposit((1.0, 1.0), weight=1.0, at=0.0)
    field.deposit((1.0, 1.0), weight=2.0, at=0.0)
    assert field.event_count() == 1              # 同点聚合
    assert field.weights_at(now=0.0) == [3.0]    # 权重相加（河道加深）


def test_trace_event_cap_prunes_weakest():
    field = TraceField(decay_rate=0.0, max_events=3)
    for i in range(5):
        field.deposit((float(i), 0.0), weight=float(i + 1), at=0.0)
    assert field.event_count() == 3
    weights = field.weights_at(now=0.0)
    assert sorted(weights) == [3.0, 4.0, 5.0]    # 最弱（1、2）被淘汰


def test_trace_clock_skew_clamped():
    field = TraceField(decay_rate=1.0)
    field.deposit((0.0,), weight=1.0, at=100.0)
    # now 早于 deposited（时钟回拨）→ 流逝按 0 处理，不爆炸
    assert field.weights_at(now=50.0) == [1.0]


def test_erosion_max_depth_creates_new_bumps():
    terrain = Terrain(dim=1, sigma=1.0, erosion_max_depth=0.5)
    terrain.erode((0.0,), 0.4)
    terrain.erode((0.01,), 0.4)   # 近邻：合并会超过上限 → 新增 bump
    assert len(terrain.erosion_bumps) == 2
    terrain.erode((0.0,), 0.05)   # 未超限 → 合并加深
    assert len(terrain.erosion_bumps) == 2


def test_eroder_biased_toward_goal_no_reverse_dig():
    terrain = Terrain(dim=1, goal=(3.0,), sigma=0.8)
    eroder = Eroder(depth=0.4, anneal=1.0, radius=0.8)
    eroder.erode(terrain, (1.0,))
    centers = sorted(c[0][0] for c in terrain.erosion_bumps)
    assert len(centers) == 1
    assert centers[0] > 1.0  # 只在目标方向开挖，不挖反向（陷阱内侧）


async def test_plugin_cleans_up_on_agent_disposed(tmp_path):
    import asyncio
    from dsh.boot import boot
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("你好")
        await agent.when_idle()
        await asyncio.sleep(0.05)  # 产生 assistant/message → 位置入册
        plugins = [m.instance for m in tree._mounted
                   if type(m.instance).__name__ == "WanterPlugin"]
        assert plugins
        wanter_plugin = plugins[0]
        assert agent.id in wanter_plugin._positions
        from dsh.agent import AgentHandle
        await AgentHandle(agent).dispose()
        await asyncio.sleep(0.05)
        assert agent.id not in wanter_plugin._positions
        assert agent.id not in wanter_plugin._position_history
    finally:
        await tree.dispose()


async def test_engine_dirty_flag_persist(tmp_path):
    from dsh.kernel import Context
    from dsh.storage.service import StorageService
    ctx = Context("wanter-dirty")
    storage = StorageService(ctx, {"path": str(tmp_path / "s.json")})
    storage.apply(ctx)
    engine = WanterEngine(ctx, {"evaporate_interval": 999})
    engine.apply(ctx)
    assert engine._dirty is False
    engine._persist()                       # 无变更 → 不落盘
    assert storage.domain("wanter") == {}
    engine.deposit((1.0, 0.0))
    assert engine._dirty is True
    engine._persist()
    assert "trace" in storage.domain("wanter")
    assert engine._dirty is False
    engine.close()
