"""
dsh.preset.presets —— Agent Presets（基础版，对应 TS 版 agent-presets）。

- preset = `~/.dsh/presets/<id>.yml`（config.paths 可换）里的插件行列表；
- ``mount(agent_ctx, id)`` 在 agent 的作用域 ctx 上挂一棵 PluginTree
  （服务/工具/分节只对该 agent 及其后代可见——isolate realm 的作用域化等价物）；
- ``join(parent_agent)``：子 agent 以父作用域为 parent 创建（composeFrom 的
  简化：同一批注册对象实例直接继承）；
- 会话 meta.agent_preset 持久化该选择（resume 时重挂）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import yaml

from ..kernel import PluginTree, Service
from ..errors import LoaderError

log = logging.getLogger("dsh.preset")


class AgentPresets(Service):
    """Agent Preset 注册表（ctx.agentPresets）。"""

    provides = "agentPresets"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        paths = (config or {}).get("paths")
        self.paths = paths or [os.path.join(os.path.expanduser("~"), ".dsh",
                                            "presets")]
        self._mounted: Dict[int, Any] = {}  # id(agent_ctx) -> (tree, preset_id)

    def apply(self, ctx) -> None:
        ctx.set("agentPresets", self)

    # ---- 读取 ----

    def _resolve_path(self, preset_id: str) -> Optional[str]:
        for root in self.paths:
            candidate = os.path.join(root, f"{preset_id}.yml")
            if os.path.exists(candidate):
                return candidate
        return None

    def list(self) -> List[str]:
        """全部 preset id。"""
        out: List[str] = []
        for root in self.paths:
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                if name.endswith(".yml"):
                    out.append(name[:-len(".yml")])
        return out

    def read(self, preset_id: str) -> List[dict]:
        """读取 preset 的行列表。"""
        path = self._resolve_path(preset_id)
        if path is None:
            raise LoaderError(f"preset {preset_id!r} not found")
        rows = yaml.safe_load(open(path, "r", encoding="utf-8")) or []
        if not isinstance(rows, list):
            raise LoaderError(f"preset {preset_id!r} must be a YAML list")
        return rows

    # ---- 挂载 ----

    async def mount(self, agent_ctx: Any, preset_id: str) -> Dict[str, Any]:
        """
        把 preset 行挂到 agent 作用域（发布前调用）。

        先为作用域建一个「父委托」的局部工具运行时：preset 行里注册的工具
        落在该作用域层，对外不可见（isolate realm 的作用域化等价物）。

        :return: {"preset": id, "rows": 行数}。
        :raises LoaderError: preset 不存在或组合不可用。
        """
        # 作用域内工具隔离：局部 ToolRuntime 委托根运行时查询
        if agent_ctx.has("tools") and "tools" not in agent_ctx._instances:
            from ..tools import ToolRuntime
            local_runtime = ToolRuntime(agent_ctx, {},
                                        parent=agent_ctx.get("tools"))
            local_runtime.apply(agent_ctx)
        rows = self.read(preset_id)
        tree = PluginTree(agent_ctx)
        for row in rows:
            tree.add_bundle_rows([row], layer_name=f"preset:{preset_id}")
        await tree.mount()
        key = id(agent_ctx)
        self._mounted[key] = (tree, preset_id)

        async def dispose_tree() -> None:
            # 只卸载插件，不销毁作用域（作用域归 agent 生命周期）
            await tree.dispose(dispose_ctx=False)
        agent_ctx.effect(lambda: dispose_tree())
        agent_ctx.effect(lambda: self._mounted.pop(key, None))
        return {"preset": preset_id, "rows": len(rows)}

    async def recompose(self, agent_ctx: Any, preset_id: str) -> Dict[str, Any]:
        """
        运行期换绑 preset：先卸旧树再挂新树（TS 版 recompose 的等价物）。

        只卸载插件、不销毁作用域（dispose_ctx=False）；作用域级局部
        ToolRuntime 保留（新树注册的工具落到同一层）。**调用方**负责
        「agent 尚未产生任何对外输出」的检查（本方法不拦截）。

        :return: {"preset": id, "rows": 行数, "recomposed": True}。
        :raises LoaderError: preset 不存在或组合不可用。
        """
        key = id(agent_ctx)
        record = self._mounted.pop(key, None)
        if record is not None:
            tree, _old_id = record
            await tree.dispose(dispose_ctx=False)
        result = await self.mount(agent_ctx, preset_id)
        result["recomposed"] = True
        return result

    def composed_preset(self, agent_ctx: Any) -> Optional[str]:
        """agent 作用域当前挂载的 preset id（无则 None）。"""
        record = self._mounted.get(id(agent_ctx))
        return record[1] if record else None

    def close(self) -> None:
        self._mounted.clear()
