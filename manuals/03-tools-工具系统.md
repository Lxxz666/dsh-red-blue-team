# 工具系统 开发手册

> 对应 TS 版概念：`ToolDefinition`、`ToolRuntime`、`ToolExecution`/`Decisions`、render-intent union（presentation）。
> 源码文件：`dsh/tools/schema.py`、`definition.py`、`pipeline.py`、`registry.py`、`presentation.py`。
> 生成方式：本文档由源码逐函数人工核对生成，所有签名均以 `inspect.signature` 验证为准。

## 1. 模块定位与架构位置

`dsh.tools` 实现工具系统：强制的 JSON Schema 子集校验、工具定义、守卫执行管线、作用域注册表，以及 UI 展示词汇。

- **职责**：定义工具（`ToolDefinition`/`define_tool`）、校验参数与输出（schema）、编排执行管线（pipeline 类型 + `ToolRuntime.execute`）、按作用域注册/遮蔽/限制（registry）、描述展示意图（presentation）。
- **ctx.\<key\> 服务名**：`ToolRuntime.provides = "tools"`，`apply` 中 `ctx.set("tools", self)` → `ctx.tools`。
- **provides/inject 依赖关系**：`ToolRuntime` 提供 `tools`，无 `inject` 依赖；继承 `dsh.kernel.Service`。
- **与其他模块的调用关系**：
  - 依赖 `..errors`（`ToolError`、`ToolArgsError`、`ToolOutputError`、`ToolNotFoundError`）。
  - 依赖 `..session.events.is_json_value`（无损失 JSON 校验）。
  - `execute` 通过 `ctx.events.waterfall/emit` 派发 `tools/pre-execute`、`tools/execute`、`tools/post-execute`、`tools/result`；ask 决策通过 `ctx.approval`（若存在）请求审批。

## 2. 文件清单表

| 文件 | 职责 |
| --- | --- |
| `dsh/tools/schema.py` | 强制的原始 JSON Schema 子集校验（enforced subset）：结构/关键字白名单、递归匹配、`parameter_schema` 编译。 |
| `dsh/tools/definition.py` | `ToolDefinition` / `ToolOutputDefinition` / `define_tool` 装饰器；模型可见 schema 白名单投影。 |
| `dsh/tools/pipeline.py` | 管线内类型：`AbortSignal`、`ToolExecution`、`ToolRunContext`、决策与结果 dataclass。 |
| `dsh/tools/registry.py` | `ToolRuntime`（ctx.tools）：作用域注册表 + 5 段守卫执行管线。 |
| `dsh/tools/presentation.py` | 展示词汇（render-intent）：`card` 标记的 dict 构造器。 |

## 3. 类型与数据结构

### 3.1 schema.py 常量

```python
_SCALAR_TYPES = ("string", "number", "integer", "boolean", "null")
_ALLOWED_KEYS = {"type", "properties", "required", "additionalProperties",
                 "items", "enum", "const", "oneOf", "description", "title",
                 "default", "examples"}
```

支持关键字：`type(object/array/string/number/integer/boolean/null)`、`properties`、`required`、`additionalProperties`、`items`、`enum`、`const`、`oneOf`；`description/title/default/examples` 为注解（不参与校验但须无损失 JSON）。未支持关键字一律拒绝。

### 3.2 definition.py 类型别名

```python
ExecuteFn = Callable[[Any, Any], Any]                # async def execute(args, run_ctx) -> value
PresentCallFn = Callable[[Any], Optional[Dict[str, Any]]]
PresentResultFn = Callable[[Any, Any], Optional[Dict[str, Any]]]
FinalizeContentFn = Callable[[Any, Any], Optional[str]]
ConcurrencyClassifier = Callable[[Any], bool]
```

### 3.3 `ToolOutputDefinition`（definition.py，dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `schema` | `Any` | `None` | canonical 值的 JSON Schema（None = 不约束）。 |
| `render` | `Callable[[Any, Any], str]` | `_default_render` | 纯投影 `(args, value) -> 模型可见文本`。 |

