"""Code Mode 测试：wire 坍缩 / presentAs / SDK 渲染 / runtime / run_code 桥 / 派发事件。"""
import asyncio
import json
import sys

import pytest

from dsh.boot import boot
from dsh.code.runtime import (CodeRunFailure, CodeRunResult, CodeRuntime,
                              CodeToolCallError)
from dsh.code.sdk import json_schema_to_py, render_tools_sdk_py
from dsh.errors import ToolError
from dsh.kernel import Context
from dsh.tools import ToolRuntime, define_tool

CODE_MODE_PATCH = [([{"id": "tools", "config": {"mode": "code"}}], "code-mode")]
BOTH_MODE_PATCH = [([{"id": "tools", "config": {"mode": "both"}}], "both-mode")]


# ---- wire 坍缩与 presentAs ----

async def test_mode_native_no_transport(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        names = {s["function"]["name"] for s in ctx.tools.schemas()}
        assert "run_code" not in names
        assert ctx.tools.get("run_code") is None
        # 保留名不可注册
        with pytest.raises(ToolError):
            ctx.tools.register(define_tool(
                name="run_code", description="x", parameters={},
                output={"type": "string"})(lambda args, run_ctx: "x"))
    finally:
        await tree.dispose()


async def test_mode_code_wire_collapse(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True, extra_patches=CODE_MODE_PATCH)
    try:
        schemas = ctx.tools.schemas()
        assert [s["function"]["name"] for s in schemas] == ["run_code"]
        assembly = await ctx.systemPrompt.assemble(None)
        text = assembly["text"]
        # SDK 分节 + code-only 规则（规则先于工具指引带，见 order 99/150）
        assert "## Writing code for run_code" in text
        assert "tools: Tools" in text
        assert "`run_code` is the only tool you can call directly" in text
        assert text.index("only tool you can call directly") < \
            text.index("## Writing code for run_code")
    finally:
        await tree.dispose()


async def test_mode_both_keeps_native_plus_transport(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True, extra_patches=BOTH_MODE_PATCH)
    try:
        names = {s["function"]["name"] for s in ctx.tools.schemas()}
        assert "run_code" in names and "bash" in names
        text = (await ctx.systemPrompt.assemble(None))["text"]
        assert "## Writing code for run_code" in text
        # both 模式不声明 code-only 规则（原生调用会执行）
        assert "only tool you can call directly" not in text
    finally:
        await tree.dispose()


async def test_collapsed_call_denied_before_policy(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True, extra_patches=CODE_MODE_PATCH)
    try:
        observed = []
        ctx.events.on("tools/pre-execute", lambda execution: observed.append(execution.name))
        result = await ctx.tools.execute("c1", "bash",
                                         {"script": "echo hi"})
        assert result.is_error and result.error.code == "UNKNOWN_TOOL"
        assert "from inside a `run_code` program instead" in result.error.message
        assert observed == []  # 策略管线绝不观察必败调用
        # 未知工具仍是 UNKNOWN_TOOL（不同路径）
        result = await ctx.tools.execute("c2", "no-such-tool", {})
        assert result.is_error and result.error.code == "UNKNOWN_TOOL"
    finally:
        await tree.dispose()


async def test_present_as_scope_mode(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        scope_ctx = ctx.scoped("agent:scope-test")
        local = ToolRuntime(scope_ctx, {}, parent=ctx.tools)
        local.apply(scope_ctx)
        dispose = scope_ctx.tools.presentAs("code")
        assert ctx.tools.mode_for("agent:scope-test") == "code"
        names = {s["function"]["name"]
                 for s in ctx.tools.schemas("agent:scope-test")}
        assert names == {"run_code"}
        assert "run_code" not in {
            s["function"]["name"] for s in ctx.tools.schemas(None)}
        # 作用域声明遮蔽部署默认；二次声明冲突
        with pytest.raises(ToolError):
            scope_ctx.tools.presentAs("both")
        dispose()
        assert ctx.tools.mode_for("agent:scope-test") == "native"
    finally:
        await tree.dispose()


async def test_present_as_requires_local_runtime(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        with pytest.raises(ToolError):
            ctx.tools.presentAs("code")  # 进程全局展示是 tools 行的 mode 配置
    finally:
        await tree.dispose()


# ---- SDK 渲染 ----

def test_sdk_render_typed_dicts_and_protocol():
    text = render_tools_sdk_py([{
        "name": "add",
        "description": "两数相加",
        "parameters": {"type": "object", "required": ["a"],
                       "properties": {"a": {"type": "integer"},
                                      "b": {"type": "number"}}},
        "output": {"type": "object",
                   "properties": {"sum": {"type": "integer"}}},
    }])
    assert "class AddArgs(TypedDict):" in text
    assert "class AddOutput(TypedDict):" in text
    assert "async def add(self, args: AddArgs) -> AddOutput:" in text
    assert "tools: Tools" in text and "ToolCallError" in text
    assert "from typing import" in text and "Protocol" in text


def test_sdk_render_exotic_name_and_literal():
    text = render_tools_sdk_py([{
        "name": "my-tool",
        "description": "异名工具",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "flag": {"type": "boolean", "const": True}}},
        "output": {"type": "null"},
    }])
    assert 'tools["my-tool"](args: MyToolArgs) -> None' in text
    assert "class MyToolArgs(TypedDict):" in text
    assert 'Literal["a", "b"]' in text and "Literal[True]" in text


def test_json_schema_to_py():
    assert json_schema_to_py({"type": "array",
                              "items": {"type": "string"}}) == "list[str]"
    assert json_schema_to_py({"type": "integer"}) == "int"
    assert json_schema_to_py({"type": "object"}) == "dict[str, Any]"
    assert json_schema_to_py(
        {"oneOf": [{"type": "string"}, {"type": "null"}]}) == "str | None"


# ---- CodeRuntime 单元 ----

def _runtime(config=None):
    ctx = Context("code-rt")
    return ctx, CodeRuntime(ctx, config or {})


async def test_runtime_basic_run():
    _, rt = _runtime()
    result = await rt.run(
        'print("hello")\nreturn {"answer": 42}',
        bindings=[{"global": "tools", "functions": {}}])
    assert result.error is None
    assert result.logs == ["hello"] and result.value == {"answer": 42}


async def test_runtime_no_return_and_prints_only():
    _, rt = _runtime()
    result = await rt.run('print("a")\nprint("b")',
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is None
    assert result.logs == ["a", "b"] and result.value is None


async def test_runtime_tool_call_error_contract():
    _, rt = _runtime()

    async def boom(args):
        raise CodeToolCallError("bash", "denied by policy")

    program = ('try:\n'
               '    await tools.bash({})\n'
               'except ToolCallError as e:\n'
               '    return {"caught": e.toolName, "msg": e.message}\n')
    result = await rt.run(
        program,
        bindings=[{"global": "tools", "functions": {"bash": boom},
                   "error_class": {"name": "ToolCallError",
                                   "memberNameProperty": "toolName"}}])
    assert result.error is None
    assert result.value == {"caught": "bash", "msg": "denied by policy"}


async def test_runtime_unknown_tool_name():
    _, rt = _runtime()
    result = await rt.run("await tools.nope({})",
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "exception"
    assert "no such tool: nope" in result.error.message


async def test_runtime_syntax_error():
    _, rt = _runtime()
    result = await rt.run("def def def",
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "exception"
    assert "syntax error" in result.error.message


async def test_runtime_timeout_keeps_logs():
    _, rt = _runtime({"timeout_ms": 150})
    result = await rt.run(
        'print("before")\nimport asyncio\n'
        'while True:\n    await asyncio.sleep(0.01)',
        bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "timeout"
    assert result.logs == ["before"]


async def test_runtime_abort_mid_run():
    from dsh.tools.pipeline import AbortSignal
    _, rt = _runtime()
    signal = AbortSignal()

    async def stop(args):
        signal.abort("user stop")
        await asyncio.sleep(30)

    result = await rt.run(
        'print("before")\nawait tools.stop({})\nprint("after")',
        bindings=[{"global": "tools", "functions": {"stop": stop}}],
        signal=signal)
    assert result.error is not None and result.error.kind == "abort"
    assert result.logs == ["before"]


async def test_runtime_invalid_output():
    _, rt = _runtime()
    result = await rt.run("return {1, 2, 3}",
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "invalid-output"


async def test_runtime_output_limit():
    _, rt = _runtime({"max_output_bytes": 100})
    result = await rt.run('print("x" * 500)',
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "output-limit"
    assert len(json.dumps(result.logs).encode("utf-8")) <= 100


async def test_runtime_program_exception():
    _, rt = _runtime()
    result = await rt.run("raise ValueError('boom')",
                          bindings=[{"global": "tools", "functions": {}}])
    assert result.error is not None and result.error.kind == "exception"
    assert "ValueError" in result.error.message


# ---- run_code 桥（端到端） ----

@define_tool(name="shout", description="大写回声（Code Mode e2e 确定性工具）",
             parameters={"text": {"type": "string", "required": True}},
             output={"type": "string"})
async def shout_tool(args, run_ctx):
    return args["text"].upper()


async def _boot_code_agent(tmp_path, script, mode_patch=CODE_MODE_PATCH,
                           tools_config=None):
    patches = list(mode_patch)
    if tools_config:
        patches.append(([{"id": "tools", "config": tools_config}], "tools-cfg"))
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True, extra_patches=patches)
    from dsh.llm.mock import MockAdapter
    # 重新注册带脚本的 mock（llm-mock 行的默认实例覆盖）
    ctx.llm.register_adapter(MockAdapter(script=script))
    agent = await ctx.agents.create(options={"provider": "mock",
                                             "model": "mock"})
    return ctx, tree, agent


async def test_run_code_end_to_end_dispatch_events(tmp_path):
    program = ('a = await tools.shout({"text": "one"})\n'
               'b = await tools.shout({"text": "two"})\n'
               'return {"joined": a + "+" + b}\n')
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-1",
                  "arguments": {"description": "Run two shouts and join",
                                "code": program}}},
        {"text": "完成"},
    ]
    ctx, tree, agent = await _boot_code_agent(tmp_path, turns)
    try:
        dispose = ctx.tools.register(shout_tool)
        agent.followup("执行程序")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        types = [e.type for e in agent.session.events]
        assert "tool/code-dispatch-start" in types
        assert "tool/code-dispatch" in types
        dispatches = [e for e in agent.session.events
                      if e.type == "tool/code-dispatch"]
        assert len(dispatches) == 2
        assert [d.data["name"] for d in dispatches] == ["shout", "shout"]
        assert [d.data["sub_call_id"] for d in dispatches] == \
            ["rc-1:code:1", "rc-1:code:2"]
        # 只有外层 run_code 的 tool/result 进入会话表面（子调用只落 dispatch 事件）
        results = [e for e in agent.session.events
                   if e.type == "tool/result"]
        assert len(results) == 1
        assert "ONE+TWO" in results[0].data["content"]
        dispose()
    finally:
        await tree.dispose()


async def test_run_code_denial_surfaces_tool_call_error(tmp_path):
    program = ('try:\n'
               '    await tools.shout({"text": "hi"})\n'
               '    return {"ok": True}\n'
               'except ToolCallError as e:\n'
               '    return {"caught": e.toolName, "msg": e.message}\n')
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-deny",
                  "arguments": {"description": "Catch a denied shout",
                                "code": program}}},
    ]
    ctx, tree, agent = await _boot_code_agent(tmp_path, turns)
    try:
        async def deny_shout(execution, next):
            from dsh.tools.pipeline import DenyDecision
            if execution.name == "shout":
                return DenyDecision("shout is denied")
            return await next()
        dispose_hook = ctx.events.on("tools/pre-execute", deny_shout)
        dispose_tool = ctx.tools.register(shout_tool)
        agent.followup("试试")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        dispatch = [e for e in agent.session.events
                    if e.type == "tool/code-dispatch"][0]
        assert dispatch.data["is_error"] is True
        assert "shout is denied" in dispatch.data["content"]
        outer = [e for e in agent.session.events
                 if e.type == "tool/result"][0]
        # 程序捕获并返回 → 外层 run_code 成功
        assert "caught" in outer.data["content"] and \
            "shout" in outer.data["content"]
        dispose_hook()
        dispose_tool()
    finally:
        await tree.dispose()


