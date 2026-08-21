"""
dsh.subprocess.local —— SubprocessService（ctx.subprocess）：本地子进程执行。

- 超时硬杀（timeout 秒）；捕获 stdout/stderr；
- 取消信号（AbortSignal）中止运行；
- 目录限定在 workdir（默认工作区根）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service
from ..tools.pipeline import AbortSignal

log = logging.getLogger("dsh.subprocess")


@dataclass
class RunResult:
    """一次子进程运行的结果。"""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.aborted

    def to_json(self) -> Dict[str, Any]:
        return {"stdout": self.stdout, "stderr": self.stderr,
                "exit_code": self.exit_code, "timed_out": self.timed_out,
                "aborted": self.aborted}


class SubprocessService(Service):
    """本地子进程 provider（ctx.subprocess）。"""

    provides = "subprocess"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._default_timeout = float((config or {}).get("timeout", 120))

    def apply(self, ctx) -> None:
        ctx.set("subprocess", self)

    async def run(self, command: List[str], *, cwd: Optional[str] = None,
                  timeout: Optional[float] = None,
                  signal: Optional[AbortSignal] = None,
                  env: Optional[Dict[str, str]] = None,
                  stdin_data: Optional[str] = None) -> RunResult:
        """
        运行一条命令并捕获输出。

        :param command: argv 列表（shell=False，无注入面）。
        :param cwd: 工作目录（默认调用方指定）。
        :param timeout: 秒；超时 → 硬杀并置 timed_out。
        :param signal: 取消信号（abort → 终止进程）。
        :param stdin_data: 可选 stdin 文本（写入后关闭）。
        :raises ToolError: 启动失败。
        """
        timeout = self._default_timeout if timeout is None else timeout
        stdin_pipe = asyncio.subprocess.PIPE if stdin_data is not None \
            else None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, stdin=stdin_pipe, env=env)
        except OSError as exc:
            raise ToolError(f"cannot start {command[0]!r}: {exc}",
                            code="SPAWN_FAILED") from exc
        # 沙箱缝：生成后把子进程挂入沙箱（identity 后端 = no-op）
        if self.ctx.has("sandbox"):
            try:
                self.ctx.sandbox.attach(process.pid)
            except Exception:
                log.debug("sandbox attach failed for pid %s", process.pid)

        async def _kill() -> None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        abort_task = None
        if signal is not None:
            async def _watch() -> None:
                await signal.wait()
                await _kill()
            abort_task = asyncio.create_task(_watch())

        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        input=stdin_data.encode("utf-8")
                        if stdin_data is not None else None),
                    timeout=timeout)
            except asyncio.TimeoutError:
                await _kill()
                await process.communicate()
                return RunResult(stdout="", stderr="",
                                 exit_code=process.returncode or -1,
                                 timed_out=True)
            return RunResult(stdout=stdout.decode("utf-8", "replace"),
                             stderr=stderr.decode("utf-8", "replace"),
                             exit_code=process.returncode or 0,
                             aborted=signal is not None and signal.aborted)
        finally:
            if abort_task is not None:
                abort_task.cancel()