### 3.4 `ToolDefinition`（definition.py，dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | — | 工具名（注册表内唯一）。 |
| `description` | `str` | — | 一句话描述。 |
| `parameters` | `Dict[str, Any]` | — | 编译后的参数 schema（object）。 |
| `output` | `ToolOutputDefinition` | — | canonical 输出契约。 |
| `execute` | `ExecuteFn` | — | 唯一执行入口。 |
| `timeout_ms` | `Optional[float]` | `None` | 协作式超时预算。 |
| `is_concurrency_safe` | `Optional[ConcurrencyClassifier]` | `None` | 并发分类器（True 才并行）。 |
| `present_call` | `Optional[PresentCallFn]` | `None` | 调用态 UI 渲染意图。 |
| `present_result` | `Optional[PresentResultFn]` | `None` | 结果态 UI 渲染意图。 |
| `finalize_content` | `Optional[FinalizeContentFn]` | `None` | 最后一里路内容变换。 |

（`timeout_ms`/`is_concurrency_safe`/`present_call`/`present_result` 等绝不泄漏进模型请求——`model_schema` 白名单只取 name/description/parameters。）

### 3.5 pipeline.py 管线类型

`AbortSignal`（类，字段 `_event`/`aborted`/`reason`）；`ToolExecution`（frozen dataclass）；`ToolRunContext`（dataclass）；决策 dataclass（`AllowDecision`/`DenyDecision`/`AskDecision`/`AcceptDecision`/`BlockDecision`）；结果 dataclass（`ToolExecutionSuccess`/`ToolExecutionFailure`）。详见第 4 节字段表。

### 3.6 registry.py `_ScopeState`（dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `tools` | `Dict[str, ToolDefinition]` | `default_factory=dict` | 该层注册的工具。 |
| `restrictions` | `List[Dict[str, Any]]` | `default_factory=list` | 继承全局工具的 allow/deny 限制。 |
| `guards` | `List[ToolGuard]` | `default_factory=list` | 单调最终拒绝 guard。 |

`RESERVED_NAMES = {"run_code"}`。

## 4. 函数与类方法详解

### 4.1 `dsh/tools/schema.py`

#### `assert_supported_schema`

```python
def assert_supported_schema(schema: Any, path: str = "root") -> None:
```

- 参数：`schema`、`path`（错误定位前缀）。
- 行为：`None` 直接返回（空注解 = 不约束）；非 dict 抛 `ToolArgsError`；关键字不在 `_ALLOWED_KEYS` 抛；`type` 非法抛；`oneOf` 非 list 或分支 < 2 抛（并递归校验各分支）；`properties` 各属性递归；`items` 递归；`required` 无 `properties` 抛；`default/examples` 必须无损失 JSON（`is_json_value`）。
- 边界：**fail loud**，绝不静默接受不支持的关键字。

#### `_matches`

```python
def _matches(node: Any, schema: Dict[str, Any]) -> bool:
```

- 行为（递归匹配，前提 schema 已通过校验）：`null` 需 node is None；`object` 需 dict、required 齐全、`additionalProperties=False` 时键为 properties 子集、逐个匹配已声明的属性（属性 schema 为 None 则跳过）；`array` 需 list，`items=None` 放行、否则全量匹配；`string/integer/number/boolean` 走类型 + 标量约束（integer 排除 bool，number 排除 bool）；`oneOf` 需**恰好一个**分支匹配；无 type 注解时 `enum` 用 `in`、`const` 用 `==`；否则 True。私有。

#### `_match_scalar_constraints`

```python
def _match_scalar_constraints(node: Any, schema: Dict[str, Any]) -> bool:
```

标量节点的 `enum`（`not in` 判 False）与 `const`（`!=` 判 False）约束。私有。

#### `validate_value`

```python
def validate_value(value: Any, schema: Any, *,
                   error_cls: type = ToolOutputError,
                   what: str = "value") -> Any:
```

