"""dsh.persistence / dsh.config / dsh.fs 测试。"""
import os

import pytest

from dsh.boot import boot
from dsh.fs.local import LocalFsService
from dsh.kernel import Context
from dsh.persistence.jsonl import JsonlPersistence
from dsh.session import SessionStore
from dsh.errors import ToolError


# ---- persistence ----

async def test_jsonl_write_load_and_crash_repair(tmp_path):
    ctx = Context("persist")
    persistence = JsonlPersistence(ctx, {"dir": str(tmp_path)})
    persistence.apply(ctx)
    store = SessionStore(ctx, {})
    store.apply(ctx)

    session = store.create(meta={"cwd": str(tmp_path)})
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "hi", "source": {"kind": "user"}},
                   surface_op="append")
    # 模拟崩溃：没有 turn/end 就 flush
    await persistence.flush(session)

    header, rows = await persistence.load(session.id)
    assert header.id == session.id
    # 崩溃修复追加 interrupted
    assert rows[-1]["type"] == "turn/end"
    assert rows[-1]["data"]["reason"] == {"kind": "interrupted"}

    ids = await persistence.list_ids()
    assert session.id in ids
    assert persistence.locate(session) is not None


# ---- config ----

async def test_boot_base_bundle(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        for key in ("sessions", "tools", "llm", "agents", "agentLoop",
                    "systemPrompt", "fs", "subprocess", "goals", "compaction",
                    "commands", "planMode", "jobs", "subagents", "approval",
                    # 补齐批次新增缝
                    "sessionTitle", "sessionTelemetry", "settings", "storage",
                    "skills", "userQuestions", "schedule", "sandbox",
                    "agentPresets", "sessionPersistence",
                    # 第二批新增缝
                    "credentials", "tokenMeter", "sessionQuery",
                    "messageFeedback", "workflowEngine"):
            assert ctx.has(key), key
        assert "mock" in ctx.llm.providers()
        assert ctx.tools.get("fs_read") is not None
        assert ctx.tools.get("bash") is not None
        assert ctx.tools.get("todo_write") is not None
        assert ctx.tools.get("subagent") is not None
        assert ctx.tools.get("web_fetch") is not None
        assert ctx.tools.get("ask_user") is not None
        assert ctx.tools.get("skill_list") is not None
        assert ctx.tools.get("schedule_register") is not None
        assert ctx.tools.get("workflow_run") is not None
    finally:
        await tree.dispose()


# ---- fs ----

async def test_local_fs_workspace_fence(tmp_path):
    ctx = Context("fs")
    fs = LocalFsService(ctx, {"root": str(tmp_path)})
    fs.apply(ctx)
    diff = await fs.write_text("a.txt", "hello")
    assert diff.old_text is None
    assert await fs.read_text("a.txt") == "hello"
    with pytest.raises(ToolError) as excinfo:
        await fs.read_text("../outside.txt")
    assert excinfo.value.code == "OUTSIDE_WORKSPACE"


async def test_fs_edit_unique_match(tmp_path):
    ctx = Context("fs")
    fs = LocalFsService(ctx, {"root": str(tmp_path)})
    fs.apply(ctx)
    await fs.write_text("a.txt", "one two")
    diff = await fs.edit_text("a.txt", "one", "ONE")
    assert "ONE two" in diff.new_text
    with pytest.raises(ToolError) as excinfo:
        await fs.edit_text("a.txt", "x", "y")
    assert excinfo.value.code == "NO_MATCH"


# ---- boot 后 fs 工具真实写盘 ----

async def test_fs_tool_through_boot(tmp_path):
    import asyncio
    from dsh.llm.mock import MockAdapter

    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        ctx.llm.register_adapter(MockAdapter(script=[
            {"tool": {"name": "fs_write",
                      "arguments": {"path": "hello.txt", "content": "hi"}}},
            {"text": "完成。"},
        ]))
        agent = await ctx.agents.create()
        agent.followup("写个文件")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        assert (tmp_path / "hello.txt").exists()
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"
        results = [e for e in agent.session.events if e.type == "tool/result"]
        assert results and not results[0].data["is_error"]
    finally:
        await tree.dispose()
