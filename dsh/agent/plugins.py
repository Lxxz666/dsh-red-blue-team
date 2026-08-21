"""
dsh.agent.plugins —— 默认 agent 选项插件（对应 agent-default-model）。

注册 ctx.agentDefaultModel；AgentLoop 的 _default_config 在 agent options
未指定 provider/model 时回退到这里。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..kernel import Service


class DefaultOptions:
    """默认模型选择（组合条目可直接使用；实时读 ctx.settings 的用户层）。"""

    def __init__(self, config: Optional[dict] = None, ctx: Any = None) -> None:
        self._config = config or {}
        self._ctx = ctx

    def current_selection(self) -> Dict[str, Any]:
        """
        默认选择 = 组合配置 + settings 的 ``agent_default_model``（用户层优先）。

        对应 TS 版 agent-default-model：组合条目在无 settings provider 时仍可用，
        挂载后其用户层被实时读取。
        """
        selection = dict(self._config)
        if self._ctx is not None and self._ctx.has("settings"):
            user_layer = self._ctx.settings.get("agent_default_model")
            if isinstance(user_layer, dict):
                for key, value in user_layer.items():
                    if value is not None:
                        selection[key] = value
        return selection

    def to_json(self) -> Dict[str, Any]:
        return self.current_selection()


class DefaultOptionsPlugin(Service):
    """注册 ctx.agentDefaultModel。"""

    provides = None

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)

    def apply(self, ctx) -> None:
        ctx.set("agentDefaultModel", DefaultOptions(self.config, ctx))

        def cleanup() -> None:
            pass
        return cleanup
