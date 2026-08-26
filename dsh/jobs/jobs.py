"""
dsh.jobs —— JobsService（ctx.jobs）：后台任务注册表 + job_* 工具。

对应 TS 版 ctx.jobs / tool-jobs。后台任务 = 一个子进程，输出滚动缓冲，
``job_output`` 增量读取，``job_kill`` 终止，``job_list`` 全量枚举。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from ..ids import new_job_id
from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.jobs")


class Job:
    """一个后台任务。"""

    def __init__(self, job_id: str, command: List[str], cwd: str,
                 timeout: float) -> None:
        self.id = job_id
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.created_at = time.time()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.output_chunks: List[str] = []
        self.status = "pending"  # pending|running|done|killed|failed|timeout
        self.exit_code: Optional[int] = None
        self._cursor = 0
        self.task: Optional[asyncio.Task] = None

    def output_since(self, offset: int) -> str:
        """增量读取 offset 之后的输出（新游标）。"""
        if offset < self._cursor:
            offset = 0  # 客户端游标落后于回收，从头给
        joined = "".join(self.output_chunks)
        if offset >= len(joined):
            return ""
        self._cursor = len(joined)
        return joined[offset:]

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "command": self.command, "cwd": self.cwd,
                "status": self.status, "exit_code": self.exit_code,
                "created_at": self.created_at}


class JobsService(Service):
    """后台任务注册表（ctx.jobs）。"""

    provides = "jobs"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._jobs: Dict[str, Job] = {}

    def apply(self, ctx) -> None:
        ctx.set("jobs", self)

    def spawn(self, command: List[str], cwd: str,
              timeout: float = 300) -> str:
        """启动后台任务，返回 job id。"""
        job = Job(new_job_id(), command, cwd, timeout)
        self._jobs[job.id] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        return job.id

    async def _run(self, job: Job) -> None:
        try:
            job.process = await asyncio.create_subprocess_exec(
                *job.command, cwd=job.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            job.status = "running"
            try:
                while True:
                    line = await asyncio.wait_for(job.process.stdout.readline(),
                                                  timeout=job.timeout)
                    if not line:
                        break
                    job.output_chunks.append(line.decode("utf-8", "replace"))
            except asyncio.TimeoutError:
                job.status = "timeout"
                job.process.kill()
                await job.process.wait()
                return
            job.exit_code = await job.process.wait()
            job.status = "killed" if job.status == "killed" else \
                ("done" if job.exit_code == 0 else "failed")
        except Exception as exc:
            log.exception("job %s crashed", job.id)
            job.status = "failed"
            job.output_chunks.append(f"[job error] {exc}\n")

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return list(self._jobs.values())

    def output(self, job_id: str, offset: int = 0) -> str:
        job = self.get(job_id)
        if job is None:
            from ..errors import ToolError
            raise ToolError(f"unknown job: {job_id}", code="UNKNOWN_JOB")
        return job.output_since(offset)

    async def kill(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            from ..errors import ToolError
            raise ToolError(f"unknown job: {job_id}", code="UNKNOWN_JOB")
        if job.process is not None and job.process.returncode is None:
            job.process.kill()
        job.status = "killed"

    def close(self) -> None:
        for job in self._jobs.values():
            if job.process is not None and job.process.returncode is None:
                job.process.kill()
            if job.task is not None:
                job.task.cancel()
        self._jobs.clear()


def build_job_tools() -> List[Any]:
    """构造 job_* 工具族。"""

    @define_tool(name="job_list", description="列出全部后台任务。",
                 parameters={}, output={"type": "array", "items": {"type": "object"}})
    async def job_list(args, run_ctx):
        jobs = _jobs_of(run_ctx)
        return [job.to_json() for job in jobs.list()]

    @define_tool(name="job_output",
                 description="增量读取后台任务输出（每次调用返回新增部分）。",
                 parameters={"job_id": {"type": "string", "required": True}},
                 output={"type": "string"})
    async def job_output(args, run_ctx):
        return _jobs_of(run_ctx).output(args["job_id"])

    @define_tool(name="job_kill", description="终止后台任务。",
                 parameters={"job_id": {"type": "string", "required": True}},
                 output={"type": "string"})
    async def job_kill(args, run_ctx):
        await _jobs_of(run_ctx).kill(args["job_id"])
        return f"killed {args['job_id']}"

    return [job_list, job_output, job_kill]


def _jobs_of(run_ctx):
    agent = run_ctx.execution.agent
    ctx = agent.ctx if agent is not None else run_ctx.root_ctx
    if not ctx.has("jobs"):
        from ..errors import ToolError
        raise ToolError("jobs service not mounted", code="NO_JOBS")
    return ctx.jobs


class ToolJobsPlugin(Service):
    """注册 job_* 工具的插件。"""

    inject = ("tools", "jobs")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx):
        for tool in build_job_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
