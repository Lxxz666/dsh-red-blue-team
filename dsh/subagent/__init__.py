"""dsh.subagent —— 子代理域。"""
from .subagent import (InProcessProviderPlugin, InProcessSubagent,
                       SubagentRegistry, ToolSubagentPlugin,
                       build_subagent_tool)

__all__ = ["SubagentRegistry", "InProcessSubagent",
           "InProcessProviderPlugin", "ToolSubagentPlugin",
           "build_subagent_tool"]
