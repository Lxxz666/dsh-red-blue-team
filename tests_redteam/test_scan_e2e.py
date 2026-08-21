"""test_scan_e2e —— 端到端验收：对靶场扫描发现率 ≥80%（实际 100%），
全加固靶场零命中（无误报），审计日志完整。"""
import json
import os

import pytest

from redteam.config import ScanConfig
from redteam.engine import ScanRunner, build_adapter
from redteam.models import Verdict
from redteam.runtime import RedTeamRuntime
from target_lab import planted_categories

from .conftest import make_config

PLANTED = planted_categories()
NEGATIVE_CATEGORIES = {"hallucination", "model_dos", "graphql",
                       "http_smuggling", "directory_listing"}


async def _scan(cfg: ScanConfig, mode: str = "e2e", order: str = "adaptive"):
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        runner = ScanRunner(runtime, cfg, adapter, scan_mode=mode, order=order)
        result = await runner.run()
        return result
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_scan_finds_at_least_80_percent_planted(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path,
                      vectors={"variants_per_sample": 2})
    result = await _scan(cfg)
    found = {f.category for f in result.findings}
    rate = len(found & PLANTED) / len(PLANTED)
    print(f"发现率: {rate:.0%}，发现类别: {sorted(found & PLANTED)}")
    assert rate >= 0.8, f"埋入漏洞发现率 {rate:.0%} 低于 80% 验收线"
    missing = PLANTED - found
    assert not missing, f"未发现的埋入漏洞类别: {missing}"


async def test_scan_no_false_positives_on_negative_controls(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    result = await _scan(cfg)
    negative_hits = [v for v in result.verdicts
                     if v.category in NEGATIVE_CATEGORIES and v.success]
    assert not negative_hits, f"对照样本误报: {[v.sample_uid for v in negative_hits]}"
    # 对照样本应判定为 failed（防御生效）
    failed = {v.category for v in result.verdicts
              if v.category in NEGATIVE_CATEGORIES
              and v.verdict == Verdict.FAILED.value}
    assert failed >= NEGATIVE_CATEGORIES


async def test_scan_hardened_lab_zero_findings(hardened_lab, tmp_path):
    lab, guards_file = hardened_lab
    cfg = make_config(lab, guards_file, tmp_path)
    result = await _scan(cfg)
    assert result.success_count == 0, \
        f"全加固靶场误报: {[f.category for f in result.findings]}"


async def test_audit_log_complete(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    result = await _scan(cfg)
    audit_path = result.audit_path
    assert audit_path and os.path.exists(audit_path)
    with open(audit_path, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    kinds = {e["event"] for e in events}
    for expected in ("scan/started", "attack/executed", "attack/verdict",
                     "finding/detected", "scan/finished"):
        assert expected in kinds, f"审计日志缺少事件 {expected}"
    executed = [e for e in events if e["event"] == "attack/executed"]
    assert len(executed) == result.total, "每次攻击都应留审计证据"
    # 审计行必须包含证据字段（载荷/响应/判定可回放）
    verdicts = [e for e in events if e["event"] == "attack/verdict"]
    assert all("sample" in json.dumps(e["payload"], ensure_ascii=False)
               or "verdict" in json.dumps(e["payload"]) for e in verdicts)


async def test_report_files_written(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    result = await _scan(cfg)
    assert result.report_path and os.path.exists(result.report_path)
    assert result.report_json_path and os.path.exists(result.report_json_path)
    with open(result.report_json_path, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["summary"]["total_samples"] == result.total
    assert len(report["findings"]) == len(result.findings)
    assert report["summary"]["severity_counts"]
    with open(result.report_path, encoding="utf-8") as fh:
        markdown = fh.read()
    for section in ("漏洞总览", "漏洞详情", "修复工单", "附录"):
        assert section in markdown


async def test_scan_is_deterministic(vuln_lab, tmp_path):
    """同一靶场两次扫描，命中集合一致（判定可信度基础）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    result1 = await _scan(cfg, mode="det-1")
    result2 = await _scan(cfg, mode="det-2")
    hits1 = {v.sample_uid for v in result1.verdicts if v.success}
    hits2 = {v.sample_uid for v in result2.verdicts if v.success}
    assert hits1 == hits2
