"""
dsh.sandbox.landlock —— Linux Landlock 后端（ctypes syscall，零依赖）。

Landlock 是进程**自限制**机制（irreversible）：正确用法是 spawn 子进程时
先应用规则再 exec。因此本模块分两半：

- ``apply_workspace_landlock(workspace)``：把**当前进程**限制为「只读
  文件系统 + 工作区可写」——由包装器进程在 exec 目标命令前调用；
- ``available()``：探针（创建一个 handled_access_fs=0 的规则集）判断内核
  是否支持（ENOSYS / EOPNOTSUPP / EACCES → False，如实降级）。

ABI：v1 起（LANDLOCK_CREATE_RULESET_VERSION 校验握手）；访问权掩码
READ_FILE/READ_DIR/EXECUTE 全局放行（handled 后默认拒绝其余），
WRITE_FILE/REMOVE_FILE/MAKE_*/REFER/TRUNCATE 仅工作区 path_beneath。

**边界（如实标注）**：仅 Linux 有效；不可逆；与 Job Object 不同，
Landlock 限制**文件系统**（网络/进程生命周期由别的机制承担）。
本模块在非 Linux 上只用于「模式解析 + 包装器 argv 构造」测试。
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional

# ---- ABI 常量 ----
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

_READONLY_GLOBAL = (_LANDLOCK_ACCESS_FS_EXECUTE
                    | _LANDLOCK_ACCESS_FS_READ_FILE
                    | _LANDLOCK_ACCESS_FS_READ_DIR)
_WRITE_MASK = (_LANDLOCK_ACCESS_FS_WRITE_FILE
               | _LANDLOCK_ACCESS_FS_REMOVE_DIR
               | _LANDLOCK_ACCESS_FS_REMOVE_FILE
               | _LANDLOCK_ACCESS_FS_MAKE_CHAR
               | _LANDLOCK_ACCESS_FS_MAKE_DIR
               | _LANDLOCK_ACCESS_FS_MAKE_REG
               | _LANDLOCK_ACCESS_FS_MAKE_SOCK
               | _LANDLOCK_ACCESS_FS_MAKE_FIFO
               | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
               | _LANDLOCK_ACCESS_FS_MAKE_SYM
               | _LANDLOCK_ACCESS_FS_REFER
               | _LANDLOCK_ACCESS_FS_TRUNCATE)

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def _syscall(number: int, *args) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.syscall
    fn.restype = ctypes.c_long
    return int(fn(ctypes.c_long(number), *args))


def available() -> bool:
    """探针：内核是否支持 Landlock ABI v1+（非 Linux → False）。"""
    if sys.platform != "linux":
        return False
    try:
        attr = _LandlockRulesetAttr(handled_access_fs=0)
        fd = _syscall(_SYS_LANDLOCK_CREATE_RULESET,
                      ctypes.byref(attr), ctypes.sizeof(attr), 0)
        if fd < 0:
            return False
        os.close(fd)
        return True
    except (OSError, AttributeError):
        return False


def apply_workspace_landlock(workspace: str) -> None:
    """
    把当前进程限制为「只读 FS + 工作区可写」（不可逆）。

    Landlock 语义：任何进入 ``handled_access_fs`` 的访问**默认全部拒绝**，
    除非有规则显式放行。因此必须加两条 path_beneath 规则：
    根路径放行读/执行（含动态库），工作区放行写掩码。

    :raises OSError: 内核不支持或应用失败（调用方降级处理）。
    """
    handled = _READONLY_GLOBAL | _WRITE_MASK
    attr = _LandlockRulesetAttr(handled_access_fs=handled)
    ruleset_fd = _syscall(_SYS_LANDLOCK_CREATE_RULESET,
                          ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        raise OSError(f"landlock_create_ruleset failed (errno "
                      f"{ctypes.get_errno()})")
    try:
        # 规则 1：全文件系统只读（含 EXECUTE，进程自身二进制/动态库）
        root_fd = os.open("/", os.O_PATH | os.O_CLOEXEC)
        try:
            read_attr = _LandlockPathBeneathAttr(
                allowed_access=_READONLY_GLOBAL, parent_fd=root_fd)
            if _syscall(_SYS_LANDLOCK_ADD_RULE, ruleset_fd,
                        _LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(read_attr), 0) < 0:
                raise OSError(f"landlock_add_rule(readonly) failed (errno "
                              f"{ctypes.get_errno()})")
        finally:
            os.close(root_fd)
        # 规则 2：工作区可写
        parent_fd = os.open(workspace, os.O_PATH | os.O_CLOEXEC)
        try:
            path_attr = _LandlockPathBeneathAttr(
                allowed_access=_WRITE_MASK, parent_fd=parent_fd)
            if _syscall(_SYS_LANDLOCK_ADD_RULE, ruleset_fd,
                        _LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(path_attr), 0) < 0:
                raise OSError(f"landlock_add_rule failed (errno "
                              f"{ctypes.get_errno()})")
        finally:
            os.close(parent_fd)
        if _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            raise OSError(f"landlock_restrict_self failed (errno "
                          f"{ctypes.get_errno()})")
    finally:
        os.close(ruleset_fd)


def wrapper_argv(workspace: str, argv: list) -> list:
    """包装器 argv：子进程先应用 landlock 再 exec 目标命令。"""
    return [sys.executable, "-m", "dsh.sandbox.landlock_exec", workspace] \
        + list(argv)
