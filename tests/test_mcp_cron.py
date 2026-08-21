"""MCP 与 cron 测试。"""
import asyncio
import os
import sys
from datetime import datetime

import pytest

from dsh.boot import boot
from dsh.kernel import Context
from dsh.mcp import McpClient, McpServerPlugin, safe_schema
from dsh.schedule import CronError, next_fire_time, parse_cron
from dsh.schedule.schedule import ScheduleService
from dsh.tools import ToolRuntime

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "mock_mcp_server.py")


# ---- cron 解析 ----

def test_cron_parse_and_matches():
    spec = parse_cron("*/5 * * * *")
    assert spec.matches(datetime(2026, 1, 1, 0, 5, 0))
    assert not spec.matches(datetime(2026, 1, 1, 0, 6, 0))

    workday = parse_cron("0 9 * * 1-5")
    monday = datetime(2026, 1, 5, 9, 0, 0)   # 周一
    sunday = datetime(2026, 1, 4, 9, 0, 0)   # 周日
    assert workday.matches(monday)
    assert not workday.matches(sunday)

    six = parse_cron("*/2 * * * * *")        # 每 2 秒
    assert six.matches(datetime(2026, 1, 1, 0, 0, 2))
    assert not six.matches(datetime(2026, 1, 1, 0, 0, 3))


def test_cron_next_after():
    nxt = next_fire_time("0 9 * * *", datetime(2026, 1, 1, 8, 0, 0))
    assert nxt == datetime(2026, 1, 1, 9, 0, 0)
    # 每秒表达式：下一触发 = now+1 秒
    now = datetime(2026, 1, 1, 0, 0, 0)
    assert next_fire_time("* * * * * *", now) == datetime(2026, 1, 1, 0, 0, 1)


def test_cron_invalid():
    with pytest.raises(CronError):
        parse_cron("61 * * * *")
    with pytest.raises(CronError):
        parse_cron("1 2 3")           # 字段数错误
    with pytest.raises(CronError):
        parse_cron("a * * * *")


# ---- schedule cron 条目 ----

async def test_schedule_cron_entry_fires(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        schedule = ScheduleService(ctx, {})
        schedule.apply(ctx)
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        schedule.register("每秒钟检查", schedule="* * * * * *")
        await asyncio.sleep(2.4)
        snapshot = agent.inbox.snapshot()
        assert len(snapshot["next_step"]) >= 2  # 至少触发两次
        schedule.close()
    finally:
        await tree.dispose()


async def test_schedule_register_tool_cron_and_validation(tmp_path):
    from dsh.schedule import build_schedule_tools
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        ctx.schedule.close()  # 停掉 boot 里的循环，避免干扰
        tools = [t for t in build_schedule_tools()
                 if t.name == "schedule_register"][0]
        from dsh.tools.pipeline import ToolExecution, ToolRunContext
        run_ctx = ToolRunContext(
            execution=ToolExecution(call_id="c1", name="schedule_register",
                                    arguments={}), root_ctx=ctx)
        result = await tools.execute(
            {"prompt": "p", "schedule": "0 9 * * *"}, run_ctx)
        assert result.startswith("scheduled: ")
        with pytest.raises(Exception):
            await tools.execute({"prompt": "p", "schedule": "bad"},
                                run_ctx)
    finally:
        await tree.dispose()


# ---- MCP ----

async def test_mcp_client_discover_and_call():
    ctx = Context("mcp")
    tools = ToolRuntime(ctx, {})
    tools.apply(ctx)
    plugin = McpServerPlugin(ctx, {
        "command": [sys.executable, FIXTURE]})
    cleanup = plugin.apply(ctx)
    await plugin.start()
    try:
        echo = ctx.tools.get("echo")
        assert echo is not None
        result = await ctx.tools.execute("call-1", "echo", {"text": "hi"})
        assert not result.is_error
        assert "echoed: hi" in result.content
        # anyOf schema 降级为开放对象
        assert ctx.tools.get("lenient").parameters == {"type": "object"}
        result = await ctx.tools.execute("call-2", "lenient",
                                         {"value": 42})
        assert not result.is_error and "42" in result.content
    finally:
        await cleanup()
        plugin.close()
        await asyncio.sleep(0.05)


async def test_mcp_client_stop_kills_process():
    client = McpClient([sys.executable, FIXTURE])
    await client.start()
    tools = await client.list_tools()
    assert {t["name"] for t in tools} == {"echo", "lenient"}
    pid = client.process.pid
    await client.stop()
    import subprocess
    # Windows 上进程应已终止（轮询确认）
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"MCP server pid {pid} still alive after stop()")


def test_mcp_safe_schema():
    assert safe_schema(None) == {"type": "object"}
    ok = {"type": "object",
          "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert safe_schema(ok) == ok
    # anyOf 超出子集 → 降级
    degraded = safe_schema({"anyOf": [{"type": "string"}]})
    assert degraded == {"type": "object"}
