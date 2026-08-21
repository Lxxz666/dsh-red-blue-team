"""
dsh.workflow.workflow —— 最小版 workflow 引擎（对应 TS 版 dynamic workflow 的
phases/parallel 子集）。

- spec = {"name", "phases": [{"title", "steps": [{"name", "prompt",
  "output"?: JSON Schema 子集}]}]}；
- phase 顺序执行，phase 内 steps 并行（每个 step 一个 in-process 子代理）；
- 子代理结果尽力解析为 JSON（成功则结构化值，否则原文文本）；
- step 声明 output schema 时按受支持子集强制校验，失败标记 invalid_output；
- 事件：workflow/start、workflow/phase、workflow/agent-start、workflow/agent-end、
  workflow/end（对应 TS 版 workflow/* 事件的子集）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..tools import define_tool

log = logging.getLogger("dsh.workflow")


def _extract_json(text: str) -> Any:
    """从文本中提取第一个配平的 JSON 对象/数组（失败返回 None）。"""
    for start, ch in enumerate(text):
        if ch not in "{[":
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # 此候选非法，继续找下一个
    return None


def _parse_value(text: str) -> Any:
    """尽力把子代理文本解析为 JSON；失败则原样文本。"""
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # 文本中夹带 JSON（如回显前缀）：提取第一个配平对象/数组
    extracted = _extract_json(stripped)
    if extracted is not None:
        return extracted
    return text


class WorkflowEngine(Service):
    """工作流引擎（ctx.workflowEngine）。"""

    provides = "workflowEngine"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)

    def apply(self, ctx) -> None:
        ctx.set("workflowEngine", self)

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        try:
            self.ctx.events.emit(name, payload)
        except Exception:
            pass

    async def run(self, spec: Dict[str, Any],
                  parent: Any) -> Dict[str, Any]:
        """
        执行一个工作流 spec（phase 顺序、step 并行）。

        :param spec: {"name", "phases": [{"title", "steps": [{"name", "prompt"}]}]}。
        :param parent: 父 Agent（子代理继承其 provider/模型）。
        :return: {"name", "results": {phase_title: {step_name: value}}}。
        :raises ValueError: spec 非法。
        """
        name = spec.get("name", "workflow")
        phases = spec.get("phases") or []
        if not isinstance(phases, list) or not phases:
            raise ValueError("workflow spec needs a non-empty 'phases' list")
        self._emit("workflow/start", {"name": name, "parent": parent.id})
        results: Dict[str, Any] = {}
        try:
            for phase in phases:
                title = phase.get("title", f"phase-{len(results) + 1}")
                steps = phase.get("steps") or []
                self._emit("workflow/phase", {"name": name, "phase": title,
                                              "steps": len(steps)})

                async def _run_step(step: Dict[str, Any]) -> tuple:
                    step_name = step.get("name", "step")
                    prompt = step.get("prompt", "")
                    self._emit("workflow/agent-start",
                               {"name": name, "phase": title,
                                "step": step_name})
                    try:
                        provider = self.ctx.subagents.get("in-process")
                        text = await provider.run(
                            parent, f"workflow step {step_name}", prompt)
                        value = _parse_value(text)
                        # 结构化输出强制：step 声明 output schema 时按
                        # 受支持子集校验，失败不抛错而是标记 invalid_output
                        output_schema = step.get("output")
                        if output_schema:
                            from ..errors import ToolError
                            from ..tools.schema import validate_value
                            try:
                                validate_value(
                                    value, output_schema,
                                    what=f"workflow step {step_name} output")
                            except ToolError as exc:
                                value = {
                                    "invalid_output": True,
                                    "value": value,
                                    "error": str(exc)}
                        self._emit("workflow/agent-end",
                                   {"name": name, "phase": title,
                                    "step": step_name, "ok": True})
                        return step_name, value
                    except Exception as exc:
                        self._emit("workflow/agent-end",
                                   {"name": name, "phase": title,
                                    "step": step_name, "ok": False,
                                    "error": str(exc)})
                        return step_name, {"error": str(exc)}

                pairs = await asyncio.gather(
                    *(_run_step(step) for step in steps))
                results[title] = dict(pairs)
            return {"name": name, "results": results}
        finally:
            self._emit("workflow/end", {"name": name})


def build_workflow_tool() -> Any:
    """构造 workflow_run 工具（模型可用 spec 表达并行任务）。"""

    @define_tool(
        name="workflow_run",
        description="执行多阶段工作流：phase 顺序执行，phase 内 step 并行"
                    "（每个 step 一个子代理），返回结构化结果。",
        parameters={
            "name": {"type": "string", "description": "工作流名"},
            "phases": {
                "type": "array", "required": True,
                "items": {"type": "object",
                          "properties": {
                              "title": {"type": "string"},
                              "steps": {"type": "array",
                                        "items": {"type": "object",
                                                  "properties": {
                                                      "name": {"type": "string"},
                                                      "prompt": {"type": "string"},
                                                      "output": {
                                                          "type": "object",
                                                          "description":
                                                              "该 step 输出的 JSON Schema（子集），不匹配时结果带 invalid_output"}},
                                                  "required": ["prompt"]},
                                        "required": True}},
                          "required": ["steps"]}},
        },
        output={"type": "object"},
        timeout_ms=600_000,
        render=lambda args, value: json.dumps(value, ensure_ascii=False,
                                              indent=2))
    async def workflow_run(args, run_ctx):
        agent = run_ctx.execution.agent
        if agent is None:
            from ..errors import ToolError
            raise ToolError("workflow_run requires a parent agent",
                            code="NO_AGENT")
        ctx = agent.ctx if hasattr(agent, "ctx") else run_ctx.root_ctx
        if not ctx.has("workflowEngine"):
            from ..errors import ToolError
            raise ToolError("workflowEngine not mounted", code="NO_ENGINE")
        return await ctx.workflowEngine.run(args, agent)

    return workflow_run


class ToolWorkflowPlugin(Service):
    """注册 workflow_run 工具的插件。"""

    inject = ("tools", "workflowEngine", "subagents")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        self._disposer = ctx.tools.register(build_workflow_tool())

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