async def test_run_code_no_recursion(tmp_path):
    program = ('try:\n'
               '    await tools.run_code({"code": "return 1", "description": "x"})\n'
               'except ToolCallError as e:\n'
               '    return {"no_recursion": e.toolName}\n')
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-rec",
                  "arguments": {"description": "Try recursion",
                                "code": program}}},
    ]
    ctx, tree, agent = await _boot_code_agent(tmp_path, turns)
    try:
        agent.followup("递归")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        outer = [e for e in agent.session.events
                 if e.type == "tool/result"][0]
        assert "no_recursion" in outer.data["content"]
        assert "run_code" in outer.data["content"]
    finally:
        await tree.dispose()


async def test_run_code_additional_contexts_deferred(tmp_path):
    from dsh.tools.pipeline import ToolExecutionResult

    @define_tool(name="note_tool", description="记一条上下文",
                 parameters={"text": {"type": "string", "required": True}},
                 output={"type": "string"})
    async def note_tool(args, run_ctx):
        run_ctx.defer_context({"content": f"note: {args['text']}",
                               "source": {"kind": "plugin"}})
        return "ok"

    ctx, tree, agent = await _boot_code_agent(
        tmp_path, [{"tool": {"name": "run_code", "call_id": "rc-ctx",
                             "arguments": {"description": "Defer a context",
                                           "code": ('x = await tools.note_tool('
                                                    '{"text": "hello"})\n'
                                                    'return {"x": x}\n')}}}])
    try:
        dispose = ctx.tools.register(note_tool)
        agent.followup("上下文")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        # 子调用的 additional_contexts 经外层结果延迟提交 → 循环追加 user/message
        users = [e for e in agent.session.events if e.type == "user/message"]
        assert any("note: hello" in (e.data.get("content") or "")
                   for e in users)
        dispose()
    finally:
        await tree.dispose()


