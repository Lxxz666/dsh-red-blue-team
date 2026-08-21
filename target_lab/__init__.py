"""target_lab —— 内置靶场：故意埋了漏洞的弱防护电商客服 agent。

自测 + 演示 + 教学用途：无真实目标时跑通"扫描 → 修复 → 回归"全闭环。
所有漏洞由 guards 防护配置控制，蓝队修复 = 收紧 guards → 回归清零。
"""
from .db import FakeDB
from .guards import (DEFAULT_GUARDS, HARDENED_GUARDS, GuardConfig,
                     build_hardened_guards_file, dump_guards, load_guards,
                     patched_guards)
from .inventory import PLANTED_VULNS, planted_categories
from .server import LabServer, build_default_guards_file, start_lab

__all__ = ["FakeDB", "GuardConfig", "DEFAULT_GUARDS", "HARDENED_GUARDS",
           "load_guards", "dump_guards", "patched_guards",
           "build_hardened_guards_file", "PLANTED_VULNS",
           "planted_categories", "LabServer", "start_lab",
           "build_default_guards_file"]
