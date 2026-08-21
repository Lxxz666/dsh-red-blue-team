"""
dsh.prompt.system_prompt —— System Prompt 组装（ctx.systemPrompt）。

与 TS 版对齐:

- ``section(name, order, text|callable, complete=False)``：分节按 order 升序拼接；
  约定 -100=harness 身份、0=persona、工具指引 100–199；
  一个生效的 ``complete`` 节成为唯一 system prompt（组装仍先跑 waterfall 以解析变量/工具）；
- ``context``：动态上下文（cache-safe，变化时才快照进日志）；
- ``variable(name, provider)``：`{{name}}` 插值变量（作用域值遮蔽全局）；
- ``tools(provider)``：工具 schema provider（默认来自 ctx.tools.schemas(scope)）；
- ``assemble``：组装 + ``system-prompt/assemble`` waterfall（返回值为权威）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..kernel import Service

log = logging.getLogger("dsh.prompt")

_VARIABLE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


@dataclass
class PromptSection:
    """一个 system prompt 分节（注册表输入）。"""

    name: str
    order: int
    text: Any  # str 或 callable(ctx) -> str
    complete: bool = False

    def resolve(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
        if callable(self.text):
            return str(self.text(assemble_ctx) or "")
        return str(self.text or "")


@dataclass
class PromptContext:
    """动态上下文贡献（cache-safe 对应物）。"""

    name: str
    order: int
    text: Any

    def resolve(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
        if callable(self.text):
            return str(self.text(assemble_ctx) or "")
        return str(self.text or "")


class SystemPromptService(Service):
    """System Prompt 组装注册表（ctx.systemPrompt）。"""

    provides = "systemPrompt"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._global_sections: Dict[str, PromptSection] = {}
        self._global_contexts: Dict[str, PromptContext] = {}
        self._global_variables: Dict[str, Callable] = {}
        self._global_tool_providers: List[Callable] = []
        self._scoped: Dict[Any, Dict[str, Any]] = {}
        self._suppressed_scopes: set = set()

    def apply(self, ctx) -> None:
        ctx.set("systemPrompt", self)

    def _scope_state(self, scope: Any) -> Dict[str, Any]:
        return self._scoped.setdefault(scope, {
            "sections": {}, "contexts": {}, "variables": {},
            "tool_providers": [], "suppress": False,
        })

    # ---- 注册 ----

    def section(self, section: PromptSection, scope: Any = None):
        """注册有序分节（作用域同名遮蔽全局）。"""
        table = self._global_sections if scope is None \
            else self._scope_state(scope)["sections"]
        if section.name in table:
            raise ValueError(f"duplicate prompt section: {section.name}")
        table[section.name] = section
        self.ctx.events.emit("system-prompt/change")

        def unregister() -> None:
            if table.pop(section.name, None) is not None:
                self.ctx.events.emit("system-prompt/change")
        return self.ctx.effect(unregister)

    def context(self, context: PromptContext, scope: Any = None):
        """注册动态上下文贡献。"""
        table = self._global_contexts if scope is None \
            else self._scope_state(scope)["contexts"]
        if context.name in table:
            raise ValueError(f"duplicate prompt context: {context.name}")
        table[context.name] = context

        def unregister() -> None:
            table.pop(context.name, None)
        return self.ctx.effect(unregister)

    def variable(self, name: str, provider: Callable, scope: Any = None):
        """注册 `{{name}}` 插值变量（provider 每次组装求值，可返回 None）。"""
        if not _VARIABLE_RE.fullmatch("{{" + name + "}}"):
            raise ValueError(f"invalid variable name: {name!r}")
        table = self._global_variables if scope is None \
            else self._scope_state(scope)["variables"]
        if name in table:
            raise ValueError(f"duplicate prompt variable: {name}")
        table[name] = provider

        def unregister() -> None:
            table.pop(name, None)
        return self.ctx.effect(unregister)

    def tools(self, provider: Callable, scope: Any = None):
        """注册工具 schema provider（每次组装求值）。"""
        lst = self._global_tool_providers if scope is None \
            else self._scope_state(scope)["tool_providers"]
        lst.append(provider)

        def unregister() -> None:
            try:
                lst.remove(provider)
            except ValueError:
                pass
        return self.ctx.effect(unregister)

    def suppress_runtime_context(self, scope: Any = None):
        """抑制动态上下文贡献（不改动拥有/执行这些事实的服务）。"""
        self._scope_state(scope)["suppress"] = True

        def restore() -> None:
            self._scope_state(scope)["suppress"] = False
        return self.ctx.effect(restore)

    # ---- 组装 ----

    _SCOPE_KEYS = {"_global_sections": "sections",
                   "_global_contexts": "contexts",
                   "_global_variables": "variables"}

    def _collect(self, scope: Any, key: str) -> List[Any]:
        """全局 + 作用域（同名遮蔽）的注册合并。"""
        if scope is None:
            return list(getattr(self, key).values())
        merged: Dict[str, Any] = dict(getattr(self, key))
        merged.update(self._scope_state(scope)[self._SCOPE_KEYS[key]])
        return list(merged.values())

    def _default_tool_provider(self, scope: Any) -> List[Dict[str, Any]]:
        if self.ctx.has("tools"):
            return self.ctx.tools.schemas(scope)
        return []

    def _tool_schemas(self, scope: Any) -> List[Dict[str, Any]]:
        schemas = self._default_tool_provider(scope)
        for provider in self._global_tool_providers:
            schemas = list(schemas) + list(provider(None) or [])
        if scope is not None:
            for provider in self._scope_state(scope)["tool_providers"]:
                schemas = list(schemas) + list(provider(scope) or [])
        # 去重（按 function.name）
        seen: Dict[str, Any] = {}
        for schema in schemas:
            name = schema.get("function", {}).get("name")
            if name:
                seen[name] = schema
        return list(seen.values())

    def _resolve_variables(self, text: str, scope: Any,
                           assemble_ctx: Optional[Dict[str, Any]]) -> str:
        variables: Dict[str, Callable] = dict(self._global_variables)
        if scope is not None:
            variables.update(self._scope_state(scope)["variables"])

        def replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            provider = variables.get(name)
            if provider is None:
                raise ValueError(
                    f"prompt references undefined variable {name!r}")
            value = provider(assemble_ctx)
            return "" if value is None else str(value)
        return _VARIABLE_RE.sub(replace, text)

    def _build(self, scope: Any,
               assemble_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """默认组装（system-prompt/assemble waterfall 的末端）。"""
        sections = self._collect(scope, "_global_sections")
        complete_sections = [s for s in sections if s.complete]
        if len(complete_sections) > 1:
            raise ValueError("more than one effective complete section")
        chosen = complete_sections[0] if complete_sections else None
        if chosen is not None:
            text = chosen.resolve(assemble_ctx)
        else:
            ordered = sorted(sections, key=lambda s: s.order)
            text = "\n\n".join(
                resolved for s in ordered
                if (resolved := s.resolve(assemble_ctx)))
        text = self._resolve_variables(text, scope, assemble_ctx)
        if scope is not None and not self._scope_state(scope)["suppress"]:
            contexts = self._collect(scope, "_global_contexts")
            for ctx_item in sorted(contexts, key=lambda c: c.order):
                resolved = ctx_item.resolve(assemble_ctx)
                if resolved:
                    text += "\n\n" + resolved
        return {"text": text, "tools": self._tool_schemas(scope)}

    async def assemble(self, scope: Any = None,
                       signal: Any = None) -> Dict[str, Any]:
        """
        组装一次 system prompt + 工具 schema（waterfall 返回值为权威）。

        :return: ``{"text": str, "tools": [模型 schema]}``。
        """
        assemble_ctx = {"scope": scope, "signal": signal}
        return await self.ctx.events.waterfall(
            "system-prompt/assemble", assemble_ctx,
            default=lambda: self._build(scope, assemble_ctx))

    def close(self) -> None:
        self._global_sections.clear()
        self._global_contexts.clear()
        self._global_variables.clear()
        self._scoped.clear()
