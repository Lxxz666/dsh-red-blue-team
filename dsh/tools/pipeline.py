"""
dsh.tools.pipeline —— 守卫执行管线的类型与决策（对应 TS 版 ToolExecution/Decisions）。

管线（由 ToolRuntime.execute 编排）::

    tools/pre-execute (waterfall allow/deny/ask)
      → 单调 guards
      → tools/execute (around-dispatch waterfall)
      → tools/post-execute (waterfall accept/replace/block)
      → finalize_content
      → tools/result (emit 不可变权威结果)

不变式: 参数在策略前一次性物化并深冻结；只有 tools/execute 包装者可替换 signal；
guard 只能收窄权限；tools/result 观察者拿到的对象已冻结。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from ..errors import ToolError


class AbortSignal:
    """协作式取消信号（对应 JS AbortSignal）。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.aborted = False
        self.reason: Any = None

    def abort(self, reason: Any = None) -> None:
        """触发取消（幂等，首个 reason 胜出）。"""
        if not self.aborted:
            self.aborted = True
            self.reason = reason
            self._event.set()

    def raise_if_aborted(self) -> None:
        """若已取消则抛 ToolError('ABORTED')。"""
        if self.aborted:
            raise ToolError("aborted", code="ABORTED")

    async def wait(self) -> None:
        """等待取消（不取消则永远挂起）。"""
        await self._event.wait()

    def __repr__(self) -> str:
        return f"<AbortSignal aborted={self.aborted}>"


@dataclass(frozen=True)
class ToolExecution:
    """
    一次工具调用的管线内身份（参数已物化/冻结，身份字段只读）。

    ``token`` 为注册表分配的关联身份（嵌套派发时以 ``parent`` 形式传递）。
    """

    call_id: str
    name: str
    arguments: Any
    agent: Any = None
    signal: AbortSignal = field(default_factory=AbortSignal)
    token: object = field(default_factory=object)
    parent: Optional[object] = None

    def defer_result(self) -> None:  # pragma: no cover - 占位，见 ToolRunContext
        pass


@dataclass
class ToolRunContext:
    """工具体拿到的运行时扩展（execute 的第二参数）。"""

    execution: ToolExecution
    deferred_contexts: List[Dict[str, Any]] = field(default_factory=list)
    _concludes_turn: bool = False
    root_ctx: Any = None
    """注册表所在根 Context（agentless 执行时解析服务用）。"""

    @property
    def signal(self) -> AbortSignal:
        return self.execution.signal

    def defer_context(self, message: Dict[str, Any]) -> None:
        """把上下文附加到本次执行的结果（循环在 tool/result 之后追加）。"""
        self.deferred_contexts.append(message)

    def conclude_turn(self) -> None:
        """把成功结果标记为 turn 终点（循环在提交后停止）。"""
        self._concludes_turn = True


# ---- 决策 ----

@dataclass(frozen=True)
class AllowDecision:
    kind: str = "allow"


@dataclass(frozen=True)
class DenyDecision:
    reason: str
    kind: str = "deny"


@dataclass(frozen=True)
class AskDecision:
    reason: Optional[str] = None
    kind: str = "ask"


PreToolDecision = Union[AllowDecision, DenyDecision, AskDecision]


@dataclass(frozen=True)
class AcceptDecision:
    kind: str = "accept"
    content: Optional[str] = None
    value: Optional[Any] = None


@dataclass(frozen=True)
class BlockDecision:
    feedback: str
    kind: str = "block"


PostToolDecision = Union[AcceptDecision, BlockDecision]


# ---- 结果 ----

@dataclass(frozen=True)
class ToolExecutionSuccess:
    """成功的 canonical 执行结果（is_error=False）。"""

    value: Any
    content: str
    meta: Optional[Dict[str, Any]] = None
    concludes_turn: bool = False
    additional_contexts: List[Dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"is_error": False, "content": self.content}
        if self.meta is not None:
            out["meta"] = self.meta
        if self.concludes_turn:
            out["concludes_turn"] = True
        return out


@dataclass(frozen=True)
class ToolExecutionFailure:
    """失败的执行结果（is_error=True，永不带 value）。"""

    error: ToolError
    content: str = ""
    meta: Optional[Dict[str, Any]] = None
    is_error: bool = True

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "is_error": True,
            "content": self.content,
            "error": {"name": type(self.error).__name__,
                      "code": getattr(self.error, "code", "TOOL_ERROR"),
                      "message": self.error.message},
        }
        if self.meta is not None:
            out["meta"] = self.meta
        return out


ToolExecutionResult = Union[ToolExecutionSuccess, ToolExecutionFailure]

ToolGuard = Callable[[ToolExecution], Optional[str]]
"""单调 guard: 返回原因 = 最终拒绝；返回 None = 维持原判（无 allow 结果）。"""
