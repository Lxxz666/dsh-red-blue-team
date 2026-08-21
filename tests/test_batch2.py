"""第二批补齐功能测试：credentials / tokenMeter / AGENTS.md / toolResultPruner /
runMaintenance / sessionQuery / messageFeedback / workflow。"""
import asyncio
import os
from types import SimpleNamespace

import pytest

from dsh.boot import boot
from dsh.kernel import Context
from dsh.credentials import CredentialsService
from dsh.llm.token_meter import TokenMeterService, TOKENS_PER_CHAR
from dsh.llm.messages import ContentBlock, Message


# ---- credentials ----

async def test_credentials_round_trip(tmp_path):
    ctx = Context("cred")
    service = CredentialsService(ctx, {"path": str(tmp_path / ".credentials.yaml")})
    service.apply(ctx)
    updated = []
    ctx.on("credentials/updated", lambda p: updated.append(p["name"]))
    service.set("deepseek", "sk-test-123")
    assert service.get("deepseek") == "sk-test-123"
    assert updated == ["deepseek"]
    assert service.names() == ["deepseek"]
    # 重载持久化
    ctx2 = Context("cred2")
    service2 = CredentialsService(ctx2, {"path": str(tmp_path / ".credentials.yaml")})
    assert service2.get("deepseek") == "sk-test-123"
    service.delete("deepseek")
    assert service.get("deepseek") is None


async def test_deepseek_adapter_credential_fallback(tmp_path):
    from dsh.llm.plugins import DeepSeekAdapterPlugin
    from dsh.llm.adapters import LlmRuntime
    ctx = Context("fallback")
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    credentials = CredentialsService(ctx, {"path": str(tmp_path / ".c.yaml")})
    credentials.apply(ctx)
    credentials.set("deepseek", "sk-from-credentials")
    # 清掉环境变量避免干扰
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        plugin = DeepSeekAdapterPlugin(ctx, {})
        plugin.apply(ctx)
        adapter = llm.get_adapter("deepseek")
        assert adapter.api_key == "sk-from-credentials"
        plugin.close()
        import asyncio as _asyncio
        await _asyncio.sleep(0.01)  # 让 close 的 aclose task 落地
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


# ---- tokenMeter ----

async def test_token_meter_estimate():
    ctx = Context("meter")
    meter = TokenMeterService(ctx, {})
    meter.apply(ctx)
    messages = [
        Message.user("x" * 100),
        Message.assistant([ContentBlock(kind="text", text="y" * 40)],
                          provider="mock", model="mock"),
    ]
    assert meter.estimate(messages) == 100 // TOKENS_PER_CHAR + 40 // TOKENS_PER_CHAR
    assert meter.count_text("a" * 8) == 2


async def test_compaction_policy_uses_token_meter(tmp_path):
    from dsh.compaction import CompactionPolicyPlugin, CompactionService
    from dsh.session import SessionStore
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    ctx = Context("compaction-meter")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    meter = TokenMeterService(ctx, {})
    meter.apply(ctx)
    service = CompactionService(ctx, {"threshold_tokens": 10, "keep_last_messages": 1})
    service.apply(ctx)
    policy = CompactionPolicyPlugin(ctx, {})
    policy.apply(ctx)

    agent = SimpleNamespace(session=store.create(), ctx_name="agent:x",
                            options={"provider": "mock", "model": "mock"},
                            _factory=SimpleNamespace(ctx=ctx))
    # 塞入足够长的消息使估算超阈值
    for i in range(3):
        agent.session.append("user/message",
                             {"content": "y" * 400, "source": {"kind": "user"}},
                             surface_op="append")
    called = []

    async def next_default():
        called.append(True)

    async def fake_next():
        return "enter"

    # 直接调用监听器（不经循环）：预检压缩是否被触发
    await policy._on_pre_step({"agent": agent}, fake_next)
    assert agent.session.events[-1].type == "compaction/summary"


# ---- AGENTS.md 指令注入 ----

async def test_agents_root_section(tmp_path):
    from dsh.context.instructions import AgentInstructionsPlugin
    from dsh.prompt import SystemPromptService
    from dsh.fs.local import LocalFsService
    (tmp_path / "AGENTS.md").write_text("根指令：遵守规范。", encoding="utf-8")
    ctx = Context("instr")
    prompt = SystemPromptService(ctx, {})
    prompt.apply(ctx)
    LocalFsService(ctx, {"root": str(tmp_path)}).apply(ctx)
    plugin = AgentInstructionsPlugin(ctx, {"interval": 10})
    plugin.apply(ctx)
    assembly = await prompt.assemble()
    assert "根指令" in assembly["text"]
    plugin.close()
    # 停掉轮询 task
    if plugin._watcher_task is not None:
        plugin._watcher_task.cancel()


