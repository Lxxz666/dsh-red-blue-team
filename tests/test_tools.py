"""dsh.tools 测试：注册表、restrict、守卫管线。"""
import asyncio
import pytest

from dsh.kernel import Context
from dsh.tools import (AbortSignal, AllowDecision, BlockDecision,
                       DenyDecision, ToolRuntime, define_tool)
from dsh.errors import ToolArgsError


async def make_runtime():
    ctx = Context()
    runtime = ToolRuntime(ctx, {})
    runtime.apply(ctx)
    return ctx, runtime


@define_tool(name="add", description="两数相加",
             parameters={"a": {"type": "number", "required": True},
                         "b": {"type": "number", "required": True}},
             output={"type": "number"})
async def add_tool(args, run_ctx):
    return args["a"] + args["b"]


@define_tool(name="slow", description="慢工具",
             parameters={}, output={"type": "string"}, timeout_ms=50)
async def slow_tool(args, run_ctx):
    await asyncio.sleep(1)
    return "done"


async def test_register_get_schemas():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)
    assert runtime.get("add") is add_tool
    schemas = runtime.schemas()
    assert schemas[0]["function"]["name"] == "add"
    assert "execute" not in str(schemas)  # 回调不泄漏
    # 重名拒绝
    with pytest.raises(Exception):
        runtime.register(add_tool)


async def test_execute_success_pipeline():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)
    result = await runtime.execute("call-1", "add", {"a": 1, "b": 2})
    assert result.is_error is False
    assert result.value == 3
    assert "3" in result.content


async def test_execute_invalid_args():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)
    result = await runtime.execute("call-1", "add", {"a": "x"})
    assert result.is_error
    assert result.error.code == "INVALID_ARGS"


async def test_execute_unknown_tool():
    ctx, runtime = await make_runtime()
    result = await runtime.execute("call-1", "nope", {})
    assert result.is_error
    assert result.error.code == "UNKNOWN_TOOL"


async def test_output_schema_enforced():
    ctx, runtime = await make_runtime()

    @define_tool(name="bad-output", description="返回字符串但声明 number",
                 parameters={}, output={"type": "number"})
    async def bad_output(args, run_ctx):
        return "not-a-number"

    runtime.register(bad_output)
    result = await runtime.execute("call-1", "bad-output", {})
    assert result.is_error
    assert result.error.code == "INVALID_TOOL_OUTPUT"


async def test_pre_execute_deny_waterfall():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)

    async def gate(execution, next):
        return DenyDecision("policy says no")

    ctx.on("tools/pre-execute", gate)
    result = await runtime.execute("call-1", "add", {"a": 1, "b": 2})
    assert result.is_error
    assert "policy says no" in result.error.message


async def test_pre_execute_ask_without_approver_denies():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)

    async def gate(execution, next):
        return __import__("dsh.tools.pipeline", fromlist=["AskDecision"]).AskDecision("confirm?")

    ctx.on("tools/pre-execute", gate)
    result = await runtime.execute("call-1", "add", {"a": 1, "b": 2})
    assert result.is_error


async def test_guard_monotonic():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)
    runtime.guard(lambda execution: "blocked by guard" if execution.name == "add" else None)
    result = await runtime.execute("call-1", "add", {"a": 1, "b": 2})
    assert result.is_error
    assert result.error.code == "GUARDED"


async def test_post_execute_block():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)

    async def post(execution, result, next):
        return BlockDecision(feedback="result not allowed")

    ctx.on("tools/post-execute", post)
    result = await runtime.execute("call-1", "add", {"a": 1, "b": 2})
    assert result.is_error
    assert result.error.code == "BLOCKED"


async def test_timeout():
    ctx, runtime = await make_runtime()
    runtime.register(slow_tool)
    result = await runtime.execute("call-1", "slow", {})
    assert result.is_error
    assert result.error.code == "TIMEOUT"


async def test_restrict_and_scoped_shadow():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)
    scope = "agent-1"
    runtime.restrict({"deny": ["add"]}, scope=scope)
    assert runtime.get("add", scope) is None

    @define_tool(name="local", description="作用域自有工具", parameters={})
    async def local(args, run_ctx):
        return "ok"

    runtime.register(local, scope=scope)
    assert runtime.get("local", scope) is local
    assert runtime.get("local") is None  # 全局不可见


async def test_execution_mode_fail_closed():
    ctx, runtime = await make_runtime()
    runtime.register(add_tool)  # 未声明并发安全
    assert runtime.execution_mode("add", {"a": 1, "b": 2}) == "exclusive"
    assert runtime.execution_mode("nope", {}) == "exclusive"