- 参数：`value`、`schema`（None = 不约束）、`error_cls`（参数校验用 `ToolArgsError`，输出校验用 `ToolOutputError`）、`what`（错误描述）。
- 返回：通过时原样返回 value。
- 行为：`schema is None` 直接返回；否则 `assert_supported_schema` 后 `_matches`，失败抛 `error_cls(f"{what} does not match schema: ...")`。

#### `parameter_schema`

```python
def parameter_schema(spec: Dict[str, Any]) -> Dict[str, Any]:
```

- 参数：`spec`（隐式开放对象，如 `{"path": {"type": "string", "required": True}}`）。
- 返回：`{"type": "object", "properties": {...}, "required": [...]}`。
- 行为：每个字段 `prop = dict(raw)`；`pop("required", False)` 为真则记入 required；`properties[name] = prop or None`。

### 4.2 `dsh/tools/definition.py`

#### 模块级 `_default_render`

```python
def _default_render(args: Any, value: Any) -> str:
```

- 行为：字符串原样返回；否则 `json.dumps(value, ensure_ascii=False, indent=2)`；`TypeError/ValueError` 时 `str(value)`。

#### `ToolOutputDefinition.validate`

```python
def validate(self, value: Any) -> Any:
```

`validate_value(value, self.schema, error_cls=ToolOutputError, what="tool output")`（失败抛 `ToolOutputError`）。

#### `ToolDefinition.model_schema`

```python
def model_schema(self) -> Dict[str, Any]:
```

返回 `{"type": "function", "function": {"name", "description", "parameters"}}`（白名单投影，回调绝不泄漏）。

#### `ToolDefinition.validate_args`

```python
def validate_args(self, args: Any) -> Any:
```

`validate_value(args, self.parameters, error_cls=ToolArgsError, what=f"args for tool {self.name}")`。

#### `ToolDefinition.classify_concurrency`

```python
def classify_concurrency(self, args: Any) -> bool:
```

`is_concurrency_safe is None` → False；否则调用分类器，**只有精确 `is True` 才并行**；异常吞掉返回 False（fail-closed）。

#### `ToolDefinition.__repr__`

```python
def __repr__(self) -> str:
```

`<ToolDefinition {name}>`。

#### 模块级 `define_tool`

```python
def define_tool(name: str, description: str, parameters: Dict[str, Any],
                output: Optional[Dict[str, Any]] = None, *,
                render: Optional[Callable[[Any, Any], str]] = None,
                timeout_ms: Optional[float] = None,
                concurrency_safe: Optional[ConcurrencyClassifier] = None,
                present_call: Optional[PresentCallFn] = None,
                present_result: Optional[PresentResultFn] = None,
                finalize_content: Optional[FinalizeContentFn] = None):
```

- 返回：装饰器（`decorator(fn) -> ToolDefinition`）。
- 行为：装饰器内 `parameter_schema(parameters)` 编译参数；`ToolOutputDefinition(schema=output, render=render or _default_render)`；组装 `ToolDefinition`，`execute=fn`。

### 4.3 `dsh/tools/pipeline.py`

#### `AbortSignal.__init__` / `abort` / `raise_if_aborted` / `wait` / `__repr__`

```python
def __init__(self) -> None:
def abort(self, reason: Any = None) -> None:
def raise_if_aborted(self) -> None:
async def wait(self) -> None:
def __repr__(self) -> str:
```

- `abort`：幂等，首个 `reason` 胜出；置 `aborted=True` 并 `_event.set()`。
- `raise_if_aborted`：已取消抛 `ToolError("aborted", code="ABORTED")`。
- `wait`：等待取消（不取消则永远挂起）。
- 对应 JS `AbortSignal`。

#### `ToolExecution`（frozen dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `call_id` | `str` | — | 调用身份。 |
| `name` | `str` | — | 工具名。 |
| `arguments` | `Any` | — | 参数（已物化/冻结）。 |
| `agent` | `Any` | `None` | 调用者 agent。 |
| `signal` | `AbortSignal` | `default_factory=AbortSignal` | 取消信号。 |
| `token` | `object` | `default_factory=object` | 注册表分配的关联身份。 |
| `parent` | `Optional[object]` | `None` | 嵌套派发时的父 token。 |

