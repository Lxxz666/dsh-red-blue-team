"""test_regression —— 蓝队修复闭环：方案 → 沙箱应用 → 回归清零 → 复扫验收。"""
import os

import yaml

from redteam.blueteam import BlueEngine
from redteam.engine import ScanRunner, build_adapter
from redteam.runtime import RedTeamRuntime

from .conftest import make_config


async def test_blue_team_full_loop(vuln_lab, tmp_path):
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        # ① 红队扫描（发现漏洞）
        runner = ScanRunner(runtime, cfg, adapter, scan_mode="pre-fix")
        result = await runner.run()
        assert result.success_count > 0

        # ② 蓝队：规划 → 应用 → 回归
        blue = BlueEngine(runtime, cfg, adapter, result)
        outcome = await blue.run(apply_fixes=True)
        assert outcome.plans, "应生成修复方案"
        assert outcome.applied_count > 0, "lab 目标应自动应用修复"
        assert outcome.regressions, "应用修复后必须回归验证"
        assert outcome.summary()["passed_all"], "全部回归应清零"

        # ③ 修复方案必须带理由（审计要求）
        for plan in outcome.plans:
            assert plan.rationale, "修复方案缺少理由（不可审计）"

        # ④ 防护配置已被收紧（guards 文件验证）
        with open(guards_file, encoding="utf-8") as fh:
            guards = yaml.safe_load(fh)
        fixed_categories = {f.category for f in result.findings}
        from target_lab.inventory import guard_of_category
        for category in fixed_categories:
            guard_key = guard_of_category(category)
            if not guard_key:
                continue
            from target_lab import DEFAULT_GUARDS
            assert guards.get(guard_key) != getattr(DEFAULT_GUARDS, guard_key), \
                f"修复未生效: {category} → {guard_key}"

        # ⑤ 复扫验收：同一攻击重跑必须清零
        adapter2 = build_adapter(cfg)
        runner2 = ScanRunner(runtime, cfg, adapter2, scan_mode="post-fix")
        result2 = await runner2.run()
        await adapter2.close()
        assert result2.success_count == 0, \
            f"修复后复扫仍有 {result2.success_count} 次命中: " \
            f"{[f.category for f in result2.findings]}"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_manual_only_findings_not_auto_applied(vuln_lab, tmp_path):
    """不可自动修复的类别只出方案、不应用。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        from redteam.models import Finding
        fake = Finding(finding_id="F-999", scan_id="manual-test",
                       category="hallucination", owasp="LLM09",
                       severity="low", sample_id="ha-001",
                       sample_uid="ha-001-student-v0", payload="x", role="student")
        blue = BlueEngine(runtime, cfg, adapter, None, findings=[fake])
        outcome = await blue.run(apply_fixes=True)
        plan = outcome.plans[0]
        assert not plan.auto_fixable
        assert plan.manual_steps
        assert outcome.applied_count == 0
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()
