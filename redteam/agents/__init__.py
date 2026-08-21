"""redteam.agents —— 主 Agent + 子 Agent 并行攻击编排。

架构（对齐 dsh 的 per-agent scope 模型）：
- AttackOrchestrator（主 Agent）：侦察 → 场景识别 → 攻击计划 → 派发子 Agent
  → 汇总 WorkerReport → 生成攻击报告；
- ReconAgent（侦察子 Agent）：探测目标能力/端点/业务场景指纹；
- StaticAgent（静态子 Agent）：本地文件夹代码级审计；
- AttackWorkerAgent（攻击子 Agent）：每个子 Agent 在自己的 dsh scoped Context
  中执行一组攻击样本（按角色分组），并行运行，返回结构化报告给主 Agent。

并发与安全：全局 semaphore 限制并发、副作用样本经共享串行通道 + 状态重置，
子 Agent 之间互不污染（dsh scoped ctx 隔离 + 靶场 reset 隔离）。
"""
from .orchestrator import AttackOrchestrator
from .worker import (AttackWorkerAgent, ReconAgent, StaticAgent, WorkerReport)

__all__ = ["AttackOrchestrator", "AttackWorkerAgent", "ReconAgent",
           "StaticAgent", "WorkerReport"]