`defer_result(self) -> None`：占位（`pragma: no cover`，见 `ToolRunContext.defer_context`）。

#### `ToolRunContext`

```python
def __init__(self, execution: ToolExecution,
             deferred_contexts: List[Dict[str, Any]] = field(default_factory=list),
             _concludes_turn: bool = False, root_ctx: Any = None) -> None:
```

字段：`execution`、`deferred_contexts`、`_concludes_turn`、`root_ctx`（注册表所在根 Context）。

```python
@property
def signal(self) -> AbortSignal:
def defer_context(self, message: Dict[str, Any]) -> None:
def conclude_turn(self) -> None:
```

- `signal`：透传 `execution.signal`。
- `defer_context`：把上下文附加到本次执行结果（循环在 tool/result 之后追加）。
- `conclude_turn`：把成功结果标记为 turn 终点（`_concludes_turn = True`）。

#### 决策 dataclass（均为 frozen）

```python
class AllowDecision:  kind: str = "allow"
class DenyDecision:   reason: str; kind: str = "deny"
class AskDecision:    reason: Optional[str] = None; kind: str = "ask"
class AcceptDecision: kind: str = "accept"; content: Optional[str] = None; value: Optional[Any] = None
class BlockDecision:  feedback: str; kind: str = "block"
```

类型别名：`PreToolDecision = Union[AllowDecision, DenyDecision, AskDecision]`；`PostToolDecision = Union[AcceptDecision, BlockDecision]`。

#### 结果 dataclass（frozen）

`ToolExecutionSuccess`：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `value` | `Any` | — | canonical 值。 |
| `content` | `str` | — | 模型可见文本。 |
| `meta` | `Optional[Dict[str, Any]]` | `None` | 元数据。 |
| `concludes_turn` | `bool` | `False` | 是否 turn 终点。 |
| `additional_contexts` | `List[Dict[str, Any]]` | `default_factory=list` | 附加上下文。 |
| `is_error` | `bool` | `False` | 恒 False。 |

`ToolExecutionFailure`：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `error` | `ToolError` | — | 结构化错误。 |
| `content` | `str` | `""` | 错误文本。 |
| `meta` | `Optional[Dict[str, Any]]` | `None` | 元数据。 |
| `is_error` | `bool` | `True` | 恒 True。 |

```python
def to_json(self) -> Dict[str, Any]:   # 两者均有
```

- Success：`{"is_error": False, "content": ...}` + 可选 `meta`/`concludes_turn`。
- Failure：`{"is_error": True, "content", "error": {"name", "code", "message"}}` + 可选 `meta`（`code` 缺省 `TOOL_ERROR`）。

`ToolExecutionResult = Union[ToolExecutionSuccess, ToolExecutionFailure]`；`ToolGuard = Callable[[ToolExecution], Optional[str]]`（返回原因 = 最终拒绝，None = 维持原判）。

### 4.4 `dsh/tools/registry.py`

#### 模块级 `_deep_freeze`

```python
def _deep_freeze(value: Any) -> Any:
```

`is_json_value` 校验（失败抛 `ToolArgsError`）后 `copy.deepcopy` 深拷贝。私有。

#### `ToolRuntime`（类属性）

`provides = "tools"`。

#### `ToolRuntime.__init__`

```python
def __init__(self, ctx, config: Optional[dict] = None,
             parent: Optional["ToolRuntime"] = None) -> None:
```

`_parent = parent`、`_scope_key = ctx.name if parent else None`（局部运行时 =
父层作用域视图）、`_global = _ScopeState()`、`_scoped`；
`default_mode = config.get("mode", "native")`（native/code/both，非法抛
`ToolError`）；`max_parallel_sub_calls = int(config.get(
"max_parallel_sub_calls", 10))`（<1 抛 `ToolError`）；`_code_transport = None`。

#### `ToolRuntime.apply`

```python
def apply(self, ctx) -> None:
```

