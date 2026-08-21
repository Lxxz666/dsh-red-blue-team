"""dsh.fs —— 文件系统能力缝。"""
from .fs import FsService, FileDiff
from .local import LocalFsService
from .tool_fs import ToolFsPlugin, build_tools

__all__ = ["FsService", "FileDiff", "LocalFsService", "ToolFsPlugin", "build_tools"]
