"""
dsh.code.runtime —— CodeRuntime（ctx.codeRuntime）：代码执行能力 seam。

对应 TS 版 dsh-code-runtime 的 Service Definition + Python 后端（在进程内执行，
无 worker 隔离——dsh_python 的选择，文档化）：

- ``run(program, bindings, signal) -> CodeRunResult``：把 ``program`` 编译为
  一个 async 函数体（顶层 ``await``/``return`` 可用），注入 ``bindings``
  命名空间（如 ``tools``），捕获 print/stdout 输出与返回值；
- 失败是结果上的字段（error.kind ∈ exception/timeout/abort/worker-exit/
  invalid-output/output-limit），绝不作为异常路径（程序失败由调用方报告）；
- 绑定调用抛出的异常统一为程序可见的 ``ToolCallError``（``toolName`` 属性）；
- ``max_output_bytes`` 统计 logs + 完成值的 JSON 序列化字节数，超限 =
  output-limit（保留能容纳的 logs 前缀）；
- 局限（in-process 后端的文档化边界）：signal 中止是协作式的（await 点
  生效，同步阻塞代码无法硬停）；stdout 捕获经全局 redirect，多 run 之间
  以锁串行（互不干扰输出）。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service
from ..session.events import is_json_value

log = logging.getLogger("dsh.code")

# 可移植标识符子集 + 保留绑定全局（与 TS 版同约定）
_PORTABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_BINDING_GLOBALS = {"console", "__dsh_main__"}

# 程序可见异常消息的长度上界（防止百万字节栈进入诊断）
_MAX_ERROR_TEXT = 4000


class CodeToolCallError(ToolError):
    """程序可见的工具调用失败（toolName + 人类可读消息，TS 版 ToolCallError）。"""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(message, code="TOOL_CALL_ERROR")
        self.toolName = tool_name
        self.message = message


@dataclass
class CodeRunFailure:
    """为何一次 run 失败（kinds 正交：exception/timeout/abort/worker-exit/
    invalid-output/output-limit）。"""

    kind: str
    message: str


@dataclass
class CodeRunResult:
    """一次 run 的结果（error 是字段，不是异常路径）。"""

    logs: List[str] = field(default_factory=list)
    value: Any = None
    error: Optional[CodeRunFailure] = None


class _ToolsNamespace:
    """程序里 ``tools`` 全局对象：属性与下标两种访问（异名/保留名走下标）。"""

    def __init__(self, functions: Dict[str, Any]) -> None:
        object.__setattr__(self, "_functions", functions)

    def __getattr__(self, name: str):
        functions = object.__getattribute__(self, "_functions")
        if name.startswith("__"):
            raise AttributeError(name)
        if name in functions:
            return functions[name]
        raise CodeToolCallError(name, f"no such tool: {name}")

    def __getitem__(self, name: str):
        functions = object.__getattribute__(self, "_functions")
        fn = functions.get(name)
        if fn is None:
            raise CodeToolCallError(name, f"no such tool: {name}")
        return fn


def _bound_error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR_TEXT]


def _fit_logs_prefix(logs: List[str], value: Any, budget: int) -> List[str]:
    """保留能塞进 max_output_bytes 的 logs 前缀（从尾部丢弃；超预算返回空）。"""
    kept = list(logs)
    while kept:
        size = len(json.dumps([kept, value], ensure_ascii=False)
                   .encode("utf-8"))
        if size <= budget:
            return kept
        kept.pop()
    return []


class CodeRuntime(Service):
    """in-process Python 代码执行后端（ctx.codeRuntime）。"""

    provides = "codeRuntime"
    language = "python"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.max_output_bytes = int((config or {}).get(
            "max_output_bytes", 64 * 1024 * 1024))
        self.timeout_ms = float((config or {}).get("timeout_ms", 30_000))
        self._run_lock = asyncio.Lock()

    def apply(self, ctx) -> None:
        ctx.set("codeRuntime", self)

    async def run(self, program: str, bindings: List[Dict[str, Any]],
                  signal: Any = None) -> CodeRunResult:
        """
        执行一个程序（async 函数体语义）。

        :param program: 程序源（async 函数体：顶层 await/return 可用）。
        :param bindings: ``[{global, functions, error_class?}]`` 命名空间列表。
        :param signal: AbortSignal（协作式中止）。
        """
        # ---- 绑定校验 ----
        namespaces: List[tuple] = []
        for spec in bindings:
            global_name = spec.get("global", "")
            if (not _PORTABLE_IDENTIFIER.fullmatch(global_name)
                    or global_name in RESERVED_BINDING_GLOBALS):
                return CodeRunResult(error=CodeRunFailure(
                    "exception",
                    f"invalid binding global: {global_name!r}"))
            namespaces.append((global_name, spec.get("functions") or {},
                               spec.get("error_class")))
        if signal is not None and signal.aborted:
            return CodeRunResult(error=CodeRunFailure(
                "abort", f"aborted ({signal.reason})"))

        # ---- error_class 全局注入（程序可见的 ToolCallError） ----
        error_names: List[str] = []
        error_args: List[type] = []
        for _, _, error_class in namespaces:
            if not error_class:
                continue
            name = error_class.get("name") or ""
            if (not _PORTABLE_IDENTIFIER.fullmatch(name)
                    or name in RESERVED_BINDING_GLOBALS
                    or name in [n for n, _, _ in namespaces]):
                return CodeRunResult(error=CodeRunFailure(
                    "exception",
                    f"invalid binding error class name: {name!r}"))
            if name not in error_names:
                error_names.append(name)
                error_args.append(CodeToolCallError)

        # ---- 编译为 async 函数体 ----
        params = ", ".join([n for n, _, _ in namespaces] + error_names)
        body = "\n".join("    " + line for line in program.splitlines())
        source = f"async def __dsh_main__({params}):\n{body or '    pass'}\n"
        try:
            code = compile(source, "<run_code>", "exec")
        except SyntaxError as exc:
            return CodeRunResult(error=CodeRunFailure(
                "exception", f"syntax error: {_bound_error_text(exc)}"))
        namespace: Dict[str, Any] = {}
        try:
            exec(code, namespace)
        except Exception as exc:
            return CodeRunResult(error=CodeRunFailure(
                "exception", f"compile error: {_bound_error_text(exc)}"))
        main = namespace["__dsh_main__"]

        async with self._run_lock:  # redirect_stdout 是全局的：串行化捕获
            buf = io.StringIO()
            namespace_objects = [
                _ToolsNamespace(functions) for _, functions, _ in namespaces]
            task = asyncio.ensure_future(
                main(*(namespace_objects + error_args)))
            budget = self.timeout_ms / 1000.0 if self.timeout_ms else None
            try:
                with contextlib.redirect_stdout(buf):
                    if signal is not None:
                        waiter = asyncio.ensure_future(signal.wait())
                        try:
                            done, _ = await asyncio.wait_for(
                                asyncio.wait({task, waiter},
                                             return_when=asyncio.FIRST_COMPLETED),
                                timeout=budget)
                        finally:
                            waiter.cancel()
                        if waiter in done:
                            task.cancel()
                            with contextlib.suppress(BaseException):
                                await task
                            logs = buf.getvalue().splitlines()
                            return CodeRunResult(
                                logs, error=CodeRunFailure(
                                    "abort", f"aborted ({signal.reason})"))
                        value = task.result()
                    else:
                        value = await (asyncio.wait_for(task, timeout=budget)
                                       if budget else task)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                logs = buf.getvalue().splitlines()
                return CodeRunResult(
                    logs, error=CodeRunFailure(
                        "timeout",
                        f"program exceeded the {self.timeout_ms}ms budget"))
            except asyncio.CancelledError:
                raise
            except CodeToolCallError as exc:
                # 逃逸出程序的绑定失败（未被程序捕获）
                return CodeRunResult(
                    buf.getvalue().splitlines(), error=CodeRunFailure(
                        "exception", f"{exc.toolName}: {exc.message}"))
            except Exception as exc:
                return CodeRunResult(
                    buf.getvalue().splitlines(), error=CodeRunFailure(
                        "exception", _bound_error_text(exc)))

        # ---- 完成值物化 ----
        logs = buf.getvalue().splitlines()
        if not is_json_value(value):
            return CodeRunResult(
                logs, error=CodeRunFailure(
                    "invalid-output",
                    f"completion value is not lossless JSON "
                    f"({type(value).__name__})"))
        size = len(json.dumps([logs, value], ensure_ascii=False)
                   .encode("utf-8"))
        if size > self.max_output_bytes:
            return CodeRunResult(
                _fit_logs_prefix(logs, value, self.max_output_bytes),
                error=CodeRunFailure(
                    "output-limit",
                    f"output exceeded {self.max_output_bytes} bytes"))
        if value is None:
            # 与 TS 版一致：无值（或显式 return None）= 缺省 result 键
            return CodeRunResult(logs=logs)
        return CodeRunResult(logs=logs, value=value)

    def close(self) -> None:
        pass
