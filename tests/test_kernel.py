"""dsh.kernel 测试：事件派发、Context、Loader、PluginTree。"""
import pytest

from dsh.kernel import Context, PluginTree, Service, apply_patch
from dsh.kernel.loader import Entry, resolve_target
from dsh.errors import LoaderError, ServiceNotFoundError


# ---- EventBus ----

async def test_waterfall_order_and_short_circuit():
    ctx = Context()
    calls = []

    async def first(step, next):
        calls.append("first")
        return await next()

    async def second(step, next):
        calls.append("second")
        return step * 2

    ctx.on("wf", first)
    ctx.on("wf", second)
    result = await ctx.waterfall("wf", 21)
    assert calls == ["first", "second"]
    assert result == 42


async def test_waterfall_short_circuit():
    ctx = Context()
    calls = []

    async def gate(step, next):
        calls.append("gate")
        return "blocked"  # 不调用 next → 短路

    async def never(step, next):
        calls.append("never")

    ctx.on("wf", gate)
    ctx.on("wf", never)
    result = await ctx.waterfall("wf", 1)
    assert result == "blocked"
    assert calls == ["gate"]


async def test_waterfall_default_factory():
    ctx = Context()
    result = await ctx.waterfall("none", 1, default=lambda: "default-value")
    assert result == "default-value"


async def test_parallel_and_serial():
    ctx = Context()
    ctx.on("p", lambda x: x + 1)
    ctx.on("p", lambda x: x + 2)
    results = await ctx.parallel("p", 10)
    assert sorted(results) == [11, 12]

    order = []
    ctx.on("s", lambda: order.append(1))
    ctx.on("s", lambda: order.append(2))
    await ctx.serial("s")
    assert order == [1, 2]


async def test_emit_contained():
    ctx = Context()

    def bad(*_args):
        raise RuntimeError("boom")

    ctx.on("e", bad)
    ctx.emit("e", 1)  # 不应抛出
    assert ctx.events.listener_count("e") == 1


# ---- Context ----

async def test_service_provider_lazy_and_scoped():
    ctx = Context()
    created = []

    class Svc:
        def __init__(self, c):
            created.append(1)

    ctx.provide("svc", lambda c: Svc(c))
    assert created == []           # 惰性
    assert ctx.svc is ctx.get("svc")
    assert len(created) == 1
    with pytest.raises(ServiceNotFoundError):
        ctx.get("missing")


async def test_effect_disposal_order():
    ctx = Context()
    order = []
    ctx.effect(lambda: order.append(1))
    ctx.effect(lambda: order.append(2))
    await ctx.dispose()
    assert order == [2, 1]  # 逆序


async def test_scoped_context_inherits_parent_services():
    ctx = Context()
    ctx.set("tools", object())
    child = ctx.scoped("agent-1")
    assert child.tools is ctx.tools
    child.set("local", "x")
    assert child.local == "x"
    with pytest.raises(ServiceNotFoundError):
        ctx.get("local")  # 子作用域注册不外泄


# ---- Loader / Patch ----

def test_resolve_target():
    target = resolve_target("dsh.kernel.service:Service")
    assert target is Service


def test_resolve_target_missing():
    with pytest.raises(LoaderError):
        resolve_target("dsh.kernel.service:NoSuchAttr")


def test_patch_replace_disable_insert():
    entries = {
        "a": Entry(id="a", target=lambda ctx: None, config={"x": 1}),
        "b": Entry(id="b", target=lambda ctx: None, config={"y": 2}),
    }
    apply_patch(entries, [
        {"id": "a", "config": {"x": 99}},      # 整体替换
        {"disable": ["b"]},
        {"insert": [{"id": "c", "plugin": "dsh.kernel.service:Service",
                     "config": {"z": 3}}]},
    ], "test")
    assert entries["a"].config == {"x": 99}
    assert entries["b"].disabled
    assert entries["c"].config == {"z": 3}


def test_patch_platform_condition():
    import sys
    entries = {
        "a": Entry(id="a", target=lambda ctx: None, config={}),
    }
    apply_patch(entries, [
        {"id": "a", "config": {"disabled": {"platform": [sys.platform]}}},
    ], "test")
    assert entries["a"].disabled


def test_entry_platform_condition_both_levels():
    import sys
    from dsh.kernel.loader import entry_from_row
    # 行级条件
    row = {"id": "a", "plugin": "dsh.kernel.service:Service",
           "disabled": {"platform": [sys.platform]}}
    entry = entry_from_row(row)
    assert entry.disabled is True
    # config 级条件
    row2 = {"id": "b", "plugin": "dsh.kernel.service:Service",
            "config": {"enabled": False}}
    entry2 = entry_from_row(row2)
    assert entry2.disabled is True
    assert "enabled" not in entry2.config  # 控制键不混入插件 config


# ---- PluginTree 拓扑挂载 ----

class BaseSvc(Service):
    provides = "base"
    inject = ()

    def apply(self, ctx):
        ctx.set("base", "base-value")
        self.ctx = ctx


class DependentSvc(Service):
    provides = None
    inject = ("base",)

    def apply(self, ctx):
        assert ctx.get("base") == "base-value"  # 依赖先就绪


async def test_tree_topo_mount_and_rollback():
    ctx = Context()
    tree = PluginTree(ctx)
    tree.add_bundle_rows([
        {"id": "dep", "plugin": "tests.test_kernel:DependentSvc"},
        {"id": "base", "plugin": "tests.test_kernel:BaseSvc"},
    ])
    mounted = await tree.mount()
    assert [m.entry.id for m in mounted] == ["base", "dep"]


class BrokenSvc(Service):
    provides = None
    inject = ()

    def apply(self, ctx):
        raise RuntimeError("broken on purpose")


class WasMounted(Service):
    provides = None
    inject = ()

    def __init__(self, ctx, config=None):
        super().__init__(ctx, config)
        self.closed = False

    def apply(self, ctx):
        ctx.set("marker", True)

    def close(self):
        self.closed = True


async def test_tree_rollback_on_failure():
    ctx = Context()
    tree = PluginTree(ctx)
    tree.add_bundle_rows([
        {"id": "ok", "plugin": "tests.test_kernel:WasMounted"},
        {"id": "bad", "plugin": "tests.test_kernel:BrokenSvc"},
    ])
    with pytest.raises(LoaderError) as excinfo:
        await tree.mount()
    assert "bad" in str(excinfo.value)
