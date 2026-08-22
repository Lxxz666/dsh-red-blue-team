"""redteam.errors —— 统一错误类型。"""
from __future__ import annotations


class RedTeamError(Exception):
    """项目基础异常。"""


class ConfigError(RedTeamError):
    """扫描配置非法（含授权缺失）。"""


class AuthorizationError(RedTeamError):
    """目标未授权 —— 合规红线，扫描直接拒绝。"""


class AdapterError(RedTeamError):
    """目标适配器失败（连接/超时/协议错误）。"""


class TargetUnavailable(AdapterError):
    """目标不可达。"""


class UnsupportedSurface(AdapterError):
    """样本类型与目标适配器不匹配（如对话样本发往 MCP 目标）→ 该样本跳过。"""


class SampleError(RedTeamError):
    """样本库加载/展开失败。"""


class StorageError(RedTeamError):
    """存储层失败。"""


class FixError(RedTeamError):
    """蓝队修复失败。"""


class RegressionError(RedTeamError):
    """回归验证失败（修复无效）。"""
