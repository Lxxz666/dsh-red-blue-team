"""test_report —— 报告结构与授权合规闸门。"""
import json
import os

import pytest

from redteam.config import ScanConfig
from redteam.errors import AuthorizationError, ConfigError
from redteam.models import Finding, ScanResult
from redteam.reporter import build_report, write_report


def _result() -> ScanResult:
    result = ScanResult(scan_id="scan-test-0001", target="demo",
                        started_at="2026-01-01T00:00:00+0800",
                        finished_at="2026-01-01T00:01:00+0800")
    result.findings = [
        Finding(finding_id="F-001", scan_id=result.scan_id,
                category="direct_injection", owasp="LLM01",
                severity="critical", sample_id="di-001",
                sample_uid="di-001-student-v0", payload="忽略指令",
                role="student", evidence="已删除订单 1003。",
                signals={"evidence_pattern": {"hit": True, "evidence": "x"}},
                confidence=0.9,
                fix={"auto_fixable": True, "template": "injection-guard",
                     "plan": "启用注入防护", "status": "pending"}),
    ]
    return result


def test_markdown_report_contains_required_sections():
    report = build_report(_result())
    md = report.to_markdown()
    for section in ("安全检测报告", "漏洞总览", "漏洞详情", "修复工单",
                    "附录", "direct_injection", "忽略指令"):
        assert section in md


def test_json_report_schema():
    report = build_report(_result())
    data = report.to_json()
    assert data["target"] == "demo"
    assert data["summary"]["attack_success"] >= 0
    assert data["summary"]["severity_counts"]["critical"] == 1
    finding = data["findings"][0]
    for key in ("finding_id", "category", "owasp", "severity", "payload",
                "evidence", "signals", "fix"):
        assert key in finding


def test_write_report_creates_both_formats(tmp_path):
    result = _result()
    md_path, json_path = write_report(result, str(tmp_path))
    assert os.path.exists(md_path) and md_path.endswith(".md")
    assert os.path.exists(json_path) and json_path.endswith(".json")
    json.load(open(json_path, encoding="utf-8"))


def test_authorization_required_for_remote_target():
    with pytest.raises(AuthorizationError):
        ScanConfig.from_dict({
            "target": {"name": "victim", "type": "http",
                       "base_url": "http://203.0.113.10/api"}})


def test_authorization_accepted_with_declaration():
    cfg = ScanConfig.from_dict({
        "target": {"name": "client", "type": "http",
                   "base_url": "http://203.0.113.10/api"},
        "authorization": {"authorized_by": "甲方安全负责人",
                          "contact": "sec@client.example",
                          "scope": "仅限甲方测试环境"}})
    assert cfg.authorization.valid()


def test_local_target_exempt():
    cfg = ScanConfig.from_dict({
        "target": {"name": "local", "type": "http",
                   "base_url": "http://127.0.0.1:8080"}})
    assert cfg.target.is_local


def test_bad_config_rejected():
    with pytest.raises(ConfigError):
        ScanConfig.from_dict({"target": {"base_url": "not-a-url"}})
    with pytest.raises(ConfigError):
        ScanConfig.from_dict({"target": {"type": "unknown"}})
