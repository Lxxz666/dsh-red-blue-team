"""补齐批次测试：sessionTitle/telemetry/settings/storage/web/ask_user/
request-context/sqlite/subagent事件/skills/hooks/presets/schedule/sandbox。"""
import asyncio
import os
from types import SimpleNamespace

import pytest

from dsh.boot import boot
from dsh.errors import ToolError
from dsh.kernel import Context
from dsh.session import SessionStore, SessionTitleService
from dsh.telemetry.service import SessionTelemetryService
from dsh.settings.service import SettingsService
from dsh.storage.service import StorageService
from dsh.tools import ToolRuntime, define_tool


# ---- sessionTitle ----

async def test_session_title_default(tmp_path):
    ctx = Context("title")
    title = SessionTitleService(ctx, {})
    title.apply(ctx)
    store = SessionStore(ctx, {})
    store.apply(ctx)
    session = store.create()
    session.append("user/message", {"content": "你好，帮我写一个函数" * 10,
                                    "source": {"kind": "user"}},
                   surface_op="append")
    result = title.title_for(session)
    assert result.startswith("你好，帮我写一个函数")
    assert len(result) <= 60


# ---- telemetry ----

async def test_telemetry_record_seam(tmp_path):
    ctx = Context("tel")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    telemetry = SessionTelemetryService(ctx, {})
    telemetry.apply(ctx)
    seen = []
    ctx.on("session-telemetry/record", lambda record: seen.append(record))
    session = store.create()
    session.append("user/message", {"content": "x", "source": {"kind": "user"}},
                   surface_op="append")
    await store.flush(session)
    assert seen and seen[0]["session"] == session.id


# ---- settings / storage ----

async def test_settings_round_trip(tmp_path):
    ctx = Context("settings")
    settings = SettingsService(ctx, {"path": str(tmp_path / "settings.json")})
    settings.apply(ctx)
    updated = []
    ctx.on("settings/updated", lambda p: updated.append(p))
    settings.set("theme", "dark")
    assert settings.get("theme") == "dark"
    assert updated and updated[0]["key"] == "theme"
    # 重载持久化
    ctx2 = Context("settings2")
    settings2 = SettingsService(ctx2, {"path": str(tmp_path / "settings.json")})
    assert settings2.get("theme") == "dark"


async def test_storage_domain_changed(tmp_path):
    ctx = Context("storage")
    storage = StorageService(ctx, {"path": str(tmp_path / "storage.json")})
    storage.apply(ctx)
    changed = []
    ctx.on("domain/changed", lambda p: changed.append(p["domain"]))
    storage.put("schedule", "job-1", {"interval": 1})
    assert storage.get("schedule", "job-1") == {"interval": 1}
    assert "schedule" in changed
    assert storage.domains() == ["schedule"]


# ---- web 工具 ----

async def test_web_fetch_bad_scheme():
    from dsh.web.tool import build_web_tools
    tool = build_web_tools()[0]
    from dsh.tools.pipeline import ToolExecution, ToolRunContext
    exec_ = ToolExecution(call_id="c1", name="web_fetch", arguments={})
    ctx = ToolRunContext(execution=exec_)
    with pytest.raises(ToolError) as excinfo:
        await tool.execute({"url": "ftp://x"}, ctx)
    assert excinfo.value.code == "BAD_URL"


# ---- ask_user ----

async def test_ask_user_channel_and_no_channel():
    from dsh.interaction.user_questions import (UserQuestionsService,
                                                build_ask_user_tool)
    ctx = Context("questions")
    questions = UserQuestionsService(ctx, {})
    questions.apply(ctx)
    tool = build_ask_user_tool()
    from dsh.tools.pipeline import ToolExecution, ToolRunContext

    async def channel(question, detail):
        return f"回答:{question}"
    questions.set_channel(channel)
    run_ctx = ToolRunContext(execution=ToolExecution(
        call_id="c1", name="ask_user", arguments={}), root_ctx=ctx)
    result = await tool.execute({"question": "喜欢什么颜色"}, run_ctx)
    assert result == "回答:喜欢什么颜色"
    questions.set_channel(None)
    with pytest.raises(ToolError) as excinfo:
        await tool.execute({"question": "x"}, run_ctx)
    assert excinfo.value.code == "NO_CHANNEL"


# ---- request/context ----