`ctx.set("tools", self)`；根运行时（`_parent is None`）且 `ctx.has("systemPrompt")`
时注册两个 Code Mode 提示分节（`tools:code-only` order 99 / `tools:sdk`
order 150，text 为按 `assemble_ctx["scope"]` 求值的 callable；native 作用域
渲染空被丢弃），经 `ctx.effect` 逆序注销。

#### `ToolRuntime._layer` / `_layers_chain`

```python
def _layer(self, scope: Any) -> _ScopeState:
def _layers_chain(self, scope: Any) -> List[_ScopeState]:
```

- 局部运行时（`_parent` 非 None）：**委托**父层的 `_scoped[_scope_key]`
  （第七批作用域委托修复：preset 工具既对外不可见、又可被循环按
  `scope=agent.ctx_name` 正常执行）。
- 根运行时：`_layer`：`scope=None` 返回 `_global`，否则 `setdefault` 建作用域层。
- `_layers_chain`：全局 + 作用域层（`scope=None` 只有全局）。

#### `ToolRuntime.register`

```python
def register(self, definition: ToolDefinition, scope: Any = None):
```

- 参数：`definition`、`scope`（None = 全局；否则只对该作用域可见，遮蔽同名全局）。
- 返回：注销函数（`ctx.effect(unregister)`）。
- 行为：保留名（`RESERVED_NAMES`）抛 `ToolError`；同层重名抛 `ToolError`；`assert_supported_schema(definition.output.schema)`；写入层并 `emit("tools/change")`；注销时 `pop` 成功再 `emit("tools/change")`。

#### `ToolRuntime.restrict`

```python
def restrict(self, filter_: Dict[str, Any], scope: Any = None):
```

- 参数：`filter_`（`{"allow": [...]}` 和/或 `{"deny": [...]}`，多限制取交集）、`scope`。
- 返回：注销函数（解除限制）。
- 行为：`scope=None` 抛 `ToolError`（全局限制非法）；`allow|deny` 中出现未知全局工具名抛 `ToolError`；追加 record 到 `layer.restrictions`；注销时移除并 `emit("tools/change")`。作用域自有注册不受限制。

#### `ToolRuntime.guard`

```python
def guard(self, guard: ToolGuard, scope: Any = None):
```

注册单调最终拒绝 guard（作用域级或全局），返回注销函数（`ctx.effect`）。

#### `ToolRuntime.get`

```python
def get(self, name: str, scope: Any = None) -> Optional[ToolDefinition]:
```

作用域视角取定义：作用域自有注册优先（遮蔽）；否则查全局可见性（`_globally_visible`）；本地全局层未命中且存在 `_parent` 时委托 `_parent.get(name, scope)`；否则返回 None。

#### `ToolRuntime._globally_visible`

```python
def _globally_visible(self, name: str, scoped_layer: _ScopeState) -> bool:
```

全局工具在该作用域是否可见：不在全局 → False；任一 `deny` 命中 → False；任一 `allow` 非空且不含 name → False；否则 True（allow/deny 交集语义）。

#### `ToolRuntime.list`

```python
def list(self, scope: Any = None) -> List[ToolDefinition]:
```

作用域视角可见工具列表：全局可见项 + 作用域自有项；存在 `_parent` 时追加父运行时贡献（按名字去重）。

#### `ToolRuntime.schemas`

```python
def schemas(self, scope: Any = None) -> List[Dict[str, Any]]:
```

模型可见 wire 白名单投影（第七批起含 Code Mode 坍缩）：native → `[t.model_schema() for t in self.list(scope)]`；非 native → 先 `_require_code_runtime()`（缺失/语言非 python 均 fail loud）→ `code` 只返回 `[run_code]`，`both` = 原生 + run_code。

#### `ToolRuntime.execution_mode`

```python
def execution_mode(self, name: str, args: Any, scope: Any = None) -> str:
```

返回 `'parallel' | 'exclusive'`（fail-closed）：定义不存在 → `exclusive`；`classify_concurrency` 精确 True → `parallel`，否则/异常 → `exclusive`。