async def test_code_dispatch_log_waterfall_replaces_content(tmp_path):
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-log",
                  "arguments": {"description": "Shout once",
                                "code": ('await tools.shout('
                                         '{"text": "SECRET"})\n')}}},
    ]
    ctx, tree, agent = await _boot_code_agent(tmp_path, turns)
    try:
        async def spill(dispatch, next):
            return "PREVIEW[...locator...]"
        dispose_hook = ctx.events.on("tools/code-dispatch-log", spill)
        dispose_tool = ctx.tools.register(shout_tool)
        agent.followup("跑")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        dispatch = [e for e in agent.session.events
                    if e.type == "tool/code-dispatch"][0]
        assert dispatch.data["content"] == "PREVIEW[...locator...]"
        dispose_hook()
        dispose_tool()
    finally:
        await tree.dispose()


async def test_run_code_concurrency_contract(tmp_path):
    record: list = []

    @define_tool(name="gate", description="并发探针",
                 parameters={"i": {"type": "integer", "required": True}},
                 output={"type": "integer"},
                 concurrency_safe=lambda args: True)
    async def gate(args, run_ctx):
        record.append(("start", args["i"]))
        await asyncio.sleep(0.05)
        record.append(("end", args["i"]))
        return args["i"]

    @define_tool(name="excl", description="独占屏障",
                 parameters={}, output={"type": "string"})
    async def excl(args, run_ctx):
        record.append(("excl", 0))
        return "x"

    program = ('import asyncio\n'
               'r1 = asyncio.ensure_future(tools.gate({"i": 1}))\n'
               'r2 = asyncio.ensure_future(tools.gate({"i": 2}))\n'
               'await asyncio.sleep(0)  # 让 r1/r2 完成提交\n'
               'x = await tools.excl({})\n'
               'r3 = asyncio.ensure_future(tools.gate({"i": 3}))\n'
               'await asyncio.gather(r1, r2, r3)\n'
               'return {"x": x}\n')
    ctx, tree, agent = await _boot_code_agent(
        tmp_path,
        [{"tool": {"name": "run_code", "call_id": "rc-cc",
                   "arguments": {"description": "Concurrency probe",
                                 "code": program}}}],
        tools_config={"mode": "code", "max_parallel_sub_calls": 2})
    try:
        dispose_gate = ctx.tools.register(gate)
        dispose_excl = ctx.tools.register(excl)
        agent.followup("并发")
        await agent.when_idle()
        await asyncio.sleep(0.3)
        # parallel 1/2 重叠（exclusive 之前两者都已启动）
        assert record.index(("start", 1)) < record.index(("start", 2))
        assert record.index(("start", 2)) < record.index(("excl", 0))
        # 独占屏障：excl 在 1/2 结束后、3 启动前
        assert record.index(("excl", 0)) > record.index(("end", 1))
        assert record.index(("excl", 0)) > record.index(("end", 2))
        assert record.index(("excl", 0)) < record.index(("start", 3))
        dispose_gate()
        dispose_excl()
    finally:
        await tree.dispose()


