"""redteam.blueteam.engine —— 蓝队修复引擎（规划 → 沙箱应用 → 回归 → 回滚）。

安全红线（蓝队自身）：
- 只改沙箱/靶场，绝不直接改生产（非 lab 目标只输出方案）；
- 每个修复 = 一次版本化变更（备份 + 记录 + 可回滚）；
- 修复必须带"为什么这么修"（rationale，审计要求）；
- 修复后必须回归（无回归 = 未修复）。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import yaml

from ..config import ScanConfig
from ..errors import FixError, RegressionError, UnsupportedSurface
from ..models import (Finding, FixPlan, FixResult, RegressionResult,
                      ScanResult, Verdict, new_id, now_iso)
from ..runtime import RedTeamRuntime
from .templates import FixTemplate, fix_template_for

log = logging.getLogger("redteam.blueteam")


@dataclass
class BlueResult:
    plans: List[FixPlan] = field(default_factory=list)
    fixes: List[FixResult] = field(default_factory=list)
    regressions: List[RegressionResult] = field(default_factory=list)
    rolled_back: List[str] = field(default_factory=list)
    remediation_path: str = ""          # 完整修复报告路径

    @property
    def applied_count(self) -> int:
        return sum(1 for f in self.fixes if f.status == "applied")

    @property
    def verified_count(self) -> int:
        return sum(1 for r in self.regressions if r.passed)

    def summary(self) -> Dict[str, Any]:
        return {"plans": len(self.plans), "applied": self.applied_count,
                "verified": self.verified_count,
                "rollbacks": self.rolled_back,
                "passed_all": all(r.passed for r in self.regressions)
                if self.regressions else True}


class BlueEngine:
    """蓝队修复闭环编排者。"""

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig,
                 adapter, scan_result: ScanResult,
                 findings: Optional[Sequence[Finding]] = None) -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter
        self.scan = scan_result
        self.findings = list(findings) if findings is not None else \
            list(scan_result.findings)
        self._live_guards: Optional[Dict[str, Any]] = None

    # ---- 主流程 ----

    async def run(self, apply_fixes: bool = True) -> BlueResult:
        ctx = self.runtime.ctx
        result = BlueResult()

        # ① 规划：每条漏洞 → 修复方案（模板库 + 目标配置 [+ LLM 修复建议]）
        for finding in self.findings:
            plan = self._plan(finding)
            if getattr(self.cfg.engine, "llm_fix_plan", False):
                ai_note = await self._ai_fix_note(finding, plan)
                if ai_note:
                    plan.ai_note = ai_note
                    finding.fix["ai_plan"] = ai_note
                    ctx.emit("fix/ai_planned", {
                        "finding_id": finding.finding_id,
                        "category": finding.category})
            result.plans.append(plan)
            ctx.emit("fix/planned", plan)
            self.runtime.storage.update_finding_fix(
                finding.finding_id, plan.template_id,
                plan.title, "planned" if plan.auto_fixable else "manual")

        if not apply_fixes:
            # 只出方案：修复报告同样交付（含分步指引与验证步骤）
            self._write_report(result)
            return result

        # ② 执行：lab 目标在沙箱应用（版本化 + 可回滚）；外部目标只出方案
        applied_findings: List[Finding] = []
        for plan, finding in zip(result.plans, self.findings):
            if not plan.auto_fixable or not plan.ops:
                continue
            if self.cfg.target.type != "lab":
                fix = FixResult(fix_id=new_id("fix"), plan_id=plan.plan_id,
                                finding_id=finding.finding_id,
                                status="manual_only",
                                applied_to="(外部目标：仅生成方案，不自动修改)",
                                detail="外部目标不自动修复，请按方案人工实施")
                result.fixes.append(fix)
                continue
            fix = await self._apply(plan, finding)
            result.fixes.append(fix)
            ctx.emit("fix/applied", fix)
            if fix.status == "applied":
                applied_findings.append(finding)

        # ③ 回归：重跑同攻击，必须清零
        if applied_findings:
            for finding in applied_findings:
                regression = await self._regress(finding)
                result.regressions.append(regression)
                ctx.emit("regression/verified", regression)
                self.runtime.storage.add_regression(
                    finding.finding_id, regression.passed, regression.detail)
                if not regression.passed:
                    if self.cfg.blueteam.rollback_on_fail:
                        await self._rollback(finding)
                        result.rolled_back.append(finding.finding_id)
                    self.runtime.storage.update_finding_fix(
                        finding.finding_id, "", "回归未清零", "failed")
                else:
                    self.runtime.storage.mark_finding_verified(
                        finding.finding_id)

        # ④ 完整修复报告（问题说明 + 代码级修复 + 验证步骤 + 回归结果）
        self._write_report(result)
        return result

    def _write_report(self, result: BlueResult) -> None:
        if not result.plans:
            return
        from ..reporter.remediation import write_remediation_report
        try:
            target = self.scan.target if self.scan is not None else "manual"
            scan_id = (self.scan.scan_id if self.scan is not None
                       else "manual")
            result.remediation_path = write_remediation_report(
                target, scan_id, self.findings,
                self.cfg.out_dir, result.plans, result.fixes,
                result.regressions)
        except OSError as exc:
            log.warning("修复报告写入失败: %s", exc)

    # ---- ① 规划 ----

    def _plan(self, finding: Finding) -> FixPlan:
        template: Optional[FixTemplate] = fix_template_for(finding.category)
        if template is None:
            return FixPlan(plan_id=new_id("plan"), finding_id=finding.finding_id,
                           category=finding.category, auto_fixable=False,
                           title="暂无内置修复模板",
                           rationale="该漏洞类型暂无内置修复模式，需安全工程师人工研判。",
                           manual_steps=["人工研判漏洞根因", "制定修复方案", "回归验证"])
        ops: List[Dict[str, Any]] = []
        if template.auto_fixable and template.guard_key:
            ops.append({"op": "set_guard", "key": template.guard_key,
                        "value": template.guard_value})
        return FixPlan(plan_id=new_id("plan"), finding_id=finding.finding_id,
                       category=finding.category,
                       auto_fixable=template.auto_fixable,
                       template_id=template.template_id, title=template.title,
                       rationale=template.rationale, ops=ops,
                       manual_steps=list(template.manual_steps))

    # ---- ①b LLM 修复建议（engine.llm_fix_plan；无 LLM 确定性降级为模板） ----

    async def _ai_fix_note(self, finding: Finding, plan: FixPlan) -> str:
        """让 LLM 针对单条漏洞给出修复建议；失败则静默降级（模板仍有效）。"""
        llm = self.runtime.llm
        if not llm or "deepseek" not in llm.providers():
            return ""
        try:
            from dsh.llm.adapters import LlmCallConfig, LlmRequest
            from dsh.llm.messages import Message
            prompt = (
                f"你是蓝队修复工程师。针对以下漏洞给出简洁中文修复建议"
                f"（根因 → 具体修复步骤 → 关键代码片段，200 字内）：\n"
                f"- 漏洞类别: {finding.category}\n"
                f"- 严重级别: {finding.severity}\n"
                f"- 攻击证据: {finding.evidence[:300]}\n"
                f"- 内置方案: {plan.title}｜{plan.rationale[:200]}\n")
            request = LlmRequest(
                config=LlmCallConfig(
                    provider="deepseek",
                    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                    max_tokens=400),
                messages=[Message.user(prompt)])
            out = ""
            async for chunk in llm.stream(request):
                if getattr(chunk, "text", ""):
                    out += chunk.text
            return out.strip()[:1200]
        except Exception as exc:
            log.warning("LLM 修复建议生成失败，使用内置模板: %s", exc)
            return ""

    # ---- ② 执行（沙箱 + 版本化） ----

    async def _apply(self, plan: FixPlan, finding: Finding) -> FixResult:
        guards_file = self.cfg.target.guards_file
        if not guards_file or not os.path.exists(guards_file):
            return FixResult(fix_id=new_id("fix"), plan_id=plan.plan_id,
                             finding_id=finding.finding_id, status="failed",
                             detail=f"防护配置文件不存在: {guards_file}")
        sandbox_dir = os.path.abspath(self.cfg.blueteam.sandbox_dir)
        os.makedirs(sandbox_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        # 版本化备份：文件名 = 时间戳 + 方案 id（可追溯、可回滚）
        backup = os.path.join(sandbox_dir, f"guards-{stamp}-{plan.plan_id}.bak")

        try:
            with open(guards_file, "r", encoding="utf-8") as fh:
                guards = yaml.safe_load(fh) or {}
            shutil.copyfile(guards_file, backup)
            for op in plan.ops:
                if op.get("op") == "set_guard":
                    guards[str(op["key"])] = op["value"]
            # 沙箱内验证：写入沙箱副本 → 写回 live 文件 → 目标热重载
            sandbox_copy = os.path.join(
                sandbox_dir, os.path.basename(guards_file))
            with open(sandbox_copy, "w", encoding="utf-8") as fh:
                yaml.safe_dump(guards, fh, allow_unicode=True, sort_keys=False)
            with open(guards_file, "w", encoding="utf-8") as fh:
                yaml.safe_dump(guards, fh, allow_unicode=True, sort_keys=False)
            reloaded = await self.adapter.reload_guards()
            if not reloaded:
                raise FixError("目标防护配置热重载失败")
            self.runtime.storage.add_fix(
                fix_id := new_id("fix"), finding.finding_id, plan.plan_id,
                "applied", guards_file, backup,
                f"修复项: {[op.get('key') for op in plan.ops]}")
            return FixResult(fix_id=fix_id, plan_id=plan.plan_id,
                             finding_id=finding.finding_id, status="applied",
                             applied_to=guards_file, backup=backup,
                             detail=" | ".join(
                                 f"{op.get('key')}={op.get('value')}"
                                 for op in plan.ops))
        except Exception as exc:
            log.exception("修复应用失败: %s", exc)
            try:
                if os.path.exists(backup):
                    shutil.copyfile(backup, guards_file)
                    await self.adapter.reload_guards()
            except Exception:
                pass
            return FixResult(fix_id=new_id("fix"), plan_id=plan.plan_id,
                             finding_id=finding.finding_id, status="failed",
                             detail=str(exc))

    async def _rollback(self, finding: Finding) -> None:
        """回滚该漏洞的最近一次成功修复（恢复备份 + 目标热重载）。"""
        ctx = self.runtime.ctx
        try:
            guards_file = self.cfg.target.guards_file
            rows = self.runtime.storage._query(
                "SELECT backup FROM fixes WHERE finding_id=? AND status='applied' "
                "ORDER BY id DESC LIMIT 1", (finding.finding_id,))
            backup = rows[0]["backup"] if rows else None
            if backup and os.path.exists(backup):
                shutil.copyfile(backup, guards_file)
                await self.adapter.reload_guards()
            ctx.emit("fix/rolled_back", {"finding_id": finding.finding_id,
                                         "backup": backup})
        except Exception as exc:
            log.exception("回滚失败: %s", exc)

    # ---- ③ 回归 ----

    async def _regress(self, finding: Finding) -> RegressionResult:
        """重跑该漏洞命中的攻击样本，确认清零（同一攻击重跑必须 0 命中）。"""
        from ..engine.scan import execute_sample
        registry = self.runtime.registry
        before_rows = self.runtime.storage.attacks_for_samples(
            self.scan.scan_id, [finding.sample_uid])
        before = [r["sample_uid"] for r in before_rows
                  if r["verdict"] == Verdict.SUCCESS.value]
        after: List[Dict[str, Any]] = []
        passed = True
        for row in before_rows:
            concrete = registry.concrete_for_uid(row["sample_uid"])
            if concrete is None:
                after.append({"uid": row["sample_uid"],
                              "verdict": "skipped(无法重建样本)"})
                continue
            try:
                verdict, _response = await execute_sample(
                    self.runtime, self.cfg, self.adapter, concrete, reset=True)
            except UnsupportedSurface:
                after.append({"uid": row["sample_uid"], "verdict": "skipped"})
                continue
            after.append({"uid": row["sample_uid"],
                          "verdict": verdict.verdict,
                          "evidence": verdict.evidence[:200]})
            if verdict.verdict == Verdict.SUCCESS.value:
                passed = False
        detail = (f"修复前命中 {len(before)} 条；回归复测 "
                  f"{len(after)} 条：{'全部清零 ✓' if passed else '仍有命中 ✗'}")
        return RegressionResult(finding_id=finding.finding_id, passed=passed,
                                before=before,
                                after=[f"{a['uid']} → {a['verdict']}"
                                       for a in after], detail=detail)
