"""test_llm_fixplan —— 蓝队 LLM 修复建议（engine.llm_fix_plan）。

设计：每条漏洞在模板方案基础上，由 LLM 生成针对性修复建议
（plan.ai_note / finding.fix.ai_plan），呈现在修复报告「🤖 AI 修复建议」节；
无 LLM 时确定性降级（仅模板），流程不中断。
"""
import pytest

from redteam.blueteam import BlueEngine
from redteam.engine import ScanRunner, build_adapter
from redteam.runtime import RedTeamRuntime

from .conftest import make_config


async def _scan_then_blue(vuln_lab, tmp_path, apply_fixes=False):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        scan = await ScanRunner(runtime, cfg, adapter,
                                scan_mode="fixplan").run()
        from redteam.cli import _load_scan_result
        scan_result = await _load_scan_result(
            runtime, runtime.storage, scan.scan_id)
        return cfg, runtime, adapter, scan_result
    except BaseException:
        if adapter is not None:
            await adapter.close()
        await runtime.close()
        raise


async def test_llm_fix_plan_note_in_remediation(vuln_lab, tmp_path,
                                               monkeypatch):
    """LLM 修复建议写入 plan.ai_note / finding.fix.ai_plan，并呈现在修复报告。"""
    cfg, runtime, adapter, scan_result = await _scan_then_blue(vuln_lab, tmp_path)
    try:
        cfg.engine.llm_fix_plan = True

        async def fake_ai_note(self, finding, plan):
            return (f"AI 建议修复 {finding.category}："
                    f"启用输入过滤与参数化查询，并补充回归用例。")

        monkeypatch.setattr(BlueEngine, "_ai_fix_note", fake_ai_note)
        outcome = await BlueEngine(runtime, cfg, adapter,
                                   scan_result).run(apply_fixes=False)
        assert outcome.plans
        assert all(getattr(p, "ai_note", "") for p in outcome.plans)
        assert all("ai_plan" in f.fix and f.fix["ai_plan"]
                   for f in scan_result.findings)
        assert outcome.remediation_path, "只出方案也必须生成修复报告"
        with open(outcome.remediation_path, encoding="utf-8") as fh:
            content = fh.read()
        assert "🤖 AI 修复建议" in content
        assert "启用输入过滤" in content
    finally:
        await adapter.close()
        await runtime.close()


async def test_llm_fix_plan_off_no_ai_section(vuln_lab, tmp_path):
    """默认关闭：报告不含 AI 建议节（确定性模板交付）。"""
    cfg, runtime, adapter, scan_result = await _scan_then_blue(vuln_lab, tmp_path)
    try:
        cfg.engine.llm_fix_plan = False
        outcome = await BlueEngine(runtime, cfg, adapter,
                                   scan_result).run(apply_fixes=False)
        with open(outcome.remediation_path, encoding="utf-8") as fh:
            content = fh.read()
        assert "🤖 AI 修复建议" not in content
        assert "① 漏洞问题说明" in content
    finally:
        await adapter.close()
        await runtime.close()


async def test_llm_fix_plan_no_llm_degrades(vuln_lab, tmp_path, monkeypatch):
    """开启但无 LLM：_ai_fix_note 返回空，模板方案照常交付（优雅降级）。"""
    cfg, runtime, adapter, scan_result = await _scan_then_blue(vuln_lab, tmp_path)
    try:
        cfg.engine.llm_fix_plan = True

        class _NoDeepseek:
            def providers(self):
                return ["mock"]

        monkeypatch.setattr(runtime, "llm", _NoDeepseek())
        outcome = await BlueEngine(runtime, cfg, adapter,
                                   scan_result).run(apply_fixes=False)
        assert outcome.plans
        assert all(not getattr(p, "ai_note", "") for p in outcome.plans)
    finally:
        await adapter.close()
        await runtime.close()
