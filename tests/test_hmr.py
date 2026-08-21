"""配置热重载（HMR）测试：替换/插入/禁用/解析失败/回滚。"""
import asyncio
import os

import pytest

from dsh.kernel import Context, PluginTree
from dsh.config.watcher import ConfigWatcherPlugin
from dsh.prompt import SystemPromptService
from dsh.prompt.sections import PersonaPlugin
from dsh.tools import ToolRuntime


def _build_ctx(tmp_path, extra_rows=None):
    """手工组树：systemPrompt + persona + tools（+ 额外行），并挂 watcher。"""
    ctx = Context("hmr")
    tree = PluginTree(ctx)
    tree.add_bundle_rows([
        {"id": "system-prompt",
         "plugin": "dsh.prompt.system_prompt:SystemPromptService"},
        {"id": "persona", "plugin": "dsh.prompt.sections:PersonaPlugin"},
        {"id": "tools", "plugin": "dsh.tools.registry:ToolRuntime"},
    ])
    for row in extra_rows or []:
        tree.add_bundle_rows([row])
    return ctx, tree


async def _boot_watcher(ctx, tree, patch_path, interval=0.05):
    await tree.mount()
    ctx.set("pluginTree", tree)
    watcher = ConfigWatcherPlugin(ctx, {"paths": [str(patch_path)],
                                        "interval": interval})
    watcher.apply(ctx)
    return watcher


def _write_patch(path, rows):
    import yaml
    path.write_text(yaml.safe_dump(rows, allow_unicode=True),
                    encoding="utf-8")


async def test_hmr_reconfigure_persona(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    _write_patch(patch, [])
    ctx, tree = _build_ctx(tmp_path)
    watcher = await _boot_watcher(ctx, tree, patch)
    try:
        _write_patch(patch, [{"id": "persona",
                              "config": {"persona": "全新的我"}}])
        await asyncio.sleep(0.3)
        text = ctx.systemPrompt._build(None, {})["text"]
        assert "全新的我" in text
        assert "严谨、可靠" not in text
    finally:
        watcher.close()
        await tree.dispose()


async def test_hmr_insert_mounts_new_plugin(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    _write_patch(patch, [])
    ctx, tree = _build_ctx(tmp_path)
    watcher = await _boot_watcher(ctx, tree, patch)
    try:
        assert ctx.tools.get("greet") is None
        _write_patch(patch, [{"insert": [
            {"id": "greet-plugin",
             "plugin": "examples.my_plugin:MyPlugin",
             "config": {"greeting": "热"}}]}])
        await asyncio.sleep(0.3)
        assert ctx.tools.get("greet") is not None
        assert tree.is_mounted("greet-plugin")
    finally:
        watcher.close()
        await tree.dispose()


async def test_hmr_disable_unloads(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    _write_patch(patch, [])
    ctx, tree = _build_ctx(tmp_path, extra_rows=[
        {"id": "greet-plugin", "plugin": "examples.my_plugin:MyPlugin",
         "config": {"greeting": "你好"}}])
    watcher = await _boot_watcher(ctx, tree, patch)
    try:
        assert ctx.tools.get("greet") is not None
        _write_patch(patch, [{"disable": ["greet-plugin"]}])
        await asyncio.sleep(0.3)
        assert ctx.tools.get("greet") is None
        assert not tree.is_mounted("greet-plugin")
        assert tree.get_entry("greet-plugin").disabled
    finally:
        watcher.close()
        await tree.dispose()


async def test_hmr_parse_failure_keeps_tree(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    _write_patch(patch, [])
    ctx, tree = _build_ctx(tmp_path)
    watcher = await _boot_watcher(ctx, tree, patch)
    failures = []
    ctx.on("hmr/config-update-failed", lambda p: failures.append(p))
    try:
        patch.write_text("not: [valid: yaml: [", encoding="utf-8")
        await asyncio.sleep(0.3)
        assert failures
        # 旧树仍在运行
        text = ctx.systemPrompt._build(None, {})["text"]
        assert "严谨、可靠" in text
    finally:
        watcher.close()
        await tree.dispose()


class _FailingReconfigurePlugin(PersonaPlugin):
    """reconfigure 必失败的插件（回滚测试）。"""

    def reconfigure(self, config):
        raise RuntimeError("nope")


async def test_hmr_reconfigure_failure_rolls_back(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    _write_patch(patch, [])
    ctx, tree = _build_ctx(tmp_path)
    # 用失败插件替换 persona 行
    tree.add_bundle_rows([
        {"id": "persona", "plugin": "tests.test_hmr:_FailingReconfigurePlugin",
         "config": {"persona": "原始人格"}}])
    watcher = await _boot_watcher(ctx, tree, patch)
    try:
        _write_patch(patch, [{"id": "persona",
                              "config": {"persona": "会被回滚"}}])
        await asyncio.sleep(0.3)
        # 配置回滚：旧配置仍生效
        text = ctx.systemPrompt._build(None, {})["text"]
        assert "原始人格" in text
        assert tree.get_entry("persona").config.get("persona") == "原始人格"
    finally:
        watcher.close()
        await tree.dispose()
