"""
dsh.commands —— 人类命令注册表（ctx.commands）+ 内置命令。

对应 TS 版 ctx.commands：斜杠命令不经过模型 turn，直接派发；
handler 可调用 ``agent.followup`` 注入后续工作。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..kernel import Service

log = logging.getLogger("dsh.commands")

CommandHandler = Callable[[Any, List[str]], Optional[str]]
"""handler(agent, args) -> 可选的用户可见回复文本。"""


class CommandRegistry(Service):
    """命令注册表（ctx.commands）。"""

    provides = "commands"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._commands: Dict[str, Dict[str, Any]] = {}

    def apply(self, ctx) -> None:
        ctx.set("commands", self)

    def register(self, name: str, handler: CommandHandler,
                 description: str = "") -> None:
        """注册命令（同名覆盖）。"""
        self._commands[name] = {"handler": handler, "description": description}
        self.ctx.events.emit("commands/change")

    def list(self) -> List[Dict[str, str]]:
        """全部命令（名称 + 描述）。"""
        return [{"name": name, "description": meta["description"]}
                for name, meta in sorted(self._commands.items())]

    def dispatch(self, agent: Any, text: str) -> Optional[Dict[str, Any]]:
        """
        派发一条以 / 开头的输入。

        :return: {"handled": bool, "reply": str|None}。非命令输入返回 handled=False。
        """
        text = text.strip()
        if not text.startswith("/"):
            return {"handled": False, "reply": None}
        parts = text[1:].split(maxsplit=1)
        name = parts[0]
        args = parts[1].split() if len(parts) > 1 else []
        meta = self._commands.get(name)
        if meta is None:
            return {"handled": False,
                    "reply": f"unknown command: /{name}（/help 查看）"}
        reply = meta["handler"](agent, args)
        return {"handled": True, "reply": reply}


def _agent_ctx(agent: Any) -> Any:
    return agent.ctx if hasattr(agent, "ctx") else None


class BuiltinCommandsPlugin(Service):
    """内置命令：/help、/goal、/compact、/new、/plan。"""

    inject = ("commands",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        commands = ctx.commands

        def cmd_help(agent, args):
            lines = ["可用命令:"]
            for item in commands.list():
                lines.append(f"  /{item['name']} - {item['description']}")
            return "\n".join(lines)

        def cmd_goal(agent, args):
            objective = " ".join(args)
            if not objective:
                return "用法: /goal <目标描述>"
            ctx.goals.create(agent.ctx_name, objective)
            agent.followup(f"创建目标: {objective}，开始执行。",
                           source={"kind": "goal"})
            return f"目标已创建: {objective}"

        def cmd_compact(agent, args):
            if not ctx.has("compaction"):
                return "compaction 服务未挂载"
            import asyncio
            asyncio.get_running_loop().create_task(
                ctx.compaction.compact(agent))
            return "已触发压缩"

        def cmd_new(agent, args):
            # 清空收件箱与日志的轻量新会话由 /new 在 UI 层处理；
            # 命令层提示。
            return "在 UI 中点击「新会话」即可；headless 每次运行都是新会话。"

        def cmd_plan(agent, args):
            if ctx.has("planMode"):
                mode = ctx.planMode
                if args and args[0] == "off":
                    mode.exit(agent)
                    return "已退出计划模式"
                mode.enter(agent)
                return ("已进入计划模式：仅允许只读工具；"
                        "给出计划后经 exit_plan_mode 工具退出。")
            return "plan-mode 服务未挂载"

        for name, handler, description in [
            ("help", cmd_help, "列出全部命令"),
            ("goal", cmd_goal, "创建同会话目标"),
            ("compact", cmd_compact, "手动压缩上下文"),
            ("new", cmd_new, "新会话提示"),
            ("plan", cmd_plan, "进入/退出计划模式"),
        ]:
            commands.register(name, handler, description)
        # 注销 = 移除内置命令
        self._disposers.append(lambda: None)

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
        return cleanup
