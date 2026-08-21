"""
dsh.kernel.loader —— 配置树加载器：Entry 解析、Patch 应用、拓扑挂载。

对应 dsh 的 boot 契约（packages/boot/app-boot）：

1. 空条目列表；
2. 依次应用各 bundle 的 patch（按顺序）；
3. 应用 profile 的 cordis.patch.yml；
4. 应用 home 级 patch；
5. 应用 --patch 覆盖层；
6. 按 ``provides/inject`` 拓扑排序后挂载；
7. 任一失败 → 逆序卸载已挂载部分并抛出带条目 id 的错误（fail loud）。

Patch 语法（YAML 顶层为列表）::

    - id: tool-fs            # 按 id 整体替换该行 config（不是深合并）
      config: { ... }
    - disable: [row-a]       # 禁用行
    - insert:                # 插入新行
      - id: my-plugin
        plugin: examples.my_tool_plugin:MyToolPlugin
        config: { ... }

条件禁用（对应 TS 版 ``!!js`` 表达式的安全子集）::

    - id: tool-bash
      disabled:
        platform: [win32]          # 平台在列表中则禁用
    - id: tool-bash
      enabled:
        platform: [linux, darwin]  # 平台在列表中才启用
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .context import Context
from .service import (PluginTarget, is_service_class, plugin_inject,
                      plugin_name, plugin_provides)
from ..errors import LoaderError

log = logging.getLogger("dsh.kernel")


@dataclass
class Entry:
    """配置树中的一行：一个待挂载插件。"""

    id: str
    target: PluginTarget
    config: Dict[str, Any] = field(default_factory=dict)
    disabled: bool = False

    def __repr__(self) -> str:
        state = "disabled" if self.disabled else "enabled"
        return f"<Entry {self.id} ({state}) plugin={plugin_name(self.target)}>"


# ---- 目标解析 ----

def resolve_target(spec: str) -> PluginTarget:
    """
    把字符串 ``"module.path:Attr"`` 解析为插件对象（类/实例/函数）。

    :raises LoaderError: 模块或属性不存在时。
    """
    if ":" not in spec:
        raise LoaderError(f"plugin spec must be 'module:attr', got {spec!r}")
    module_path, attr_path = spec.split(":", 1)
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise LoaderError(f"cannot import plugin module {module_path!r}: {exc}") from exc
    obj: Any = module
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise LoaderError(f"plugin attr {attr_path!r} not found in {module_path!r}") from exc
    return obj


# ---- 平台条件 ----

def _platform_condition_met(condition: dict) -> bool:
    """求值 enabled/disabled 平台条件。"""
    import sys
    platform = sys.platform
    if "platform" in condition:
        return platform in list(condition["platform"])
    return False


def row_disabled(config: Dict[str, Any]) -> bool:
    """根据 config 内的 enabled/disabled 条件计算该行是否禁用（disabled 优先）。"""
    if "disabled" in config:
        cond = config["disabled"]
        if isinstance(cond, bool):
            return cond
        if isinstance(cond, dict):
            return _platform_condition_met(cond)
    if "enabled" in config:
        cond = config["enabled"]
        if isinstance(cond, bool):
            return not cond
        if isinstance(cond, dict):
            return not _platform_condition_met(cond)
    return False


def entry_from_row(row: dict) -> Entry:
    """
    把一行 YAML 配置转成 Entry。

    禁用条件支持两级（行级优先，config 级次之）：:

        - id: tool-bash
          disabled: { platform: [win32] }     # 行级
        - id: tool-bash
          config: { disabled: true }          # config 级

    无论哪级，``disabled``/``enabled`` 都是控制元数据，不会混入插件的 config。
    """
    row_id = row.get("id")
    plugin_spec = row.get("plugin")
    if not plugin_spec:
        raise LoaderError("config row missing 'plugin'", entry_id=str(row_id or "?"))
    target = resolve_target(plugin_spec) if isinstance(plugin_spec, str) else plugin_spec
    config = dict(row.get("config") or {})
    disabled: Optional[bool] = None
    if "disabled" in row:
        cond = row["disabled"]
        disabled = cond if isinstance(cond, bool) else (
            _platform_condition_met(cond) if isinstance(cond, dict) else False)
    elif "enabled" in row:
        cond = row["enabled"]
        enabled = cond if isinstance(cond, bool) else (
            _platform_condition_met(cond) if isinstance(cond, dict) else True)
        disabled = not enabled
    if disabled is None:
        disabled = row_disabled(config)
    for key in ("disabled", "enabled"):
        config.pop(key, None)
    return Entry(id=str(row_id or plugin_name(target)), target=target,
                 config=config, disabled=disabled)


def apply_patch(entries: Dict[str, Entry], patch_rows: Sequence[dict],
                layer_name: str) -> None:
    """
    对条目表应用一个 patch 层（按 id 替换整行 / disable / insert）。

    :param entries: 有序 id→Entry 表（原地修改）。
    :param patch_rows: YAML 顶层列表。
    :param layer_name: 层名（错误信息用）。
    :raises LoaderError: patch 语法非法时。
    """
    for row in patch_rows or []:
        if not isinstance(row, dict):
            raise LoaderError(f"patch layer {layer_name!r}: rows must be mappings")
        if "disable" in row:
            for row_id in row["disable"]:
                if row_id in entries:
                    entries[row_id].disabled = True
                else:
                    log.warning("patch layer %r names unknown id %r", layer_name, row_id)
        elif "insert" in row:
            for insert_row in row["insert"]:
                entry = entry_from_row(insert_row)
                if entry.id in entries:
                    raise LoaderError(f"patch layer {layer_name!r}: duplicate id {entry.id!r}")
                entries[entry.id] = entry
        elif "id" in row:
            row_id = row["id"]
            if row_id not in entries:
                log.warning("patch layer %r names unknown id %r", layer_name, row_id)
                continue
            old = entries[row_id]
            # 整体替换 config（与 dsh 语义一致：restate unchanged fields）
            # 控制键按 row 级语义解析且不混入 config
            disabled, clean_config = _row_disabled_state(row)
            old.config = clean_config
            old.disabled = disabled
        else:
            raise LoaderError(
                f"patch layer {layer_name!r}: row needs 'id'+'config', 'disable', or 'insert'")


def _row_disabled_state(row: dict):
    """求一行 patch 的 (disabled, 纯净 config)：行级条件优先，config 级次之。"""
    config = dict(row.get("config") or {})
    if "disabled" in row:
        cond = row["disabled"]
        disabled: bool = cond if isinstance(cond, bool) else (
            _platform_condition_met(cond) if isinstance(cond, dict) else False)
    elif "enabled" in row:
        cond = row["enabled"]
        enabled = cond if isinstance(cond, bool) else (
            _platform_condition_met(cond) if isinstance(cond, dict) else True)
        disabled = not enabled
    else:
        disabled = row_disabled(config)
    for key in ("disabled", "enabled"):
        config.pop(key, None)
    return disabled, config