#### `ToolRuntime.execute`（核心，第七批起三段调度）

```python
async def execute(self, call_id: str, name: str, arguments: Any,
                  agent: Any = None, signal: Optional[AbortSignal] = None,
                  scope: Any = None,
                  parent: Optional[object] = None) -> ToolExecutionResult:
```

`parent` 非 None = run_code 嵌套子调用（豁免 code 模式坍缩）。内部 =
`prepare` → `dispatch_prepared` → `finalize_prepared`（三段，run_code 桥复用同一
三段以保持原生并发契约；详见第 5 节与 18 号手册）：

- `prepare`（有序段）：
  1. `scope` 未给且 agent 非 None → `scope = getattr(agent, "ctx_name", None)`；
  2. `definition = self.get(name, scope)`；
  3. **Code Mode 坍缩**：`definition` 可见、`parent is None`、`mode_for(scope)
     == "code"` 且 `name != "run_code"` → 信号已中止先
     `ABORTED_BEFORE_DISPATCH`，否则 `UNKNOWN_TOOL`（消息带路线提示：
     `only `run_code` is callable directly — call `x` from inside a `run_code`
     program instead`）——**策略管线之前**确定性拒绝；
  4. None → `ToolExecutionFailure(ToolNotFoundError(name))`；
  5. `args = _deep_freeze(arguments)` + `definition.validate_args`；`ToolError`
     → Failure；
  6. 构造 `ToolExecution` 与 `ToolRunContext(root_ctx=self.ctx)`；
  7. ① pre-execute waterfall（default `AllowDecision()`）→ 非决策归 allow →
     ask 走 `_resolve_ask` → deny `_failure(code="DENIED")` → 外部 signal 已
     abort → `ABORTED_BEFORE_DISPATCH`；
  8. ② 单调 guards 任一返回原因 → `_failure(code="GUARDED")`；
  9. 通过 → `_Prepared(kind="dispatch", ...)`；预结算路径 →
     `_Prepared(kind="result", result=...)`。
- `dispatch_prepared`（可并发段）：③ around-dispatch `tools/execute` waterfall
  （default `_run_body`）→ 异常归一化 → 非法返回类型归一化 → signal abort
  且成功 → `code="ABORTED"`。
- `finalize_prepared`（有序结算段）：④ post-execute waterfall（default
  `AcceptDecision()`）→ block → `BLOCKED`；accept 且成功 → `_apply_accept`；
  ⑤ finalize_content（仅成功且有回调）；deferred contexts / `conclude_turn`
  合并；`emit("tools/result", execution, result)`；返回 frozen。
- `finish_prepared`：预结算结果收尾（仅广播 `tools/result`，不跑 post-execute）。

#### `ToolRuntime._resolve_ask`

```python
async def _resolve_ask(self, execution: ToolExecution,
                       decision: AskDecision) -> PreToolDecision:
```

`ctx.has("approval")` → `ctx.approval`；无审批通道 → `DenyDecision(decision.reason or "approval unavailable")`；否则 `await approver.request(...)`，granted → `AllowDecision()`，否则 `DenyDecision`。

#### `ToolRuntime._apply_accept`

```python
def _apply_accept(self, result: ToolExecutionSuccess,
                  definition: ToolDefinition, args: Any,
                  post: AcceptDecision) -> ToolExecutionResult:
```

`post.value` 非 None → `definition.output.validate` + `render`（`ToolError` → Failure）；`post.content` 非 None → 替换 content（value/meta/concludes_turn/additional_contexts 保留）；否则原样返回。value 与 content 互斥。

#### `ToolRuntime._guards_for`

```python
def _guards_for(self, scope: Any) -> List[ToolGuard]:
```

全局 guards + 作用域 guards。

#### `ToolRuntime._run_body`

```python
async def _run_body(self, definition: ToolDefinition, args: Any,
                    run_ctx: ToolRunContext) -> ToolExecutionResult:
```