async def test_request_context_logged(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("hi")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        context = agent.session.request_context()
        assert context is not None
        assert context["provider"] == "mock"
        assert context.get("context_window") == 8192  # MockAdapter 广告
    finally:
        await tree.dispose()


# ---- SQLite 后端 ----

async def test_sqlite_backend_parity(tmp_path):
    from dsh.persistence.sqlite import SqlitePersistence
    ctx = Context("sqlite")
    persistence = SqlitePersistence(ctx, {"path": str(tmp_path / "s.db")})
    persistence.apply(ctx)
    store = SessionStore(ctx, {})
    store.apply(ctx)
    session = store.create(meta={"cwd": str(tmp_path)})
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "hi", "source": {"kind": "user"}},
                   surface_op="append")
    await persistence.flush(session)
    header, rows = await persistence.load(session.id)
    assert header.id == session.id
    assert rows[-1]["type"] == "turn/end"  # 崩溃修复
    assert rows[-1]["data"]["reason"] == {"kind": "interrupted"}
    assert session.id in await persistence.list_ids()
    assert persistence.locate(session) is None


# ---- subagent start/end 事件 ----

async def test_subagent_start_end_events():
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.subagent import (InProcessProviderPlugin, SubagentRegistry,
                              ToolSubagentPlugin)
    from dsh.tools import ToolRuntime
    ctx = Context("sub-events")
    store = SessionStore(ctx, {}); store.apply(ctx)
    SystemPromptService(ctx, {}).apply(ctx)
    tools = ToolRuntime(ctx, {}); tools.apply(ctx)
    llm = LlmRuntime(ctx, {}); llm.apply(ctx)
    llm.register_adapter(MockAdapter(script=[
        {"tool": {"name": "subagent", "arguments": {"description": "d",
                                                    "prompt": "回复 ok"}}}]))
    registry = AgentRegistry(ctx, {}); registry.apply(ctx)
    loop = AgentLoopService(ctx, {}); loop.apply(ctx)
    ApprovalService(ctx, {}).apply(ctx)
    SubagentRegistry(ctx, {}).apply(ctx)
    InProcessProviderPlugin(ctx, {}).apply(ctx)
    ToolSubagentPlugin(ctx, {}).apply(ctx)
    events = []
    ctx.on("subagent/start", lambda p: events.append(("start", p["parent"])))
    ctx.on("subagent/end", lambda p: events.append(("end", p["ok"])))
    parent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    parent.followup("委托")
    await parent.when_idle()
    await asyncio.sleep(0.05)
    assert events[0][0] == "start"
    assert events[-1] == ("end", True)


# ---- skills ----

async def test_skills_discovery_and_load(tmp_path):
    from dsh.skill.skill import FilesystemSkillProvider, SkillService
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: 我的技能\ndescription: 测试\n---\n步骤一：做 A。",
        encoding="utf-8")
    ctx = Context("skills")
    service = SkillService(ctx, {})
    service.apply(ctx)
    service.register_provider(FilesystemSkillProvider(paths=[str(tmp_path)]))
    skills = service.list()
    assert len(skills) == 1 and skills[0].id == "my-skill"
    assert service.get("my-skill").instructions.startswith("步骤一")


# ---- hooks ----

async def test_hooks_pre_tool_deny(tmp_path):
    import sys
    from dsh.hooks.hooks import HooksPlugin
    from dsh.subprocess.local import SubprocessService
    from dsh.tools.pipeline import ToolExecution, ToolRunContext
    ctx = Context("hooks")
    subprocess = SubprocessService(ctx, {})
    subprocess.apply(ctx)
    store = SessionStore(ctx, {})
    store.apply(ctx)
    hooks_path = tmp_path / "hooks.yml"
    command = "Write-Error blocked; exit 1" if sys.platform == "win32" \
        else "echo blocked >&2; exit 1"
    hooks_path.write_text(
        f"hooks:\n  - name: guard\n    on: pre-tool\n    tools: [add]\n"
        f"    command: \"{command}\"\n    decision: deny\n",
        encoding="utf-8")
    plugin = HooksPlugin(ctx, {"path": str(hooks_path)})
    plugin.apply(ctx)
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)

    @define_tool(name="add", description="加", parameters={}, output={"type": "number"})
    async def add(args, run_ctx):
        return 1
    tools.register(add)
    fake_agent = SimpleNamespace(session=store.create(), ctx=ctx)
    result = await tools.execute("c1", "add", {}, agent=fake_agent)
    assert result.is_error and result.error.code == "DENIED"
    types = [e.type for e in fake_agent.session.events]
    assert "hook/invoked" in types and "hook/result" in types


# ---- presets ----

async def test_agent_preset_mount(tmp_path):
    from dsh.preset.presets import AgentPresets
    preset_file = tmp_path / "greet.yml"
    preset_file.write_text(
        "- id: greet-tool\n  plugin: examples.my_plugin:MyPlugin\n"
        "  config: { greeting: 早安 }\n", encoding="utf-8")
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True,
                           extra_patches=[([{"insert": [{
                               "id": "presets",
                               "plugin": "dsh.preset.presets:AgentPresets",
                               "config": {"paths": [str(tmp_path)]}}]}],
                               "test")])
    try:
        agent = await ctx.agents.create(
            options={"provider": "mock", "model": "mock"},
            meta={"agent_preset": "greet"})
        assert ctx.agentPresets.composed_preset(agent.ctx) == "greet"
        assert agent.ctx.tools.get("greet") is not None     # 作用域可见
        assert ctx.tools.get("greet") is None               # 全局不可见
    finally:
        await tree.dispose()


