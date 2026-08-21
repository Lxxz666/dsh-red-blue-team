"""
dsh.kernel.service —— Service 插件基类与插件解析。

插件形态（对应 Cordis 的 Service）：

- 类插件：继承 :class:`Service`，声明 ``provides``（它提供的 ctx.<key>）与 ``inject``（依赖的服务 key）；
- 函数插件：``apply(ctx) -> Disposer|None`` 的普通函数。

``provides`` 用于 Loader 按依赖拓扑排序：一个插件的 ``inject`` 名必须由某插件 ``provides``。
"""
from __future__ import annotations

from typing import Any, Callable, ClassVar, Optional, Tuple, Union

from .context import Context, Disposer

#: 函数插件签名
PluginFunction = Callable[[Context], Optional[Disposer]]


class Service:
    """插件基类。子类声明 ``provides`` / ``inject`` 并实现 :meth:`apply`。"""

    name: ClassVar[str] = ""
    """插件名（配置行 id 未给定时用它）。"""

    provides: ClassVar[Optional[str]] = None
    """本插件提供的 ctx.<key> 服务名（Loader 拓扑排序用）。"""

    inject: ClassVar[Tuple[str, ...]] = ()
    """本插件依赖的服务 key（这些服务必须由其它插件提供）。"""

    def __init__(self, ctx: Context, config: Optional[dict] = None) -> None:
        self.ctx = ctx
        self.config = config or {}
        self._disposer: Optional[Disposer] = None

    def apply(self, ctx: Context) -> Optional[Disposer]:
        """
        挂载插件：完成注册（服务、事件监听器、工具、prompt 分节……）。

        返回的 disposer 由 Loader 记录，卸载时调用；内部用 ``ctx.effect``
        注册的副作用由 Context 统一回滚。

        :return: 可选的额外清理函数。
        """
        return None

    async def start(self) -> None:
        """可选的异步启动钩子（apply 之后调用）。"""

    def reconfigure(self, config: Optional[dict]) -> bool:
        """
        HMR 钩子：用新配置热更新已挂载实例（配置热重载的消费者接口）。

        默认不支持（返回 False = 需要重启）；实现者须自行保证失败时不残留
        半更新状态（watcher 会在返回 False 或抛异常时回滚旧配置）。

        :return: True 表示已应用新配置；False 表示不支持/拒绝。
        """
        return False

    def close(self) -> None:
        """卸载钩子：调用 apply 返回的 disposer。"""
        if self._disposer is not None:
            self._disposer()
            self._disposer = None

    def __repr__(self) -> str:
        return f"<Service {self.name or type(self).__name__}>"


PluginTarget = Union[type, Service, PluginFunction]
"""插件目标：Service 子类、Service 实例、或函数插件。"""


def is_service_class(target: PluginTarget) -> bool:
    """判断目标是 Service 子类。"""
    return isinstance(target, type) and issubclass(target, Service)


def plugin_inject(target: PluginTarget) -> Tuple[str, ...]:
    """读取插件声明的依赖服务名（函数插件无依赖）。"""
    if is_service_class(target):
        return tuple(getattr(target, "inject", ()))
    return ()


def plugin_provides(target: PluginTarget) -> Optional[str]:
    """读取插件提供的服务名。"""
    if is_service_class(target):
        return getattr(target, "provides", None)
    return None


def plugin_name(target: PluginTarget) -> str:
    """读取插件名（类插件的 ``name`` 或类名）。"""
    if is_service_class(target):
        return getattr(target, "name", None) or target.__name__
    return getattr(target, "__name__", repr(target))
