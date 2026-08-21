"""
dsh.llm.plugins —— 适配器注册插件（bundle 配置行指向这里）。

- MockAdapterPlugin: 注册 ``mock`` provider（无密钥演示/测试）；
- DeepSeekAdapterPlugin: 注册 ``deepseek`` provider（读 DEEPSEEK_API_KEY）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..kernel import Service
from .adapters import LlmRuntime
from .deepseek import DeepSeekAdapter
from .mock import MockAdapter


class MockAdapterPlugin(Service):
    """注册 mock 适配器。"""

    inject = ("llm",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        adapter = MockAdapter(script=list((self.config or {}).get("script") or []),
                              text=(self.config or {}).get("text"))
        self._disposer = ctx.llm.register_adapter(adapter)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup


class DeepSeekAdapterPlugin(Service):
    """注册 DeepSeek 适配器（provider=deepseek）。"""

    inject = ("llm",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None
        self._adapter: Optional[DeepSeekAdapter] = None

    def apply(self, ctx) -> None:
        config = self.config or {}
        api_key = config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        # 凭据缝回退：env 未提供时读 ctx.credentials 的 "deepseek"
        if not api_key and ctx.has("credentials"):
            api_key = ctx.credentials.get("deepseek") or ""
        self._adapter = DeepSeekAdapter(
            api_key=api_key, base_url=config.get("base_url"),
            model=config.get("model"), timeout=float(config.get("timeout", 300)))
        self._disposer = ctx.llm.register_adapter(self._adapter)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup

    def close(self) -> None:
        if self._adapter is not None:
            import asyncio
            try:
                asyncio.get_running_loop().create_task(self._adapter.close())
            except RuntimeError:
                pass
