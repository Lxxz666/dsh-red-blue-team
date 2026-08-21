"""自指 cordis 测试：define/run/stop/undefine、审批门控、沙箱预检、inspect、工具、隔离与幂等关闭。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from dsh.boot import boot
from dsh.errors import ToolError
from dsh.kernel import Context, Service
from dsh.tools import ToolRuntime, define_tool

HOST_DOUBLER = (
    'from dsh.tools import define_tool\n'
    '\n'
    '@define_tool(name="doubler", description="把输入翻倍",\n'
    '             parameters={"n": {"type": "integer", "required": True}},\n'
    '             output={"type": "integer"})\n'
    'async def doubler(args, run_ctx):\n'
    '    return args["n"] * 2\n'
    '\n'
    'async def double_it(args):\n'
    '    return {"value": int(args["n"]) * 2}\n'
    '\n'
    'def plugin(ctx):\n'
    '    dispose_tool = harness.registerTool(ctx, doubler)\n'
    '    dispose_handle = harness.handle("double", double_it)\n'
    '    def cleanup():\n'
    '        dispose_tool()\n'
    '        dispose_handle()\n'
    '    return cleanup\n'
    '\n'
    'return plugin\n'
)


async def _runner_ctx(config=None):
    from dsh.cordis.runner import DynamicCordisRunnerService
    from dsh.session.store import SessionStore
    ctx = Context("cordis-test")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    from dsh.agent.approval import ApprovalService
    ApprovalService(ctx, {}).apply(ctx)
    runner = DynamicCordisRunnerService(ctx, config or {})
    runner.apply(ctx)
    return ctx, runner


def _fake_agent(session_id="sess-1"):
    return SimpleNamespace(id=session_id)


def _define(runner, agent, *, name="doubler", purpose="test plugin",
            plugin=None, code=None):
    return runner.define({
        "name": name, "purpose": purpose,
        "plugin": plugin or {"kind": "new", "idPrefix": "tst"},
        "code": code or {"host": HOST_DOUBLER},
    }, agent.id)


# ---- define 校验 ----

async def test_define_validation():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    with pytest.raises(ToolError):
        _define(runner, agent, name="  ")
    with pytest.raises(ToolError):
        _define(runner, agent, purpose="  ")
    with pytest.raises(ToolError):
        _define(runner, agent, code={"host": None})
    with pytest.raises(ToolError):
        _define(runner, agent, plugin={"kind": "new", "idPrefix": "AB12"})
    # client-only 包可定义（存储/检查用）
    receipt = _define(runner, agent, code={"client": "console.log(1)"})
    assert receipt["hasClientHalf"] is True and receipt["hasHostHalf"] is False


async def test_define_precheck_syntax_and_imports():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    with pytest.raises(ToolError) as exc:
        _define(runner, agent, code={"host": "def def def"})
    assert "failed to parse" in str(exc.value)
    with pytest.raises(ToolError) as exc:
        _define(runner, agent, code={"host": "import os\nreturn None"})
    assert "imports `os`" in str(exc.value)
    with pytest.raises(ToolError) as exc:
        _define(runner, agent, code={"host": "from subprocess import run\nreturn None"})
    assert "imports `subprocess`" in str(exc.value)


async def test_define_new_and_existing_immutable():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    first = _define(runner, agent)
    assert first["pluginId"].startswith("tst-")
    assert first["packageId"].startswith("dyn-")
    second = _define(runner, agent, name="doubler v2",
                     plugin={"kind": "existing",
                             "pluginId": first["pluginId"]})
    assert second["pluginId"] == first["pluginId"]
    assert second["packageId"] != first["packageId"]
    plugin = runner._registry.get(first["pluginId"])
    assert len(plugin["packages"]) == 2
    # 所有权：其他会话不可追加
    with pytest.raises(ToolError):
        _define(runner, _fake_agent("sess-2"),
                plugin={"kind": "existing", "pluginId": first["pluginId"]})


# ---- run 生命周期 ----

async def test_run_host_only_activates_and_invokes():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent)
    events = []
    ctx.events.on("cordis/dynamic-package", events.append)
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is True and response["status"] == "running"
    assert events and events[0]["pluginId"] == receipt["pluginId"]
    plugin = runner._registry.get(receipt["pluginId"])
    run = plugin["run"]
    # 调用：成功 / 未知方法 / 非 JSON 结果 / handler 异常
    result = await runner.invoke(receipt["pluginId"], run["plugin_run_id"],
                                 "double", {"n": 21})
    assert result == {"ok": True, "value": {"value": 42}}
    assert (await runner.invoke(receipt["pluginId"], run["plugin_run_id"],
                                "nope", {}))["code"] == "method-not-found"
    assert (await runner.invoke(receipt["pluginId"], "run-stale",
                                "double", {}))["code"] == "stale-run"
    # 工具注册落在包自己的层：根不可见
    assert ctx.tools.get("doubler") is None
    assert run["scope"].tools.get("doubler") is not None


async def test_run_handler_error_and_non_json():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent, code={"host": (
        'async def boom(args):\n'
        '    raise ValueError("boom")\n'
        '\n'
        'async def bad(args):\n'
        '    return {"set"}  # 非 JSON\n'
        '\n'
        'def plugin(ctx):\n'
        '    harness.handle("boom", boom)\n'
        '    harness.handle("bad", bad)\n'
        '    return lambda: None\n'
        '\n'
        'return plugin\n')})
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is True
    run_id = response["pluginRunId"]
    boom = await runner.invoke(receipt["pluginId"], run_id, "boom", {})
    assert boom["ok"] is False and boom["code"] == "handler-error"
    assert "ValueError: boom" in boom["message"]
    bad = await runner.invoke(receipt["pluginId"], run_id, "bad", {})
    assert bad["ok"] is False and bad["code"] == "handler-error"
    assert "not lossless JSON" in bad["message"]


async def test_run_invalid_modes():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent)
    pid, did = receipt["pluginId"], receipt["packageId"]
    assert (await runner.run(agent, pid, did, "update"))["reason"] \
        == "invalid-mode"
    assert (await runner.run(agent, "missing", did, "run"))["reason"] \
        == "plugin-missing"
    assert (await runner.run(agent, pid, "dyn-999", "run"))["reason"] \
        == "package-missing"
    assert (await runner.run(agent, pid, did, "reload"))["reason"] \
        == "invalid-mode"
    assert (await runner.run(agent, pid, did, "run"))["ok"] is True
    # 重跑当前版本：run 允许（幂等重启）
    second = _define(runner, agent, name="v2",
                     plugin={"kind": "existing", "pluginId": pid})
    assert (await runner.run(agent, pid, second["packageId"], "run"))[
        "reason"] == "invalid-mode"  # 应 update
    assert (await runner.run(agent, pid, did, "update"))["reason"] \
        == "invalid-mode"  # did 已当前


async def test_run_update_flow():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    first = _define(runner, agent)
    await runner.run(agent, first["pluginId"], first["packageId"], "run")
    second = _define(runner, agent, name="v2",
                     plugin={"kind": "existing",
                             "pluginId": first["pluginId"]})
    response = await runner.run(agent, first["pluginId"],
                                second["packageId"], "update")
    assert response["ok"] is True
    assert response["currentPackageId"] == second["packageId"]
    row = runner.snapshot(agent)[0]
    assert row["currentPackageId"] == second["packageId"]
    assert row["activeRun"]["packageId"] == second["packageId"]
    assert [p["packageId"] for p in row["packages"]] == \
        [first["packageId"], second["packageId"]]


async def test_run_client_half_refused():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent, code={"client": "console.log(1)"})
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is False
    assert response["reason"] == "client-half-failed"
    assert "browser client runtime" in response["message"]
    row = runner.snapshot(agent)[0]
    assert row["latestRun"]["status"] == "failed"
    assert row["latestRun"]["error"]["phase"] == "client-load"


async def test_run_requires_approval_granted():
    ctx, runner = await _runner_ctx({"requires_approval": True})
    agent = _fake_agent()
    receipt = _define(runner, agent)
    seen = []
    ctx.events.on("cordis/request-run", lambda p: seen.append(("req", p)))
    ctx.events.on("cordis/request-run-resolved",
                  lambda p: seen.append(("res", p)))

    async def allow(payload, next):
        return True
    dispose = ctx.events.on("approval/request", allow)
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    dispose()
    assert response["ok"] is True and response["status"] == "running"
    assert seen[0][0] == "req" and seen[0][1]["requiresApproval"] is True
    assert seen[1][0] == "res" and seen[1][1]["outcome"] == "approved"


async def test_run_requires_approval_rejected():
    ctx, runner = await _runner_ctx({"requires_approval": True})
    agent = _fake_agent()
    receipt = _define(runner, agent)
    resolved = []
    ctx.events.on("cordis/request-run-resolved", resolved.append)

    async def deny(payload, next):
        return False
    dispose = ctx.events.on("approval/request", deny)
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    dispose()
    assert response["ok"] is False and response["reason"] == "rejected"
    assert resolved[0]["outcome"] == "rejected"
    plugin = runner._registry.get(receipt["pluginId"])
    assert plugin["latest_run"].status == "failed"
    assert plugin["latest_run"].error.phase == "approval"
    assert plugin["current_package_id"] is None


async def test_run_transition_in_flight_and_stop_cancels():
    ctx, runner = await _runner_ctx({"requires_approval": True})
    agent = _fake_agent()
    receipt = _define(runner, agent)
    gate = asyncio.Event()

    async def slow_approve(payload, next):
        await gate.wait()
        return True
    dispose = ctx.events.on("approval/request", slow_approve)
    first_task = asyncio.ensure_future(
        runner.run(agent, receipt["pluginId"], receipt["packageId"], "run"))
    await asyncio.sleep(0.05)  # 让第一个进入 awaiting-approval
    second = await runner.run(agent, receipt["pluginId"],
                              receipt["packageId"], "run")
    assert second["reason"] == "transition-in-flight"
    # stop 撤销挂起请求
    resolved = []
    ctx.events.on("cordis/request-run-resolved", resolved.append)
    stop = await runner.stop(agent, receipt["pluginId"])
    assert stop["ok"] is True
    gate.set()
    first = await first_task
    dispose()
    assert first["reason"] == "cancelled"
    assert resolved and resolved[0]["outcome"] == "cancelled"


async def test_stop_and_undefine():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent)
    await runner.run(agent, receipt["pluginId"], receipt["packageId"], "run")
    retracts = []
    ctx.events.on("cordis/dynamic-retract", retracts.append)
    stop = await runner.stop(agent, receipt["pluginId"])
    assert stop["ok"] is True
    assert retracts and retracts[0]["pluginId"] == receipt["pluginId"]
    assert (await runner.stop(agent, receipt["pluginId"]))["reason"] \
        == "not-running"
    plugin = runner._registry.get(receipt["pluginId"])
    assert plugin["latest_run"].status == "stopped"
    assert plugin["run"] is None
    # undefine：保留版本、删除插件
    await runner.undefine(agent, receipt["pluginId"])
    assert runner._registry.get(receipt["pluginId"]) is None
    missing = await runner.undefine(agent, receipt["pluginId"])
    assert missing["ok"] is False and missing["reason"] == "plugin-missing"
    # undefine 运行中插件
    receipt2 = _define(runner, agent)
    await runner.run(agent, receipt2["pluginId"], receipt2["packageId"], "run")
    removed = await runner.undefine(agent, receipt2["pluginId"])
    assert removed == {"ok": True, "wasRunning": True}
    assert (await runner.invoke(receipt2["pluginId"], "any", "double", {})
            )["code"] == "plugin-not-running"


async def test_host_half_failure_and_timeout():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    bad = _define(runner, agent, code={"host": "raise RuntimeError('nope')"})
    response = await runner.run(agent, bad["pluginId"], bad["packageId"],
                                "run")
    assert response["ok"] is False and response["reason"] == "host-half-failed"
    assert "RuntimeError: nope" in response["message"]
    plugin = runner._registry.get(bad["pluginId"])
    assert plugin["latest_run"].error.phase == "host-load"
    # 非插件返回值
    not_plugin = _define(runner, agent, code={"host": "return 42"})
    response = await runner.run(agent, not_plugin["pluginId"],
                                not_plugin["packageId"], "run")
    assert response["ok"] is False
    assert "must return a plugin" in response["message"]
    # 预算超时
    slow = _define(runner, agent, code={"host": (
        'import asyncio\n'
        'while True:\n'
        '    await asyncio.sleep(0.01)\n')})
    runner.vm_timeout_ms = 150
    response = await runner.run(agent, slow["pluginId"], slow["packageId"],
                                "run")
    assert response["ok"] is False
    assert "budget" in response["message"]


async def test_harness_duplicate_handle():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent, code={"host": (
        'async def one(args):\n    return 1\n'
        '\n'
        'def plugin(ctx):\n'
        '    harness.handle("m", one)\n'
        '    harness.handle("m", one)\n'
        '    return lambda: None\n'
        '\n'
        'return plugin\n')})
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is False
    assert "duplicate host method" in response["message"]


async def test_service_class_plugin():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent, code={"host": (
        'from dsh.kernel import Service\n'
        '\n'
        'class MyDyn(Service):\n'
        '    def apply(self, ctx):\n'
        '        from dsh.tools import define_tool\n'
        '        return ctx.tools.register(define_tool(\n'
        '            name="ping", description="p", parameters={},\n'
        '            output={"type": "string"})(\n'
        '                lambda args, run_ctx: "pong"))\n'
        '\n'
        'return MyDyn\n')})
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is True
    plugin = runner._registry.get(receipt["pluginId"])
    assert plugin["run"]["scope"].tools.get("ping") is not None
    assert ctx.tools.get("ping") is None


# ---- inspect 目录 ----

async def test_inspect_registry_builtin_and_query():
    ctx, runner = await _runner_ctx()
    providers = runner.inspect_registry.list()
    assert providers and providers[0]["platform"] == "host"
    names = {p["id"] for p in providers}
    assert "harness" in names
    ok = await runner.inspect_registry.query("harness", "ctx", {})
    assert ok["ok"] is True and ok["data"]["signatures"]
    assert (await runner.inspect_registry.query("missing", "m", {}))[
        "reason"] == "provider-missing"
    assert (await runner.inspect_registry.query("harness", "nope", {}))[
        "reason"] == "method-missing"
    # 内建 input schema 关闭对象 → 未知键 = invalid-input
    assert (await runner.inspect_registry.query(
        "harness", "ctx", {"extra": 1}))["reason"] == "invalid-input"


async def test_inspect_register_provider():
    ctx, runner = await _runner_ctx()
    runner.inspect_registry.register_provider("mine", "demo", [{
        "name": "double",
        "description": "翻倍",
        "input_schema": {"type": "object", "required": ["n"],
                         "properties": {"n": {"type": "integer"}}},
        "output_schema": {"type": "object",
                          "properties": {"v": {"type": "integer"}}},
        "call": lambda input: {"v": input["n"] * 2},
    }])
    ok = await runner.inspect_registry.query("mine", "double", {"n": 3})
    assert ok == {"ok": True, "data": {"v": 6}}
    assert (await runner.inspect_registry.query("mine", "double", {"n": "x"}
                                                ))["reason"] == "invalid-input"


# ---- 工具（经注册表管线） ----

async def test_cordis_tools_registered_and_define(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        for name in ("cordis_define", "cordis_run", "cordis_stop",
                     "cordis_undefine", "cordis_inspect_list",
                     "cordis_inspect_query", "cordis_inspect_self"):
            assert ctx.tools.get(name) is not None, f"{name} 未注册"
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        result = await ctx.tools.execute(
            "c1", "cordis_define",
            {"name": "doubler", "purpose": "测试插件",
             "plugin": {"kind": "new", "idPrefix": "tst"},
             "code": {"host": HOST_DOUBLER}},
            agent=agent, scope=agent.ctx_name)
        assert not result.is_error, result.content
        receipt = result.value
        assert receipt["pluginId"].startswith("tst-")
        # 经管线跑 run（含 mode 校验错误路径）
        bad = await ctx.tools.execute(
            "c2", "cordis_run",
            {"pluginId": receipt["pluginId"],
             "packageId": receipt["packageId"], "mode": "update"},
            agent=agent, scope=agent.ctx_name)
        assert not bad.is_error and bad.value["reason"] == "invalid-mode"
        ok = await ctx.tools.execute(
            "c3", "cordis_run",
            {"pluginId": receipt["pluginId"],
             "packageId": receipt["packageId"], "mode": "run"},
            agent=agent, scope=agent.ctx_name)
        assert ok.value["ok"] is True and ok.value["status"] == "running"
        # invoke 经 runner
        runner = ctx.dynamicCordisRunner
        invoked = await runner.invoke(receipt["pluginId"],
                                      ok.value["pluginRunId"], "double",
                                      {"n": 5})
        assert invoked == {"ok": True, "value": {"value": 10}}
        # inspect_self
        snap = await ctx.tools.execute(
            "c4", "cordis_inspect_self", {}, agent=agent,
            scope=agent.ctx_name)
        assert snap.value[0]["pluginId"] == receipt["pluginId"]
    finally:
        await tree.dispose()


async def test_cordis_tools_require_agent(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        result = await ctx.tools.execute("c1", "cordis_inspect_list", {})
        assert not result.is_error  # list 不需要 agent
        result = await ctx.tools.execute(
            "c2", "cordis_define", {"name": "x", "purpose": "y",
                                    "plugin": {"kind": "new",
                                               "idPrefix": "tst"},
                                    "code": {"host": HOST_DOUBLER}})
        assert result.is_error and result.error.code == "NO_AGENT"
    finally:
        await tree.dispose()


# ---- e2e（agent 循环驱动 cordis 工具） ----

async def test_e2e_define_run_through_loop(tmp_path):
    from dsh.llm.mock import MockAdapter
    define_args = {"name": "doubler", "purpose": "翻倍",
                   "plugin": {"kind": "new", "idPrefix": "tst"},
                   "code": {"host": HOST_DOUBLER}}
    turns = [
        {"tool": {"name": "cordis_define", "call_id": "cd-1",
                  "arguments": define_args}},
        {"tool": {"name": "cordis_run", "call_id": "cr-1",
                  "arguments": {"pluginId": "PLACEHOLDER",
                                "packageId": "PLACEHOLDER", "mode": "run"}}},
        {"text": "完成"},
    ]
    # 第二轮 run 的 id 依赖第一轮收据 → 用 mock 前一轮结果注入：
    # 简化：先直接定义（同一会话），mock 只驱动 run + 结束
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        receipt = ctx.dynamicCordisRunner.define(define_args, agent.id)
        turns[1] = {"tool": {"name": "cordis_run", "call_id": "cr-1",
                             "arguments": {
                                 "pluginId": receipt["pluginId"],
                                 "packageId": receipt["packageId"],
                                 "mode": "run"}}}
        ctx.llm.register_adapter(MockAdapter(script=turns))
        events = []
        ctx.events.on("cordis/dynamic-package", events.append)
        agent.followup("定义并运行插件")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        results = [e for e in agent.session.events
                   if e.type == "tool/result"]
        assert len(results) == 2  # define + run
        assert events and events[0]["pluginId"] == receipt["pluginId"]
        invoked = await ctx.dynamicCordisRunner.invoke(
            receipt["pluginId"], events[0]["pluginRunId"], "double", {"n": 4})
        assert invoked == {"ok": True, "value": {"value": 8}}
    finally:
        await tree.dispose()


# ---- 幂等关闭 ----

async def test_runner_close_idempotent_and_disposes():
    ctx, runner = await _runner_ctx()
    agent = _fake_agent()
    receipt = _define(runner, agent, code={"host": (
        'def plugin(ctx):\n'
        '    def cleanup():\n'
        '        return None\n'
        '    return cleanup\n'
        '\n'
        'return plugin\n')})
    response = await runner.run(agent, receipt["pluginId"],
                                receipt["packageId"], "run")
    assert response["ok"] is True
    plugin = runner._registry.get(receipt["pluginId"])
    scope = plugin["run"]["scope"]
    assert not scope._disposed
    runner.close()
    runner.close()  # 幂等
    if runner._retract_task is not None:
        await runner._retract_task
    assert scope._disposed
    assert runner._registry.get(receipt["pluginId"]) is None
    assert (await runner.invoke(receipt["pluginId"], "any", "m", {})
            )["code"] == "plugin-not-running"
