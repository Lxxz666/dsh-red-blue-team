"""
dsh.code.mode —— 保留的 ``run_code`` 传输与子调用派发桥。

对应 TS 版 code-mode.ts + 注册表的 dispatch bridge：

- ``run_code`` 是 Code Mode 的保留传输（注册表保留名，不可注册/遮蔽/过滤）；
- 程序内对 SDK 声明的每个可见工具的绑定调用 = 一次**嵌套子调用**：携带父
  令牌（豁免 code 坍缩）、走完整 5 段管线、按原生并发契约调度
  （提交序启动；parallel 类重叠 ≤ max_parallel_sub_calls；exclusive 类独占
  并持障直到 post-execute 结算）；
- 每个子调用落 ``tool/code-dispatch-start``（派发进入）与
  ``tool/code-dispatch``（结算，含模型可见内容）；结算副本先经
  ``tools/code-dispatch-log`` waterfall（监听者可用预览+定位替换，如 spill）；
  两者都不进派生历史（derive_messages 忽略）；
- 拒绝/失败以程序可见的 ``ToolCallError``（``toolName``）抵达程序；
  ``additionalContexts`` 经外层结果延迟提交（保持调用/结果相邻）；
- 外层只返回 ``{logs, result?}``；渲染 ``(run_code completed with no output)``
  当两者皆空。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from ..errors import ToolError
from ..session.events import is_json_value, register_event_type
from ..tools.definition import define_tool
from ..tools.pipeline import (AbortSignal, ToolExecutionFailure,
                              ToolExecutionResult)
from .runtime import CodeRunResult, CodeToolCallError

log = logging.getLogger("dsh.code")

RUN_CODE_NAME = "run_code"

register_event_type(
    "tool/code-dispatch-start",
    "run_code 子调用进入派发（父/子调用 id + 参数快照，按提交序编号）。")
register_event_type(
    "tool/code-dispatch",
    "run_code 子调用结算（模型可见内容副本；tools/code-dispatch-log 监听器可替换）。")

RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION = (
    "Clear, concise description of what this program does in active voice, "
    "5-10 words (shown in the UI). Examples: \"Count TODO markers across "
    "packages\"; \"Read failing test and its fixture\".")

PYTHON_FLAVOR = {
    "description": (
        "Execute a Python program against the available tools. Takes two "
        "required arguments: `code`, the BODY of an async function (top-level "
        "`await` and `return` work), and `description`, a short summary of "
        "what the program does. Call tools as `await tools.name(args)` per "
        "the declarations in the system prompt. Answer with `print(...)` "
        "and/or `return <value>` — only that comes back, so curate it."),
    "code_description": "The program: the body of an async Python function.",
}


class CodeRunFailedError(ToolError):
    """程序 run 本身失败（异常/预算/中止/输出超限）→ 管线结构化的 isError 结果。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="CODE_RUN_FAILED")


_MISSING = object()


