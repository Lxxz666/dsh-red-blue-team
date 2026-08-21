"""
dsh.kernel.events —— EventBus：Cordis 式类型化事件总线。

四种派发模式（与 docs/cordis-primer.md 的表格一一对应）:

===========  ========  ============================  ==========
模式          是否await  派发顺序                      有返回值
===========  ========  ============================  ==========
emit         否        注册序（fire-and-forget）       无
waterfall    是        注册序（洋葱中间件，next()委派）  有
parallel     是        并行                           无
serial       是        顺序                           有
===========  ========  ============================  ==========

emit 的异步监听器会被调度为后台 task（异常被隔离记录），同步监听器立即调用；
parallel 汇聚全部监听器（异常隔离）；waterfall 的异常向上传播（短路语义由监听器决定）；
serial 顺序执行并返回结果列表。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

log = logging.getLogger("dsh.kernel")

Handler = Callable[..., Any]
"""事件监听器：同步函数或协程函数均可。"""


def _is_coro(fn: Callable[..., Any]) -> bool:
    """判断函数是否返回协程（含 functools.partial / 装饰器包装后的对象）。"""
    return inspect.iscoroutinefunction(fn) or bool(getattr(fn, "_is_coroutine", False))


async def _maybe_await(fn: Callable[..., Any], args: tuple) -> Any:
    """统一调用：同步直接调、异步 await。"""
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


class EventBus:
    """轻量事件总线：按名字登记监听器并按四种模式派发。"""

    def __init__(self) -> None:
        self._listeners: Dict[str, List[Handler]] = {}

    # ---- 注册 ----

    def on(self, name: str, handler: Handler, *, prepend: bool = False) -> Callable[[], None]:
        """
        注册监听器。

        :param name: 事件名（如 ``tools/pre-execute``）。
        :param handler: 监听器（同步或协程函数）。
        :param prepend: True 时插到队首（先于普通注册执行）。
        :return: 注销函数（幂等）。
        """
        lst = self._listeners.setdefault(name, [])
        if prepend:
            lst.insert(0, handler)
        else:
            lst.append(handler)

        def off() -> None:
            try:
                lst.remove(handler)
            except ValueError:
                pass

        return off

    def listener_count(self, name: str) -> int:
        """返回某事件的监听器数量（调试/测试用）。"""
        return len(self._listeners.get(name, []))

    def snapshot(self, name: str) -> List[Handler]:
        """返回某事件当前监听器快照（派发用，注册与派发解耦）。"""
        return list(self._listeners.get(name, []))

    # ---- emit ----

    def emit(self, name: str, *args: Any) -> List[asyncio.Task]:
        """
        emit 派发：fire-and-forget，监听器异常被隔离记录，不影响调用者。

        :return: 异步监听器对应的后台 task 列表（调用者可选择 await 或忽略）。
        """
        tasks: List[asyncio.Task] = []
        for handler in self.snapshot(name):
            if _is_coro(handler):
                tasks.append(asyncio.get_running_loop().create_task(
                    self._contained(handler, name, args)))
            else:
                try:
                    handler(*args)
                except Exception:
                    log.exception("listener %s on event %r failed",
                                  getattr(handler, "__name__", handler), name)
        return tasks

    async def _contained(self, handler: Handler, name: str, args: tuple) -> None:
        """在 task 内执行异步监听器并隔离异常。"""
        try:
            await handler(*args)
        except Exception:
            log.exception("async listener %s on event %r failed",
                          getattr(handler, "__name__", handler), name)

    # ---- parallel ----

    async def parallel(self, name: str, *args: Any) -> List[Any]:
        """
        parallel 派发：全部监听器并发执行并等待全部结束；单个失败被隔离。

        :return: 各监听器返回值列表（同步监听器的返回值原样保留）。
        """
        handlers = self.snapshot(name)

        async def _run(handler: Handler) -> Any:
            try:
                return await _maybe_await(handler, args)
            except Exception:
                log.exception("listener %s on event %r failed",
                              getattr(handler, "__name__", handler), name)
                return None

        return await asyncio.gather(*(_run(h) for h in handlers))

    # ---- waterfall ----

    async def waterfall(self, name: str, *args: Any,
                        default: Union[Any, Callable[[], Any]] = None) -> Any:
        """
        waterfall（洋葱中间件）派发。

        每个监听器签名 ``handler(*args, next)``：
        调用 ``await next()`` 委派给下一个监听器（其返回值向上传播）；
        不调用 ``next()`` 直接返回即为短路。

        :param default: 监听器链的末端返回值。若为 callable 则调用它生成默认值
            （用于「默认放行」这类动态决策）。
        :return: 链的最终返回值（最外层监听器的返回值）。
        :raises: 监听器抛出的异常原样向上传播（waterfall 不隔离异常）。
        """
        handlers = self.snapshot(name)
        index = 0

        async def next_() -> Any:
            nonlocal index
            if index >= len(handlers):
                if callable(default):
                    result = default()
                else:
                    result = default
                if inspect.isawaitable(result):
                    return await result
                return result
            handler = handlers[index]
            index += 1
            return await _maybe_await(handler, (*args, next_))

        return await next_()

    # ---- serial ----

    async def serial(self, name: str, *args: Any) -> List[Any]:
        """
        serial 派发：按注册序依次 await 每个监听器（无 next 参数），异常向上传播。

        :return: 各监听器返回值列表。
        """
        handlers = self.snapshot(name)
        results: List[Any] = []
        for handler in handlers:
            results.append(await _maybe_await(handler, args))
        return results
