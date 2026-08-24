"""redteam.reporter.remediation —— 蓝队完整修复报告。

结构（每条漏洞）：
1. 漏洞问题说明（现象 → 根因 → 影响）；
2. 修复方案（分步指引 + 修复理由）；
3. 代码级修复示例（修复前后对比）；
4. 修复执行状态（planned/applied/verified/manual_only/rolled_back）；
5. 回归验证结果与人工验证步骤。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..blueteam.templates import fix_template_for
from ..models import Finding, FixPlan, FixResult, RegressionResult

_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                  "low": "🟢", "info": "⚪"}

_TEMPLATE = """# 漏洞修复报告（蓝队交付）

> 目标: {target} ｜ 扫描编号: `{scan_id}` ｜ 生成时间: {generated_at}
> 漏洞总数: {total} ｜ 已自动修复: {applied} ｜ 回归通过: {verified} ｜ 回滚: {rolled_back}

## 修复总览

| 优先级 | 漏洞 | 级别 | 修复方式 | 状态 |
|:---:|:---|:---:|:---|:---:|
{overview}

{details}

## 回归验证汇总

{regressions}

## 交付说明

- 自动修复仅作用于沙箱/靶场防护配置（版本化备份、可回滚），**生产环境修改须人工审批**；
- 标注「需人工」的条目已给出完整实施步骤与代码级示例，按步骤实施后请执行验证步骤；
- 每项修复均记录修复理由（rationale），满足安全审计可追溯要求；
- 回归验证原则：同一攻击样本复测必须清零，未清零的修复已自动回滚。
"""


def build_remediation_report(target: str, scan_id: str, findings: List[Finding],
                             plans: Optional[List[FixPlan]] = None,
                             fixes: Optional[List[FixResult]] = None,
                             regressions: Optional[List[RegressionResult]] = None,
                             ) -> str:
    from ..models import now_iso
    plans = plans or []
    fixes = fixes or []
    regressions = regressions or []
    plan_by_finding = {p.finding_id: p for p in plans}
    fix_by_finding: Dict[str, List[FixResult]] = {}
    for fix in fixes:
        fix_by_finding.setdefault(fix.finding_id, []).append(fix)
    regression_by_finding = {r.finding_id: r for r in regressions}

    def status_of(finding: Finding) -> str:
        reg = regression_by_finding.get(finding.finding_id)
        if reg is not None:
            return "✅ 回归通过" if reg.passed else "❌ 回归未清零（已回滚）"
        fxs = fix_by_finding.get(finding.finding_id)
        if fxs and any(f.status == "applied" for f in fxs):
            return "🔧 已应用"
        if fxs and any(f.status == "manual_only" for f in fxs):
            return "📝 方案已出（人工实施）"
        if finding.fix.get("auto_fixable"):
            return "⏳ 待修复"
        return "📝 需人工"

    overview_rows = []
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_findings = sorted(findings,
                             key=lambda f: order.get(f.severity, 9))
    for index, finding in enumerate(sorted_findings, start=1):
        overview_rows.append(
            f"| {index} | {finding.category} | "
            f"{_SEVERITY_ICON.get(finding.severity, '')} {finding.severity} | "
            f"{'自动' if finding.fix.get('auto_fixable') else '人工'} | "
            f"{status_of(finding)} |")

    detail_blocks: List[str] = []
    for finding in sorted_findings:
        detail_blocks.append(_finding_block(
            finding, plan_by_finding.get(finding.finding_id),
            fix_by_finding.get(finding.finding_id) or [],
            regression_by_finding.get(finding.finding_id)))

    regression_rows = []
    if regressions:
        for reg in regressions:
            mark = "✅" if reg.passed else "❌"
            regression_rows.append(f"- {mark} **{reg.finding_id}**: {reg.detail}")
    regression_text = "\n".join(regression_rows) if regression_rows \
        else "- 本轮无自动修复条目（全部为人工实施指引），请按各漏洞的验证步骤人工复核。\n"

    return _TEMPLATE.format(
        target=target, scan_id=scan_id, generated_at=now_iso(),
        total=len(findings),
        applied=sum(1 for f in fixes if f.status == "applied"),
        verified=sum(1 for r in regressions if r.passed),
        rolled_back=sum(1 for f in fixes if f.status == "failed"),
        overview="\n".join(overview_rows) if overview_rows else "| - | - | - | - | - |",
        details="\n---\n\n".join(detail_blocks) if detail_blocks
        else "## 详情\n\n无漏洞条目。\n",
        regressions=regression_text)


def _finding_block(finding: Finding, plan: Optional[FixPlan],
                   fixes: List[FixResult],
                   regression: Optional[RegressionResult]) -> str:
    template = fix_template_for(finding.category)
    lines = [
        f"## {finding.finding_id} ｜ {_SEVERITY_ICON.get(finding.severity, '')} "
        f"{finding.severity.upper()} ｜ {finding.category}",
        "",
        "### ① 漏洞问题说明",
        "",
        (template.explanation if template else
         "该漏洞类别暂无内置说明，请安全工程师人工研判。"),
        "",
        "### ② 修复方案",
        "",
    ]
    if template:
        lines.append(f"**{template.title}**")
        lines.append("")
        for step in template.how_to_fix:
            lines.append(f"1. {step}")
        lines.append("")
        lines.append(f"> 修复理由（审计）: {template.rationale}")
        lines.append("")
        if template.code_before or template.code_after:
            lines.append("### ③ 代码级修复示例")
            lines.append("")
            if template.code_before:
                lines.append("**修复前（存在漏洞）**:")
                lines.append("")
                lines.append(f"```python\n{template.code_before}\n```")
                lines.append("")
            if template.code_after:
                lines.append("**修复后（安全写法）**:")
                lines.append("")
                lines.append(f"```python\n{template.code_after}\n```")
                lines.append("")
    else:
        lines.append("无内置修复模板，需人工研判。")
        lines.append("")
    if plan is not None and getattr(plan, "ai_note", ""):
        lines.append("### ③½ 🤖 AI 修复建议（LLM）")
        lines.append("")
        lines.append(plan.ai_note)
        lines.append("")
    lines.append("### ④ 修复执行状态")
    lines.append("")
    if fixes:
        for fix in fixes:
            lines.append(f"- `{fix.fix_id}`: {fix.status} → {fix.applied_to or '-'}"
                         f"{'（备份: ' + fix.backup + '）' if fix.backup else ''}")
            if fix.detail:
                lines.append(f"  - 详情: {fix.detail}")
    elif plan is not None:
        lines.append(f"- 方案 `{plan.plan_id}` 已生成（{plan.title}），"
                     f"状态: 待实施")
    else:
        lines.append("- 未生成修复方案。")
    lines.append("")
    lines.append("### ⑤ 回归验证")
    lines.append("")
    if regression is not None:
        lines.append(f"- **结果**: {'✅ 清零（修复有效）' if regression.passed else '❌ 未清零（已回滚）'}")
        lines.append(f"- 复测样本: {'、'.join(regression.after[:6])}")
    else:
        lines.append("- 自动回归仅对 lab 靶场目标执行；外部目标请人工实施后按验证步骤复测。")
    if template and template.verify_steps:
        lines.append("- **人工验证步骤**:")
        for step in template.verify_steps:
            lines.append(f"  1. {step}")
    lines.append("")
    return "\n".join(lines)


def write_remediation_report(target: str, scan_id: str, findings: List[Finding],
                             out_dir: str, plans=None, fixes=None,
                             regressions=None) -> str:
    """写修复报告（Markdown），返回路径。"""
    os.makedirs(out_dir, exist_ok=True)
    content = build_remediation_report(target, scan_id, findings, plans,
                                       fixes, regressions)
    safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                          for ch in target)
    path = os.path.join(out_dir, f"remediation_{safe_target}_{scan_id}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