def _render_value(value: Any) -> str:
    """完成值渲染：字符串原样，其余 JSON 美化。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _render_run_code(args: Any, value: Any) -> str:
    logs_text = "\n".join(value.get("logs") or [])
    rendered = "" if value.get("result", _MISSING) is _MISSING \
        else _render_value(value["result"])
    parts = [part for part in (logs_text, rendered) if part]
    return ("\n".join(parts) if parts
            else "(run_code completed with no output)")


# ---- 派发调度器（原生并发契约的有序 lane） ----

class _PendingDispatch:
    """一次绑定调用的待办条目：start（有序 prepare + 并发 body）→ commit（有序
    finalize → 程序取值 → 日志副本），提交序与启动序一致。"""

    def __init__(self, scheduler: "_DispatchScheduler", sub_call_id: str,
                 name: str, args: Any, logged_args: Any) -> None:
        self.scheduler = scheduler
        self.sub_call_id = sub_call_id
        self.name = name
        self.args = args
        self.logged_args = logged_args
        self.settled = False
        self.mode: Optional[str] = None
        self.prepared = None
        self.result: Optional[ToolExecutionResult] = None
        self.flight: Optional[asyncio.Task] = None
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()

    def classify(self) -> str:
        return self.scheduler.registry.execution_mode(
            self.name, self.args, self.scheduler.scope)

    def abandon(self) -> None:
        if not self.future.done():
            self.future.set_exception(CodeToolCallError(
                self.name,
                f"run_code run is over ({self.scheduler.run_signal.reason}); "
                f"{self.name} tool call abandoned"))

    async def start(self) -> None:
        self.scheduler.append_start(self)
        self.prepared = await self.scheduler.registry.prepare(
            self.sub_call_id, self.name, self.args,
            agent=self.scheduler.agent, signal=self.scheduler.run_signal,
            scope=self.scheduler.scope, parent=self.scheduler.parent_token)
        if self.prepared.kind == "dispatch":
            self.flight = asyncio.ensure_future(
                self.scheduler.registry.dispatch_prepared(self.prepared))
            self.flight.add_done_callback(self._on_flight_done)
        else:
            self.result = self.prepared.result
            self.settled = True
            self.scheduler.wakeup()

    def _on_flight_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            self.result = ToolExecutionFailure(
                ToolError("sub-dispatch cancelled", code="ABORTED"),
                content="Error: sub-dispatch cancelled")
        else:
            try:
                self.result = task.result()
            except Exception:
                self.result = ToolExecutionFailure(
                    ToolError("sub-dispatch crashed", code="INTERNAL"))
        self.settled = True
        self.scheduler.wakeup()

    async def commit(self) -> None:
        if self.result is None:
            self.result = ToolExecutionFailure(
                ToolError("sub-dispatch settled without a result",
                          code="INTERNAL"))
        if self.prepared.kind == "dispatch":
            self.result = await self.scheduler.registry.finalize_prepared(
                self.prepared, self.result)
        else:
            self.result = self.scheduler.registry.finish_prepared(self.prepared)
        result = self.result
        # additionalContexts 经外层结果延迟提交（保持调用/结果相邻）
        if not result.is_error:
            for context in result.additional_contexts:
                self.scheduler.outer_run_ctx.defer_context(context)
            if result.concludes_turn:
                self.scheduler.outer_run_ctx.conclude_turn()
        # 程序先拿到值：日志监听绝不延迟绑定
        if result.is_error:
            exc = CodeToolCallError(self.name, result.error.message)
            if not self.future.done():
                self.future.set_exception(exc)
        elif not self.future.done():
            self.future.set_result(result.value)
        await self.scheduler.schedule_log(self)


class _DispatchScheduler:
    """单次 run_code 的子调用调度器（单有序 lane + 并发池 + 独占屏障）。"""

    def __init__(self, registry: Any, max_parallel: int,
                 run_signal: AbortSignal, outer_exec: Any,
                 outer_run_ctx: Any, agent: Any, scope: Any) -> None:
        self.registry = registry
        self.max_parallel = max_parallel
        self.run_signal = run_signal
        self.outer_exec = outer_exec
        self.outer_run_ctx = outer_run_ctx
        self.agent = agent
        self.scope = scope
        self.parent_token = outer_exec.token
        self.pending: List[_PendingDispatch] = []
        self.commit_queue: List[_PendingDispatch] = []
        self.in_flight: Set[asyncio.Task] = set()
        self.exclusive_active = False
        self._counter = 0
        self._wake: Optional[asyncio.Future] = None
        self._driver_task: Optional[asyncio.Task] = None
        self._log_tasks: Set[asyncio.Task] = set()

    def wakeup(self) -> None:
        wake = self._wake
        if wake is not None and not wake.done():
            wake.set_result(None)

    def binding(self, name: str) -> Callable[..., Any]:
        async def call(args: Any):
            if self.run_signal.aborted:
                raise CodeToolCallError(
                    name, f"run_code run is over ({self.run_signal.reason}); "
                    f"{name} not dispatched")
            if not is_json_value(args):
                raise CodeToolCallError(
                    name, "tool arguments must be lossless JSON (call the "
                    "tool with an arguments object, e.g. `{}`)")
            frozen = copy.deepcopy(args)
            logged = copy.deepcopy(frozen)
            self._counter += 1
            sub_call_id = f"{self.outer_exec.call_id}:code:{self._counter}"
            pending = _PendingDispatch(self, sub_call_id, name, frozen, logged)
            self.pending.append(pending)
            self.wakeup()
            self._ensure_driver()
            return await pending.future
        return call

    def _ensure_driver(self) -> None:
        if self._driver_task is None or self._driver_task.done():
            self._driver_task = asyncio.ensure_future(self._driver())

    async def _driver(self) -> None:
        """单有序 lane：先结算提交序头部，再按容量启动下一个条目。"""
        while True:
            head = self.commit_queue[0] if self.commit_queue else None
            if head is not None and head.settled:
                self.commit_queue.pop(0)
                await head.commit()
                if head.mode == "exclusive":
                    self.exclusive_active = False
                continue
            head = self.pending[0] if self.pending else None
            if head is not None:
                if self.run_signal.aborted:
                    self.pending.pop(0)
                    head.abandon()
                    continue
                mode = head.classify()
                capacity = not self.exclusive_active and (
                    len(self.in_flight) < self.max_parallel
                    if mode == "parallel" else len(self.in_flight) == 0)
                if capacity:
                    if mode == "exclusive":
                        self.exclusive_active = True
                    head.mode = mode
                    self.pending.pop(0)
                    self.commit_queue.append(head)
                    await head.start()
                    if head.flight is not None:
                        self.in_flight.add(head.flight)
                        head.flight.add_done_callback(
                            lambda task: (self.in_flight.discard(task),
                                          self.wakeup()))
                    continue
            if (not self.pending and not self.commit_queue
                    and not self.in_flight):
                return
            self._wake = asyncio.get_running_loop().create_future()
            try:
                await self._wake
            finally:
                self._wake = None

    def append_start(self, pending: _PendingDispatch) -> None:
        agent = self.agent
        if agent is None:
            return
        agent.session.append("tool/code-dispatch-start", {
            "root_call_id": self.outer_exec.call_id,
            "parent_call_id": self.outer_exec.call_id,
            "sub_call_id": pending.sub_call_id,
            "name": pending.name,
            "arguments": pending.logged_args,
        })

    async def schedule_log(self, pending: _PendingDispatch) -> None:
        agent = self.agent
        if agent is None:
            return
        dispatch = {"exec": self.outer_exec, "agent": agent,
                    "sub_call_id": pending.sub_call_id, "name": pending.name,
                    "is_error": pending.result.is_error,
                    "content": pending.result.content}

        async def log_task() -> None:
            try:
                content = await self.registry.ctx.events.waterfall(
                    "tools/code-dispatch-log", dispatch,
                    default=lambda: dispatch["content"])
            except Exception:
                log.exception("tools/code-dispatch-log listener failed for %s",
                              pending.name)
                content = dispatch["content"]
            agent.session.append("tool/code-dispatch", {
                "root_call_id": self.outer_exec.call_id,
                "parent_call_id": self.outer_exec.call_id,
                "sub_call_id": pending.sub_call_id,
                "name": pending.name,
                "arguments": pending.logged_args,
                "is_error": pending.result.is_error,
                "content": content,
            })
        task = asyncio.ensure_future(log_task())
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)
        # 背压：日志副本积压不得超过池容量（TS 版同契约）
        if len(self._log_tasks) > self.max_parallel:
            await asyncio.wait(list(self._log_tasks),
                               return_when=asyncio.FIRST_COMPLETED)

    async def drain(self) -> None:
        """run 结算：驱动器到 quiescence（在途派发全部结算），再等日志任务。"""
        if self._driver_task is not None:
            try:
                await asyncio.shield(self._driver_task)
            except Exception:
                log.exception("run_code dispatch driver crashed")
        while self._log_tasks:
            await asyncio.gather(*list(self._log_tasks),
                                 return_exceptions=True)


# ---- run_code 工具 ----

def build_run_code_tool(registry: Any, require_runtime: Callable[[], Any],
                        max_parallel: int = 10):
    """构造保留的 run_code 传输定义（每进程一个，挂在根注册表）。"""

    @define_tool(
        name=RUN_CODE_NAME,
        description=PYTHON_FLAVOR["description"],
        parameters={
            "code": {"type": "string", "required": True,
                     "description": PYTHON_FLAVOR["code_description"]},
            "description": {"type": "string", "required": True,
                            "description": RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION},
        },
        output={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "logs": {"type": "array",
                         "items": {"type": "string"}},
                "result": None,
            },
            "required": ["logs"],
        },
        render=_render_run_code,
        present_call=lambda args: {
            "card": "generic",
            "title": (args or {}).get("description", RUN_CODE_NAME),
            "kind": "execute",
            "raw_input": (args or {}).get("code", ""),
        },
    )
    async def run_code(args, run_ctx):
        if not (args.get("description") or "").strip():
            raise ToolError("invalid description: expected a non-empty string")
        runtime = require_runtime()
        execution = run_ctx.execution
        agent = execution.agent
        scope = getattr(agent, "ctx_name", None) if agent is not None else None
        run_signal = AbortSignal()
        outer = execution.signal

        async def follow_outer() -> None:
            await outer.wait()
            run_signal.abort("outer abort")
        follower: Optional[asyncio.Task] = None
        if outer.aborted:
            run_signal.abort("outer abort")
        else:
            follower = asyncio.ensure_future(follow_outer())

        scheduler = _DispatchScheduler(registry, max_parallel, run_signal,
                                       execution, run_ctx, agent, scope)
        # 绑定枚举：调用 agent 的可见集合（与 SDK 声明同一视图；run_code 自身不暴露）
        bindings: Dict[str, Any] = {}
        for definition in registry.list(scope):
            if definition.name == RUN_CODE_NAME:
                continue
            bindings[definition.name] = scheduler.binding(definition.name)
        try:
            result: CodeRunResult = await runtime.run(
                program=args.get("code", ""),
                bindings=[{"global": "tools", "functions": bindings,
                           "error_class": {"name": "ToolCallError",
                                           "memberNameProperty": "toolName"}}],
                signal=run_signal)
        finally:
            run_signal.abort("run_code settled")
            await scheduler.drain()
            if follower is not None:
                follower.cancel()
        if result.error is not None:
            logs_text = ("\nCaptured output:\n" + "\n".join(result.logs)
                         if result.logs else "")
            raise CodeRunFailedError(
                f"code run failed ({result.error.kind}): "
                f"{result.error.message}{logs_text}")
        out: Dict[str, Any] = {"logs": result.logs}
        if result.value is not None:
            out["result"] = result.value
        return out

    return run_code
