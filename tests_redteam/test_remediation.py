"""test_remediation —— 蓝队完整修复报告（问题说明+代码级修复+验证+回归状态）。"""
import os

from redteam.blueteam import BlueEngine
from redteam.engine import ScanRunner, build_adapter
from redteam.models import Finding
from redteam.reporter.remediation import (build_remediation_report,
                                          write_remediation_report)
from redteam.runtime import RedTeamRuntime

from .conftest import make_config


def _finding(category="sqli", severity="critical", fix_auto=True) -> Finding:
    return Finding(finding_id="F-001", scan_id="scan-rep", category=category,
                   owasp="A03", severity=severity, sample_id="sqli-001",
                   sample_uid="sqli-001-student-v0", payload="' OR '1'='1",
                   role="student", evidence="登录成功",
                   signals={"evidence_pattern": {"hit": True, "evidence": "x"}},
                   confidence=0.9,
                   fix={"auto_fixable": fix_auto, "template": "sqli-filter",
                        "plan": "参数化查询", "status": "pending"})


def test_remediation_report_contains_required_sections():
    report = build_remediation_report(
        "demo", "scan-rep", [_finding()])
    for section in ("① 漏洞问题说明", "② 修复方案", "③ 代码级修复示例",
                    "④ 修复执行状态", "⑤ 回归验证", "修复理由（审计）",
                    "人工验证步骤", "参数化查询"):
        assert section in report


def test_remediation_report_code_before_after():
    report = build_remediation_report("demo", "scan-rep", [_finding()])
    assert "SELECT * FROM users WHERE name=" in report   # 修复前代码
    assert "WHERE name=? AND password=?" in report      # 修复后代码


def test_remediation_report_manual_finding():
    report = build_remediation_report(
        "demo", "scan-rep", [_finding(category="hallucination",
                                      fix_auto=False)])
    assert "需人工" in report or "人工实施" in report


def test_write_remediation_report_file(tmp_path):
    path = write_remediation_report("demo", "scan-rep", [_finding()],
                                    str(tmp_path))
    assert os.path.exists(path) and path.endswith(".md")


async def test_blue_engine_writes_remediation_report(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        result = await ScanRunner(runtime, cfg, adapter,
                                  scan_mode="remediation-test").run()
        blue = BlueEngine(runtime, cfg, adapter, result)
        outcome = await blue.run(apply_fixes=True)
        assert outcome.remediation_path, "蓝队必须输出完整修复报告"
        assert os.path.exists(outcome.remediation_path)
        with open(outcome.remediation_path, encoding="utf-8") as fh:
            content = fh.read()
        # 报告覆盖全部确认漏洞（含场景漏洞的问题说明）
        for finding in result.findings:
            assert finding.finding_id in content, \
                f"修复报告缺少 {finding.finding_id}"
        assert "修复总览" in content and "回归验证汇总" in content
        assert "✅" in content, "回归通过状态应标注"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()
