"""test_agents —— 主Agent+子Agent 编排：派发/并行/汇总/审计事件。"""
import json
import os

from redteam.agents import AttackOrchestrator
from redteam.engine import build_adapter
from redteam.runtime import RedTeamRuntime

from .conftest import make_config


async def test_orchestrator_dispatches_workers_and_aggregates(vuln_lab, tmp_path):
    """主Agent派发侦察+攻击子Agent，子Agent返回报告，主Agent汇总出完整扫描。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        orchestrator = AttackOrchestrator(runtime, cfg, adapter,
                                          scan_mode="agent-test")
        result = await orchestrator.run()
        # 汇总完整：判定+漏洞+报告
        assert result.total > 50
        assert result.success_count > 0
        assert result.report_path and os.path.exists(result.report_path)
        # 审计日志含主Agent/子Agent事件
        with open(result.audit_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        kinds = {e["event"] for e in events}
        assert "agent/dispatched" in kinds
        assert "agent/report" in kinds
        dispatched = [e for e in events if e["event"] == "agent/dispatched"]
        agents = {e["payload"]["agent"] for e in dispatched
                  if isinstance(e["payload"], dict)}
        assert "recon" in agents, "侦察子代理应被派发"
        assert any(a.startswith("attacker-") for a in agents), "攻击子代理应被派发"
        reports = [e for e in events if e["event"] == "agent/report"]
        assert len(reports) >= 2
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_worker_isolation_on_failure(vuln_lab, tmp_path):
    """子代理隔离：单个子代理失败不影响整体扫描（无 worker 崩溃级联）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        from redteam.agents.worker import AttackWorkerAgent
        import asyncio
        from redteam.engine.scan import build_adapter as _ba
        worker = AttackWorkerAgent(
            agent_id="attacker-test", label="测试子代理",
            runtime=runtime, cfg=cfg, adapter=adapter,
            semaphore=asyncio.Semaphore(2), side_lock=asyncio.Lock(),
            scan_id="isolation-test")
        # 传入一个会炸的样本（不存在的方法/路径由执行层捕获为 error 判定，不抛异常）
        from redteam.models import AttackSample, ConcreteSample
        bad = ConcreteSample(
            uid="bad-001-student-v0",
            sample=AttackSample(id="bad-001", category="xss", name="bad",
                                severity="low", surface="api", method="GET",
                                path="/api/orders/99999",
                                evidence_patterns=["never-match"]),
            role="student", payload="")
        report = await worker.run([bad])
        assert report.agent_id == "attacker-test"
        assert len(report.verdicts) == 1
        assert report.verdicts[0].verdict in ("failed", "error")
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_folder_mode_end_to_end(tmp_path):
    """文件夹模式：主Agent派发静态子Agent → 静态发现入报告与工单。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text(
        'API_KEY = "sk-live-9f8a7b6c9f8a7b6c9f8a7b6c"\nDEBUG = True\n',
        encoding="utf-8")
    (proj / "cart.py").write_text("# cart\n", encoding="utf-8")
    (proj / "order.py").write_text("# order\n", encoding="utf-8")
    from redteam.config import ScanConfig
    cfg = ScanConfig.from_dict({
        "profile": "quick",
        "target": {"name": "proj", "type": "folder",
                   "folder_path": str(proj), "scenario": "auto"},
        "storage": {"db_path": str(tmp_path / "rt.db"),
                    "audit_dir": str(tmp_path / "audit")},
        "out_dir": str(tmp_path / "reports"),
    })
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    try:
        result = await AttackOrchestrator(
            runtime, cfg, None, scan_mode="folder-test").run()
        categories = {f.category for f in result.findings}
        assert "hardcoded_secret" in categories
        assert "debug_mode" in categories
        assert result.report_path and os.path.exists(result.report_path)
        with open(result.report_path, encoding="utf-8") as fh:
            markdown = fh.read()
        assert "hardcoded_secret" in markdown
        # 场景识别（文件夹指纹）
        assert result.audit_path
    finally:
        await runtime.close()
