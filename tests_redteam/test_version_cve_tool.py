"""测试 LLM agent 的 version_cve 工具：注册注入 + 版本 CVE 精确匹配执行。"""
import asyncio
from types import SimpleNamespace

from redteam.agents.llm_agent import LlmAttackAgent
from redteam.infra.cve_version import run_version_cves, summarize_version_cves


def _make_agent(host="target.example"):
    agent = object.__new__(LlmAttackAgent)
    agent.cfg = SimpleNamespace(
        engine=SimpleNamespace(llm_explorer_tools=True),
        target=SimpleNamespace(base_url=f"http://{host}/"),
    )
    agent.adapter = SimpleNamespace(base_url=f"http://{host}/")
    agent._tool_calls_total = 0
    agent.agent_id = "t"
    return agent


def test_version_cve_tool_registered():
    """version_cve 工具必须出现在 tools 注册表里。"""
    agent = _make_agent()
    names = [t["function"]["name"] for t in agent._tool_schemas()]
    assert "version_cve" in names
    for required in ("host_scan", "service_fp", "vuln_scan", "deep_vuln", "version_cve"):
        assert required in names


def test_version_cve_dispatch_handled():
    """执行分发必须能识别 version_cve（方法存在且可协程调用）。"""
    agent = _make_agent()
    assert hasattr(agent, "_execute_version_cve")
    assert asyncio.iscoroutinefunction(agent._execute_version_cve)


def test_version_cve_executes_on_banner():
    """version_cve 对已知服务版本 banner 精确命中 CVE（不联网）。"""
    ports = [
        {"port": 80, "service": "nginx", "banner": "nginx/1.19.4"},
        {"port": 22, "service": "ssh", "banner": "SSH-2.0-OpenSSH_8.0"},
    ]
    checks = run_version_cves(ports)
    text = summarize_version_cves(checks)
    # nginx 1.19.4 → CVE-2021-23017 命中；OpenSSH 8.0 不在 regreSSHion 区间不命中
    assert "CVE-2021-23017" in text
    assert "CVE-2024-6387" not in text
    assert "命中" in text
