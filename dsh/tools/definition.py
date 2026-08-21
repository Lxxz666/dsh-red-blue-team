"""
dsh.tools.definition —— ToolDefinition 与 define_tool（对应 TS 版 ToolDefinition）。

契约要点:

- ``output`` 为 canonical 输出声明（schema + render）：工具体返回 JSON 值，
  注册表校验后经 render 投影为模型可见内容；
- ``execute(args, run_ctx)`` 是唯一执行入口，``args`` 由注册表按 parameters 校验；
- ``timeout_ms`` / ``is_concurrency_safe`` / ``present_call`` / ``present_result``
  绝不泄漏进模型请求（``schemas()`` 白名单只取 name/description/parameters）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..session.events import is_json_value
from ..errors import ToolOutputError

ExecuteFn = Callable[[Any, Any], Any]
"""execute 签名: ``async def execute(args, run_ctx: ToolRunContext) -> value``。"""

PresentCallFn = Callable[[Any], Optional[Dict[str, Any]]]
PresentResultFn = Callable[[Any, Any], Optional[Dict[str, Any]]]
FinalizeContentFn = Callable[[Any, Any], Optional[str]]
ConcurrencyClassifier = Callable[[Any], bool]


def _default_render(args: Any, value: Any) -> str:
    """默认输出投影：字符串原样、其余 JSON 序列化。"""
    import json
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class ToolOutputDefinition:
    """工具自有的 canonical 输出契约。"""

    schema: Any = None
    """canonical 值的 JSON Schema（None = 不约束）。"""

    render: Callable[[Any, Any], str] = field(default=_default_render)
    """纯投影: (args, value) -> 模型可见文本。"""

    def validate(self, value: Any) -> Any:
        """校验 canonical 值（失败抛 ToolOutputError）。"""
        from .schema import validate_value
        return validate_value(value, self.schema, error_cls=ToolOutputError,
                              what="tool output")


@dataclass
class ToolDefinition:
    """一个已注册工具：模型可见 schema + 执行函数 + 可选回调。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    output: ToolOutputDefinition
    execute: ExecuteFn
    timeout_ms: Optional[float] = None
    is_concurrency_safe: Optional[ConcurrencyClassifier] = None
    present_call: Optional[PresentCallFn] = None
    present_result: Optional[PresentResultFn] = None
    finalize_content: Optional[FinalizeContentFn] = None

    def model_schema(self) -> Dict[str, Any]:
        """模型可见投影：只允许 name/description/parameters。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate_args(self, args: Any) -> Any:
        """按 parameters 校验参数（失败抛 ToolArgsError）。"""
        from .schema import validate_value
        return validate_value(args, self.parameters,
                              error_cls=__import__("dsh.errors", fromlist=["ToolArgsError"]).ToolArgsError,
                              what=f"args for tool {self.name}")

    def classify_concurrency(self, args: Any) -> bool:
        """并发分类：只有精确 True 才并行（fail-closed）。"""
        if self.is_concurrency_safe is None:
            return False
        try:
            return self.is_concurrency_safe(args) is True
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"<ToolDefinition {self.name}>"


def define_tool(name: str, description: str, parameters: Dict[str, Any],
                output: Optional[Dict[str, Any]] = None, *,
                render: Optional[Callable[[Any, Any], str]] = None,
                timeout_ms: Optional[float] = None,
                concurrency_safe: Optional[ConcurrencyClassifier] = None,
                present_call: Optional[PresentCallFn] = None,
                present_result: Optional[PresentResultFn] = None,
                finalize_content: Optional[FinalizeContentFn] = None):
    """
    工具定义装饰器（一等工具的推荐写法）::

        @define_tool(
            name="add",
            description="两数相加",
            parameters={"a": {"type": "number", "required": True},
                        "b": {"type": "number", "required": True}},
            output={"type": "number"},
        )
        async def add(args, run_ctx):
            return args["a"] + args["b"]

    :param name: 工具名（注册表内唯一）。
    :param description: 一句话描述（进模型 schema）。
    :param parameters: 隐式开放对象参数声明（见 schema.parameter_schema）。
    :param output: canonical 输出 schema。
    :param render: 输出投影 (args, value) -> 文本。
    :param timeout_ms: 协作式超时预算。
    :param concurrency_safe: 并发分类器（True 才并行）。
    :param present_call/present_result: UI 渲染意图。
    :param finalize_content: 最后一里路内容变换。
    """
    from .schema import parameter_schema

    def decorator(fn: ExecuteFn) -> ToolDefinition:
        compiled = parameter_schema(parameters)
        out_def = ToolOutputDefinition(
            schema=output, render=render or _default_render)
        return ToolDefinition(
            name=name, description=description, parameters=compiled,
            output=out_def, execute=fn, timeout_ms=timeout_ms,
            is_concurrency_safe=concurrency_safe,
            present_call=present_call, present_result=present_result,
            finalize_content=finalize_content)
    return decorator
