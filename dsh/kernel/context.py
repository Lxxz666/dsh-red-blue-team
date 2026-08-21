"""
dsh.kernel.context —— Context：服务仓库 + 可逆效应 + 事件派发门面。

对应 Cordis 的「Context 是服务仓库」理念：

- ``ctx.provide(key, factory, *deps)`` 注册服务工厂，首次 ``ctx.get(key)`` 时惰性实例化；
- ``ctx.<key>`` 属性语法糖可直接取服务（如 ``ctx.tools``）；
- 子作用域 ``ctx.scoped(name)`` 共享事件总线、可注册局部服务（用于逐 agent 作用域）；
- 一切注册都通过 ``ctx.effect(disposer)`` 安装，``ctx.dispose()`` 时逆序回滚（HMR 的基础）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, TypeVar

from .events import EventBus, Handler
from ..errors import ContextError, ServiceNotFoundError

log = logging.getLogger("dsh.kernel")

Disposer = Callable[[], Any]
T = TypeVar("T")


class Context:
    """服务仓库 + 事件总线门面。"""

    def __init__(self, name: str = "root", parent: Optional["Context"] = None,
                 bus: Optional[EventBus] = None) -> None:
        self.name = name
        self.parent = parent
        self.events = bus if bus is not None else (parent.events if parent else EventBus())
        self._providers: Dict[str, tuple] = {}      # key -> (factory, deps)
        self._instances: Dict[str, Any] = {}
        self._effects: List[Disposer] = []
        self._disposed = False

    # ---- 服务注册与解析 ----

    def provide(self, key: str, factory: Callable[["Context"], Any],
                *deps: str) -> None:
        """
        注册一个服务工厂。惰性实例化：首次 :meth:`get` 时调用工厂。
        ``deps`` 为依赖的其他服务 key（用于文档与校验，实例化时先解析它们）。

        :raises ContextError: 上下文已销毁时抛出。
        """
        self._check_open()
        self._providers[key] = (factory, deps)
        self._instances.pop(key, None)

    def set(self, key: str, instance: Any) -> None:
        """直接注入已构造的服务实例（替代工厂注册）。"""
        self._check_open()
        self._instances[key] = instance

    def get(self, key: str) -> Any:
        """
        解析服务：优先本层实例 → 本层工厂 → 父层。

        :raises ServiceNotFoundError: 服务不存在。
        """
        if key in self._instances:
            return self._instances[key]
        if key in self._providers:
            factory, deps = self._providers[key]
            for dep in deps:
                self.get(dep)  # 确保依赖先实例化
            instance = factory(self)
            self._instances[key] = instance
            return instance
        if self.parent is not None:
            return self.parent.get(key)
        raise ServiceNotFoundError(key)

    def has(self, key: str) -> bool:
        """服务是否可解析。"""
        if key in self._instances or key in self._providers:
            return True
        return self.parent.has(key) if self.parent else False

    def __getattr__(self, key: str) -> Any:
        """``ctx.tools`` 语法糖：把属性访问映射到服务解析。"""
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self.get(key)
        except ServiceNotFoundError as exc:
            raise AttributeError(key) from exc

    # ---- 事件门面 ----

    def on(self, name: str, handler: Handler, *, prepend: bool = False) -> Disposer:
        """注册事件监听器（等价于 ``self.events.on``，返回注销函数）。"""
        return self.events.on(name, handler, prepend=prepend)

    def emit(self, name: str, *args: Any):
        return self.events.emit(name, *args)

    async def parallel(self, name: str, *args: Any):
        return await self.events.parallel(name, *args)

    async def waterfall(self, name: str, *args: Any, default=None):
        return await self.events.waterfall(name, *args, default=default)

    async def serial(self, name: str, *args: Any):
        return await self.events.serial(name, *args)

    # ---- 作用域 ----

    def scoped(self, name: str, parent: Optional["Context"] = None) -> "Context":
        """
        创建子作用域：共享事件总线、继承父服务，可在本层注册局部服务。

        对应 dsh 的 per-agent scope：子代理在 ``agent.ctx`` 上注册局部工具/分节，
        不影响其他 agent。``parent`` 省略 = 以本 ctx 为父；传父 agent 的 ctx
        即可让子代理继承父作用域的注册（preset/join 语义）。
        """
        self._check_open()
        return Context(name=name, parent=self if parent is None else parent,
                       bus=self.events)

    # ---- 可逆效应 ----

    def effect(self, disposer: Disposer) -> Disposer:
        """
        登记一个可逆副作用：dispose 时逆序调用。

        :return: 传入的 disposer（可继续作为注销函数单独调用）。
        """
        self._check_open()
        self._effects.append(disposer)
        return disposer

    # ---- 生命周期 ----

    def _check_open(self) -> None:
        if self._disposed:
            raise ContextError(f"context {self.name!r} already disposed")

    async def dispose(self) -> None:
        """逆序执行全部 disposer（可 await 的会被 await），然后关闭子实例。"""
        if self._disposed:
            return
        self._disposed = True
        for disposer in reversed(self._effects):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("disposer failed during context %r disposal", self.name)
        for instance in reversed(list(self._instances.values())):
            close = getattr(instance, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    log.exception("service close failed during context %r disposal", self.name)