执行工具体并物化 canonical 输出：`timeout_s = timeout_ms/1000`（有则 `asyncio.wait_for`）；`asyncio.TimeoutError` → `ToolError(code="TIMEOUT")`；`ToolError` 原样抛；其它异常 → `ToolError`；然后 `run_ctx.signal.raise_if_aborted()`、`output.validate(value)`、`output.render(args, value)`，返回 `ToolExecutionSuccess(value, content)`。

#### `ToolRuntime._failure`

```python
def _failure(self, execution: ToolExecution, run_ctx: ToolRunContext,
             error: ToolError) -> ToolExecutionResult:
```

`ToolExecutionFailure(error, content=f"Error: {error.message}")`。

#### `ToolRuntime.close`

```python
def close(self) -> None:
```

清空 `_global.tools` 与 `_scoped`。

### 4.5 `dsh/tools/presentation.py`

所有函数返回 `card` 标记的 dict（provider 中立，UI 桥按 card 类型 switch），投影必须纯、无副作用。

```python
def generic_call(title: str, kind: str = "other",
                 raw_input: Optional[Any] = None,
                 locations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
```

pending 态通用卡片。`kind` ∈ read/edit/delete/move/search/execute/fetch/other。`raw_input`、`locations` 非空才写入。

```python
def terminal_call(title: str, description: Optional[str] = None,
                  cwd: Optional[str] = None) -> Dict[str, Any]:
```

pending 态终端卡片。`description`/`cwd` 真值才写入。

```python
def generic_result(title: str, content: Optional[str] = None) -> Dict[str, Any]:
```

完成态通用卡片。`content is not None` 才写入。

```python
def terminal_result(title: str, output: str, exit_code: Optional[int] = None,
                    signal: Optional[str] = None) -> Dict[str, Any]:
```

完成态终端卡片（必含 output）。`exit_code is not None`、`signal` 真值才写入。

```python
def diff_result(title: str, diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
```

完成态 diff 卡片。`diffs = [{"path", "old_text"|None, "new_text"}]`。

```python
def read_result(title: str, path: str, offset: int, lines: List[Dict[str, Any]],
                total_lines: int, lang: Optional[str] = None,
                content: Optional[str] = None) -> Dict[str, Any]:
```

完成态读取卡片（带行号代码视图）。`lang` 真值、`content is not None` 才写入。

```python
def search_result(title: str, shape: str, matches: Any, total: int,
                  truncated: bool = False) -> Dict[str, Any]:
```

完成态搜索卡片。`shape` ∈ matches(grep)/paths(glob)。

```python
def web_result(title: str, kind: str, *, url: Optional[str] = None,
               status_code: Optional[int] = None,
               truncated: bool = False,
               sources: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
```

完成态 Web 卡片（对应 TS 版 web card）。`kind='fetch'` 携带 `url`/`status_code`/`truncated`；`kind='search'` 携带结构化 `sources` 与 `truncated`。`url`/`status_code`/`sources` 非 None 才写入。

## 5. 关键流程

### 5.1 工具执行管线（`ToolRuntime.execute` 的 5 段流程）

```
1. tools/pre-execute（waterfall，default=AllowDecision）
   → 归一化决策；Ask → _resolve_ask（无审批通道则 Deny）；Deny → DENIED 失败
2. 单调 guards（全局+作用域顺序执行；任一返回 reason → GUARDED 失败，无法翻案）
3. tools/execute（around-dispatch waterfall，default=_run_body）
   → 工具体执行/超时/输出物化；异常归一化为失败；取消时成功→ABORTED
4. tools/post-execute（waterfall，default=AcceptDecision）
   → Block → BLOCKED 失败；Accept → 替换 value(重校验+render) 或 content
5. finalize_content（成功且有回调时改写 content）
   → 合并 deferred_contexts / concludes_turn
   → emit "tools/result"（不可变权威结果）
```

不变式：参数在策略前一次性 `_deep_freeze` 深冻结；只有 `tools/execute` 包装者可替换 signal；guard 只能收窄权限；`tools/result` 观察者拿到的对象已冻结。

### 5.2 参数/输出校验顺序（execute 前置）

