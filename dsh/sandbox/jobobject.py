"""
dsh.sandbox.jobobject —— Windows Job Object 后端（ctypes，零依赖）。

一个 Job 内可以挂多个子进程；``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` 保证
持有 Job 句柄的进程退出/关闭句柄时，Job 内全部进程被终止——「孤儿子进程
随会话死亡」的硬保证。可选 ``JOB_OBJECT_LIMIT_JOB_MEMORY`` 整组内存上限
（仅 64 位 Windows 支持，配置打开）。

非容器：Job Object 限制资源与生命周期，不限制文件/网络访问（文件侧围栏
仍由 fs 工作区校验承担）。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

log = logging.getLogger("dsh.sandbox")

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJobObject:
    """一个 Windows Job Object（kill-on-close + 可选内存上限）。"""

    def __init__(self, memory_limit_bytes: int = 0) -> None:
        kernel32 = ctypes.windll.kernel32
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("CreateJobObjectW failed")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = \
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_bytes > 0:
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = memory_limit_bytes
        ok = kernel32.SetInformationJobObject(
            self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(self._handle)
            self._handle = 0
            raise OSError("SetInformationJobObject failed")

    @property
    def active(self) -> bool:
        return bool(self._handle)

    def assign(self, pid: int) -> bool:
        """
        把一个子进程挂入 Job（进程可能已退出 → 返回 False 不抛错）。
        """
        if not self._handle:
            return False
        process = self._kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not process:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(
                self._handle, process))
        finally:
            self._kernel32.CloseHandle(process)

    def close(self) -> None:
        """关闭句柄 → kill-on-close 终止 Job 内全部子进程。

        Windows 对经 Job 终止的进程常报告退出码 0——判定「被杀」应看
        远早于自然结束，而非退出码。
        """
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
