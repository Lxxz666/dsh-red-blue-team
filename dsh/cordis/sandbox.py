"""
dsh.cordis.sandbox —— 动态包的 host 半沙箱（in-process Python，对应 TS 版
sandbox.ts 的 host 半）。

- 程序体 = async 函数体（顶层 await/return），必须 ``return <plugin>``；
- 注入全局：``ctx``（该运行的作用域 Context——**非容器**，文档化信任立场：
  宿主闭包是逃逸通道，与 run_code 同级别）、``harness``（handle /
  defineTool / registerTool 注册助手）、``console``（带包标签的日志）；
- ``precheck_code``：定义期预检——编译 + AST 级危险导入拒绝
  （os/subprocess/socket/... 一律拒绝并给出 cordis 服务替代指引），
  保证不可解析代码进不了注册表；
- 语法错误给出源码行 + 插入号上下文与教学提示（Python 风格：类型注解
  不是问题，「函数体」缩进与括号配平才是）。
"""
from __future__ import annotations

import ast
import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..errors import ToolError

log = logging.getLogger("dsh.cordis")

#: 危险导入拒绝表（AST 级；宿主服务替代指引与 TS 版 require/fetch 陷阱同构）
_DENIED_IMPORTS = {
    "os": "use the cordis fs service: declare `ctx.tools`/`ctx.fs` on ctx instead",
    "subprocess": "use the cordis subprocess service on ctx instead",
    "socket": "network access goes through the cordis web service on ctx",
    "shutil": "use the cordis fs service on ctx instead",
    "ctypes": "native FFI is unavailable in dynamic packages",
    "importlib": "module loading is unavailable in dynamic packages",
    "sys": "the sys module is unavailable in dynamic packages",
    "pathlib": "use the cordis fs service on ctx instead",
}

HOST_BUILTIN_INSPECTION = [
    {"name": "ctx", "description":
     "受限的 Cordis Context（作用域化：注册只落在该包自己的层）。",
     "signatures": ["ctx.get(name): 服务 | 未找到抛错",
                    "ctx.on(name, listener): 注销函数",
                    "ctx.effect(disposer, label?): 注销函数"]},
    {"name": "harness", "description":
     "宿主助手：包私有 Host 方法（Client RPC 对应物）与模型可见动态工具。",
     "signatures": ["harness.handle(method, handler): 注销函数",
                    "harness.defineTool(definition): definition",
                    "harness.registerTool(ctx, tool): 注销函数"]},
    {"name": "console", "description": "带包标签的 Host 日志。",
     "signatures": ["console.log(...values): None",
                    "console.error(...values): None"]},
]


class _TaggedConsole:
    """带包标签的日志控制台（写 logging，测试可捕获）。"""

    def __init__(self, plugin_id: str) -> None:
        self._log = logging.getLogger(f"dsh.cordis.pkg.{plugin_id}")

    def log(self, *values: Any) -> None:
        self._log.info(" ".join(str(v) for v in values))

    def info(self, *values: Any) -> None:
        self.log(*values)

    def warn(self, *values: Any) -> None:
        self._log.warning(" ".join(str(v) for v in values))

    def debug(self, *values: Any) -> None:
        self._log.debug(" ".join(str(v) for v in values))

    def error(self, *values: Any) -> None:
        self._log.error(" ".join(str(v) for v in values))


def _denied_imports(code: str, half: str) -> Optional[str]:
    """AST 扫描危险导入（返回第一条拒绝消息或 None）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # 语法错误由编译路径报告
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DENIED_IMPORTS:
                    return (f"dynamic package `{half}` imports `{root}`, "
                            f"which is unavailable — {_DENIED_IMPORTS[root]}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DENIED_IMPORTS:
                return (f"dynamic package `{half}` imports `{root}`, "
                        f"which is unavailable — {_DENIED_IMPORTS[root]}")
    return None


def _syntax_context(exc: SyntaxError) -> str:
    """源码行 + 插入号上下文（模型自纠所需）。"""
    if exc.text is None:
        return str(exc)
    caret = " " * (exc.offset - 1) + "^" if exc.offset else ""
    return f"{exc.text.rstrip()}\n{caret}"


_PRELUDE = (
    "Note: it runs as the BODY of an async function (top-level `await` and "
    "`return` work; line numbers are offset by the 1-line wrapper). It must "
    "end with `return <plugin>` where <plugin> is a function "
    "`(ctx) -> disposer` or a Service class. Check indentation and "
    "bracket balance."
)


def precheck_code(code: str, half: str) -> None:
    """
    定义期预检：只编译不执行（不可解析/危险导入的代码进不了注册表）。

    :raises ToolError: 语法错误（含行上下文 + 教学提示）或危险导入。
    """
    denied = _denied_imports(code, half)
    if denied is not None:
        raise ToolError(denied)
    wrapper = f"async def __dsh_plugin__():\n" + "\n".join(
        "    " + line for line in code.splitlines()) + "\n"
    try:
        compile(wrapper, f"cordis-dyn-{half}.py", "exec")
    except SyntaxError as exc:
        raise ToolError(
            f"dynamic package `{half}` failed to parse:\n"
            f"{_syntax_context(exc)}\n{_PRELUDE}") from None


async def evaluate_host_code(code: str, plugin_id: str, vm_timeout_ms: int,
                             scope_ctx: Any, harness: Any,
                             console: Any) -> Any:
    """
    在作用域 ctx + 注入全局下执行 host 半，返回插件对象。

    ``vm_timeout_ms`` 预算只约束 await 段（协作式；同步阻塞为文档化边界）。

    :raises ToolError: 语法/运行失败（含教学提示）。
    """
    wrapper = f"async def __dsh_plugin__():\n" + "\n".join(
        "    " + line for line in code.splitlines()) + "\n"
    try:
        compiled = compile(wrapper, f"cordis-dyn-{plugin_id}.py", "exec")
    except SyntaxError as exc:
        raise ToolError(
            f"dynamic package `code.host` failed to parse:\n"
            f"{_syntax_context(exc)}\n{_PRELUDE}") from None
    namespace: Dict[str, Any] = {"ctx": scope_ctx, "harness": harness,
                                 "console": console}
    try:
        exec(compiled, namespace)
    except Exception as exc:
        raise ToolError(f"host half failed to load: "
                        f"{type(exc).__name__}: {exc}") from exc
    task = asyncio.ensure_future(namespace["__dsh_plugin__"]())
    try:
        if vm_timeout_ms:
            return await asyncio.wait_for(task, timeout=vm_timeout_ms / 1000.0)
        return await task
    except asyncio.TimeoutError:
        task.cancel()
        raise ToolError(f"host half exceeded the {vm_timeout_ms}ms "
                        "evaluation budget") from None
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"host half failed: {type(exc).__name__}: {exc}") \
            from exc
