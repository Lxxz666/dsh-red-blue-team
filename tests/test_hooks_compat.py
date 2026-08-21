"""hooks 兼容桥测试：Claude Code / Codex 配置解析 + 归一化 + 拦截语义。"""
import json
import sys

import pytest

from dsh.boot import boot
from dsh.hooks.compat import (discover_paths, load_claude_hooks,
                              load_codex_hooks)

CLAUDE_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "fs_write", "hooks": [
                {"type": "command", "command": "echo blocked; exit 1"}]},
            {"matcher": "nomatch", "hooks": [
                {"type": "command", "command": "exit 0"}]},
        ],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "exit 0"}]}],
        "Notification": [
            {"hooks": [{"type": "command", "command": "echo n"}]}],  # 不映射
        "Stop": [
            {"hooks": [{"type": "command", "command": "echo s"}]}],
    }
}

CODEX_TOML = """
model = "gpt-5"

[hooks]
SessionStart = "echo start"
Command = "echo block $CWD; exit 1"
Stop = "echo stop"
Notification = "echo n"
"""


# ---- 解析 ----

def test_load_claude_hooks_normalization(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(CLAUDE_SETTINGS), encoding="utf-8")
    rows = load_claude_hooks(str(path))
    assert len(rows) == 4  # Notification 不映射
    pre_tool = [r for r in rows if r["on"] == "pre-tool"]
    assert len(pre_tool) == 2
    assert pre_tool[0]["tool_pattern"] == "fs_write"
    assert pre_tool[0]["stdin_json"] is True  # Claude 契约
    assert pre_tool[0]["decision"] == "deny"
    assert any(r["on"] == "pre-step" for r in rows)
    assert any(r["on"] == "turn-stopping" for r in rows)


def test_load_claude_hooks_missing_file():
    assert load_claude_hooks("/nonexistent/settings.json") == []
    assert load_claude_hooks("/nonexistent/config.toml") == []


def test_load_codex_hooks_normalization(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CODEX_TOML, encoding="utf-8")
    rows = load_codex_hooks(str(path))
    assert len(rows) == 3  # Notification 不映射
    command = [r for r in rows if r["on"] == "pre-tool"][0]
    assert command["vars"] is True
    assert "$CWD" in command["command"]  # 运行期替换
    assert any(r["on"] == "session-start" for r in rows)


def test_discover_paths_override(tmp_path):
    paths = discover_paths({"paths": [str(tmp_path / "x.json")]})
    assert paths == [str(tmp_path / "x.json")]


# ---- 拦截语义（经 boot + 插件） ----

async def test_compat_pre_tool_deny_by_matcher(tmp_path):
    claude = tmp_path / "settings.json"
    claude.write_text(json.dumps(CLAUDE_SETTINGS), encoding="utf-8")
    ctx, tree = await boot(
        profile="headless", workspace=str(tmp_path), mock_llm=True,
        extra_patches=[([{"id": "hooks-compat",
                          "config": {"paths": [str(claude)]}}], "test")])
    try:
        result = await ctx.tools.execute(
            "c1", "fs_write", {"path": "x.txt", "content": "hi"})
        # 命中 matcher → fail-closed 拒绝（本环境无 pwsh 时 spawn 失败同样拒绝）
        assert result.is_error and result.error.code == "DENIED"
        # matcher 不命中 → hook 不拦截（文件缺失是工具自身语义，非 hook）
        (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
        result = await ctx.tools.execute("c2", "fs_read", {"path": "x.txt"})
        assert not result.is_error
    finally:
        await tree.dispose()


async def test_compat_codex_command_deny(tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(CODEX_TOML, encoding="utf-8")
    ctx, tree = await boot(
        profile="headless", workspace=str(tmp_path), mock_llm=True,
        extra_patches=[([{"id": "hooks-compat",
                          "config": {"paths": [str(codex)]}}], "test")])
    try:
        result = await ctx.tools.execute(
            "c1", "fs_write", {"path": "x.txt", "content": "hi"})
        assert result.is_error and result.error.code == "DENIED"
    finally:
        await tree.dispose()


async def test_compat_no_files_is_noop(tmp_path):
    ctx, tree = await boot(
        profile="headless", workspace=str(tmp_path), mock_llm=True,
        extra_patches=[([{"id": "hooks-compat",
                          "config": {"paths": [str(tmp_path / "none.json")]}}],
                        "test")])
    try:
        result = await ctx.tools.execute(
            "c1", "fs_write", {"path": "x.txt", "content": "hi"})
        assert not result.is_error  # 无配置 = 不拦截
    finally:
        await tree.dispose()
