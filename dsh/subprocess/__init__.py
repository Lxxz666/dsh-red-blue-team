"""dsh.subprocess —— 子进程能力缝（本地实现 + 终端工具）。"""
from .local import RunResult, SubprocessService
from .tool_bash import IS_WINDOWS, ToolBashPlugin, build_bash_tool

__all__ = ["RunResult", "SubprocessService", "ToolBashPlugin",
           "build_bash_tool", "IS_WINDOWS"]