async def test_agents_subdir_inject(tmp_path):
    from dsh.context.instructions import AgentInstructionsPlugin
    from dsh.prompt import SystemPromptService
    from dsh.fs.local import LocalFsService
    from dsh.session import SessionStore
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.tools import ToolRuntime
    ctx = Context("instr2")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    prompt = SystemPromptService(ctx, {})
    prompt.apply(ctx)
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
    LocalFsService(ctx, {"root": str(tmp_path)}).apply(ctx)
    plugin = AgentInstructionsPlugin(ctx, {"interval": 0.1})
    plugin.apply(ctx)
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    # 初始扫描
    await asyncio.sleep(0.2)
    # 新建子目录 AGENTS.md → 触发注入
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("子目录指令。", encoding="utf-8")
    await asyncio.sleep(0.4)
    snapshot = agent.inbox.snapshot()
    contents = " ".join(m["content"] for m in snapshot["next_step"])
    assert "子目录指令" in contents
    plugin.close()
    if plugin._watcher_task is not None:
        plugin._watcher_task.cancel()


# ---- toolResultPruner ----

async def test_tool_result_pruner():
    from dsh.compaction.pruner import ToolResultPrunerService
    from dsh.llm.messages import ContentBlock, Message
    ctx = Context("pruner")
    service = ToolResultPrunerService(ctx, {"max_chars": 50})
    service.apply(ctx)
    long = "x" * 200
    message = Message(
        id="m1", role="user",
        content=[ContentBlock(kind="tool-result", tool_call_id="c1",
                              content=long)],
        source={"kind": "tool"})
    pruned = service.prune([message])
    block = pruned[0].content[0]
    assert len(str(block.content)) < 120
    assert "截断" in block.content
    assert service.prune_text("short") == "short"


# ---- runMaintenance ----

async def test_run_maintenance_and_when_idle():
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.session import SessionStore
    from dsh.tools import ToolRuntime
    from dsh.errors import AgentError
    ctx = Context("maint")
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
    agent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})

    done = []

    async def task(signal):
        await asyncio.sleep(0.05)
        done.append("ran")
        return "ok"

    t = agent.run_maintenance(task)
    assert agent.status == "idle"  # 维护任务不改变公共状态
    await asyncio.sleep(0.1)
    assert done == ["ran"]
    assert await t == "ok"
    await agent.when_idle()

    # 驱动运行中不可再启维护任务：用「挂起直到取消」的慢适配器使 turn 确定处于 running
    from dsh.llm.adapters import LlmAdapter
    from dsh.llm.stream import StreamChunk

    class SlowAdapter(LlmAdapter):
        name = "mock"

        async def stream(self, request):
            await request.signal.wait()
            yield StreamChunk.finish("stop")

    ctx.llm.register_adapter(SlowAdapter())
    agent.followup("x")
    for _ in range(50):
        if agent.status == "running":
            break
        await asyncio.sleep(0.01)
    assert agent.status == "running"
    with pytest.raises(AgentError):
        agent.run_maintenance(task)
    agent.cancel({"kind": "user"})
    await agent.when_idle()
    await asyncio.sleep(0.05)


# ---- sessionQuery ----

async def test_session_query_list_search_trace(tmp_path):
    from dsh.persistence.jsonl import JsonlPersistence
    from dsh.session import SessionQueryService, SessionStore
    ctx = Context("query")
    persistence = JsonlPersistence(ctx, {"dir": str(tmp_path)})
    persistence.apply(ctx)
    store = SessionStore(ctx, {})
    store.apply(ctx)
    query = SessionQueryService(ctx, {})
    query.apply(ctx)

    s1 = store.create(meta={"cwd": str(tmp_path)})
    s1.append("user/message", {"content": "你好世界", "source": {"kind": "user"}},
              surface_op="append")
    await persistence.flush(s1)
    s2 = store.create(meta={"cwd": str(tmp_path), "origin": "subagent"})
    s2.append("user/message", {"content": "另一个话题", "source": {"kind": "user"}},
              surface_op="append")
    await persistence.flush(s2)

    listed = await query.list()
    assert {row["id"] for row in listed} == {s1.id, s2.id}
    only_sub = await query.list(origin="subagent")
    assert [row["id"] for row in only_sub] == [s2.id]
    found = await query.search("你好")
    assert len(found) == 1 and found[0]["session_id"] == s1.id
    trace = await query.trace(s1.id)
    assert trace["header"]["id"] == s1.id
    assert len(trace["events"]) >= 1


