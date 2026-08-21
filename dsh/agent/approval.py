"""
dsh.agent.approval —— ApprovalService（ctx.approval）：人机审批通道。

对应 TS 版 user-approval + ``approval/request`` 事件：``tools/pre-execute`` 的
ask 决策、ask_user 工具、权限系统都经此申请人工许可。

- ``set_channel(callback)``：注册应答通道（Web UI 弹出问题等待人工回答；
  headless 模式无通道 = 自动拒绝）；
- ``request(question, detail)``：先经 ``approval/request`` waterfall 事件派发
  （任何监听者可应答/短路；对应 TS 事件矩阵）；无监听者应答 → 通道 → 默认值。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..kernel import Service

log = logging.getLogger("dsh.agent")

AnswerCallback = Callable[[str, str], Any]  # (question, detail) -> bool | awaitable


class ApprovalService(Service):
    """审批服务（ctx.approval）。"""

    provides = "approval"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._channel: Optional[AnswerCallback] = None
        self._pending: List[Dict[str, Any]] = []
        self._default: bool = bool((config or {}).get("default_allow", False))

    def apply(self, ctx) -> None:
        ctx.set("approval", self)

    def set_channel(self, callback: AnswerCallback) -> None:
        """
        注册人工应答通道。

        :param callback: ``async def callback(question, detail) -> bool``。
        """
        self._channel = callback

    def clear_channel(self) -> None:
        self._channel = None

    async def request(self, question: str, detail: str = "") -> bool:
        """
        申请一次许可（allowed-once）。

        应答顺序（先到先得）：

        1. ``approval/request`` waterfall 事件——监听者收到
           ``(payload, next)``，payload = {question, detail}；返回 bool
           （True/False）即短路应答，``await next()`` 则委派；
        2. 人工通道 ``set_channel``；
        3. 默认值（config.default_allow，默认 False = 拒绝）。

        :return: True = 允许一次；False = 拒绝。
        """
        record = {"question": question, "detail": detail, "state": "pending"}
        self._pending.append(record)
        try:
            # 1) approval/request 事件（waterfall：监听者可短路应答）
            decision = await self.ctx.events.waterfall(
                "approval/request", {"question": question, "detail": detail},
                default=None)
            if decision is not None:
                allowed = bool(decision)
                record["state"] = "allowed" if allowed else "denied"
                return allowed
            # 2) 人工通道
            if self._channel is None:
                record["state"] = "denied"
                return self._default
            try:
                result = self._channel(question, detail)
                if asyncio.iscoroutine(result):
                    result = await result
                allowed = bool(result)
                record["state"] = "allowed" if allowed else "denied"
                return allowed
            except Exception:
                log.exception("approval channel failed")
                record["state"] = "denied"
                return False
        finally:
            try:
                self._pending.remove(record)
            except ValueError:
                pass

    def pending_questions(self) -> List[Dict[str, Any]]:
        """当前挂起的问题快照（调试/UI 用）。"""
        return list(self._pending)
