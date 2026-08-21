"""
dsh.todo —— todo 工具（todo_write）：全量列表快照（对应 todo/write 事件）。

与 TS 版 TodoItem 对齐：条目 = {content, status(pending/in_progress/completed)}，
每次写入整体替换（最新写入胜出），因此条目无需稳定身份。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..tools import define_tool


def build_todo_tool() -> Any:
    """构造 todo_write 工具。"""

    @define_tool(
        name="todo_write",
        description="更新任务清单（全量替换；status ∈ pending/in_progress/completed）。",
        parameters={"todos": {"type": "array", "required": True,
                              "items": {"type": "object",
                                        "properties": {
                                            "content": {"type": "string"},
                                            "status": {"type": "string"}},
                                        "required": ["content", "status"]}}},
        output={"type": "string"})
    async def todo_write(args, run_ctx):
        agent = run_ctx.execution.agent
        if agent is None:
            from ..errors import ToolError
            raise ToolError("todo_write requires an agent", code="NO_AGENT")
        todos = args["todos"]
        for item in todos:
            if item.get("status") not in ("pending", "in_progress", "completed"):
                from ..errors import ToolArgsError
                raise ToolArgsError(f"invalid status: {item.get('status')!r}")
        agent.session.append("todo/write", {"todos": todos})
        return f"todo list updated ({len(todos)} items)"

    return todo_write


class ToolTodoPlugin(Service):
    """注册 todo 工具的插件。"""

    inject = ("tools",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx):
        self._disposer = ctx.tools.register(build_todo_tool())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