# ---- messageFeedback ----

async def test_message_feedback_round_trip(tmp_path):
    from dsh.feedback import MessageFeedbackService
    from dsh.storage.service import StorageService
    ctx = Context("feedback")
    storage = StorageService(ctx, {"path": str(tmp_path / "storage.json")})
    storage.apply(ctx)
    service = MessageFeedbackService(ctx, {})
    service.apply(ctx)
    updated = []
    ctx.on("message-feedback/updated",
           lambda p: updated.append(p["session_id"]))
    record = service.put("s1", 3, "up", note="很好")
    assert record["kind"] == "up"
    assert updated == ["s1"]
    assert service.get("s1") == [record]
    with pytest.raises(ValueError):
        service.put("s1", 4, "meh")
    # 持久化：新实例 restore
    ctx2 = Context("feedback2")
    storage2 = StorageService(ctx2, {"path": str(tmp_path / "storage.json")})
    storage2.apply(ctx2)
    service2 = MessageFeedbackService(ctx2, {})
    service2.apply(ctx2)
    assert service2.get("s1") == [record]


# ---- workflow ----

async def _wf_ctx():
    from dsh.agent import AgentLoopService, AgentRegistry, ApprovalService
    from dsh.llm.adapters import LlmRuntime
    from dsh.llm.mock import MockAdapter
    from dsh.prompt import PromptSection, SystemPromptService
    from dsh.session import SessionStore
    from dsh.subagent import InProcessProviderPlugin, SubagentRegistry
    from dsh.tools import ToolRuntime
    from dsh.workflow import WorkflowEngine
    ctx = Context("wf")
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
    SubagentRegistry(ctx, {}).apply(ctx)
    InProcessProviderPlugin(ctx, {}).apply(ctx)
    engine = WorkflowEngine(ctx, {})
    engine.apply(ctx)
    return ctx


async def test_workflow_engine_phases_parallel():
    ctx = await _wf_ctx()

    events = []
    for name in ("workflow/start", "workflow/phase", "workflow/agent-start",
                 "workflow/agent-end", "workflow/end"):
        ctx.on(name, lambda p, n=name: events.append(n))

    parent = await ctx.agents.create(options={"provider": "mock", "model": "mock"})
    result = await ctx.workflowEngine.run({
        "name": "demo",
        "phases": [
            {"title": "收集", "steps": [
                {"name": "a", "prompt": "回复: 1"},
                {"name": "b", "prompt": "回复: 2"}]},
            {"title": "汇总", "steps": [
                {"name": "c", "prompt": '回复: {"total": 3}'}]},
        ],
    }, parent)
    assert set(result["results"].keys()) == {"收集", "汇总"}
    assert result["results"]["汇总"]["c"] == {"total": 3}
    assert events[0] == "workflow/start"
    assert events[-1] == "workflow/end"
    assert "workflow/agent-start" in events


async def test_workflow_structured_output_enforcement():
    """step 声明 output schema 时按子集强制校验，失败标记 invalid_output。"""
    ctx = await _wf_ctx()
    parent = await ctx.agents.create(options={"provider": "mock",
                                              "model": "mock"})
    result = await ctx.workflowEngine.run({
        "name": "schema",
        "phases": [{"title": "t", "steps": [
            {"name": "ok", "prompt": '回复: {"answer": 42}',
             "output": {"type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"]}},
            {"name": "bad", "prompt": '回复: {"answer": 42}',
             "output": {"type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"]}},
            {"name": "num-bad", "prompt": "回复: 不是数字",
             "output": {"type": "number"}},
        ]}],
    }, parent)
    results = result["results"]["t"]
    assert results["ok"] == {"answer": 42}          # 通过校验 → 原值
    bad = results["bad"]
    assert bad["invalid_output"] is True and "error" in bad
    assert bad["value"] == {"answer": 42}           # 原值保留
    num_bad = results["num-bad"]
    assert num_bad["invalid_output"] is True
