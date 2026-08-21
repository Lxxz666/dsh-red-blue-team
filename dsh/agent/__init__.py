"""
dsh.agent —— Agent 域：句柄、注册表、收件箱、审批、驱动循环。

导出: Agent/AgentHandle/AgentRegistry（ctx.agents）、Inbox、
ApprovalService（ctx.approval）、AgentLoopService（ctx.agentLoop）。
"""
from .agent import Agent, AgentHandle, AgentRegistry, AgentStatus
from .approval import ApprovalService
from .inbox import Inbox, make_message
from .loop import AgentLoopService

__all__ = [
    "Agent", "AgentHandle", "AgentRegistry", "AgentStatus",
    "Inbox", "make_message", "ApprovalService", "AgentLoopService",
]
