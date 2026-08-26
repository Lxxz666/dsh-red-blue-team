"""redteam.reporter.report —— 漏洞报告生成（JSON + Markdown 双格式）。

报告结构：漏洞总览（按严重级别/攻击类型）→ 漏洞详情（类型/OWASP 映射/
载荷/证据/信号/修复建议）→ 修复工单（蓝队消费）→ 审计日志索引。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import Finding, ScanResult, Severity, Verdict

_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                  "low": "🟢", "info": "⚪"}

_REPORT_TEMPLATE = """# 安全检测报告：{target}

> 扫描编号: `{scan_id}` ｜ 扫描时间: {started_at} → {finished_at}
> 扫描模式: {mode} ｜ 目标: {base_url} ｜ 攻击样本: {total} 条
> 业务场景: {scenarios} ｜ 判定原则: 确定性信号（证据模式/泄露模式/副作用探测）优先；弱信号仅标记存疑

{narrative}
{attack_summary}
## 漏洞总览

| 严重级别 | 数量 |
|:---:|:---:|
{severity_rows}

**结果统计**：攻击成功 {success} 条 ｜ 防御生效 {failed} 条 ｜ 存疑待复核 {suspicious} 条 ｜ 执行错误 {error} 条

## 漏洞详情

{findings}

## 修复工单（蓝队使用）

{work_orders}

## 附录