async def test_agent_preset_recompose(tmp_path):
    """运行期换绑：卸旧树挂新树，旧工具消失、新工具可见、全局不可见。"""
    (tmp_path / "greet.yml").write_text(
        "- id: greet-tool\n  plugin: examples.my_plugin:MyPlugin\n"
        "  config: { greeting: 早安 }\n", encoding="utf-8")
    (tmp_path / "farewell.yml").write_text(
        "- id: farewell-tool\n  plugin: examples.my_plugin:MyPlugin\n"
        "  config: { greeting: 再见, tool: farewell }\n", encoding="utf-8")
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True,
                           extra_patches=[([{"insert": [{
                               "id": "presets",
                               "plugin": "dsh.preset.presets:AgentPresets",
                               "config": {"paths": [str(tmp_path)]}}]}],
                               "test")])
    try:
        agent = await ctx.agents.create(
            options={"provider": "mock", "model": "mock"},
            meta={"agent_preset": "greet"})
        assert agent.ctx.tools.get("greet") is not None
        result = await ctx.agentPresets.recompose(agent.ctx, "farewell")
        assert result["preset"] == "farewell" and result["recomposed"] is True
        assert ctx.agentPresets.composed_preset(agent.ctx) == "farewell"
        assert agent.ctx.tools.get("farewell") is not None   # 新工具可见
        assert agent.ctx.tools.get("greet") is None          # 旧工具已卸载
        assert ctx.tools.get("farewell") is None             # 全局不可见
        # 再次换绑（幂等路径）
        await ctx.agentPresets.recompose(agent.ctx, "greet")
        assert agent.ctx.tools.get("greet") is not None
        assert agent.ctx.tools.get("farewell") is None
    finally:
        await tree.dispose()


async def test_agent_request_done_event(tmp_path):
    """每次成功的模型请求广播 agent/request-done（provider/model/usage/latency）。"""
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        seen = []
        ctx.events.on("agent/request-done", seen.append)
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("你好")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        assert seen, "agent/request-done 未触发"
        payload = seen[0]
        assert payload["provider"] == "mock" and payload["model"] == "mock"
        assert isinstance(payload["latency_ms"], int) and payload["latency_ms"] >= 0
        assert payload["usage"] is None or isinstance(payload["usage"], dict)
        assert payload["turn"] == 1 and payload["step"] == 1
    finally:
        await tree.dispose()

async def test_schedule_injects_notification(tmp_path):
    from dsh.schedule.schedule import ScheduleService
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        schedule = ScheduleService(ctx, {})
        schedule.apply(ctx)
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        schedule.register("检查一下", interval_seconds=0.2)
        await asyncio.sleep(1.2)
        snapshot = agent.inbox.snapshot()
        assert len(snapshot["next_step"]) >= 1
        schedule.close()
    finally:
        await tree.dispose()


# ---- settings → agentDefaultModel ----

async def test_settings_drive_agent_default_model(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True,
                           extra_patches=[([{"id": "settings",
                                             "config": {"path": str(tmp_path /
                                                                    "settings.json")}}],
                                           "test")])
    try:
        ctx.settings.set("agent_default_model",
                         {"provider": "mock", "model": "mock"})
        selection = ctx.agentDefaultModel.current_selection()
        assert selection.get("provider") == "mock"
        assert selection.get("model") == "mock"
    finally:
        await tree.dispose()


# ---- sandbox 缝 ----

async def test_sandbox_seam_identity(tmp_path):
    import sys
    from dsh.sandbox.sandbox import SandboxService
    ctx = Context("sandbox")
    # 显式 local 模式：任何平台都是 identity
    sandbox = SandboxService(ctx, {"mode": "local"})
    sandbox.apply(ctx)
    argv = ["bash", "-lc", "echo hi"]
    assert sandbox.confine(argv, str(tmp_path)) == argv
    describe = sandbox.describe()
    assert describe["confinement"] == "none (identity)"
    assert describe["active"] is False
    sandbox.attach(12345)  # identity 后端 no-op 不抛错
    sandbox.close()
    sandbox.close()  # 幂等
    # 未知模式 fail loud
    from dsh.errors import ToolError
    with pytest.raises(ToolError):
        SandboxService(Context("bad"), {"mode": "weird"})
