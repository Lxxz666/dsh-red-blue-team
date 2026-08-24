"""
dsh.sandbox.sandbox —— SandboxService（ctx.sandbox）：进程限制缝。

对应 TS 版 ctx.sandbox（landlock / sandbox-exec / Windows ACL）：

- ``confine(argv, cwd)`` 在生成子进程前改写 argv（消费者 = bash 工具）；
- ``attach(pid)`` 在子进程生成后把进程挂入沙箱（消费者 = subprocess 服务）；
- mode 配置：``auto``（默认；win32 → jobobject，linux → landlock，否则
  local）/ ``jobobject``（Windows Job Object：kill-on-close + 可选整组
  内存上限）/ ``landlock``（Linux：包装器进程应用「只读 FS + 工作区可写」
  后 exec）/ ``local``（identity，不限制）；
- 后端不可用（平台/权限/内核不支持）时**降级为 local 并在 describe()
  里如实标注**——不装死，如实报告；
- 文件侧围栏由沙箱承担（landlock）/ fs 工作区校验兜底（其余后端）。
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import Service

log = logging.getLogger("dsh.sandbox")


def _resolve_auto() -> str:
    if sys.platform == "win32":
        return "jobobject"
    if sys.platform == "linux":
        return "landlock"
    return "local"


class SandboxService(Service):
    """进程限制缝（ctx.sandbox）。"""

    provides = "sandbox"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        config = config or {}
        mode = str(config.get("mode", "auto"))
        if mode == "auto":
            mode = _resolve_auto()
        if mode not in ("jobobject", "landlock", "local"):
            raise ToolError(f"unknown sandbox mode: {mode!r}")
        self._mode = mode
        self._job = None
        self._landlock_ok = False
        self._job_error: Optional[str] = None
        self._landlock_error: Optional[str] = None
        if mode == "jobobject":
            if sys.platform != "win32":
                raise ToolError("sandbox mode 'jobobject' requires Windows")
            try:
                from .jobobject import WindowsJobObject
                self._job = WindowsJobObject(
                    memory_limit_bytes=int(config.get("memory_limit_mb", 0))
                    * 1024 * 1024)
            except Exception as exc:
                log.warning("job object unavailable (%s) → local identity", exc)
                self._job_error = str(exc)
                self._job = None
        elif mode == "landlock":
            from . import landlock
            self._landlock_ok = landlock.available()
            if not self._landlock_ok:
                self._landlock_error = ("landlock unavailable (kernel ABI "
                                        "v1+ required)")
                log.warning("sandbox: %s → local identity",
                            self._landlock_error)

    def apply(self, ctx) -> None:
        ctx.set("sandbox", self)

    def attach(self, pid: int) -> None:
        """子进程生成后挂入沙箱（identity/landlock 后端 = no-op，绝不抛错）。"""
        if self._job is not None:
            try:
                self._job.assign(pid)
            except Exception:
                log.debug("sandbox attach failed for pid %s", pid)

    def confine(self, argv: List[str], cwd: str) -> List[str]:
        """
        在生成前改写 argv（消费者包装点）。

        - jobobject/local：原样返回（生命周期/内存由 Job 承担，cwd 由调用方限定）；
        - landlock：返回包装器 argv——子进程先应用「只读 FS + cwd 可写」再
          exec 目标命令；降级状态下原样返回（如实，不伪装）。
        """
        if self._mode == "landlock" and self._landlock_ok:
            from .landlock import wrapper_argv
            return wrapper_argv(cwd, list(argv))
        return list(argv)

    def describe(self) -> Dict[str, Any]:
        """当前限制模式描述（诊断/UI 用，如实标注降级）。"""
        if self._mode == "landlock":
            confinement = ("landlock (read-only FS + workspace writable)"
                           if self._landlock_ok else "none (degraded)")
            active = self._landlock_ok
        else:
            active = self._job is not None and self._job.active
            confinement = "jobobject (kill-on-close)" if active \
                else "none (identity)"
        out: Dict[str, Any] = {"mode": self._mode, "confinement": confinement,
                               "active": active}
        degraded = self._job_error or self._landlock_error
        if degraded:
            out["degraded"] = degraded
        return out

    def close(self) -> None:
        """关闭后端资源（Job 句柄 → kill-on-close；幂等）。"""
        if self._job is not None:
            self._job.close()
            self._job = None
