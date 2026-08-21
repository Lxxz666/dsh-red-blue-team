"""
dsh.boot —— 组合 + 挂载入口（CLI 与 server 共用）。

对应 app-boot 的 boot()：组合 profile → 应用 patch 层 → 挂载 →
失败 fail loud（逆序卸载部分树）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config.profile import compose
from .kernel import Context, PluginTree

log = logging.getLogger("dsh.boot")


def build_patches(workspace: Optional[str] = None,
                  mock_llm: bool = False,
                  provider: Optional[str] = None,
                  model: Optional[str] = None,
                  profile: str = "web") -> List[Tuple[List[dict], str]]:
    """构造 --patch 覆盖层（workspace / mock / provider 选择 / HMR 路径）。"""
    layers: List[Tuple[List[dict], str]] = []
    if workspace:
        layers.append(([{"id": "fs",
                         "config": {"root": os.path.abspath(workspace)}}],
                       "--workspace"))
    if mock_llm:
        layers.append(([{"disable": ["llm-deepseek"]}], "--mock"))
    if provider or model:
        config: Dict[str, Any] = {}
        if provider:
            config["provider"] = provider
        if model:
            config["model"] = model
        layers.append(([{"insert": [{
            "id": "agent-default-options",
            "plugin": "dsh.agent.plugins:DefaultOptionsPlugin",
            "config": config}]}], "--provider"))
    # 配置热重载：把本 profile 的 patch 路径注入 watcher
    from .config.profile import profile_dir, resolve_home
    watcher_paths = [str(profile_dir(profile) / "cordis.patch.yml"),
                     str(resolve_home() / "cordis.patch.yml")]
    layers.append(([{"id": "config-watcher",
                     "config": {"paths": watcher_paths}}], "--hmr"))
    return layers


async def boot(profile: str = "web", workspace: Optional[str] = None,
               mock_llm: bool = False, provider: Optional[str] = None,
               model: Optional[str] = None,
               extra_patches: Optional[Sequence[Tuple[List[dict], str]]] = None
               ) -> Tuple[Context, PluginTree]:
    """
    组合并挂载一个 profile。

    :return: (根 Context, 已挂载 PluginTree)。失败时部分树已卸载并抛 LoaderError。
    """
    tree, _layers = compose(profile)
    layers = build_patches(workspace=workspace, mock_llm=mock_llm,
                           provider=provider, model=model, profile=profile)
    layers.extend(extra_patches or [])
    for rows, label in layers:
        tree.apply_patch_rows(rows, label)
    await tree.mount()
    # HMR 入口：watcher 经 ctx.pluginTree 做运行期增量操作
    tree.ctx.set("pluginTree", tree)
    return tree.ctx, tree
