"""
dsh.cordis.inspect —— Cordis 只读检查目录（对应 TS 版 inspect-registry.ts 的
host 侧）。

- 提供者 = {id, description, methods}；method = {name, description,
  input_schema, output_schema, call}（call 为可调用对象，返回 lossless JSON）；
- host 内建提供者 ``harness``：ctx/harness/console 三个签名文档方法
  （HOST_BUILTIN_INSPECTION 的可查询形态）；
- ``cordis_inspect_list`` 列目录（platform='host'），``cordis_inspect_query``
  解析一个只读查询：输入按 input_schema 校验（invalid-input），输出按
  output_schema 校验（provider-error），handler 异常 → provider-error。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..errors import ToolArgsError, ToolOutputError
from ..kernel import Service
from ..session.events import is_json_value
from ..tools.schema import validate_value
from .sandbox import HOST_BUILTIN_INSPECTION
from .types import inspect_resolution_fail, inspect_resolution_ok

log = logging.getLogger("dsh.cordis")

BUILTIN_PROVIDER_ID = "harness"


class CordisInspectRegistryService(Service):
    """只读检查提供者目录（ctx.cordisInspect）。"""

    provides = "cordisInspect"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._providers: Dict[str, Dict[str, Any]] = {}
        self._register_builtin()

    def apply(self, ctx) -> None:
        ctx.set("cordisInspect", self)

    def _register_builtin(self) -> None:
        def doc_method(name: str) -> Callable[..., Any]:
            def call(input: Dict[str, Any]) -> Dict[str, Any]:
                entry = next(e for e in HOST_BUILTIN_INSPECTION
                             if e["name"] == name)
                return {"name": entry["name"],
                        "description": entry["description"],
                        "signatures": entry["signatures"]}
            return call
        methods = [{"name": entry["name"],
                    "description": entry["description"],
                    "input_schema": {"type": "object",
                                     "additionalProperties": False,
                                     "properties": {}},
                    "output_schema": {"type": "object"},
                    "call": doc_method(entry["name"])}
                   for entry in HOST_BUILTIN_INSPECTION]
        self._providers[BUILTIN_PROVIDER_ID] = {
            "id": BUILTIN_PROVIDER_ID,
            "description": "动态包 host 沙箱暴露的宿主面（自省用）。",
            "methods": {m["name"]: m for m in methods},
        }

    def register_provider(self, provider_id: str, description: str,
                          methods: List[Dict[str, Any]]):
        """注册一个 host 提供者（同名替换）。"""
        if provider_id in self._providers and provider_id != BUILTIN_PROVIDER_ID:
            raise ValueError(f"duplicate inspect provider: {provider_id}")
        table = {m["name"]: dict(m) for m in methods}
        self._providers[provider_id] = {"id": provider_id,
                                        "description": description,
                                        "methods": table}

        def unregister() -> None:
            self._providers.pop(provider_id, None)
        return self.ctx.effect(unregister)

    def list(self) -> List[Dict[str, Any]]:
        """全部提供者目录（platform='host'）。"""
        return [{"platform": "host", "id": p["id"],
                 "description": p["description"],
                 "methods": [{"name": m["name"],
                              "description": m["description"],
                              "inputSchema": m["input_schema"],
                              "outputSchema": m["output_schema"]}
                             for m in p["methods"].values()]}
                for p in self._providers.values()]

    async def query(self, provider_id: str, method: str,
                    input: Any = None) -> Dict[str, Any]:
        """解析一个只读查询（输入/输出 schema 校验 + handler 异常归一化）。"""
        provider = self._providers.get(provider_id)
        if provider is None:
            return inspect_resolution_fail(
                "provider-missing", f"no inspect provider {provider_id!r}")
        method_entry = provider["methods"].get(method)
        if method_entry is None:
            return inspect_resolution_fail(
                "method-missing",
                f"provider {provider_id!r} has no method {method!r}")
        try:
            validated = validate_value(
                input or {}, method_entry["input_schema"],
                error_cls=ToolArgsError,
                what=f"input for {provider_id}.{method}")
        except ToolArgsError as exc:
            return inspect_resolution_fail("invalid-input", str(exc))
        try:
            result = method_entry["call"](validated)
            import asyncio
            if asyncio.iscoroutine(result):
                result = await result
            data = validate_value(
                result, method_entry["output_schema"],
                error_cls=ToolOutputError,
                what=f"output of {provider_id}.{method}")
        except ToolError as exc:
            return inspect_resolution_fail("provider-error", str(exc))
        except Exception as exc:
            log.exception("inspect provider %s.%s crashed", provider_id,
                          method)
            return inspect_resolution_fail(
                "provider-error", f"{type(exc).__name__}: {exc}")
        if not is_json_value(data):
            return inspect_resolution_fail(
                "provider-error", "inspect result is not lossless JSON")
        return inspect_resolution_ok(data)

    def close(self) -> None:
        self._providers.clear()
        self._register_builtin()
