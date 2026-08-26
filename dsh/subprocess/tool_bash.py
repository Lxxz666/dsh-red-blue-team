"""
dsh.subprocess.tool_bash —— 终端工具（bash/pwsh 自动按平台选择）。

- 前台执行返回退出码 + 输出；``run_in_background`` 时登记 jobs 服务并立即返回 job id；
- Windows 下用 pwsh，POSIX 用 bash（对应 dsh-base 的平台条件行）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..tools import define_tool
from ..tools.presentation import terminal_call, terminal_result

IS_WINDOWS = sys.platform == "win32"


def _shell_command(script: str) -> List[str]:
    """按平台构造 shell argv。

    Windows 优先 pwsh；未安装则回退 git-bash 的 bash（本机常见环境），
    再退 Windows PowerShell 5.1。POSIX 直接用 bash。
    """
    if IS_WINDOWS:
        if _which("pwsh"):
            return ["pwsh", "-NoProfile", "-Command", script]
        if _which("bash"):
            return ["bash", "-lc", script]
        return ["powershell.exe", "-NoProfile", "-Command", script]
    return ["bash", "-lc", script]


def _which(name: str) -> Optional[str]:
    """定位可执行文件（找不到返回 None）。"""
    import shutil
    return shutil.which(name)


def _spawn_background(run_ctx, script: str, cwd: str, timeout: float):
    """后台运行：经 ctx.jobs 登记（jobs 未挂载则报错）。"""
    agent = run_ctx.execution.agent
    ctx = agent.ctx if agent is not None else run_ctx.root_ctx
    if not ctx.has("jobs"):
        from ..errors import ToolError
        raise ToolError("background jobs require the jobs service",
                        code="NO_JOBS")
    return ctx.jobs.spawn(_shell_command(script), cwd=cwd, timeout=timeout)


def build_bash_tool() -> Any:
    """构造终端工具（注册由 ToolBashPlugin.apply 完成）。"""

    @define_tool(
        name="bash",
        description="在工作区执行 shell 脚本（Windows=pwsh，POSIX=bash）。"
                    "run_in_background=True 时返回 job id。",
        parameters={"script": {"type": "string", "required": True},
                    "run_in_background": {"type": "boolean"}},
        output={"type": "string"},
        timeout_ms=300_000,
        present_call=lambda args: terminal_call(
            title="终端", description=args["script"][:120]),
        present_result=lambda args, result: terminal_result(
            title="终端", output=str(result.value)) if not args.get("run_in_background")
            else None,
    )
    async def bash_tool(args, run_ctx):
        script = args["script"]
        if args.get("run_in_background"):
            job_id = _spawn_background(run_ctx, script,
                                       cwd=_workspace_of(run_ctx),
                                       timeout=300)
            return f"started background job: {job_id}"

        agent = run_ctx.execution.agent
        ctx = agent.ctx if agent is not None else run_ctx.root_ctx
        subprocess = ctx.subprocess
        argv = _shell_command(script)
        # 沙箱缝：生成前改写 argv（stub = identity；换实现即整体换行为）
        if ctx.has("sandbox"):
            argv = ctx.sandbox.confine(argv, _workspace_of(run_ctx))
        result = await subprocess.run(
            argv, cwd=_workspace_of(run_ctx),
            timeout=300, signal=run_ctx.signal)
        body = result.stdout
        if result.stderr:
            body += "\n[stderr]\n" + result.stderr
        if result.timed_out:
            body += "\n[timed out]"
        if result.aborted:
            body += "\n[aborted]"
        if result.exit_code != 0 and not body.strip():
            body = f"(exit {result.exit_code})"
        return body
    return bash_tool


def _workspace_of(run_ctx) -> str:
    agent = run_ctx.execution.agent
    ctx = agent.ctx if agent is not None else run_ctx.root_ctx
    if ctx.has("fs"):
        return ctx.fs.workspace_root()
    return os.getcwd()


class ToolBashPlugin(Service):
    """注册终端工具的插件（base bundle 的一行）。"""

    inject = ("tools", "subprocess")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx):
        self._disposer = ctx.tools.register(build_bash_tool())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
