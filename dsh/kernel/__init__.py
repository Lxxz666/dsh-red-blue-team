"""
dsh.kernel —— Cordis 式插件内核。

导出: Context(服务仓库/事件门面/可逆效应)、EventBus(四种派发)、
Service(插件基类)、Entry/apply_patch/resolve_target(配置)、PluginTree(树组装挂载)。
"""
from .context import Context, Disposer
from .events import EventBus, Handler
from .loader import Entry, apply_patch, resolve_target
from .service import (PluginFunction, PluginTarget, Service, is_service_class,
                      plugin_inject, plugin_name, plugin_provides)
from .tree import MountedPlugin, PluginTree

__all__ = [
    "Context", "Disposer", "EventBus", "Handler",
    "Entry", "apply_patch", "resolve_target",
    "PluginFunction", "PluginTarget", "Service",
    "is_service_class", "plugin_inject", "plugin_name", "plugin_provides",
    "MountedPlugin", "PluginTree",
]