`_deep_freeze(arguments)`（无损失 JSON 校验 + 深拷贝）→ `definition.validate_args`（按 parameters 校验，失败 `INVALID_ARGS`）→ 构造 `ToolExecution`。输出在 `_run_body` 中 `output.validate`（失败 `INVALID_TOOL_OUTPUT`）。

### 5.3 作用域可见性（get / list / restrict）

全局层 + 作用域层两层链；作用域自有注册遮蔽同名全局；`restrict` 只过滤继承的全局工具（allow/deny 交集），作用域自有注册豁免。第七批起构造时传 `parent` 的**局部运行时**整体委托父层的 `_scoped[ctx.name]`（preset 隔离：注册进父层作用域、对外不可见、循环经根运行时可执行）。

## 6. 事件与扩展点

| 事件名 | 派发方式 | 含义 |
| --- | --- | --- |
| `tools/pre-execute` | `waterfall` | 执行前策略：返回 `AllowDecision`/`DenyDecision`/`AskDecision`；洋葱语义（可 `await next()`）。 |
| `tools/execute` | `waterfall` | around-dispatch 包装器：可替换/包裹工具体执行；默认链末端跑 `_run_body`。 |
| `tools/post-execute` | `waterfall` | 执行后策略：返回 `AcceptDecision`/`BlockDecision`（accept 可 replace value/content）。 |
| `tools/result` | `emit` | 不可变权威结果广播（execution, result），观察者只读。 |
| `tools/change` | `emit` | 工具注册/注销/限制变化时广播。 |
| `tools/code-dispatch-log` | `waterfall` | （Code Mode）run_code 子调用结算副本替换：监听者返回替代 content（spill 语义）；异常回退原内容。 |

扩展方式：`ctx.on("tools/pre-execute", handler)` 等挂监听器；guard 用 `ToolRuntime.guard` 注册单调拒绝策略。Code Mode（mode/presentAs/run_code/三段调度）见 **18 号手册**。

## 7. 常见改动指引

### 如何新增一个工具

```python
from dsh.tools import define_tool

@define_tool(name="add", description="两数相加",
             parameters={"a": {"type": "number", "required": True},
                         "b": {"type": "number", "required": True}},
             output={"type": "number"})
async def add(args, run_ctx):
    return args["a"] + args["b"]
```

然后 `runtime.register(add)`（或作用域 `register(add, scope="agent-1")`）。

### 如何新增一个策略钩子

- 前置放行/拒绝/询问：`ctx.on("tools/pre-execute", lambda execution, next: ...)`。
- 结果改写：`ctx.on("tools/post-execute", lambda execution, result, next: ...)` 返回 `AcceptDecision(value=...)` 或 `BlockDecision(feedback=...)`。
- 单调拒绝：`runtime.guard(lambda execution: "原因" if 条件 else None)`。

### 如何扩展 JSON Schema 子集

在 `schema.py` 的 `_ALLOWED_KEYS` 增关键字，并在 `assert_supported_schema`（结构校验）与 `_matches`（语义匹配）中同步实现；保持「未知关键字 fail loud」的约定。

### 如何新增一个展示卡片

在 `presentation.py` 新增一个返回 `{"card": "<type>", ...}` 的纯函数，UI 桥按 card 类型 switch。

## 8. 相关测试

`tests/test_tools.py`：覆盖 register/get/schemas（重名拒绝、回调不泄漏）、execute 成功管线、非法参数（INVALID_ARGS）、未知工具（UNKNOWN_TOOL）、输出 schema 强制（INVALID_TOOL_OUTPUT）、pre-execute deny/ask（无审批者拒绝）、guard 单调拒绝（GUARDED）、post-execute block（BLOCKED）、超时（TIMEOUT）、restrict 与作用域遮蔽、execution_mode fail-closed。
`tests/test_seams.py`：`test_agent_preset_mount`（ToolRuntime `parent` 父委托/preset 隔离）、`test_web_fetch_bad_scheme`（web 工具与 web card `web_result`）。
