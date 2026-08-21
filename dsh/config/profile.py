"""
dsh.config.profile —— Profile / Bundle / Patch 组合机制（对应 app-boot 的 Profiles）。

- Profile: ``$DSH_HOME/profiles/<name>/``（默认 ~/.dsh），内含 ``profile.yml``
  （``bundles`` 有序列表）与用户自己的 ``cordis.patch.yml``；
- Bundle: 先解析安装内自带的（``dsh/config/bundles/*.yml``），再解析 profile 目录
  下的 ``bundles/<name>.yml``；
- 组合顺序: 各 bundle 行 → profile patch → home 级 patch → --patch 覆盖层。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from ..kernel import PluginTree
from ..kernel.loader import apply_patch

log = logging.getLogger("dsh.config")

SHIPPED_BUNDLES_DIR = Path(__file__).parent / "bundles"

PROFILE_TEMPLATES = ("web", "headless")
DEFAULT_BUNDLES = ["base"]


def resolve_home() -> Path:
    """Harness home：$DSH_HOME 或 ~/.dsh（expanduser 失败时回退 Path.home()）。"""
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env).expanduser()
    try:
        return Path.home() / ".dsh"
    except Exception:
        return Path("~/.dsh").expanduser()


def profiles_dir() -> Path:
    return resolve_home() / "profiles"


def profile_dir(name: str) -> Path:
    return profiles_dir() / name


def init_profile(name: str, bundles: Optional[List[str]] = None) -> Path:
    """初始化 profile 目录（幂等）：写 profile.yml 模板与空 patch。"""
    directory = profile_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "profile.yml"
    if not manifest.exists():
        manifest.write_text(
            yaml.safe_dump({"bundles": bundles or DEFAULT_BUNDLES},
                           allow_unicode=True), encoding="utf-8")
    patch = directory / "cordis.patch.yml"
    if not patch.exists():
        patch.write_text("# 用户 patch 层：按 id 替换/禁用/插入配置行\n[]\n",
                         encoding="utf-8")
    return directory


def load_profile_manifest(name: str) -> Dict[str, Any]:
    """读取 profile 清单（自动初始化模板 profile）。"""
    if name in PROFILE_TEMPLATES:
        init_profile(name)
    path = profile_dir(name) / "profile.yml"
    if not path.exists():
        raise FileNotFoundError(
            f"profile {name!r} not found; run 'dsh-python plugin init {name}'")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_bundle_rows(name: str, profile: Optional[str] = None) -> List[dict]:
    """
    解析一个 bundle 的行列表：安装内自带优先，profile 目录次之。

    :raises FileNotFoundError: 两处都没有该 bundle。
    """
    shipped = SHIPPED_BUNDLES_DIR / f"{name}.yml"
    if shipped.exists():
        return yaml.safe_load(shipped.read_text(encoding="utf-8")) or []
    if profile is not None:
        local = profile_dir(profile) / "bundles" / f"{name}.yml"
        if local.exists():
            return yaml.safe_load(local.read_text(encoding="utf-8")) or []
    raise FileNotFoundError(f"bundle {name!r} not found")


def read_patch_rows(path: Path) -> List[dict]:
    """读取一个 patch 文件（空/注释-only 视为空层）。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"patch file {path} must be a YAML list")
    return data


def compose(profile: str = "web",
            extra_patches: Optional[Sequence[Tuple[List[dict], str]]] = None
            ) -> Tuple[PluginTree, List[Tuple[List[dict], str]]]:
    """
    组合配置树（不挂载）。

    :return: (PluginTree, 各层列表) —— 层顺序即优先级（后者覆盖前者）。
    """
    from ..kernel import Context
    manifest = load_profile_manifest(profile)
    bundle_names = manifest.get("bundles") or DEFAULT_BUNDLES
    ctx = Context(f"boot:{profile}")
    tree = PluginTree(ctx)
    for name in bundle_names:
        for row in load_bundle_rows(name, profile):
            tree.add_bundle_rows([row], layer_name=f"bundle:{name}")

    layers: List[Tuple[List[dict], str]] = []
    profile_patch = read_patch_rows(profile_dir(profile) / "cordis.patch.yml")
    home_patch = read_patch_rows(resolve_home() / "cordis.patch.yml")
    for rows, label in [(profile_patch, f"profile:{profile}"),
                        (home_patch, "home")]:
        if rows:
            tree.apply_patch_rows(rows, label)
            layers.append((rows, label))
    for rows, label in extra_patches or []:
        tree.apply_patch_rows(rows, label)
        layers.append((rows, label))
    return tree, layers


def dump_config(tree: PluginTree) -> str:
    """渲染组合后的配置树（一行一个插件）。"""
    lines: List[str] = []
    for entry in tree.entries():
        state = "disabled" if entry.disabled else "enabled"
        lines.append(f"- id: {entry.id}  [{state}]")
        lines.append(f"    plugin: {entry.target!r}")
        if entry.config:
            lines.append(f"    config: {entry.config!r}")
    return "\n".join(lines)
