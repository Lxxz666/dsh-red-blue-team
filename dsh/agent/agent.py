"""
dsh.agent.agent —— Agent 公共句柄 + AgentRegistry（ctx.agents）。

对应 TS 版 Agent 接口:

- ``followup``（普通后续 turn，唤醒驱动）/ ``steer``（最近一步 steering）/
  ``inject``（面向模型上下文，不唤醒）；
- ``cancel(cause, keep_inbox)``：首个 cause 胜出，keep_inbox 保留待办；
- ``whenIdle``：整个 agent 静默后 resolve；
- 发起者作用域用 Python contextvars 实现（withInitiator/withoutInitiator）。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Callable, Dict, List, Optional

from ..errors import AgentError
from ..ids import new_session_id
from ..kernel import Service
from ..session import Session
from .inbox import Inbox, make_message

log = logging.getLogger("dsh.agent")

#: 发起者作用域（进程内因果归属，不是授权）
_initiator_var: contextvars.ContextVar[Optional["Agent"]] = \
    contextvars.ContextVar("dsh_agent_initiator", default=None)

AgentStatus = str  # 'idle' | 'running'


class Agent:
    """一个活跃 agent（UI/hook/编排器面对的公共句柄）。"""

    def __init__(self, session: Session, options: Dict[str, Any],
                 scope_ctx: Any, factory: "AgentLoopBase") -> None:
        self.id = session.id
        self.session = session
        self.options = dict(options)
        self.inbox = Inbox()
        self.ctx = scope_ctx
        self.ctx_name = scope_ctx.name if scope_ctx is not None else None
        self.status: AgentStatus = "idle"
        self._factory = factory

        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._disposed = asyncio.Event()
        self._turn_signal: Optional[Any] = None
        self._turn_number = 0
        self._step_number = 0
        self._cancel_cause: Optional[Dict[str, Any]] = None
        self._driver_task: Optional[asyncio.Task] = None
        self._last_failure: Optional[Dict[str, Any]] = None
        """最近一次模型失败（retry 上限触发时用作 turn/end error 载荷）。"""
        self._maintenance: Optional[asyncio.Task] = None
        """true idle 阶段的维护任务（runMaintenance 启动，cancel 中止）。"""

    # ---- 状态 ----

    @property
    def running(self) -> bool:
        return self.status == "running"

    def _set_status(self, status: AgentStatus) -> None:
        if self.status != status:
            self.status = status
            self._factory.emit_agent_event("agent/status",
                                           {"agent": self, "status": status})

    # ---- 投递 ----

    def send(self, message: Dict[str, Any], target: str, wakeup: bool) -> None:
        """路由到收件箱边界并可选唤醒驱动（followup/steer/inject 的底层）。"""
        if self._disposed.is_set():
            return
        self.inbox.append(target, message)
        self._factory.emit_agent_event("agent/inbox/inserted",
                                       {"agent": self, "message": message})
        if wakeup:
            self._wakeup.set()

    def followup(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
        """排队一个普通后续 turn 并唤醒驱动。"""
        self.send(make_message(text, source or {"kind": "user"}), "next-turn",
                  wakeup=True)

    def steer(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
        """为最近一步提交 steering。"""
        self.send(make_message(text, source or {"kind": "steer"}), "next-step",
                  wakeup=True)

    def inject(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
        """排队面向模型的上下文（不唤醒驱动）。"""
        self.send(make_message(text, source or {"kind": "plugin"}),
                  "next-step", wakeup=False)

    # ---- 取消 ----

    def cancel(self, cause: Optional[Dict[str, Any]] = None,
               keep_inbox: bool = False) -> None:
        """
        清除排队工作（除非 keep_inbox）并中止活跃 turn。

        :param cause: {'kind': 'user'|'parent'|'hook'|'disposed', ...}。
        """
        if self._disposed.is_set():
            return
        if self._cancel_cause is None:
            self._cancel_cause = cause or {"kind": "user"}
        if not keep_inbox:
            for message in self.inbox.next_turn + self.inbox.next_step:
                self._factory.emit_agent_event("agent/inbox/discarded",
                                               {"agent": self, "message": message})
            self.inbox.clear()
        if self._turn_signal is not None:
            self._turn_signal.abort(self._cancel_cause)
        else:
            # 无活跃 turn（取消发生在驱动认领之前）：cause 立即消费完毕，
            # 不得残留到下一个 turn
            self._cancel_cause = None
        if self._maintenance is not None:
            self._maintenance.cancel()
        self._wakeup.set()

    def run_maintenance(self, task) -> "asyncio.Task":
        """
        在 true idle 阶段运行一个非 turn 维护任务（对应 TS 版 runMaintenance）。

        - 任务同步启动（占用 idle 阶段，公共状态保持 idle）；
        - 后续唤醒输入留在收件箱直到任务结束；
        - ``when_idle`` 会跟随任务完成；``cancel`` 会中止它。

        :param task: ``async def task(signal: AbortSignal) -> T``。
        :return: 任务 asyncio.Task。
        :raises AgentError: turn 驱动中或已有维护任务时。
        """
        if self.status == "running" or self._maintenance is not None:
            raise __import__("dsh.errors", fromlist=["AgentError"]).AgentError(
                f"agent {self.id} busy")
        if self._disposed.is_set():
            raise __import__("dsh.errors", fromlist=["AgentError"]).AgentError(
                f"agent {self.id} disposed")
        from ..tools.pipeline import AbortSignal
        signal = AbortSignal()
        self._maintenance = asyncio.get_running_loop().create_task(
            self._run_maintenance(task, signal))
        return self._maintenance

    async def _run_maintenance(self, task, signal) -> Any:
        try:
            return await task(signal)
        finally:
            self._maintenance = None

    def dispose(self, cause: Optional[Dict[str, Any]] = None) -> None:
        """标记销毁（驱动 drain 后由 registry 移除）。"""
        self.cancel(cause or {"kind": "disposed"})
        self._disposed.set()
        self._wakeup.set()

    # ---- 等待 ----

    async def when_idle(self) -> None:
        """
        整个 agent 静默（无活跃 turn、无排队工作、维护任务完成）后 resolve。

        同时检查 idle 标志与收件箱/唤醒位，避免「驱动尚未开始处理刚排队的
        消息」时提前返回（子代理等待场景的关键保证）。
        """
        while True:
            await self._idle.wait()
            if (not self._wakeup.is_set()
                    and not self.inbox.has_next_step()
                    and not self.inbox.has_next_turn()):
                if self._maintenance is not None:
                    await asyncio.gather(self._maintenance,
                                         return_exceptions=True)
                    continue
                return
            await asyncio.sleep(0)

    async def _wait_disposed(self) -> None:
        await self._disposed.wait()

    def __repr__(self) -> str:
        return f"<Agent {self.id} ({self.status})>"


class AgentHandle:
    """创建者拥有的句柄：dispose 是能力（capability）。"""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def dispose(self) -> None:
        """停止循环、等待退出、注销 agent、移除会话。"""
        self.agent.dispose()
        await self.agent._factory.teardown(self.agent)


class AgentRegistry(Service):
    """活跃 agent 注册表（ctx.agents）。创建由 AgentFactory（agent-loop）提供。"""

    provides = "agents"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._agents: Dict[str, Agent] = {}
        self._factory: Optional[Any] = None

    def apply(self, ctx) -> None:
        ctx.set("agents", self)

    # ---- 工厂 ----

    def set_factory(self, factory: Any) -> None:
        """注册 agent 创建工厂（agent-loop 在构造时调用）。"""
        if self._factory is not None:
            raise AgentError("agent factory already registered")
        self._factory = factory

        def clear() -> None:
            self._factory = None
        return self.ctx.effect(clear)

    @property
    def factory(self) -> Any:
        if self._factory is None:
            raise AgentError("no agent factory registered")
        return self._factory

    # ---- 创建 ----

    async def create(self, options: Optional[Dict[str, Any]] = None,
                     meta: Optional[Dict[str, Any]] = None,
                     session_id: Optional[str] = None,
                     scope_parent: Any = None) -> Agent:
        """
        创建并发布一个新 agent（经注册工厂）。

        :param scope_parent: 父 agent 作用域（子代理继承注册）。
        :return: 已发布的 agent（拥有句柄者用 AgentHandle 管理生命周期）。
        """
        return await self.factory.create(options=options, meta=meta,
                                         session_id=session_id,
                                         scope_parent=scope_parent)

    async def resume(self, session_id: str,
                     options: Optional[Dict[str, Any]] = None) -> Agent:
        """从持久化恢复一个会话上的 agent。"""
        return await self.factory.resume(session_id=session_id,
                                         options=options)

    # ---- 查询 ----

    def get(self, agent_id: str) -> Optional[Agent]:
        return self._agents.get(agent_id)

    def list(self) -> List[Agent]:
        return list(self._agents.values())

    def register(self, agent: Agent) -> None:
        """登记一个已构造 agent 并公告 agent/created。"""
        if agent.id in self._agents:
            raise AgentError(f"agent {agent.id} already registered")
        self._agents[agent.id] = agent
        self.ctx.events.emit("agent/created", {"agent": agent})

    def remove(self, agent: Agent) -> None:
        """移除 agent 并公告 agent/disposed。"""
        if self._agents.pop(agent.id, None) is None:
            return
        self.ctx.events.emit("agent/disposed", {"agent": agent})

    # ---- 发起者作用域 ----

    def current_initiator(self) -> Optional[Agent]:
        """读取进程内发起者（可无）。"""
        return _initiator_var.get()

    def require_initiator(self) -> Agent:
        """读取发起者，无则抛错。"""
        initiator = _initiator_var.get()
        if initiator is None:
            raise AgentError("no initiator boundary active")
        return initiator

    def with_initiator(self, agent: Agent, operation: Callable[[], Any]) -> Any:
        """在指定 agent 作为进程内发起者的边界里运行 operation（返回值原样）。"""
        token = _initiator_var.set(agent)
        try:
            return operation()
        finally:
            _initiator_var.reset(token)

    def without_initiator(self, operation: Callable[[], Any]) -> Any:
        """在隐藏继承发起者的边界里运行 operation（惰性共享定时器/队列泵用）。"""
        token = _initiator_var.set(None)
        try:
            return operation()
        finally:
            _initiator_var.reset(token)

    def close(self) -> None:
        for agent in list(self._agents.values()):
            agent.dispose()
        self._agents.clear()
