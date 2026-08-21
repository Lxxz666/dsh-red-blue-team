"""
dsh.llm.adapters —— LLM 接缝：调用配置、请求、适配器抽象与注册表（ctx.llm）。

这是能力缝（Definition + Provider + Consumer）的典型实现:

- :class:`LlmAdapter` 是 Service Definition（Provider 实现它）；
- :class:`LlmRuntime`（ctx.llm）是注册表；
- Consumer 是 agent-loop（经 ``llm/stream`` waterfall 派发）。

``llm/stream`` 是 waterfall：默认 next 走适配器流，监听者（重放/指标/checkpoint）
可在两侧包装。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from ..errors import LlmFailure
from ..kernel import Service
from ..tools.pipeline import AbortSignal
from .messages import Message
from .stream import StreamChunk

log = logging.getLogger("dsh.llm")


@dataclass(frozen=True)
class LlmCallConfig:
    """一次模型调用的配置（agent/request waterfall 的产出）。"""

    provider: str
    model: str
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"provider": self.provider, "model": self.model}
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.reasoning_effort is not None:
            out["reasoning_effort"] = self.reasoning_effort
        if self.extra:
            out["extra"] = self.extra
        return out


@dataclass(frozen=True)
class LlmRequest:
    """组装好的模型请求（派生历史 + 工具 schema + system prompt）。"""

    config: LlmCallConfig
    messages: List[Message]
    tools: List[Dict[str, Any]] = field(default_factory=list)
    system: Optional[str] = None
    signal: Optional[AbortSignal] = None

    def tool_schemas(self) -> List[Dict[str, Any]]:
        """OpenAI 兼容 tools 数组（函数调用格式）。"""
        return [schema for schema in self.tools]


class LlmAdapter(ABC):
    """模型适配器抽象（Provider 契约）。"""

    name: str = "adapter"
    """provider 路由名。"""

    supports_reasoning: bool = False

    context_window: Optional[int] = None
    """路由可广告的最大上下文（token）。None = 不广告（request/context 记 absent）。"""

    @abstractmethod
    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
        """
        以块流产出一次请求的回复。

        :param request: 组装好的请求。
        :yields: StreamChunk（必须以 finish 块收尾；失败抛 LlmFailure）。
        """
        raise NotImplementedError
        yield  # pragma: no cover

    async def _watch_signal(self, request: LlmRequest) -> None:
        """适配器可选地观察取消信号（循环负责在取消时停止消费）。"""


class LlmRuntime(Service):
    """LLM 适配器注册表（ctx.llm）。"""

    provides = "llm"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._adapters: Dict[str, LlmAdapter] = {}

    def apply(self, ctx) -> None:
        ctx.set("llm", self)

    def register_adapter(self, adapter: LlmAdapter):
        """
        注册一个 provider 适配器（同名覆盖 = 换 provider 换产品行为）。

        :return: 注销函数。
        """
        self._adapters[adapter.name] = adapter
        self.ctx.events.emit("llm/adapters-updated")

        def unregister() -> None:
            if self._adapters.pop(adapter.name, None) is adapter:
                self.ctx.events.emit("llm/adapters-updated")
        return self.ctx.effect(unregister)

    def get_adapter(self, provider: str) -> Optional[LlmAdapter]:
        """按 provider 路由名取适配器。"""
        return self._adapters.get(provider)

    def providers(self) -> List[str]:
        """已注册 provider 列表。"""
        return list(self._adapters.keys())

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
        """
        经 ``llm/stream`` waterfall 派发一次请求（默认 = 适配器流）。

        监听者接收 ``(request, next)``：``next()`` 返回下游异步生成器，
        监听者可包装/替换后返回自己的异步生成器（或任何可异步迭代对象）。

        :raises LlmFailure: 适配器不存在或请求失败。
        """
        adapter = self.get_adapter(request.config.provider)
        if adapter is None:
            raise LlmFailure(f"no adapter registered for provider "
                             f"{request.config.provider!r}", code="NO_ADAPTER",
                             provider=request.config.provider)

        async def default_stream():
            async for chunk in adapter.stream(request):
                yield chunk

        agen = await self.ctx.events.waterfall(
            "llm/stream", request, default=default_stream)
        async for chunk in agen:
            yield chunk

    def close(self) -> None:
        self._adapters.clear()
