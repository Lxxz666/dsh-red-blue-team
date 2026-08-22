"""redteam.agents.worker —— 子代理实现（侦察/静态/攻击）。

每个子代理在自己的 dsh scoped Context（per-agent scope）中工作，
任务结束后把结构化 WorkerReport 返回给主 Agent（AttackOrchestrator）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import ScanConfig
from ..engine.scan import execute_sample
from ..errors import UnsupportedSurface
from ..models import ConcreteSample, TargetResponse, Verdict, VerdictResult
from ..runtime import RedTeamRuntime

log = logging.getLogger("redteam.agents")


@dataclass
class WorkerReport:
    """子代理 → 主 Agent 的结构化报告。"""
    agent_id: str
    label: str
    task_type: str                    # recon / static / attack
    verdicts: List[VerdictResult] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for v in self.verdicts if v.success)

    def to_json(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "label": self.label,
                "task_type": self.task_type,
                "verdicts": [v.to_json() for v in self.verdicts],
                "extra": self.extra, "error": self.error}


class AttackWorkerAgent:
    """攻击子代理：在自己的 dsh scoped ctx 中执行一组攻击样本。

    :param semaphore: 全局并发上限（跨子代理共享）。
    :param side_lock: 副作用样本串行通道（跨子代理共享，防状态污染）。
    """

    def __init__(self, agent_id: str, label: str, runtime: RedTeamRuntime,
                 cfg: ScanConfig, adapter: Any, semaphore: asyncio.Semaphore,
                 side_lock: asyncio.Lock, scan_id: str = "") -> None:
        self.agent_id = agent_id
        self.label = label
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter
        self.semaphore = semaphore
        self.side_lock = side_lock
        self.scan_id = scan_id
        # dsh per-agent scope：子代理自己的作用域（继承根 ctx 服务）
        self.ctx = runtime.ctx.scoped(f"attacker/{agent_id}")

    async def run(self, samples: List[ConcreteSample]) -> WorkerReport:
        report = WorkerReport(agent_id=self.agent_id, label=self.label,
                              task_type="attack")
        runtime = self.runtime
        try:
            for sample in samples:
                verdict = await self._attack_one(sample)
                report.verdicts.append(verdict)
                runtime.ctx.emit("attack/verdict", verdict)
                runtime.storage.record_attack(
                    self.scan_id, verdict,
                    verdict.chain[-1]["resp"] if verdict.chain else "")
                if runtime.terrain is not None:
                    runtime.terrain.record(sample, verdict.verdict)
        except Exception as exc:  # 子代理失败不影响其他子代理（隔离）
            log.exception("攻击子代理 %s 失败", self.agent_id)
            report.error = str(exc)
        runtime.ctx.emit("agent/report", report)
        return report

    async def _attack_one(self, sample: ConcreteSample) -> VerdictResult:
        # 状态型样本：串行通道 + 状态重置（防污染），副作用期望样本另做前后快照
        try:
            if sample.sample.stateful or "side_effect" in sample.sample.expected_signals:
                async with self.side_lock:
                    verdict, response = await execute_sample(
                        self.runtime, self.cfg, self.adapter, sample, reset=True)
            else:
                async with self.semaphore:                      # 并发上限
                    verdict, response = await execute_sample(
                        self.runtime, self.cfg, self.adapter, sample)
        except UnsupportedSurface as exc:
            # 样本不适配目标（如对话样本 → MCP 目标）：跳过而非报错
            from ..models import now_iso
            verdict = VerdictResult(
                sample_uid=sample.uid, category=sample.category,
                role=sample.role, verdict=Verdict.SKIPPED.value,
                confidence=1.0, evidence=f"样本不适配目标: {exc}",
                created_at=now_iso())
            response = TargetResponse(status=0, text="(skipped)")
        self.runtime.ctx.emit("attack/executed", {
            "agent": self.agent_id, "sample": sample.describe(),
            "status": response.status,
            "response": response.snippet(300)})
        return verdict


#: 攻击执行所在扫描编号（worker 与主 Agent 共享同一扫描会话）


class ReconAgent:
    """侦察子代理：目标可达性/对话能力/安全头/业务场景指纹。"""

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig,
                 adapter: Optional[Any]) -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter

    async def run(self) -> WorkerReport:
        report = WorkerReport(agent_id="recon", label="侦察子代理",
                              task_type="recon")
        if self.adapter is None:
            return report
        probe = await self.adapter.probe()
        report.extra["probe"] = probe.to_json()
        # 端点发现（业务场景指纹，场景 auto 且无业务元信息时）
        if self.cfg.target.scenario == "auto" and not probe.scenarios:
            endpoints = await _discover_endpoints(self.adapter)
            report.extra["endpoints"] = sorted(endpoints)
        self.runtime.ctx.emit("agent/report", report)
        return report


class StaticAgent:
    """静态子代理：本地项目文件夹代码级审计。"""

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig) -> None:
        self.runtime = runtime
        self.cfg = cfg

    async def run(self, folder: str) -> WorkerReport:
        report = WorkerReport(agent_id="static", label="静态扫描子代理",
                              task_type="static")
        try:
            from ..static.scanner import StaticScanner
            scanner = StaticScanner()
            findings = await asyncio.to_thread(scanner.scan, folder)
            report.extra["static_findings"] = [f.to_json() for f in findings]
        except Exception as exc:
            log.exception("静态扫描失败")
            report.error = str(exc)
        self.runtime.ctx.emit("agent/report", report)
        return report


#: 端点发现列表（仅 GET，有界、低频；未命中即 404，安全）
_DISCOVERY_PATHS = [
    "/api/meta/business", "/api/orders", "/api/cart", "/api/checkout",
    "/api/pay", "/api/coupons", "/api/wallet", "/api/transfer",
    "/api/withdraw", "/api/exams", "/api/scores", "/api/answers",
    "/api/tenants", "/api/billing", "/api/posts", "/api/live",
    "/api/resumes", "/api/interviews", "/api/patients", "/api/records",
    "/api/appointments", "/api/coins", "/api/rewards", "/api/delivery",
    "/api/subscription", "/api/points", "/api/citizens", "/api/cases",
]


async def _discover_endpoints(adapter: Any) -> set:
    """探测常见业务端点（返回存在的端点路径集合）。"""
    from ..adapters.http_adapter import HttpAdapter
    if not isinstance(adapter, HttpAdapter):
        return set()
    found: set = set()
    client = await adapter._ensure_client()
    import httpx
    for path in _DISCOVERY_PATHS:
        try:
            response = await client.get(path)
            if response.status_code < 500 and response.status_code != 404:
                found.add(path)
        except httpx.HTTPError:
            continue
    return found