- 事件溯源审计日志: `{audit_path}`（每次攻击的载荷/响应/判定证据完整可回放）
- 机器可读报告: `{report_json_path}`
- 完整修复报告: `dsh-redteam fix --config <config> --scan {scan_id}` 生成（问题说明+代码级修复方案+验证步骤）
- 合规声明: 本项目仅限授权测试。使用前请确认已获目标所有者书面授权。
"""


@dataclass
class Report:
    scan: ScanResult
    findings: List[Finding] = field(default_factory=list)
    base_url: str = ""
    mode: str = "full"
    audit_path: str = ""
    report_json_path: str = ""
    scenarios: List[str] = field(default_factory=list)
    narrative: str = ""

    def summary_counts(self) -> Dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def to_json(self) -> Dict[str, Any]:
        return {
            "report_id": self.scan.scan_id,
            "target": self.scan.target,
            "scanned_at": self.scan.started_at,
            "finished_at": self.scan.finished_at,
            "scanner_version": "0.2.0",
            "mode": self.mode,
            "scenarios": self.scenarios,
            "narrative": self.narrative,
            "summary": {
                "total_samples": self.scan.total,
                "attack_success": self.scan.success_count,
                "suspicious": self.scan.suspicious_count,
                "severity_counts": self.summary_counts(),
            },
            "findings": [f.to_json() for f in self.findings],
            "audit_log": self.audit_path,
        }

    def to_markdown(self) -> str:
        counts = self.summary_counts()
        severity_rows = "\n".join(
            f"| {_SEVERITY_ICON[s.value]} {s.value} | {counts[s.value]} |"
            for s in Severity if counts[s.value])
        if not severity_rows:
            severity_rows = "| （无漏洞） | 0 |"

        narrative = f"> {(self.narrative or _auto_narrative(self.scan, counts))}\n"
        scenarios_text = "、".join(self.scenarios) if self.scenarios else "通用"

        findings_md: List[str] = []
        for finding in self.findings:
            signals = "\n".join(
                f"   - {name}: {info['evidence']}"
                for name, info in finding.signals.items())
            chain = _chain_md(finding.chain)
            fix = finding.fix
            auto = "✅ 可自动修复" if fix.get("auto_fixable") else "⚠️ 需人工修复"
            findings_md.append(
                f"### {finding.finding_id} ｜ {_SEVERITY_ICON[finding.severity]} "
                f"{finding.severity.upper()} ｜ {finding.category}\n\n"
                f"- **漏洞类型**: {finding.category}（OWASP: {finding.owasp}）\n"
                f"- **样本**: {finding.sample_id}（{finding.sample_uid}）｜ 角色: {finding.role}\n"
                f"- **攻击载荷**:\n\n  ```text\n  {finding.payload.strip()}\n  ```\n"
                f"{chain}"
                f"- **证据**: {finding.evidence}\n"
                f"- **验证信号**:\n{signals}\n"
                f"- **置信度**: {finding.confidence}\n"
                f"- **修复建议**: {auto} ｜ {fix.get('plan', '')} "
                f"（{fix.get('template', '人工研判')}）\n")
        findings_text = "\n---\n\n".join(findings_md) if findings_md \
            else "**未发现确认漏洞。** 存疑样本请见附录审计日志复核。\n"

        work_orders: List[str] = []
        for finding in self.findings:
            work_orders.append(
                f"- [ ] **{finding.finding_id}** {finding.category} "
                f"({finding.severity})：{finding.fix.get('plan', '人工研判')}"
                f"　→ 状态: {finding.fix.get('status', 'pending')}")
        work_orders_text = "\n".join(work_orders) if work_orders \
            else "- 无待修复漏洞。\n"

        return _REPORT_TEMPLATE.format(
            target=self.scan.target, scan_id=self.scan.scan_id,
            started_at=self.scan.started_at, finished_at=self.scan.finished_at,
            mode=self.mode, base_url=self.base_url, total=self.scan.total,
            scenarios=scenarios_text, narrative=narrative,
            attack_summary=_attack_summary_md(self.scan),
            severity_rows=severity_rows, success=self.scan.success_count,
            failed=sum(1 for v in self.scan.verdicts
                       if v.verdict == Verdict.FAILED.value),
            suspicious=self.scan.suspicious_count,
            error=sum(1 for v in self.scan.verdicts
                      if v.verdict == Verdict.ERROR.value),
            findings=findings_text, work_orders=work_orders_text,
            audit_path=self.audit_path,
            report_json_path=self.report_json_path or "")


def _chain_md(chain: List[Dict[str, Any]]) -> str:
    if not chain:
        return ""
    lines = ["- **攻击链路**:", "  ```text"]
    for turn in chain:
        msg = str(turn.get("msg", "")).replace("\n", " ")[:120]
        resp = str(turn.get("resp", "")).replace("\n", " ")[:120]
        lines.append(f"  [{turn.get('turn')}] {turn.get('role')}> {msg}")
        lines.append(f"       目标> {resp}")
    lines.append("  ```")
    return "\n".join(lines) + "\n"


def _attack_summary_md(scan) -> str:
    """攻击活动总结：按攻击部位（role）/攻击类别汇总，说明攻击了哪些部分、情况如何。"""
    from collections import Counter
    if not scan.verdicts:
        return "> 本次扫描未发起有效攻击。\n"
    roles: Dict[str, Dict[str, int]] = {}
    cats: Counter = Counter()
    for v in scan.verdicts:
        role = v.role or "未知部位"
        r = roles.setdefault(role, {"total": 0, "success": 0, "suspicious": 0})
        r["total"] += 1
        if v.success:
            r["success"] += 1
        if v.verdict == Verdict.SUSPICIOUS.value:
            r["suspicious"] += 1
        cats[v.category or "unknown"] += 1
    role_finds: Dict[str, set] = {}
    for f in scan.findings:
        role_finds.setdefault(f.role or "未知部位", set()).add(f.category)

    lines = ["## 攻击活动总结", "",
             "本次扫描对不同功能部位/角色发起了攻击，各部位攻击情况如下：", "",
             "| 攻击部位（角色） | 攻击次数 | 成功 | 存疑 | 失败/防御 | 命中漏洞类别 |",
             "|---|---:|---:|---:|---:|---|"]
    for role, r in sorted(roles.items()):
        failed = r["total"] - r["success"] - r["suspicious"]
        hits = "、".join(sorted(role_finds.get(role, set()))) or "—"
        lines.append(f"| {role} | {r['total']} | {r['success']} | "
                     f"{r['suspicious']} | {failed} | {hits} |")
    lines += ["", "| 攻击类别 | 攻击次数 |", "|---|---:|"]
    for cat, n in cats.most_common():
        lines.append(f"| {cat} | {n} |")
    lines.append("")
    return "\n".join(lines)


def _auto_narrative(scan, counts) -> str:
    """自动生成报告开头的叙事总结：攻击了哪些部分、总体情况怎么样。"""
    if not scan.verdicts:
        return "本次扫描未发起有效攻击。"
    roles = sorted({v.role for v in scan.verdicts if v.role})
    cats = sorted({v.category for v in scan.verdicts if v.category})
    total = scan.total
    success = scan.success_count
    rate = int(100 * success / max(total, 1))
    parts = "、".join(roles) if roles else "未标注"
    cats_txt = "、".join(cats) if cats else "—"
    text = (f"本次攻击覆盖 **{len(roles)}** 个功能部位（{parts}）、"
            f"**{len(cats)}** 类攻击向量（{cats_txt}），共发起 **{total}** 次测试，"
            f"确定性命中 **{success}** 次（成功率 {rate}%）。")
    if counts.get("critical"):
        text += f" 确认 **{counts['critical']}** 个严重级漏洞，需立即处置。"
    elif counts.get("high"):
        text += f" 确认 **{counts['high']}** 个高危漏洞，需优先修复。"
    if counts.get("medium"):
        text += f" 另有 **{counts['medium']}** 个中危漏洞。"
    if success == 0:
        text += " 未发现确定性漏洞，目标防御对本次攻击向量总体生效。"
    return text


def build_report(scan: ScanResult, base_url: str = "", mode: str = "full",
                 audit_path: str = "", report_json_path: str = "",
                 scenarios: Optional[List[str]] = None,
                 narrative: str = "") -> Report:
    return Report(scan=scan, findings=list(scan.findings), base_url=base_url,
                  mode=mode, audit_path=audit_path,
                  report_json_path=report_json_path,
                  scenarios=list(scenarios or []), narrative=narrative)


def write_report(scan: ScanResult, out_dir: str,
                 probe: Optional[Dict[str, Any]] = None,
                 base_url: str = "", mode: str = "full",
                 audit_path: str = "", scenarios: Optional[List[str]] = None,
                 narrative: str = "") -> "tuple[str, str]":
    """写报告到 out_dir，返回 (markdown 路径, json 路径)。"""
    os.makedirs(out_dir, exist_ok=True)
    stamp = scan.started_at.replace(":", "").replace("+", "")[:15]
    safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                          for ch in scan.target)
    stem = f"report_{safe_target}_{stamp}"
    report = build_report(scan, base_url=base_url, mode=mode,
                          audit_path=audit_path, scenarios=scenarios,
                          narrative=narrative)
    md_path = os.path.join(out_dir, f"{stem}.md")
    json_path = os.path.join(out_dir, f"{stem}.json")
    report.report_json_path = json_path
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report.to_markdown())
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_json(), fh, ensure_ascii=False, indent=2)
    return md_path, json_path
