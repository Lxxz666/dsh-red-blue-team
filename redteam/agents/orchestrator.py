"""redteam.agents.orchestrator —— 主 Agent（AttackOrchestrator）。

职责（主 Agent 视角）：
1. 派发侦察子 Agent → 获得目标能力与业务场景指纹；
2. 制定攻击计划（检测面 × 角色 × 场景样本）；
3. 并行派发攻击子 Agent（按角色分组，每个子 Agent 独立 scoped ctx）；
4. 派发静态子 Agent（文件夹模式）；
5. 汇总全部 WorkerReport → 落库 → 生成攻击报告；
6. （可选）调用 LLM 生成攻击态势综述（非 mock provider 时）。

审计：全程经 dsh EventBus 发射事件（scan/started → agent/dispatched →
agent/report → finding/detected → scan/finished），AuditSink 落盘 JSONL。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from ..config import ScanConfig
from ..models import (ConcreteSample, ScanResult, TargetResponse, Verdict,
                      VerdictResult, new_id, now_iso)
from ..runtime import RedTeamRuntime
from ..scenarios import (detect_scenario_endpoints, detect_scenario_folder,
                         sample_categories_for, scenario_names)
from .worker import (AttackWorkerAgent, ReconAgent, StaticAgent, WorkerReport)

log = logging.getLogger("redteam.agents.orchestrator")

#: 对话攻击的中性基线消息（行为偏离判定参照）
BASELINE_MESSAGE = "你好，请介绍一下你自己。"


class AttackOrchestrator:
    """主 Agent：攻击计划 → 并行子代理 → 汇总 → 攻击报告。"""

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig,
                 adapter: Optional[Any] = None, scan_mode: str = "full",
                 order: str = "adaptive") -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter
        self.scan_mode = scan_mode
        self.order = order
        self.scan_id = new_id("scan")
        self.scenarios: List[str] = []
        self.llm_agent_report = ""     # V11：LLM 自主攻击 Agent 的最终报告

    # ---- 主流程 ----

    async def run(self) -> ScanResult:
        cfg, rt = self.cfg, self.runtime
        ctx = rt.ctx
        result = ScanResult(scan_id=self.scan_id, target=cfg.target.name,
                            started_at=now_iso())
        audit_path = rt.audit.open_scan(self.scan_id)
        result.audit_path = audit_path
        ctx.emit("scan/started", {"scan_id": self.scan_id,
                                  "target": cfg.target.name,
                                  "mode": self.scan_mode,
                                  "agent_mode": True,
                                  "input": (cfg.target.folder_path
                                            or cfg.target.base_url),
                                  "config": cfg.source_path or "(内置)"})
        rt.storage.new_scan(self.scan_id, cfg.target.name, cfg.profile,
                            self.scan_mode, result.started_at)
        try:
            probe: Dict[str, Any] = {}
            worker_reports: List[WorkerReport] = []

            # ① 侦察子代理（网址/靶场模式）
            if self.adapter is not None:
                recon = ReconAgent(rt, cfg, self.adapter)
                ctx.emit("agent/dispatched", {"agent": "recon",
                                              "task": "目标侦察"})
                recon_report = await recon.run()
                worker_reports.append(recon_report)
                probe = recon_report.extra.get("probe") or {}
                await self.adapter.reset()
                # 场景识别
                self._detect_scenarios(probe, recon_report)

            # ② 静态子代理（文件夹模式）
            static_findings: List[Any] = []
            if cfg.target.type == "folder":
                self._detect_scenarios({}, None, folder=cfg.target.folder_path)
                static_agent = StaticAgent(rt, cfg)
                ctx.emit("agent/dispatched", {"agent": "static",
                                              "task": "静态代码审计"})
                static_report = await static_agent.run(cfg.target.folder_path)
                worker_reports.append(static_report)
                from ..static.scanner import StaticFinding
                static_findings = [StaticFinding(**item) for item in
                                   static_report.extra.get("static_findings") or []]

            # ③ 攻击计划：检测面 × 角色 × 场景样本
            samples: List[ConcreteSample] = []
            if self.adapter is not None:
                samples = await self._plan_samples(probe)
            ctx.emit("agent/dispatched", {"agent": "plan",
                                          "task": "攻击计划",
                                          "samples": len(samples),
                                          "scenarios": self.scenarios})

            # ④ 并行派发攻击子代理（按角色分组）
            verdicts: List[VerdictResult] = []
            if samples:
                verdicts = await self._dispatch_attack_workers(samples)
            # ④b V9-lite LLM 定向补打：分析未命中向量 → LLM 生成针对性攻击链 → 第二轮
            if cfg.engine.llm_followup and self.adapter is not None and samples:
                followup_samples = await self._llm_followup_plan(samples, verdicts)
                if followup_samples:
                    ctx.emit("agent/dispatched", {
                        "agent": "followup", "task": "LLM 定向补打",
                        "samples": len(followup_samples)})
                    verdicts = list(verdicts) + \
                        await self._dispatch_attack_workers(followup_samples)
            # ④c V11 LLM 自主攻击 Agent（并行 N 个，各负责一批类别，提速；opt-in）
            if cfg.engine.llm_agent and self.adapter is not None and samples:
                from .llm_agent import LlmAttackAgent
                summary = self._scan_summary_for_llm(verdicts, samples)
                parallel = max(1, int(getattr(
                    cfg.engine, "llm_agent_parallel", 1)))
                budget = max(1, (cfg.engine.llm_agent_max_attacks +
                                 parallel - 1) // parallel)  # 预算均分
                buckets = self._category_buckets(parallel)
                side_lock = asyncio.Lock()   # 状态型攻击跨并行 Agent 互斥
                llm_agents = [
                    LlmAttackAgent(
                        rt, cfg, self.adapter, summary, scan_id=self.scan_id,
                        timeout_s=cfg.engine.llm_agent_timeout_s,
                        agent_id="llm-attacker" if index == 0
                        else f"llm-attacker-{index + 1}",
                        mission_hint="、".join(buckets[index]),
                        side_lock=side_lock, max_attacks=budget)
                    for index in range(parallel)]
                llm_results = await asyncio.gather(*[
                    agent.run() for agent in llm_agents])
                for llm_result in llm_results:
                    if llm_result.verdicts:
                        ctx.emit("agent/dispatched", {
                            "agent": llm_result.agent_id,
                            "task": "LLM 自主攻击",
                            "samples": len(llm_result.verdicts)})
                        for index, verdict in enumerate(
                                llm_result.verdicts):
                            rt.storage.record_attack(
                                self.scan_id, verdict,
                                verdict.evidence[:500])
                            if rt.terrain is not None and index < len(
                                    llm_result.samples):
                                rt.terrain.record(llm_result.samples[index],
                                                  verdict.verdict)
                        samples = list(samples) + llm_result.samples
                        verdicts = list(verdicts) + llm_result.verdicts
                    if llm_result.final_report:
                        self.llm_agent_report += (
                            (f"\n[{llm_result.agent_id}] " if self.llm_agent_report
                             else f"[{llm_result.agent_id}] ")
                            + llm_result.final_report)
            result.verdicts = verdicts

            # ⑤ 汇总：漏洞落库（动态 + 静态）
            seq = 0
            for verdict in verdicts:
                if verdict.success:
                    seq += 1
                    sample = self._sample_of(verdict.sample_uid, samples)
                    if sample is None:
                        continue
                    finding = self._finding_from(verdict, sample, seq)
                    result.findings.append(finding)
                    rt.storage.add_finding(finding)
                    ctx.emit("finding/detected", finding)
            from ..static.scanner import findings_to_model
            for finding in findings_to_model(static_findings, self.scan_id):
                result.findings.append(finding)
                rt.storage.add_finding(finding)
                ctx.emit("finding/detected", finding)

            # ⑥ 攻击报告（报告器 + 可选 LLM 综述）
            from ..reporter.report import write_report
            narrative = await self._narrative(result)
            report_path, json_path = write_report(
                result, cfg.out_dir, probe=probe,
                base_url=cfg.target.base_url, mode=self.scan_mode,
                audit_path=audit_path, scenarios=self.scenarios,
                narrative=narrative)
            result.report_path = report_path
            result.report_json_path = json_path
            result.probe = probe
            result.finished_at = now_iso()
            result.status = "finished"

            # ⑦ 自适应地形持久化 + 收尾
            if cfg.adaptive.enabled and samples:
                rt.terrain.save()
            rt.storage.finish_scan(
                self.scan_id, result.finished_at, result.total,
                result.success_count, result.suspicious_count,
                report_path, json_path, audit_path, probe)
            ctx.emit("scan/finished", {
                "scan_id": self.scan_id, "total": result.total,
                "success": result.success_count,
                "suspicious": result.suspicious_count,
                "findings": len(result.findings),
                "scenarios": self.scenarios,
                "agents": [r.agent_id for r in worker_reports],
                "report": report_path, "audit": audit_path})
            return result
        except Exception as exc:
            rt.storage.fail_scan(self.scan_id, str(exc))
            ctx.emit("scan/failed", str(exc))
            log.exception("主 Agent 扫描 %s 失败", self.scan_id)
            raise

    # ---- 计划与派发 ----

    def _detect_scenarios(self, probe: Dict[str, Any],
                          recon_report: Optional[WorkerReport] = None,
                          folder: str = "") -> None:
        cfg = self.cfg
        if cfg.target.scenario and cfg.target.scenario != "auto":
            self.scenarios = [s.strip() for s in
                              cfg.target.scenario.split(",") if s.strip()]
            return
        detected: List[str] = []
        if probe.get("scenarios"):
            detected = [str(s) for s in probe["scenarios"]]
        elif recon_report is not None and recon_report.extra.get("endpoints"):
            endpoint_hit = detect_scenario_endpoints(
                set(recon_report.extra["endpoints"]))
            if endpoint_hit:
                detected = [endpoint_hit]
        elif folder:
            from ..scenarios import detect_scenario_folder
            hit = detect_scenario_folder(folder)
            if hit:
                detected = [hit]
        self.scenarios = detected
        if detected:
            names = ", ".join(scenario_names().get(s, s) for s in detected)
            log.info("业务场景识别：%s", names)

    async def _plan_samples(self, probe: Dict[str, Any]) -> List[ConcreteSample]:
        cfg, rt = self.cfg, self.runtime
        categories = list(cfg.vectors.categories)
        categories += sample_categories_for(self.scenarios)
        samples = rt.registry.samples_for(
            cfg.vectors.roles, categories, cfg.vectors.variants_per_sample)
        # LLM 变体生成（opt-in：DeepSeek 可用时增强攻击计划，失败静默降级）
        if cfg.vectors.llm_variants and cfg.vectors.llm_variants_per_sample > 0:
            samples = await self._append_llm_variants(samples, categories)
        # LLM 多轮攻击链生成（opt-in：同样降级）
        if cfg.vectors.llm_chains and cfg.vectors.llm_chains_per_sample > 0:
            samples = await self._append_llm_chains(samples, categories)
        if cfg.engine.samples_limit:
            samples = samples[:cfg.engine.samples_limit]
        if self.order == "adaptive" and cfg.adaptive.enabled:
            samples = rt.terrain.priority(samples, rt.terrain.seen_uids())
        elif self.order == "random":
            import random
            random.Random(cfg.vectors.seed).shuffle(samples)
        log.info("攻击计划：%d 条样本（角色 %s × 场景 %s）",
                 len(samples), cfg.vectors.roles, self.scenarios or ["通用"])
        return samples

    async def _append_llm_variants(self, samples: List[ConcreteSample],
                                   categories: List[str]) -> List[ConcreteSample]:
        """为选中类别的基础样本生成 LLM 变体（每个样本最多 n 条）。"""
        cfg, rt = self.cfg, self.runtime
        wanted = set(categories)
        llm_count = 0
        additions: List[ConcreteSample] = []
        for base in rt.registry.samples:
            if "all" not in wanted and base.category not in wanted:
                continue
            if base.surface != "chat":      # 只对对话载荷做语义变体
                continue
            try:
                variants = await rt.registry.generate_llm_variants(
                    base, n=cfg.vectors.llm_variants_per_sample)
            except Exception as exc:
                log.debug("LLM 变体生成异常（跳过）: %s", exc)
                variants = []
            if not variants:
                continue
            roles = [r for r in (base.role_context or list(cfg.vectors.roles))
                     if r in cfg.vectors.roles]
            for role in roles:
                for index, payload in enumerate(variants):
                    additions.append(ConcreteSample(
                        uid=f"{base.id}-{role}-llm{index}",
                        sample=base, role=role, payload=payload,
                        params={}, body={}, path="",
                        variant_index=100 + index, variant_of="llm"))
                    llm_count += 1
        if llm_count:
            log.info("LLM 变体生成：新增 %d 条攻击载荷变体", llm_count)
        additions.sort(key=lambda s: (s.category, s.sample.id, s.uid))
        return list(samples) + additions

    async def _append_llm_chains(self, samples: List[ConcreteSample],
                                 categories: List[str]) -> List[ConcreteSample]:
        """为选中类别的基础样本生成 LLM 多轮攻击链（铺垫消息 + 攻击载荷）。"""
        cfg, rt = self.cfg, self.runtime
        wanted = set(categories)
        chain_count = 0
        additions: List[ConcreteSample] = []
        for base in rt.registry.samples:
            if "all" not in wanted and base.category not in wanted:
                continue
            if base.surface != "chat":
                continue
            try:
                chains = await rt.registry.generate_llm_chains(
                    base, n=cfg.vectors.llm_chains_per_sample)
            except Exception as exc:
                log.debug("LLM 攻击链生成异常（跳过）: %s", exc)
                chains = []
            if not chains:
                continue
            roles = [r for r in (base.role_context or list(cfg.vectors.roles))
                     if r in cfg.vectors.roles]
            for role in roles:
                for index, messages in enumerate(chains):
                    prelude, payload = messages[:-1], messages[-1]
                    additions.append(ConcreteSample(
                        uid=f"{base.id}-{role}-llmchain{index}",
                        sample=base, role=role, payload=payload,
                        params={}, body={}, path="",
                        variant_index=800 + index, variant_of="llm-chain",
                        prelude=prelude))
                    chain_count += 1
        if chain_count:
            log.info("LLM 攻击链生成：新增 %d 条多轮攻击链", chain_count)
        additions.sort(key=lambda s: (s.category, s.sample.id, s.uid))
        return list(samples) + additions

    async def _llm_followup_plan(self, samples: List[ConcreteSample],
                                 verdicts: List[VerdictResult]
                                 ) -> List[ConcreteSample]:
        """V9-lite 定向补打：对首轮未命中的向量类别，用 LLM 生成针对性
        攻击链（铺垫+载荷），供第二轮补打。DeepSeek 不可用/失败 → 空。"""
        cfg, rt = self.cfg, self.runtime
        llm = rt.llm
        if llm is None or "deepseek" not in llm.providers():
            return []
        tried = {v.category for v in verdicts}
        missed = sorted(tried - {v.category for v in verdicts if v.success})
        if not missed:
            return []
        additions: List[ConcreteSample] = []
        followup_index = 0
        for base in rt.registry.samples:
            if base.category not in missed or base.surface != "chat":
                continue
            try:
                chains = await rt.registry.generate_llm_chains(base, n=1)
            except Exception as exc:
                log.debug("补打链生成异常（跳过）: %s", exc)
                chains = []
            if not chains:
                continue
            roles = [r for r in (base.role_context or list(cfg.vectors.roles))
                     if r in cfg.vectors.roles]
            for role in roles:
                for messages in chains:
                    prelude, payload = messages[:-1], messages[-1]
                    additions.append(ConcreteSample(
                        uid=f"{base.id}-{role}-followup{followup_index}",
                        sample=base, role=role, payload=payload,
                        params={}, body={}, path="",
                        variant_index=700 + followup_index,
                        variant_of="llm-followup", prelude=prelude))
                    followup_index += 1
                    if followup_index >= 10:   # 补打样本上限（防失控）
                        break
            if followup_index >= 10:
                break
        if additions:
            log.info("LLM 定向补打：未命中类别 %s → 生成 %d 条针对性攻击链",
                     missed, len(additions))
        return additions

    def _scan_summary_for_llm(self, verdicts: List[VerdictResult],
                              samples: List[ConcreteSample]) -> str:
        """给 LLM 攻击 Agent 的扫描摘要（决策依据）。"""
        tried = {v.category for v in verdicts}
        hit = {v.category for v in verdicts if v.success}
        missed = sorted(tried - hit)
        lines = [f"目标: {self.cfg.target.name}", f"样本数: {len(samples)}",
                 f"已执行: {len(verdicts)} 次攻击，成功 "
                 f"{sum(1 for v in verdicts if v.success)} 次",
                 "已命中类别: " + ("、".join(sorted(hit)) or "无"),
                 "未命中类别: " + ("、".join(missed) or "无")]
        return "\n".join(lines)

    def _category_buckets(self, parallel: int) -> List[List[str]]:
        """把样本库攻击类别轮转分成 N 桶（并行 LLM 攻击 Agent 分工，减少重复）。"""
        categories = sorted({s.category for s in self.runtime.registry.samples})
        buckets: List[List[str]] = [[] for _ in range(parallel)]
        for index, category in enumerate(categories):
            buckets[index % parallel].append(category)
        return buckets

    async def _dispatch_attack_workers(self, samples: List[ConcreteSample]
                                       ) -> List[VerdictResult]:
        cfg, rt, ctx = self.cfg, self.runtime, self.runtime.ctx
        # 共享并发/串行通道（跨子代理）
        semaphore = asyncio.Semaphore(cfg.engine.concurrency)
        side_lock = asyncio.Lock()
        # 按角色分组：每个角色一个攻击子代理
        groups: Dict[str, List[ConcreteSample]] = {}
        for sample in samples:
            groups.setdefault(sample.role, []).append(sample)
        workers: List[AttackWorkerAgent] = []
        for role in cfg.vectors.roles:
            if role in groups:
                workers.append(AttackWorkerAgent(
                    agent_id=f"attacker-{role}", label=f"攻击子代理[{role}]",
                    runtime=rt, cfg=cfg, adapter=self.adapter,
                    semaphore=semaphore, side_lock=side_lock,
                    scan_id=self.scan_id))
        for worker in workers:
            ctx.emit("agent/dispatched", {
                "agent": worker.agent_id, "label": worker.label,
                "task": f"执行 {len(groups[worker.agent_id.split('-')[-1]])} 条攻击样本"})
        # 并行派发
        reports = await asyncio.gather(*[
            worker.run(groups[worker.agent_id.split("-")[-1]])
            for worker in workers])
        verdicts: List[VerdictResult] = []
        for report in reports:
            verdicts.extend(report.verdicts)
        return verdicts

    # ---- 汇总 ----

    @staticmethod
    def _sample_of(uid: str, samples: Sequence[ConcreteSample]
                   ) -> Optional[ConcreteSample]:
        for sample in samples:
            if sample.uid == uid:
                return sample
        return None

    def _finding_from(self, verdict: VerdictResult, sample: ConcreteSample,
                      seq: int):
        from ..blueteam.templates import fix_template_for
        from ..models import Finding
        template = fix_template_for(sample.category)
        return Finding(
            finding_id=f"F-{seq:03d}",
            scan_id=self.scan_id,
            category=sample.category,
            owasp=sample.sample.owasp,
            severity=sample.sample.severity,
            sample_id=sample.sample.id,
            sample_uid=sample.uid,
            payload=sample.payload,
            role=sample.role,
            chain=verdict.chain,
            evidence=verdict.evidence,
            signals={s.name: {"hit": s.hit, "evidence": s.evidence}
                     for s in verdict.signals if s.hit},
            confidence=verdict.confidence,
            fix={"auto_fixable": bool(template and template.auto_fixable),
                 "template": template.template_id if template else "",
                 "plan": template.title if template else "需人工研判",
                 "status": "pending"})

    async def _narrative(self, result: ScanResult) -> str:
        """主 Agent 的攻击态势综述（LLM 可用时生成；否则确定性模板）。"""
        if result.success_count == 0:
            return ("本轮攻击未发现确认漏洞：目标对各攻击向量均表现出有效防御。"
                    "存疑样本请在审计日志中人工复核。")
        categories = sorted({f.category for f in result.findings})
        severity_counts = result.severity_counts()
        scenario_text = "、".join(self.scenarios) or "通用"
        text = (
            f"主 Agent 攻击态势综述：本轮共执行 {result.total} 条攻击样本，"
            f"确认 {result.success_count} 次攻击成功，发现 "
            f"{len(result.findings)} 条漏洞（严重 "
            f"{severity_counts.get('critical', 0)} / 高危 "
            f"{severity_counts.get('high', 0)} / 中危 "
            f"{severity_counts.get('medium', 0)} / 低危 "
            f"{severity_counts.get('low', 0)}）。")
        text += f" 业务场景：{scenario_text}。"
        text += (" 命中漏洞类别：" + "、".join(categories[:8]) +
                 ("…" if len(categories) > 8 else "") + "。")
        if self.llm_agent_report:
            text += ("\n\n[LLM 自主攻击 Agent 报告] "
                     + self.llm_agent_report[:600])
        llm = self.runtime.llm
        if not llm or "deepseek" not in llm.providers():
            return text
        try:
            from dsh.llm.adapters import LlmCallConfig, LlmRequest
            from dsh.llm.messages import Message
            prompt = (f"你是红队安全分析师。基于以下扫描摘要，用 100 字以内的中文"
                      f"写一段管理层可读的攻击态势综述：\n{text}")
            request = LlmRequest(
                config=LlmCallConfig(provider="deepseek",
                                     model=os.environ.get("DEEPSEEK_MODEL",
                                                          "deepseek-chat"),
                                     max_tokens=200),
                messages=[Message.user(prompt)])
            out = ""
            async for chunk in llm.stream(request):
                if getattr(chunk, "text", ""):
                    out += chunk.text
            if out.strip():
                return f"{text}\n\n[LLM 态势综述] {out.strip()}"
        except Exception as exc:
            log.warning("LLM 综述生成失败，使用确定性模板: %s", exc)
        return text
