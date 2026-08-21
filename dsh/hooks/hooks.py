"""
dsh.hooks.hooks —— Hooks 桥（最小版，对应 hook-protocol + claude/codex 桥的 YAML 化）。

配置文件 `~/.dsh/hooks.yml`（config.path 可换）::

    hooks:
      - name: guard-write
        on: pre-tool          # session-start | pre-step | pre-tool | post-tool | turn-stopping
        tools: [fs_write]     # 可选过滤（仅 pre-tool/post-tool 生效）
        command: "echo blocked; exit 1"   # shell 命令（经 ctx.subprocess 执行）
        decision: deny        # pre-tool 时: deny | ask（默认 allow）
                              # pre-step 非零退出 = reject

- 每次调用向 agent 会话追加 log-only 事件 ``hook/invoked`` / ``hook/result``
  （handler_id 关联，与 TS 版 hook-bridges 的 hook/invoked、hook/result 对应）；
- 监听点全部走既有扩展点（agent/session-start、agent/pre-step、tools/pre-execute、
  tools/post-execute、agent/turn-stopping）——不修改循环；
- 行级扩展（第十一批，hooks 兼容桥用）：``tool_pattern`` 正则过滤、
  ``stdin_json``（Claude Code 契约：JSON 写命令 stdin）、``vars``
  （Codex 契约：command 内 ``$VAR``/``${VAR}`` 替换）；
- 运行器为模块级函数（``hooks_for``/``run_hook``），
  ``dsh/hooks/compat.py`` 的 Claude/Codex 兼容桥复用同一套。
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import yaml

from ..kernel import Service
from ..session import register_event_type
from ..tools.pipeline import (AskDecision, BlockDecision, DenyDecision)

log = logging.getLogger("dsh.hooks")

register_event_type("hook/invoked",
                    "一次 hook 调用开始（log-only，handler_id 关联）。")
register_event_type("hook/result",
                    "一次 hook 调用结束（log-only：exit_code/output）。")

HOOK_EVENTS = ("session-start", "pre-step", "pre-tool", "post-tool",
               "turn-stopping")


class _NoBoolLoader(yaml.SafeLoader):
    """禁用 YAML 1.1 布尔解析（否则裸键 ``on`` 会被解析成 True）。"""


_NoBoolLoader.add_constructor(
    "tag:yaml.org,2002:bool",
    lambda loader, node: loader.construct_scalar(node))


# ---- 可复用运行器（hooks.yml 与 claude/codex 兼容桥共用） ----

def hooks_for(hooks: List[Dict[str, Any]], event: str,
              tool_name: Optional[str] = None) -> List[dict]:
    """过滤某事件的 hook（`tools` 精确列表 或 `tool_pattern` 正则）。"""
    import re
    out = []
    for hook in hooks:
        if hook.get("on") != event:
            continue
        tools = hook.get("tools")
        if tools and (tool_name is None or tool_name not in tools):
            continue
        pattern = hook.get("tool_pattern")
        if pattern and (tool_name is None
                        or re.search(pattern, tool_name) is None):
            continue
        out.append(hook)
    return out


def _substitute_vars(command: str, ctx, agent: Optional[Any],
                     tool_name: Optional[str]) -> str:
    """Codex 风格的 ``$VAR``/``${VAR}`` 替换（best-effort）。"""
    import re
    values = dict(os.environ)
    values["CWD"] = (ctx.fs.workspace_root() if ctx.has("fs")
                     else os.getcwd())
    if agent is not None:
        values["SESSION_ID"] = agent.id
    if tool_name:
        values["ARGUMENTS"] = tool_name

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        return values.get(name, match.group(0))
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                  replace, command)


async def run_hook(ctx, agent: Optional[Any], hook: Dict[str, Any],
                   tool_name: Optional[str] = None,
                   env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """执行一个 hook 并落 hook/invoked + hook/result 日志（模块级可复用）。

    - ``hook.stdin_json`` 为真时把 {"session_id","hook_event_name",
      "tool_name"} 写到命令 stdin（Claude Code 兼容契约）；
    - ``hook.vars`` 存在时对 command 做 ``$VAR``/``${VAR}`` 替换
      （Codex 兼容，best-effort）。
    """
    handler_id = f"hook-{uuid.uuid4().hex[:8]}"
    if agent is not None:
        try:
            agent.session.append("hook/invoked",
                                 {"handler_id": handler_id,
                                  "name": hook.get("name"),
                                  "on": hook.get("on"),
                                  "tool": tool_name})
        except Exception:
            pass
    command = hook["command"]
    if hook.get("vars"):
        command = _substitute_vars(command, ctx, agent, tool_name)
    run_env = dict(os.environ, **(env or {}))
    if tool_name:
        run_env["DSH_HOOK_TOOL"] = tool_name
    stdin_data = None
    if hook.get("stdin_json"):
        import json
        stdin_data = json.dumps({
            "session_id": agent.id if agent is not None else "",
            "hook_event_name": hook.get("on", ""),
            "tool_name": tool_name or "",
        }, ensure_ascii=False)
    try:
        from ..subprocess import IS_WINDOWS
        shell_argv = ["pwsh", "-NoProfile", "-Command", command] \
            if IS_WINDOWS else ["bash", "-lc", command]
        result = await ctx.subprocess.run(
            shell_argv,
            cwd=ctx.fs.workspace_root() if ctx.has("fs") else os.getcwd(),
            timeout=60, env=run_env, stdin_data=stdin_data)
        outcome = {"exit_code": result.exit_code,
                   "output": (result.stdout + result.stderr).strip()[:2000]}
    except Exception as exc:
        outcome = {"exit_code": -1, "output": f"hook error: {exc}"}
    if agent is not None:
        try:
            agent.session.append("hook/result",
                                 {"handler_id": handler_id,
                                  "name": hook.get("name"),
                                  "exit_code": outcome["exit_code"],
                                  "output": outcome["output"]})
        except Exception:
            pass
    return outcome


class HooksPlugin(Service):
    """hooks.yml 桥：把配置映射到扩展点监听器。"""

    inject = ("subprocess",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.path = os.path.expanduser(
            (config or {}).get("path", "~/.dsh/hooks.yml"))
        self._disposers: List[Any] = []
        self._hooks: List[Dict[str, Any]] = []

    def apply(self, ctx) -> None:
        self._load()
        self._disposers.append(ctx.on("agent/session-start", self._on_session_start))
        self._disposers.append(ctx.on("agent/pre-step", self._on_pre_step))
        self._disposers.append(ctx.on("tools/pre-execute", self._on_pre_tool))
        self._disposers.append(ctx.on("tools/post-execute", self._on_post_tool))
        self._disposers.append(ctx.on("agent/turn-stopping", self._on_turn_stopping))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup

    def _load(self) -> None:
        try:
            text = open(self.path, "r", encoding="utf-8").read()
            data = yaml.load(text, Loader=_NoBoolLoader) or {}
        except (OSError, yaml.YAMLError):
            self._hooks = []
            return
        self._hooks = [h for h in (data.get("hooks") or [])
                       if h.get("on") in HOOK_EVENTS]

    # ---- 监听点 ----

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
                return {"kind": "reject"}  # 非零退出 = 拒绝本步
        return await next()

    async def _on_pre_tool(self, execution, next):
        for hook in hooks_for(self._hooks, "pre-tool", execution.name):
            outcome = await run_hook(self.ctx, execution.agent, hook,
                                     execution.name)
            if outcome["exit_code"] != 0:
                decision = hook.get("decision", "deny")
                if decision == "ask":
                    return AskDecision(outcome["output"] or "hook asks")
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
