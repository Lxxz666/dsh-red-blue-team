"""
dsh.tools.registry —— ToolRuntime（ctx.tools）：作用域注册表 + 守卫执行管线。

与 TS 版 ToolRuntime 对齐:

- 注册分全局层与作用域层（作用域注册遮蔽全局；作用域 = agent 的 ctx 名或任意 hashable key）；
- ``schemas()`` 白名单投影，执行/展示回调绝不泄漏；
- ``restrict`` 只过滤继承的全局工具（作用域自有注册豁免）；
- ``execute`` 编排 5 段管线（pre-execute → guards → execute → post-execute → result）；
- ``guard`` 注册单调最终拒绝策略（无 allow 结果，顺序无法翻案）。

Code Mode（第六批补齐）:

- ``mode`` 配置（native/code/both）+ ``presentAs`` 作用域声明；``code`` 下模型线束
  坍缩为保留的 ``run_code`` 传输，可见工具的直接调用在策略管线**之前**以
  UNKNOWN_TOOL 确定性拒绝（带回到程序的路线提示）；嵌套子调用（parent 令牌）豁免；
- ``run_code`` 工具、tools:sdk / tools:code-only 提示分节、tools/code-dispatch-log
  waterfall 见 ``dsh/code/``；
- 分段调度：``prepare``（参数物化 + pre-execute + guards，有序）→ ``dispatch``
  （around-dispatch + 工具体，可并发）→ ``finalize``（post-execute + finalize_content
  + tools/result，有序提交序）。run_code 桥复用同一三段以保持原生并发契约。

作用域委托（预设隔离 bug 修复）:

- ``ToolRuntime(parent=根运行时)`` 的局部运行时是**父运行时作用域层的视图**：
  注册/限制/守卫/查询/执行全部委托到父层的 ``_scoped[ctx.name]``。这样 preset
  挂到 agent 作用域的工具既对外不可见，又能被循环经根运行时按 scope 正常执行
  （此前局部运行时自持层，循环经根运行时执行时查不到这些工具）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import (ServiceNotFoundError, ToolArgsError, ToolError,
                      ToolNotFoundError)
from ..kernel import Service
from ..session.events import is_json_value
from .definition import ToolDefinition
from .pipeline import (AbortSignal, AcceptDecision, AllowDecision,
                       AskDecision, BlockDecision, DenyDecision,
                       PostToolDecision, PreToolDecision, ToolExecution,
                       ToolExecutionFailure, ToolExecutionResult,
                       ToolExecutionSuccess, ToolRunContext, ToolGuard)

log = logging.getLogger("dsh.tools")

RESERVED_NAMES = {"run_code"}

# 提示分节顺序（与 TS 版一致：坍缩规则先于工具指引带 100–199）
COLLAPSE_SECTION_ORDER = 99
SDK_SECTION_ORDER = 150

_CODE_ONLY_INSTRUCTION = (
    "`run_code` is the only tool you can call directly — a tool call naming "
    "any other tool fails. Reach every tool the SDK declares below from "
    "inside the program."
)


@dataclass
class _ScopeState:
    """一个作用域层的完整贡献（全局或精确作用域）。"""

    tools: Dict[str, ToolDefinition] = field(default_factory=dict)
    restrictions: List[Dict[str, Any]] = field(default_factory=list)
    guards: List[ToolGuard] = field(default_factory=list)
    mode: Optional[str] = None
    """presentAs 声明的作用域展示模式（最近层胜出）。"""


def _deep_freeze(value: Any) -> Any:
    """深拷贝 + 无损失 JSON 校验（失败抛 ToolArgsError）。"""
    if not is_json_value(value):
        raise ToolArgsError(f"arguments must be lossless JSON, got {type(value).__name__}")
    return copy.deepcopy(value)


@dataclass
class _Prepared:
    """一次调用的「准备」阶段产物（分段调度的第一段）。"""

    kind: str  # 'dispatch'（走工具体） | 'result'（预结算：拒绝/未知/中止）
    execution: ToolExecution
    definition: Optional[ToolDefinition]
    args: Any
    run_ctx: ToolRunContext
    result: Optional[ToolExecutionResult]


class ToolRuntime(Service):
    """工具注册表与执行管线（ctx.tools）。"""

    provides = "tools"

    def __init__(self, ctx, config: Optional[dict] = None,
                 parent: Optional["ToolRuntime"] = None) -> None:
        super().__init__(ctx, config)
        self._parent = parent
        """父运行时（局部运行时 = 父层作用域的视图；见模块 docstring）。"""
        self._scope_key: Optional[Any] = ctx.name if parent is not None else None
        """局部运行时绑定的父层作用域键（= agent 作用域 ctx.name）。"""
        self._global = _ScopeState()
        self._scoped: Dict[Any, _ScopeState] = {}
        self.default_mode = (config or {}).get("mode", "native")
        if self.default_mode not in ("native", "code", "both"):
            raise ToolError(f"invalid tools mode: {self.default_mode!r} "
                            "(expected 'native' | 'code' | 'both')")
        self.max_parallel_sub_calls = int(
            (config or {}).get("max_parallel_sub_calls", 10))
        if self.max_parallel_sub_calls < 1:
            raise ToolError("max_parallel_sub_calls must be a positive integer")
        self._code_transport: Optional[ToolDefinition] = None

    def apply(self, ctx) -> None:
        ctx.set("tools", self)
        # Code Mode 提示分节：注册在根运行时（按作用域渲染，native 作用域输出空）
        if self._parent is None and ctx.has("systemPrompt"):
            from ..prompt.system_prompt import PromptSection
            disposers = [
                ctx.systemPrompt.section(PromptSection(
                    name="tools:code-only", order=COLLAPSE_SECTION_ORDER,
                    text=self._collapse_text)),
                ctx.systemPrompt.section(PromptSection(
                    name="tools:sdk", order=SDK_SECTION_ORDER,
                    text=self._sdk_text)),
            ]

            def cleanup() -> None:
                for disposer in disposers:
                    disposer()
            ctx.effect(cleanup)

    # ---- 作用域层 ----

    def _root(self) -> "ToolRuntime":
        return self._parent if self._parent is not None else self

    def _layer(self, scope: Any) -> _ScopeState:
        """取作用域层（局部运行时委托父层的绑定作用域）。"""
        if self._parent is not None:
            return self._parent._layer(self._scope_key)
        if scope is None:
            return self._global
        return self._scoped.setdefault(scope, _ScopeState())

    def _layers_chain(self, scope: Any) -> List[_ScopeState]:
        """作用域链：全局 + 作用域层。"""
        if self._parent is not None:
            return self._parent._layers_chain(self._scope_key)
        if scope is None:
            return [self._global]
        return [self._global, self._layer(scope)]

    # ---- Code Mode：展示模式 ----

    def mode_for(self, scope: Any = None) -> str:
        """某作用域生效的展示模式：最近层的 presentAs 声明，否则部署默认。"""
        for layer in reversed(self._layers_chain(scope)):
            if layer.mode is not None:
                return layer.mode
        return self.default_mode

    def presentAs(self, mode: str):
        """
        把**调用作用域**的工具展示为 ``mode``（native/code/both），遮蔽部署默认。

        只接受局部（agent 作用域）运行时调用：进程全局展示是 tools 行的
        ``mode`` 配置字段。一个作用域只允许一次声明。

        :return: 还原部署默认的注销函数。
        """
        if self._parent is None:
            raise ToolError(
                "tools.presentAs() requires an agent-scoped runtime (a preset-"
                "mounted local runtime): a process-global presentation is the "
                "`mode` config field on the tools row")
        if mode not in ("native", "code", "both"):
            raise ToolError(f"invalid presentation mode: {mode!r}")
        layer = self._layer(None)
        if layer.mode is not None:
            raise ToolError(
                f'tools.presentAs({mode!r}) conflicts with {layer.mode!r} '
                "already declared for this scope; one composition selects "
                "one presentation")
        layer.mode = mode

        def restore() -> None:
            layer.mode = None
        return self.ctx.effect(restore)

    def _collapse_text(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
        scope = (assemble_ctx or {}).get("scope")
        return (_CODE_ONLY_INSTRUCTION
                if self.mode_for(scope) == "code" else "")

    def _sdk_text(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
        scope = (assemble_ctx or {}).get("scope")
        mode = self.mode_for(scope)
        if mode == "native":
            return ""
        self._require_code_runtime()
        from ..code.sdk import render_tools_sdk_py
        return render_tools_sdk_py(self.sdk_schemas(scope))

    def sdk_schemas(self, scope: Any = None) -> List[Dict[str, Any]]:
        """SDK 声明输入：可见工具（不含 run_code）的 {name, description, parameters, output}。"""
        out: List[Dict[str, Any]] = []
        for definition in self.list(scope):
            out.append({
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
                "output": definition.output.schema,
            })
        return out

    def _require_code_runtime(self):
        """取 ctx.codeRuntime（缺失/未知语言 fail loud）。"""
        try:
            runtime = self.ctx.get("codeRuntime")
        except ServiceNotFoundError as exc:
            raise ToolError(
                'dsh-tools: mode "code"/"both" requires a code runtime — '
                "mount dsh.code.runtime:CodeRuntime or set tools mode to "
                '"native"') from exc
        language = getattr(runtime, "language", None)
        if language != "python":
            raise ToolError(
                f"dsh-tools: no SDK renderer registered for runtime language "
                f"{language!r} (known: \"python\")")
        return runtime

    def _require_code_transport(self) -> ToolDefinition:
        if self._code_transport is None:
            from ..code.mode import build_run_code_tool
            self._code_transport = build_run_code_tool(
                self, require_runtime=self._require_code_runtime,
                max_parallel=self.max_parallel_sub_calls)
        return self._code_transport

    # ---- 注册 ----

    def register(self, definition: ToolDefinition, scope: Any = None):
        """
        注册工具（全局或作用域）。

        :param scope: None = 全局层（局部运行时上 = 委托到父层绑定作用域）；
            否则只对该作用域可见（遮蔽同名全局工具）。
        :return: 注销函数。
        :raises ToolError: 同层重名 / 保留名 / 输出 schema 非法。
        """
        if definition.name in RESERVED_NAMES:
            raise ToolError(
                f'tool name {definition.name!r} is reserved for the Code Mode '
                "presentation transport and cannot be registered or shadowed")
        layer = self._layer(scope)
        if definition.name in layer.tools:
            raise ToolError(f"duplicate tool in one layer: {definition.name}")
        from .schema import assert_supported_schema
        assert_supported_schema(definition.output.schema)
        layer.tools[definition.name] = definition
        self.ctx.events.emit("tools/change")

        def unregister() -> None:
            if layer.tools.pop(definition.name, None) is not None:
                self.ctx.events.emit("tools/change")
        return self.ctx.effect(unregister)

    def restrict(self, filter_: Dict[str, Any], scope: Any = None):
        """
        限制作用域继承的全局工具。

        :param filter_: ``{"allow": [...]}`` 和/或 ``{"deny": [...]}``；
            多个限制取交集；作用域自有注册不受影响。
        :return: 注销函数（解除该限制）。
        """
        if self._parent is not None:
            return self._parent.restrict(filter_, self._scope_key)
        if scope is None:
            raise ToolError("restrict requires a scope (global restriction is invalid)")
        layer = self._layer(scope)
        allow = set(filter_.get("allow") or [])
        deny = set(filter_.get("deny") or [])
        if "run_code" in (allow | deny):
            raise ToolError("run_code is the reserved Code Mode transport and "
                            "cannot be restricted")
        unknown = (allow | deny) - set(self._global.tools.keys())
        if unknown:
            raise ToolError(f"restrict names unknown global tools: {sorted(unknown)}")
        record = {"allow": allow, "deny": deny}
        layer.restrictions.append(record)

        def lift() -> None:
            try:
                layer.restrictions.remove(record)
                self.ctx.events.emit("tools/change")
            except ValueError:
                pass
        return self.ctx.effect(lift)

    def guard(self, guard: ToolGuard, scope: Any = None):
        """注册单调最终拒绝 guard（作用域级或全局）。"""
        layer = self._layer(scope)
        layer.guards.append(guard)

        def remove() -> None:
            try:
                layer.guards.remove(guard)
            except ValueError:
                pass
        return self.ctx.effect(remove)

    # ---- 查询 ----

    def get(self, name: str, scope: Any = None) -> Optional[ToolDefinition]:
        """按作用域视角取工具定义（遮蔽 + 限制 + 保留传输）。"""
        if self._parent is not None:
            return self._parent.get(name, self._scope_key)
        if scope is not None:
            scoped_layer = self._layer(scope)
            if name in scoped_layer.tools:
                return scoped_layer.tools[name]
            if not self._globally_visible(name, scoped_layer):
                # 保留传输不受 allow/deny 过滤
                if name == "run_code" and self.mode_for(scope) != "native":
                    return self._require_code_transport()
                return None
        if name in self._global.tools:
            return self._global.tools[name]
        # 保留传输：非 native 作用域解析到 run_code（不在任何可过滤层中）
        if name == "run_code" and self.mode_for(scope) != "native":
            return self._require_code_transport()
        return None

    def _globally_visible(self, name: str, scoped_layer: _ScopeState) -> bool:
        """全局工具在该作用域是否可见（allow/deny 交集）。"""
        if name not in self._global.tools:
            return False
        for record in scoped_layer.restrictions:
            if record["deny"] and name in record["deny"]:
                return False
        for record in scoped_layer.restrictions:
            if record["allow"] and name not in record["allow"]:
                return False
        return True

    def list(self, scope: Any = None) -> List[ToolDefinition]:
        """作用域视角的可见工具列表（含父运行时贡献，按名字去重）。"""
        if self._parent is not None:
            return self._parent.list(self._scope_key)
        out: List[ToolDefinition] = []
        for name, definition in self._global.tools.items():
            if scope is None or self._globally_visible(name, self._layer(scope)):
                out.append(definition)
        if scope is not None:
            out.extend(self._layer(scope).tools.values())
        return out

    def schemas(self, scope: Any = None) -> List[Dict[str, Any]]:
        """模型可见 wire schema 白名单投影（非 native 模式按 Code Mode 坍缩）。"""
        if self._parent is not None:
            return self._parent.schemas(self._scope_key)
        mode = self.mode_for(scope)
        definitions = self.list(scope)
        if mode == "native":
            return [t.model_schema() for t in definitions]
        self._require_code_runtime()
        transport = self._require_code_transport()
        if mode == "code":
            return [transport.model_schema()]
        return ([t.model_schema() for t in definitions]
                + [transport.model_schema()])

    def execution_mode(self, name: str, args: Any, scope: Any = None) -> str:
        """调度模式分类: 'parallel' | 'exclusive'（fail-closed）。"""
        if self._parent is not None:
            return self._parent.execution_mode(name, args, self._scope_key)
        definition = self.get(name, scope)
        if definition is None:
            return "exclusive"
        try:
            return "parallel" if definition.classify_concurrency(args) else "exclusive"
        except Exception:
            return "exclusive"

    # ---- 分段执行管线 ----

    async def execute(self, call_id: str, name: str, arguments: Any,
                      agent: Any = None, signal: Optional[AbortSignal] = None,
                      scope: Any = None,
                      parent: Optional[object] = None) -> ToolExecutionResult:
        """
        执行一次工具调用（完整 5 段管线；prepare → dispatch → finalize）。

        :param call_id: 调用身份（与 tool/result 配对）。
        :param name: 工具名。
        :param arguments: 原始参数（JSON 值）。
        :param agent: 调用者 agent（作用域 key 与展示用）。
        :param signal: 调用者取消信号。
        :param scope: 视角作用域（默认取 agent 的作用域名）。
        :param parent: 嵌套子调用的父令牌（run_code 桥；非 None 时豁免
            code 模式坍缩）。
        :return: 物化后的最终结果（frozen）。
        """
        if self._parent is not None:
            return await self._parent.execute(
                call_id, name, arguments, agent=agent, signal=signal,
                scope=self._scope_key, parent=parent)
        prepared = await self.prepare(call_id, name, arguments, agent=agent,
                                      signal=signal, scope=scope, parent=parent)
        if prepared.kind != "dispatch":
            return self.finish_prepared(prepared)
        dispatched = await self.dispatch_prepared(prepared)
        return await self.finalize_prepared(prepared, dispatched)

    async def prepare(self, call_id: str, name: str, arguments: Any,
                      agent: Any = None, signal: Optional[AbortSignal] = None,
                      scope: Any = None,
                      parent: Optional[object] = None) -> _Prepared:
        """
        准备段（有序）：参数物化 + Code Mode 坍缩 + pre-execute + guards。

        :return: kind='dispatch' 待执行工具体；kind='result' 预结算结果。
        """
        if self._parent is not None:
            return await self._parent.prepare(
                call_id, name, arguments, agent=agent, signal=signal,
                scope=self._scope_key, parent=parent)
        if scope is None and agent is not None:
            scope = getattr(agent, "ctx_name", None)
        definition = self.get(name, scope)
        args_error: Optional[ToolError] = None
        args: Any = {}
        if definition is not None:
            try:
                args = _deep_freeze(arguments)
                definition.validate_args(args)
            except ToolError as exc:
                args_error = exc
        execution = ToolExecution(call_id=call_id, name=name, arguments=args,
                                  agent=agent, signal=signal or AbortSignal(),
                                  parent=parent)
        run_ctx = ToolRunContext(execution=execution, root_ctx=self.ctx)

        def result_of(failure: ToolExecutionResult) -> _Prepared:
            return _Prepared("result", execution, definition, args, run_ctx,
                             failure)

        # 0) Code Mode 坍缩：可见工具的直接调用在策略管线之前确定性拒绝
        #    （pre-execute 监听者、审批 ask 与 guards 绝不观察一个必败的调用）。
        collapsed = (definition is not None and parent is None
                     and self.mode_for(scope) == "code"
                     and name != "run_code")
        if collapsed:
            if execution.signal.aborted:
                return result_of(ToolExecutionFailure(ToolError(
                    "aborted", code="ABORTED_BEFORE_DISPATCH")))
            return result_of(self._failure(execution, run_ctx, ToolError(
                f"only `run_code` is callable directly — call `{name}` from "
                "inside a `run_code` program instead",
                code="UNKNOWN_TOOL")))
        if definition is None:
            return result_of(ToolExecutionFailure(ToolNotFoundError(name)))
        if args_error is not None:
            return result_of(self._failure(execution, run_ctx, args_error))

        # 1) pre-execute waterfall
        decision: PreToolDecision = await self.ctx.events.waterfall(
            "tools/pre-execute", execution, default=AllowDecision())
        if not isinstance(decision, (AllowDecision, DenyDecision, AskDecision)):
            decision = AllowDecision()
        if isinstance(decision, AskDecision):
            decision = await self._resolve_ask(execution, decision)
        if isinstance(decision, DenyDecision):
            return result_of(self._failure(execution, run_ctx,
                                           ToolError(decision.reason,
                                                     code="DENIED")))
        if execution.signal.aborted:
            return result_of(self._failure(
                execution, run_ctx,
                ToolError("aborted", code="ABORTED_BEFORE_DISPATCH")))

        # 2) 单调 guards
        for guard in self._guards_for(scope):
            reason = guard(execution)
            if reason:
                return result_of(self._failure(execution, run_ctx,
                                               ToolError(reason, code="GUARDED")))

        return _Prepared("dispatch", execution, definition, args, run_ctx, None)

    async def dispatch_prepared(self, prepared: _Prepared) -> ToolExecutionResult:
        """分发段：around-dispatch waterfall + 工具体（可并发，不跑 post-execute）。"""
        execution = prepared.execution
        try:
            result: ToolExecutionResult = await self.ctx.events.waterfall(
                "tools/execute", execution,
                default=lambda: self._run_body(prepared.definition,
                                               prepared.args,
                                               prepared.run_ctx))
        except ToolError as exc:
            result = ToolExecutionFailure(exc, content=f"Error: {exc.message}")
        except Exception as exc:
            log.exception("tool %s crashed", execution.name)
            result = ToolExecutionFailure(
                ToolError(f"{type(exc).__name__}: {exc}"),
                content=f"Error: {type(exc).__name__}: {exc}")

        if not isinstance(result, (ToolExecutionSuccess, ToolExecutionFailure)):
            result = ToolExecutionFailure(
                ToolError("tools/execute returned invalid result"))

        # 取消（工具体已启动则结果保留其结构化错误，成功结果替换为 ABORTED）
        if execution.signal.aborted and not result.is_error:
            result = ToolExecutionFailure(ToolError("aborted", code="ABORTED"),
                                          content="Error: aborted")
        return result

    async def finalize_prepared(self, prepared: _Prepared,
                                result: ToolExecutionResult
                                ) -> ToolExecutionResult:
        """结算段（有序）：post-execute + finalize_content + 上下文 + tools/result。"""
        execution = prepared.execution
        run_ctx = prepared.run_ctx
        definition = prepared.definition
        args = prepared.args

        # 4) post-execute waterfall
        post: PostToolDecision = await self.ctx.events.waterfall(
            "tools/post-execute", execution, result,
            default=lambda: AcceptDecision())
        if isinstance(post, BlockDecision):
            result = ToolExecutionFailure(ToolError(post.feedback,
                                                    code="BLOCKED"))
        elif isinstance(post, AcceptDecision) and not result.is_error:
            result = self._apply_accept(result, definition, args, post)

        # 5) finalize_content
        if not result.is_error and definition.finalize_content is not None:
            try:
                new_content = definition.finalize_content(execution, result)
                if new_content is not None:
                    result = ToolExecutionSuccess(
                        value=result.value, content=str(new_content),
                        meta=result.meta, concludes_turn=result.concludes_turn,
                        additional_contexts=result.additional_contexts)
            except Exception as exc:
                log.exception("finalize_content crashed for %s",
                              execution.name)
                result = ToolExecutionFailure(ToolError(
                    f"{type(exc).__name__}: {exc}"))

        # deferred contexts / turn 终点标记
        if not result.is_error and (run_ctx.deferred_contexts
                                    or run_ctx._concludes_turn):
            result = ToolExecutionSuccess(
                value=result.value, content=result.content, meta=result.meta,
                concludes_turn=result.concludes_turn or run_ctx._concludes_turn,
                additional_contexts=list(run_ctx.deferred_contexts))

        self.ctx.events.emit("tools/result", execution, result)
        return result

    def finish_prepared(self, prepared: _Prepared) -> ToolExecutionResult:
        """预结算结果收尾：仅广播 tools/result（策略管线对必败调用不可见）。"""
        result = prepared.result or ToolExecutionFailure(
            ToolError("prepared result missing", code="INTERNAL"))
        self.ctx.events.emit("tools/result", prepared.execution, result)
        return result

    async def _resolve_ask(self, execution: ToolExecution,
                           decision: AskDecision) -> PreToolDecision:
        """ask 决策 → 审批服务（无审批通道 = 拒绝）。"""
        approver = None
        if self.ctx.has("approval"):
            approver = self.ctx.approval
        if approver is None:
            return DenyDecision(decision.reason or "approval unavailable")
        granted = await approver.request(
            f"允许执行工具 {execution.name}?",
            detail=decision.reason or "")
        return AllowDecision() if granted else DenyDecision(
            decision.reason or "approval denied")

    def _apply_accept(self, result: ToolExecutionSuccess,
                      definition: ToolDefinition, args: Any,
                      post: AcceptDecision) -> ToolExecutionResult:
        """应用 post 决策：替换 content 或重校验 value（互斥）。"""
        if post.value is not None:
            try:
                value = definition.output.validate(post.value)
                content = definition.output.render(args, value)
            except ToolError as exc:
                return ToolExecutionFailure(exc)
            return ToolExecutionSuccess(value=value, content=content,
                                        meta=result.meta,
                                        concludes_turn=result.concludes_turn,
                                        additional_contexts=result.additional_contexts)
        if post.content is not None:
            return ToolExecutionSuccess(value=result.value, content=post.content,
                                        meta=result.meta,
                                        concludes_turn=result.concludes_turn,
                                        additional_contexts=result.additional_contexts)
        return result

    def _guards_for(self, scope: Any) -> List[ToolGuard]:
        guards: List[ToolGuard] = list(self._global.guards)
        if scope is not None:
            guards.extend(self._layer(scope).guards)
        return guards

    async def _run_body(self, definition: ToolDefinition, args: Any,
                        run_ctx: ToolRunContext) -> ToolExecutionResult:
        """执行工具体并物化 canonical 输出（含超时）。"""
        timeout_s = definition.timeout_ms / 1000 if definition.timeout_ms else None
        try:
            if timeout_s:
                value = await asyncio.wait_for(
                    definition.execute(args, run_ctx), timeout=timeout_s)
            else:
                value = await definition.execute(args, run_ctx)
        except asyncio.TimeoutError as exc:
            raise ToolError(
                f"tool {definition.name} timed out after {definition.timeout_ms}ms",
                code="TIMEOUT") from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        run_ctx.signal.raise_if_aborted()
        value = definition.output.validate(value)
        content = definition.output.render(args, value)
        return ToolExecutionSuccess(value=value, content=content)

    def _failure(self, execution: ToolExecution, run_ctx: ToolRunContext,
                 error: ToolError) -> ToolExecutionResult:
        return ToolExecutionFailure(error, content=f"Error: {error.message}")

    def close(self) -> None:
        self._global.tools.clear()
        self._scoped.clear()
        self._code_transport = None
