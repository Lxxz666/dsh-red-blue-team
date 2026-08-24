"""test_llm_explorer —— V11-explorer：LLM 主动侦察工具（http_probe/http_attack）。

设计目标：LLM 不局限于预定义业务场景/样本库，可直接对任意路径/方法
发起原始 HTTP 探测与攻击；判定仍走确定性信号管线（leak_pattern 等），
产出 llm_explored 类别漏洞，可并入主扫描结果与修复流程。
"""
import pytest

from redteam.agents.llm_agent import LlmAttackAgent
from redteam.engine import build_adapter
from redteam.runtime import RedTeamRuntime

from .conftest import make_config

#: 脚本化 LLM：先侦察业务元数据接口，再对 ping 接口发起命令注入攻击
_EXPLORER_SCRIPT = [
    {"tool": {"name": "http_probe", "call_id": "p-1",
              "arguments": {"path": "/api/meta/business"}}},
    {"tool": {"name": "http_attack", "call_id": "a-1",
              "arguments": {"method": "POST", "path": "/api/ping",
                            "payload": "{\"host\": \"; cat /etc/passwd\"}"}}},
    {"tool": {"name": "finalize_report", "call_id": "fr-1",
              "arguments": {"summary": "ping 接口存在命令注入，泄露 /etc/passwd"}}},
    {"text": "done"},
]


async def test_explorer_tools_dispatch(vuln_lab, tmp_path):
    """完整 loop：http_probe 侦察 + http_attack 命中 → llm_explored 判定。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    cfg.engine.llm_explorer_tools = True
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        agent = LlmAttackAgent(runtime, cfg, adapter, "摘要",
                               script=_EXPLORER_SCRIPT)
        result = await agent.run()
        assert not result.skipped
        explored = [v for v in result.verdicts
                    if v.category == "llm_explored"]
        assert len(explored) == 1, "脚本中的 http_attack 应产出 1 条主动攻击判定"
        verdict = explored[0]
        assert verdict.success, f"ping 命令注入应命中，实际: {verdict.verdict}"
        assert any(s.name == "leak_pattern" and s.hit
                   for s in verdict.signals), "判定应来自确定性泄露信号"
        assert "root:x:0:0" in verdict.evidence
        assert verdict.sample_uid.startswith("llm-explored-")
        assert result.samples[0].variant_of == "llm_explored"
        assert result.samples[0].uid == verdict.sample_uid
        assert result.final_report, "finalize_report 应提交最终报告"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_explorer_probe_returns_recon_text(vuln_lab, tmp_path):
    """http_probe 单步：返回状态码与响应截断（侦察信息回喂 LLM）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        agent = LlmAttackAgent(runtime, cfg, adapter, "摘要")
        text = await agent._execute_http_probe(
            '{"path": "/api/meta/business"}')
        assert "HTTP 200" in text
        text404 = await agent._execute_http_probe('{"path": "/no/such/x"}')
        assert "HTTP 404" in text404
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_explorer_attack_failed_on_hardened(vuln_lab, tmp_path):
    """确定性把关：加固目标上同类攻击必须判定失败（LLM 不能自我宣称成功）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        agent = LlmAttackAgent(runtime, cfg, adapter, "摘要")
        verdict = await agent._execute_http_attack(
            '{"method": "GET", "path": "/", "payload": "hello"}')
        assert verdict is not None
        assert verdict.verdict == "failed"
        assert verdict.category == "llm_explored"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_explorer_tools_gated_by_config(vuln_lab, tmp_path):
    """engine.llm_explorer_tools 关闭时：不暴露 http_probe/http_attack 工具。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    try:
        agent = LlmAttackAgent(runtime, cfg, None, "摘要")
        names = [t["function"]["name"] for t in agent._tool_schemas()]
        assert names == ["attack_vector", "finalize_report"]
        cfg.engine.llm_explorer_tools = True
        names = [t["function"]["name"] for t in agent._tool_schemas()]
        assert "http_probe" in names and "http_attack" in names
        # 提示词应提及主动侦察能力
        assert "http_probe" in agent._mission_prompt()
        assert "http_probe" in agent._redteam_system_prompt()
    finally:
        await runtime.close()


async def test_orchestrator_merges_explored_findings(vuln_lab, tmp_path,
                                                    monkeypatch):
    """主 Agent 合并：llm_explored 判定落入扫描结果 → 漏洞落库（人工修复方案）。"""
    from redteam.agents import AttackOrchestrator
    from redteam.agents.llm_agent import LlmAgentResult
    from redteam.engine import build_adapter
    from redteam.models import AttackSample, ConcreteSample, Signal, VerdictResult
    from redteam.runtime import RedTeamRuntime

    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path)
    cfg.engine.llm_agent = True
    cfg.vectors.categories = ["direct_injection"]
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)

        async def fake_run(self):
            sample = ConcreteSample(
                uid="llm-explored-0",
                sample=AttackSample(
                    id="llm-explored-0", category="llm_explored",
                    name="LLM 主动攻击", severity="medium",
                    surface="api", method="POST", path="/api/ping"),
                role="student", payload='{"host": "cat /etc/passwd"}',
                variant_index=990, variant_of="llm_explored")
            verdict = VerdictResult(
                sample_uid=sample.uid, category="llm_explored",
                role="student", verdict="success", confidence=0.85,
                signals=[Signal(name="leak_pattern", hit=True,
                                confidence=0.85,
                                evidence="疑似 /etc/passwd 内容泄露")],
                evidence="root:x:0:0:root:/root:/bin/bash")
            return LlmAgentResult(verdicts=[verdict], samples=[sample],
                                  final_report="主动侦察发现命令注入",
                                  skipped=False)

        monkeypatch.setattr(
            "redteam.agents.llm_agent.LlmAttackAgent.run", fake_run)
        result = await AttackOrchestrator(
            runtime, cfg, adapter, scan_mode="llm-explorer-integration").run()
        explored = [f for f in result.findings
                    if f.category == "llm_explored"]
        assert explored, "llm_explored 漏洞应落库"
        assert explored[0].sample_uid == "llm-explored-0"
        assert explored[0].fix["auto_fixable"] is False, \
            "LLM 主动侦察漏洞无内置模板 → 人工修复方案"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()