async def test_preset_tool_executable_inside_program(tmp_path):
    """预设工具经作用域委托可在 run_code 程序内执行（委托重构的回归锚点）。"""
    preset_file = tmp_path / "greet.yml"
    preset_file.write_text(
        "- id: greet-tool\n  plugin: examples.my_plugin:MyPlugin\n"
        "  config: { greeting: 早安 }\n", encoding="utf-8")
    patches = [([{"insert": [{
        "id": "presets",
        "plugin": "dsh.preset.presets:AgentPresets",
        "config": {"paths": [str(tmp_path)]}}]}], "presets"),
        ([{"id": "tools", "config": {"mode": "code"}}], "code-mode")]
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-preset",
                  "arguments": {"description": "Greet via preset tool",
                                "code": ('g = await tools.greet({"name": "世界"})\n'
                                         'return {"greeting": g}\n')}}},
    ]
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True, extra_patches=patches)
    try:
        from dsh.llm.mock import MockAdapter
        ctx.llm.register_adapter(MockAdapter(script=turns))
        agent = await ctx.agents.create(
            options={"provider": "mock", "model": "mock"},
            meta={"agent_preset": "greet"})
        agent.followup("问候")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        outer = [e for e in agent.session.events
                 if e.type == "tool/result"][0]
        assert "早安" in outer.data["content"] and "世界" in outer.data["content"]
    finally:
        await tree.dispose()


async def test_run_code_empty_output_rendering(tmp_path):
    turns = [
        {"tool": {"name": "run_code", "call_id": "rc-empty",
                  "arguments": {"description": "Do nothing", "code": ""}}},
    ]
    ctx, tree, agent = await _boot_code_agent(tmp_path, turns)
    try:
        agent.followup("空跑")
        await agent.when_idle()
        await asyncio.sleep(0.1)
        outer = [e for e in agent.session.events
                 if e.type == "tool/result"][0]
        assert "(run_code completed with no output)" in outer.data["content"]
    finally:
        await tree.dispose()
