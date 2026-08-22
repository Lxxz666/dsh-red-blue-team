"""test_mcp —— MCP 目标适配器：工具发现、工具滥用攻击、对话样本跳过。"""
import os
import sys

from redteam.config import ScanConfig
from redteam.engine import ScanRunner, build_adapter
from redteam.models import Verdict
from redteam.runtime import RedTeamRuntime

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "vuln_mcp_server.py")
MCP_CMD = [sys.executable, FIXTURE]


def _mcp_config(tmp_path, categories):
    return ScanConfig.from_dict({
        "profile": "quick",
        "target": {"name": "vuln-mcp", "type": "mcp",
                   "mcp_command": MCP_CMD},
        "vectors": {"categories": categories, "variants_per_sample": 1},
        "detector": {"baseline": False},
        "storage": {"db_path": str(tmp_path / "mcp.db"),
                    "audit_dir": str(tmp_path / "audit")},
        "out_dir": str(tmp_path / "reports"),
        "engine": {"concurrency": 2, "min_interval_ms": 0},
    })


async def test_mcp_scan_finds_tool_abuse(tmp_path):
    cfg = _mcp_config(tmp_path, ["mcp_tool_abuse", "mcp_data_disclosure",
                                 "mcp_memory_poisoning"])
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        result = await ScanRunner(runtime, cfg, adapter,
                                  scan_mode="mcp-test").run()
        found = {f.category for f in result.findings}
        assert {"mcp_tool_abuse", "mcp_data_disclosure",
                "mcp_memory_poisoning"} <= found, \
            f"MCP 工具面漏洞未全部检出: {found}"
        # 侦察记录了工具清单
        assert result.probe.get("reachable")
        assert any("apply_refund" in note for note in result.probe["notes"])
        # 修复报告：MCP 类别为人工实施指引（含代码级示例）
        from redteam.blueteam.templates import fix_template_for
        template = fix_template_for("mcp_tool_abuse")
        assert template is not None and not template.auto_fixable
        assert template.code_after, "MCP 修复模板需含代码级示例"
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def test_chat_samples_skipped_not_error_on_mcp(tmp_path):
    """对话样本对 MCP 目标：跳过（skipped）而非错误（error）。"""
    cfg = _mcp_config(tmp_path, ["direct_injection", "mcp_tool_abuse"])
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        result = await ScanRunner(runtime, cfg, adapter,
                                  scan_mode="mcp-skip-test").run()
        chat_verdicts = [v for v in result.verdicts
                         if v.category == "direct_injection"]
        assert chat_verdicts, "对话样本应在计划内"
        assert all(v.verdict == Verdict.SKIPPED.value for v in chat_verdicts), \
            "对话样本应跳过而非报错"
        assert not any(v.verdict == Verdict.ERROR.value for v in chat_verdicts)
        mcp_verdicts = [v for v in result.verdicts
                        if v.category == "mcp_tool_abuse"]
        assert any(v.success for v in mcp_verdicts)
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


def test_mcp_config_requires_command():
    import pytest
    from redteam.errors import ConfigError
    with pytest.raises(ConfigError):
        ScanConfig.from_dict({"target": {"type": "mcp", "name": "x"}})
