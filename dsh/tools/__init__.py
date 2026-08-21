"""
dsh.tools —— 工具系统（ctx.tools）。

导出: ToolDefinition/define_tool（定义）、ToolRuntime（注册表+管线）、
管线类型与决策（pipeline）、JSON Schema 子集校验（schema）、展示词汇（presentation）。
"""
from .definition import (ToolDefinition, ToolOutputDefinition, define_tool)
from .pipeline import (AbortSignal, AcceptDecision, AllowDecision,
                       AskDecision, BlockDecision, DenyDecision,
                       PostToolDecision, PreToolDecision, ToolExecution,
                       ToolExecutionFailure, ToolExecutionResult,
                       ToolExecutionSuccess, ToolGuard, ToolRunContext)
from .registry import ToolRuntime
from .schema import (assert_supported_schema, parameter_schema,
                     validate_value)

__all__ = [
    "ToolDefinition", "ToolOutputDefinition", "define_tool",
    "ToolRuntime", "AbortSignal",
    "ToolExecution", "ToolRunContext", "ToolExecutionResult",
    "ToolExecutionSuccess", "ToolExecutionFailure", "ToolGuard",
    "PreToolDecision", "PostToolDecision",
    "AllowDecision", "DenyDecision", "AskDecision",
    "AcceptDecision", "BlockDecision",
    "assert_supported_schema", "parameter_schema", "validate_value",
]
