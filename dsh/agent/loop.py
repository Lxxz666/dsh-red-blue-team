"""
dsh.agent.loop —— AgentLoop（ctx.agentLoop）：具体驱动循环（对应 agent-loop 包）。

turn/step 流转（与 docs/agent-lifecycle.md 的序列图一致）::

    turn/start
      → claim batch（next-step 全量 + 一条 next-turn）
      → agent/pre-step waterfall（reject | enter(messages)）
      → step/start → user/message*
      → system-prompt/assemble → agent/request waterfall → llm/stream
      → assistant/chunk* → assistant/message
      → tool/call* → tools.execute 管线 → tool/result*
      → step/end
      → agent/turn-stopping serial（steering 可再开一步）
    turn/end

每次成功的模型请求广播 ``agent/request-done`` {agent, turn, step, provider,
model, usage, latency_ms}（观测/指标用）。

持久事件走 session 日志；``agent/*`` 是活跃控制/状态。模型可见即入日志。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional

from ..errors import LlmFailure
from ..kernel import Service
from ..llm.adapters import LlmCallConfig, LlmRequest
from ..llm.stream import AssistantAssembler, StreamChunk
from ..session import Session, SessionHeader
from ..tools.pipeline import AbortSignal
from .agent import Agent

log = logging.getLogger("dsh.agent")


class _TurnFailed(Exception):
    """turn 因错误结束的内部信号（由 _run_turn 捕获转为 error 原因）。"""

    def __init__(self, failure: Dict[str, Any]) -> None:
        super().__init__(failure.get("message", "turn failed"))
        self.failure = failure


class AgentLoopService(Service):
    """具体 agent 工厂与驱动（ctx.agentLoop）。"""

    provides = "agentLoop"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._drivers: Dict[str, asyncio.Task] = {}

    def apply(self, ctx) -> None:
        ctx.set("agentLoop", self)
        if ctx.has("agents"):
            ctx.agents.set_factory(self)

    # ---- 事件门面 ----

    def emit_agent_event(self, name: str, payload: Dict[str, Any]) -> None:
        """fire-and-forget 广播 agent/* 事件（异常隔离）。"""
        try:
            self.ctx.events.emit(name, payload)
        except Exception:
            log.exception("agent event %s failed", name)

    # ---- 创建 / 恢复 / 拆卸 ----

    async def create(self, options: Optional[Dict[str, Any]] = None,
                     meta: Optional[Dict[str, Any]] = None,
                     session_id: Optional[str] = None,
                     scope_parent: Any = None) -> Agent:
        """
        创建并发布一个新 agent（session + 驱动 task 一并启动）。

        :param scope_parent: 父 agent 的作用域 ctx（子代理继承父作用域注册，
            composeFrom/join 语义）。
        """
        store = self.ctx.sessions
        session = store.prepare(session_id, meta)
        store.enter(session)
        agent = await self._spawn(session, options or {}, meta or {},
                                  scope_parent=scope_parent)
        try:
            store.announce(session)
            self.ctx.agents.register(agent)
        except Exception:
            store.remove(session)
            raise
        self.emit_agent_event("agent/session-start",
                              {"agent": agent, "source": "startup"})
        self._start_driver(agent)
        return agent

    async def resume(self, session_id: str,
                     options: Optional[Dict[str, Any]] = None) -> Agent:
        """从持久化恢复会话并启动 agent。"""
        if not self.ctx.has("sessionPersistence"):
            raise LlmFailure("session persistence is not configured",
                             code="NO_PERSISTENCE")
        persistence = self.ctx.sessionPersistence
        header, events = await persistence.load(session_id)
        store = self.ctx.sessions
        session = Session.from_seed(session_id, header, events,
                                    publish=store._publish)
        store.enter(session)
        agent = await self._spawn(session, options or {}, {})
        try:
            store.announce(session)
            self.ctx.agents.register(agent)
        except Exception:
            store.remove(session)
            raise
        self.emit_agent_event("agent/session-start",
                              {"agent": agent, "source": "resume"})
        self._start_driver(agent)
        return agent

    async def _spawn(self, session: Session, options: Dict[str, Any],
                     meta: Dict[str, Any],
                     scope_parent: Any = None) -> Agent:
        """
        构造 Agent（作用域 ctx + 收件箱 + 驱动状态）。

        - ``scope_parent`` 存在时，作用域以其为父层（继承父作用域的服务注册）；
        - ``meta.agent_preset`` 存在时，把该 preset 的行挂到作用域
          （发布前挂载——对应 TS 版 setup 回调的时序保证）。
        """
        scope_ctx = self.ctx.scoped(f"agent:{session.id}", parent=scope_parent)
        agent = Agent(session, options, scope_ctx, self)
        preset_id = (meta or {}).get("agent_preset")
        if preset_id:
            if not self.ctx.has("agentPresets"):
                raise LlmFailure("agentPresets service not mounted",
                                 code="NO_PRESETS")
            await self.ctx.agentPresets.mount(scope_ctx, preset_id)
        return agent

    def _start_driver(self, agent: Agent) -> None:
        task = asyncio.get_running_loop().create_task(self._drive(agent))
        agent._driver_task = task
        self._drivers[agent.id] = task

    async def teardown(self, agent: Agent) -> None:
        """停止驱动、移除注册表与会话、展开作用域（AgentHandle.dispose 调用）。"""
        agent.dispose()
        task = self._drivers.pop(agent.id, None)
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self.ctx.agents.remove(agent)
        self.ctx.sessions.remove(agent.session)
        await agent.ctx.dispose()

    # ---- 驱动 ----

    async def _drive(self, agent: Agent) -> None:
        """每 agent 一个驱动 task：idle 等待唤醒 → drain 全部 turn。"""
        try:
            while not agent._disposed.is_set():
                await agent._wakeup.wait()
                agent._wakeup.clear()
                if agent._disposed.is_set():
                    break
                agent._set_status("running")
                agent._idle.clear()
                try:
                    while not agent._disposed.is_set():
                        batch = agent.inbox.claim_turn_batch()
                        if batch is None:
                            break
                        await self._run_turn(agent, batch)
                finally:
                    if not agent._disposed.is_set():
                        agent._set_status("idle")
                    agent._idle.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("driver for agent %s crashed", agent.id)

    # ---- turn ----

    async def _run_turn(self, agent: Agent,
                        batch: List[Dict[str, Any]]) -> None:
        """运行一个完整 turn（零或多个 step）。"""
        session = agent.session
        agent._turn_number += 1
        turn = agent._turn_number
        # 每个 turn 重新武装取消语义：上个 turn 的 cause 不得泄漏到本 turn
        agent._cancel_cause = None
        agent._turn_signal = AbortSignal()
        session.append("turn/start", {"turn": turn})
        reason: Dict[str, Any] = {"kind": "completed"}
        step = 1
        retries = 0
        try:
            while True:
                if agent._turn_signal.aborted:
                    reason = self._abort_reason(agent)
                    break
                # first=True 仅指本 turn 的第一次尝试（重试不递增 step，故须排除 retries）
                outcome = await self._run_step(agent, turn, step, batch,
                                               first=(step == 1 and retries == 0))
                if outcome == "retry":
                    retries += 1
                    if retries >= 3:
                        reason = {"kind": "error", "error": agent._last_failure
                                  or {"message": "retry limit reached",
                                      "code": "RETRY_LIMIT"}}
                        self.emit_agent_event("agent/error",
                                              {"agent": agent, "turn": turn,
                                               "step": step,
                                               "error": reason["error"]})
                        break
                    batch = []  # 同一步重试（不递增 step）
                    continue
                if outcome == "abort":
                    reason = self._abort_reason(agent)
                    break
                step += 1
                batch = agent.inbox.claim_next_step()
                if outcome == "stop":
                    # 自然停止 → 终态检查点（监听者可 steer 出下一步）
                    payload = {"agent": agent, "turn": turn}
                    await self.ctx.events.serial("agent/turn-stopping", payload)
                    if agent.inbox.has_next_step() and not agent._turn_signal.aborted:
                        batch = agent.inbox.claim_next_step()
                        continue
                    break
                # outcome == 'continue'：模型欠一个对工具结果的回复
        except _TurnFailed as failed:
            reason = {"kind": "error", "error": failed.failure}
            self.emit_agent_event("agent/error",
                                  {"agent": agent, "turn": turn, "step": step,
                                   "error": failed.failure})
        except Exception as exc:
            log.exception("turn %s crashed", turn)
            reason = {"kind": "error",
                      "error": {"message": f"{type(exc).__name__}: {exc}",
                                "code": "UNKNOWN"}}
            self.emit_agent_event("agent/error",
                                  {"agent": agent, "turn": turn, "step": step,
                                   "error": reason["error"]})
        if agent._turn_signal is not None and agent._turn_signal.aborted:
            reason = self._abort_reason(agent)
        # turn 收尾即清除取消语义：cause 不得跨 turn 泄漏到后续轮次
        agent._cancel_cause = None
        session.append("turn/end", {"turn": turn, "reason": reason})

    def _abort_reason(self, agent: Agent) -> Dict[str, Any]:
        cause = agent._cancel_cause or {"kind": "user"}
        return {"kind": "aborted", "reason": dict(cause)}

    # ---- step ----

    async def _run_step(self, agent: Agent, turn: int, step: int,
                        batch: List[Dict[str, Any]], first: bool) -> str:
        """
        运行一步。返回值:

        - 'continue'：工具欠回复（继续下一步）；
        - 'stop'：自然停止 / 首步被拒；
        - 'retry'：request-error 监听者请求重试；
        - 'abort'：concludes_turn 或取消。

        :param first: 是否本 turn 第一步（首步被拒/空进入 = 无 step 关闭 turn；
            工具续步允许空批次进入）。
        """
        session = agent.session
        for message in batch:
            self.emit_agent_event("agent/inbox/claimed",
                                  {"agent": agent, "message": message,
                                   "turn": turn})

        # 1) pre-step waterfall（首步拒绝 = 关闭无 step 的 turn）
        payload = {"agent": agent, "messages": batch, "turn": turn,
                   "step": step, "signal": agent._turn_signal}
        decision = await self.ctx.events.waterfall(
            "agent/pre-step", payload,
            default=lambda: {"kind": "enter", "messages": batch})
        if (not isinstance(decision, dict)
                or decision.get("kind") != "enter"
                or (first and not decision.get("messages"))):
            return "stop"

        entered = decision["messages"]
        session.append("step/start", {"turn": turn, "step": step})
        for message in entered:
            session.append(
                "user/message",
                {"content": message.get("content", ""),
                 "source": message.get("source", {"kind": "user"})},
                surface_op="append")
        if agent._turn_signal.aborted:
            session.append("step/end", {"turn": turn, "step": step})
            return "abort"

        # 2) prompt 组装 + 请求配置
        assembly = await self.ctx.events.waterfall(
            "system-prompt/assemble",
            {"scope": agent.ctx_name, "signal": agent._turn_signal},
            default=lambda: self._default_assemble(agent))
        system_text = assembly.get("text")
        tool_schemas = assembly.get("tools") or []
        req_payload = {"agent": agent, "turn": turn, "step": step,
                       "signal": agent._turn_signal}
        config = await self.ctx.events.waterfall(
            "agent/request", req_payload,
            default=lambda: self._default_config(agent))
        session.append("request/header",
                       {"header": config.to_json(), "reason": "initial"})

        # 路由容量元数据：仅当路由/容量变化时记录（request/context）
        adapter = self.ctx.llm.get_adapter(config.provider)
        context_window = getattr(adapter, "context_window", None)
        new_context: Dict[str, Any] = {"provider": config.provider,
                                       "model": config.model}
        if context_window is not None:
            new_context["context_window"] = context_window
        current_context = session.request_context()
        if current_context is None or \
                {k: current_context.get(k) for k in new_context} != new_context:
            session.append("request/context", new_context)

        # 3) 模型请求（流式）
        request = LlmRequest(config=config,
                             messages=session.derive_messages(),
                             tools=tool_schemas, system=system_text,
                             signal=agent._turn_signal)
        assembler = AssistantAssembler()
        chunk_seqs: List[int] = []
        started_ms = int(_time.perf_counter() * 1000)
        try:
            async for chunk in self.ctx.llm.stream(request):
                chunk_seqs.append(session.append(
                    "assistant/chunk",
                    {"turn": turn, "step": step,
                     "chunk": self._chunk_json(chunk)}).seq)
                assembler.feed(chunk)
        except LlmFailure as failure:
            session.append("step/end", {"turn": turn, "step": step})
            agent._last_failure = {"message": failure.message,
                                   "code": failure.code,
                                   "provider": failure.provider}
            action = await self.ctx.events.waterfall(
                "agent/request-error",
                dict(req_payload,
                     failure={"message": failure.message,
                              "code": failure.code,
                              "provider": failure.provider}),
                default=None)
            if isinstance(action, dict) and action.get("kind") == "retry":
                return "retry"
            raise _TurnFailed({"message": failure.message,
                               "code": failure.code})

        finished = assembler.finish()
        blocks = finished["blocks"]
        session.append(
            "assistant/message",
            {"blocks": [block.to_json() for block in blocks],
             "provider": config.provider, "model": config.model,
             "usage": finished["usage"]},
            surface_op="append", source_event_seqs=chunk_seqs)
        # 请求观测：每次成功的模型请求广播一次（latency + usage）
        self.emit_agent_event(
            "agent/request-done",
            {"agent": agent, "turn": turn, "step": step,
             "provider": config.provider, "model": config.model,
             "usage": finished["usage"],
             "latency_ms": int(_time.perf_counter() * 1000) - started_ms})

        tool_calls = [block for block in blocks if block.kind == "tool-call"]
        if not tool_calls:
            session.append("step/end", {"turn": turn, "step": step})
            return "stop"

        # 4) 工具执行（顺序；concludes_turn 后停止）
        stop_after = False
        for block in tool_calls:
            if agent._turn_signal.aborted:
                stop_after = True
                break
            arguments = self._parse_args(block.arguments)
            session.append("tool/call",
                           {"turn": turn, "step": step,
                            "call_id": block.call_id, "name": block.name,
                            "arguments": block.arguments or "{}"})
            result = await self.ctx.tools.execute(
                block.call_id, block.name, arguments, agent=agent,
                signal=agent._turn_signal, scope=agent.ctx_name)
            data: Dict[str, Any] = {
                "turn": turn, "step": step, "call_id": block.call_id,
                "name": block.name, "content": result.content,
                "is_error": result.is_error}
            if result.is_error:
                data["error"] = {"code": result.error.code,
                                 "message": result.error.message}
            session.append("tool/result", data, surface_op="append")
            if not result.is_error:
                for extra in result.additional_contexts:
                    session.append(
                        "user/message",
                        {"content": extra.get("content", ""),
                         "source": extra.get("source", {"kind": "plugin"})},
                        surface_op="append")
                if result.concludes_turn:
                    stop_after = True
        session.append("step/end", {"turn": turn, "step": step})
        if stop_after or agent._turn_signal.aborted:
            return "abort"
        return "continue"

    # ---- 默认实现（waterfall 末端） ----

    def _default_assemble(self, agent: Agent) -> Dict[str, Any]:
        """默认 prompt 组装：走 systemPrompt 的普通组装。"""
        return self.ctx.systemPrompt._build(
            agent.ctx_name, {"scope": agent.ctx_name,
                             "signal": agent._turn_signal})

    def _default_config(self, agent: Agent) -> LlmCallConfig:
        """默认调用配置：agent options → ctx.agentDefaultModel → 首个注册 provider。"""
        defaults: Dict[str, Any] = {}
        if self.ctx.has("agentDefaultModel"):
            defaults = self.ctx.agentDefaultModel.current_selection()
        provider = agent.options.get("provider") or defaults.get("provider")
        model = agent.options.get("model") or defaults.get("model")
        if not provider:
            import os
            providers = self.ctx.llm.providers()
            if not providers:
                raise LlmFailure("no LLM provider registered",
                                 code="NO_PROVIDER")
            # 无密钥时自动回退 mock（PyCharm 开箱即用）
            provider = ("deepseek" if os.environ.get("DEEPSEEK_API_KEY")
                        and "deepseek" in providers
                        else ("mock" if "mock" in providers else providers[0]))
        if not model:
            model = "mock" if provider == "mock" else "deepseek-chat"
        # `is not None` 判断（`or` 会把合法的 0 值如 temperature=0 当未设置）
        return LlmCallConfig(
            provider=provider, model=model,
            max_tokens=(agent.options.get("max_tokens")
                        if agent.options.get("max_tokens") is not None
                        else defaults.get("max_tokens")),
            temperature=(agent.options.get("temperature")
                         if agent.options.get("temperature") is not None
                         else defaults.get("temperature")),
            reasoning_effort=(agent.options.get("reasoning_effort")
                              if agent.options.get("reasoning_effort")
                              is not None
                              else defaults.get("reasoning_effort")))

    @staticmethod
    def _chunk_json(chunk: StreamChunk) -> Dict[str, Any]:
        """StreamChunk → 日志 JSON。"""
        out: Dict[str, Any] = {"type": chunk.type}
        if chunk.text is not None:
            out["text"] = chunk.text
        if chunk.tool_call is not None:
            out["tool_call"] = chunk.tool_call
        if chunk.usage is not None:
            out["usage"] = chunk.usage
        if chunk.finish_reason is not None:
            out["finish_reason"] = chunk.finish_reason
        return out

    @staticmethod
    def _parse_args(arguments: Optional[str]) -> Any:
        if not arguments:
            return {}
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {}

    def close(self) -> None:
        """取消全部驱动任务（幂等：重复调用无副作用）。"""
        tasks = list(self._drivers.values())
        self._drivers.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
