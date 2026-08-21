"""
dsh.errors —— 全局异常体系。

每个域的错误都继承 :class:`DshError`，工具错误携带结构化的 `code`，
与 TypeScript 版的 `ToolFailure { message, info }` 对应。
"""
from __future__ import annotations

from typing import Optional


class DshError(Exception):
    """框架根异常。"""


# ---- kernel ----

class ContextError(DshError):
    """Context / 事件派发相关错误。"""


class ServiceNotFoundError(ContextError):
    """请求的 ctx.<key> 服务不存在。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"service not found: ctx.{key}")
        self.key = key


class LoaderError(DshError):
    """配置树加载/挂载失败（含条目 id 与失败阶段）。"""

    def __init__(self, message: str, entry_id: Optional[str] = None,
                 stage: Optional[str] = None) -> None:
        prefix = ""
        if entry_id:
            prefix = f"[{entry_id}]"
            if stage:
                prefix += f"({stage}) "
            else:
                prefix += " "
        super().__init__(prefix + message)
        self.entry_id = entry_id
        self.stage = stage


# ---- session ----

class SessionError(DshError):
    """会话域错误。"""


class SessionFormatError(SessionError):
    """无法忠实解读的日志格式（版本过新/过旧、未知必填事件）。"""

    def __init__(self, message: str, direction: Optional[str] = None) -> None:
        super().__init__(message)
        self.direction = direction


# ---- tools ----

class ToolError(DshError):
    """工具调用结构化失败。`code` 用于策略与持久化诊断。"""

    def __init__(self, message: str, code: str = "TOOL_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ToolNotFoundError(ToolError):
    """未知工具（对应 UNKNOWN_TOOL）。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool: {name}", code="UNKNOWN_TOOL")
        self.name = name


class ToolArgsError(ToolError):
    """参数校验失败（对应 INVALID_ARGS）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_ARGS")


class ToolOutputError(ToolError):
    """输出不符合声明的 canonical schema（对应 INVALID_TOOL_OUTPUT）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_TOOL_OUTPUT")


# ---- llm ----

class LlmFailure(DshError):
    """模型请求的结构化失败（在适配器边界归一化）。"""

    def __init__(self, message: str, code: str = "UNKNOWN",
                 provider: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.provider = provider


class LlmTimeoutError(LlmFailure):
    def __init__(self, provider: Optional[str] = None, timeout: Optional[float] = None) -> None:
        super().__init__(f"LLM request timed out ({timeout}s)", code="TIMEOUT", provider=provider)


# ---- agent ----

class AgentError(DshError):
    """Agent 域错误。"""


# ---- approval ----

class ApprovalDeniedError(DshError):
    """审批被拒绝。"""
