"""
dsh.hooks.compat —— Claude Code / Codex hooks 兼容桥（对应 TS 版
hook-protocol + claude/codex 桥的**文件格式兼容**子集）。

- 读取 ``~/.claude/settings.json``、``~/.claude/settings.local.json``、
  ``./.claude/settings.json``、``./.claude/settings.local.json``（Claude Code）
  与 ``~/.codex/config.toml``、``./.codex/config.toml``（OpenAI Codex）；
- 解析为归一化 HookSpec（与 hooks.yml 同一行形状：on/command/decision +
  扩展键 tool_pattern / stdin_json / vars），复用
  ``dsh.hooks.hooks`` 的 ``run_hook``/``hooks_for`` 运行器；
- 事件映射：

  | Claude Code | Codex | dsh 监听点 |
  |---|---|---|
  | PreToolUse | Command | pre-tool（非零 = deny） |
  | PostToolUse | — | post-tool（非零 = block） |
  | UserPromptSubmit | — | pre-step（非零 = reject） |
  | SessionStart | SessionStart | session-start |
  | Stop | Stop / SessionEnd | turn-stopping |
  | Notification / SubagentStop / PreCompact | Notification | 不映射（记录日志） |

- 语义差异（如实标注）：Claude matcher 支持正则工具名匹配（tool_pattern）；
  Claude 命令经 stdin 收 JSON（stdin_json）；Codex 命令内
  ``$VAR``/``${VAR}`` 做 best-effort 环境替换（vars）。非零退出/exit 2
  的具体 Claude 语义差异见手册。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 fallback
    import tomli as tomllib  # type: ignore

from ..kernel import Service
from .hooks import hooks_for, run_hook
from ..tools.pipeline import (AskDecision, BlockDecision, DenyDecision)

log = logging.getLogger("dsh.hooks")

# 事件名映射（TS 兼容语义见模块 docstring）
CLAUDE_EVENT_MAP = {
    "PreToolUse": ("pre-tool", "deny"),
    "PostToolUse": ("post-tool", "block"),
    "UserPromptSubmit": ("pre-step", "reject"),
    "SessionStart": ("session-start", None),
    "Stop": ("turn-stopping", None),
}
CODEX_EVENT_MAP = {
    "Command": ("pre-tool", "deny"),
    "SessionStart": ("session-start", None),
    "Stop": ("turn-stopping", None),
    "SessionEnd": ("turn-stopping", None),
}


def discover_paths(config: Optional[dict] = None) -> List[str]:
    """默认发现顺序（先全局后项目、先主后 local）。"""
    if config and config.get("paths"):
        return [os.path.expanduser(p) for p in config["paths"]]
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".claude", "settings.json"),
        os.path.join(home, ".claude", "settings.local.json"),
        os.path.join(".claude", "settings.json"),
        os.path.join(".claude", "settings.local.json"),
        os.path.join(home, ".codex", "config.toml"),
        os.path.join(".codex", "config.toml"),
    ]


def load_claude_hooks(path: str) -> List[Dict[str, Any]]:
    """解析 Claude Code settings.json 的 hooks 键 → 归一化行。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    hooks = (data or {}).get("hooks") or {}
    rows: List[Dict[str, Any]] = []
    for event, entries in hooks.items():
        mapped = CLAUDE_EVENT_MAP.get(event)
        if mapped is None:
            log.debug("claude hook event %s not mapped (ignored)", event)
            continue
        on, _decision = mapped
        for i, entry in enumerate(entries or []):
            matcher = entry.get("matcher")
            for j, sub in enumerate(entry.get("hooks") or []):
                if sub.get("type") != "command" or not sub.get("command"):
                    continue
                rows.append({
                    "name": f"claude:{event}:{i}:{j}",
                    "on": on,
                    "command": sub["command"],
                    "decision": "deny",
                    "tool_pattern": matcher or None,
                    "stdin_json": True,   # Claude 契约：JSON 走 stdin
                })
    return rows


def load_codex_hooks(path: str) -> List[Dict[str, Any]]:
    """解析 Codex config.toml 的 [hooks] 段 → 归一化行。"""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    hooks = (data or {}).get("hooks") or {}
    rows: List[Dict[str, Any]] = []
    for event, command in hooks.items():
        mapped = CODEX_EVENT_MAP.get(event)
        if mapped is None:
            log.debug("codex hook event %s not mapped (ignored)", event)
            continue
        on, _decision = mapped
        if not isinstance(command, str) or not command.strip():
            continue
        rows.append({
            "name": f"codex:{event}",
            "on": on,
            "command": command,
            "decision": "deny",
            "vars": True,  # Codex 契约：$VAR 替换
        })
    return rows


class HooksCompatPlugin(Service):
    """Claude/Codex 配置兼容桥：解析 → 归一化 → 挂监听点。"""

    inject = ("subprocess",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.paths = discover_paths(config)
        self._disposers: List[Any] = []
        self._hooks: List[Dict[str, Any]] = []

    def apply(self, ctx) -> None:
        self._hooks = []
        for path in self.paths:
            if not os.path.exists(path):
                continue
            if path.endswith(".json"):
                rows = load_claude_hooks(path)
            else:
                rows = load_codex_hooks(path)
            if rows:
                log.info("hooks compat: %d hooks from %s", len(rows), path)
                self._hooks.extend(rows)
        if self._hooks:
            self._disposers.append(
                ctx.on("agent/session-start", self._on_session_start))
            self._disposers.append(ctx.on("agent/pre-step", self._on_pre_step))
            self._disposers.append(
                ctx.on("tools/pre-execute", self._on_pre_tool))
            self._disposers.append(
                ctx.on("tools/post-execute", self._on_post_tool))
            self._disposers.append(
                ctx.on("agent/turn-stopping", self._on_turn_stopping))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
            self._hooks = []
        return cleanup

    def _on_session_start(self, payload: Dict[str, Any]) -> None:
        agent = payload.get("agent")
        for hook in hooks_for(self._hooks, "session-start"):
            import asyncio
            asyncio.get_running_loop().create_task(
                run_hook(self.ctx, agent, hook))

    async def _on_pre_step(self, payload: Dict[str, Any], next):
        agent = payload.get("agent")
        for hook in hooks_for(self._hooks, "pre-step"):
            outcome = await run_hook(self.ctx, agent, hook)
            if outcome["exit_code"] != 0:
                return {"kind": "reject"}
        return await next()

    async def _on_pre_tool(self, execution, next):
        for hook in hooks_for(self._hooks, "pre-tool", execution.name):
            outcome = await run_hook(self.ctx, execution.agent, hook,
                                     execution.name)
            if outcome["exit_code"] != 0:
                return DenyDecision(outcome["output"] or "hook denied")
        return await next()

    async def _on_post_tool(self, execution, result, next):
        for hook in hooks_for(self._hooks, "post-tool", execution.name):
            outcome = await run_hook(self.ctx, execution.agent, hook,
                                     execution.name)
            if outcome["exit_code"] != 0:
                return BlockDecision(outcome["output"] or "hook blocked")
        return await next()

    async def _on_turn_stopping(self, payload: Dict[str, Any]) -> None:
        agent = payload.get("agent")
        for hook in hooks_for(self._hooks, "turn-stopping"):
            await run_hook(self.ctx, agent, hook)
