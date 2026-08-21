# 系统提示词 开发手册

> 对应 TS 版概念：`system-prompt` 包的 `section`/`context`/`variable`/`tools`/`assemble` 组装语义、`-100=harness 身份`/`0=persona`/`工具指引 100–199` 的 order 约定、`system-prompt/assemble` waterfall。
> 源码文件清单：`dsh/prompt/system_prompt.py`、`dsh/prompt/sections.py`。
> 生成方式：人工完整读取上述源码，并用 `python -c "import inspect; ..."` 逐个验证签名后撰写；仅记录代码真实行为。

## 1. 模块定位与架构位置

**职责**：把 system prompt 组装抽象成注册表（`SystemPromptService`，`ctx.systemPrompt`）——按 `order` 升序拼接分节、插值 `{{name}}` 变量、追加动态上下文、聚合工具 schema，最终经 `system-prompt/assemble` waterfall 产出 `{"text","tools"}`。内置 `PersonaPlugin` 注册 harness 身份与 persona 两个分节。

**ctx 服务名与 provides/inject**：

| 类 | provides | inject | ctx.<key> |
|---|---|---|---|
| `SystemPromptService` | `"systemPrompt"` | — | `ctx.systemPrompt` |
| `PersonaPlugin` | `None` | `("systemPrompt",)` | 仅消费 `ctx.systemPrompt` |

**与其他模块的调用关系**：
- `dsh.agent.loop.AgentLoopService._default_assemble` 调用 `ctx.systemPrompt._build(...)`（作为 waterfall 末端）；`_run_step` 里以 `system-prompt/assemble` waterfall 为权威入口。
- `SystemPromptService._default_tool_provider` 调用 `ctx.tools.schemas(scope)` 取默认工具 schema。
- `PromptSection`/`PromptContext` 是纯数据结构，供插件在 `apply` 里注册；`sections.py` 的 `PersonaPlugin` 是首个消费者。

**能力缝三角色分析**：
- **Definition**：`PromptSection`/`PromptContext`（注册表输入词汇）+ 注册 API 契约（`section/context/variable/tools` 的重复注册抛错语义）。
- **Provider**：`SystemPromptService`（实现组装算法与 waterfall 末端 `_build`）。
- **Consumer**：`AgentLoopService`（每步组装 system prompt + tools）。
- 组装仍先跑 `system-prompt/assemble` waterfall，监听者返回值为权威（可完全替换/短路 `_build` 结果）。

## 2. 文件清单表

| 文件 | 职责 |
|---|---|
| `dsh/prompt/system_prompt.py` | `PromptSection`/`PromptContext`、`SystemPromptService` 组装注册表 |
| `dsh/prompt/sections.py` | `PersonaPlugin`：注册 harness 身份（-100）与 persona（0）分节 |

## 3. 类型与数据结构

**模块级常量**：`_VARIABLE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")` —— 插值变量名只接受小写字母开头、后跟小写字母/数字/下划线。

**`PromptSection`（`@dataclass`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | `str` | （必填） | 分节名（全局/作用域内唯一） |
| `order` | `int` | （必填） | 升序拼接；`-100`=harness 身份、`0`=persona、工具指引 `100–199` |
| `text` | `Any` | （必填） | 字符串或 `callable(assemble_ctx)->str` |
| `complete` | `bool` | `False` | 为真且只有一个生效时，成为唯一 system prompt |

**`PromptContext`（`@dataclass`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | `str` | （必填） | 上下文贡献名（全局/作用域内唯一） |
| `order` | `int` | （必填） | 升序追加 |
| `text` | `Any` | （必填） | 字符串或 `callable(assemble_ctx)->str`（cache-safe，变化时才快照） |

**`SystemPromptService` 内部状态**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_global_sections` | `Dict[str, PromptSection]` | 全局分节 |
| `_global_contexts` | `Dict[str, PromptContext]` | 全局上下文 |
| `_global_variables` | `Dict[str, Callable]` | 全局变量 provider |
| `_global_tool_providers` | `List[Callable]` | 全局工具 schema provider |
| `_scoped` | `Dict[Any, Dict]` | 每作用域 `{"sections","contexts","variables","tool_providers","suppress"}` |
| `_suppressed_scopes` | `set` | `__init__` 初始化但组装未直接读取；抑制实际走 `_scope_state(scope)["suppress"]` |

**`_SCOPE_KEYS` 类常量**：`{"_global_sections":"sections", "_global_contexts":"contexts", "_global_variables":"variables"}`（`_collect` 用全局属性名映射到作用域状态键）。

## 4. 函数与类方法详解

### 4.1 `dsh/prompt/system_prompt.py`

#### `PromptSection`（dataclass）

```python
def resolve(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
```
- **参数**：`assemble_ctx`（`Optional[Dict[str, Any]]`，无默认值）——组装上下文（`{"scope","signal"}`），传给 callable 型 text。
- **返回值与副作用**：`str`；纯函数，无副作用。
- **行为**：若 `self.text` 可调用 → `str(self.text(assemble_ctx) or "")`（callable 返回 `None`/空值回退空串）；否则 `str(self.text or "")`。

#### `PromptContext`（dataclass）

```python
def resolve(self, assemble_ctx: Optional[Dict[str, Any]]) -> str:
```
- 参数/返回值/行为与 `PromptSection.resolve` 完全一致（callable 或字符串 → 字符串）。

#### `SystemPromptService`（`Service`，`provides="systemPrompt"`）

```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```
- 初始化四个全局注册表 + `_scoped`/`_suppressed_scopes`（见状态表）。`config` 未用于本服务。

```python
def apply(self, ctx) -> None:
```
- 无参数（`ctx` 由 Loader 传入）。`ctx.set("systemPrompt", self)`；返回 `None`。

```python
def _scope_state(self, scope: Any) -> Dict[str, Any]:
```
- **参数**：`scope`（`Any`，作用域键，如 `agent:<id>` 或 `None`）。
- **行为**：`self._scoped.setdefault(scope, {...})` 惰性创建作用域状态（sections/contexts/variables 空 dict、tool_providers 空 list、suppress=False）。

```python
def section(self, section: PromptSection, scope: Any = None):
```
- **参数**：`section`（`PromptSection`）、`scope`（`Any`，默认 `None`）。
- **返回值**：`ctx.effect(unregister)` 的返回值（注销函数）。
- **行为**：目标表 = 全局（`scope is None`）或 `_scope_state(scope)["sections"]`；`section.name` 已存在 → `raise ValueError(f"duplicate prompt section: {section.name}")`；登记后 `emit("system-prompt/change")`；注销函数弹出成功后再 emit。
- **边界**：重名抛错；注销幂等（弹出为 None 不 emit）。

```python
def context(self, context: PromptContext, scope: Any = None):
```
- **参数**：`context`（`PromptContext`）、`scope`（默认 `None`）。
- **返回值**：`ctx.effect(unregister)` 返回值。
- **行为**：目标表 = 全局/作用域 `contexts`；重名 → `raise ValueError(f"duplicate prompt context: {context.name}")`；不 emit change；注销幂等。

```python
def variable(self, name: str, provider: Callable, scope: Any = None):
```
- **参数**：`name`（`str`，变量名）、`provider`（`Callable`，每次组装求值、可返回 `None`）、`scope`（默认 `None`）。
- **返回值**：`ctx.effect(unregister)` 返回值。
- **行为**：先校验 `_VARIABLE_RE.fullmatch("{{"+name+"}}")`，非法 → `raise ValueError(f"invalid variable name: {name!r}")`；重名 → `raise ValueError(f"duplicate prompt variable: {name}")`；登记到全局/作用域 `variables`；注销幂等。

```python
def tools(self, provider: Callable, scope: Any = None):
```
- **参数**：`provider`（`Callable`，签名 `(scope)->List[schema]` 或可接受 `None`）、`scope`（默认 `None`）。
- **返回值**：`ctx.effect(unregister)` 返回值。
- **行为**：追加到全局/作用域 `tool_providers` 列表（允许同名多次追加）；注销用 `list.remove`（`ValueError` 忽略）。

```python
def suppress_runtime_context(self, scope: Any = None):
```
- **参数**：`scope`（默认 `None`）。
- **返回值**：`ctx.effect(restore)` 返回值。
- **行为**：`_scope_state(scope)["suppress"] = True`；恢复函数置回 `False`。语义：抑制动态上下文贡献，不改动拥有/执行这些事实的服务。

```python
def _collect(self, scope: Any, key: str) -> List[Any]:
```
- **参数**：`scope`（`Any`）、`key`（`str`，全局属性名，见 `_SCOPE_KEYS`）。
- **行为**：`scope is None` → `list(getattr(self, key).values())`；否则 `dict(getattr(self, key))` 拷贝后 `update(_scope_state(scope)[_SCOPE_KEYS[key]])`（作用域同名遮蔽全局），返回 `list(merged.values())`。

```python
def _default_tool_provider(self, scope: Any) -> List[Dict[str, Any]]:
```
- **参数**：`scope`（`Any`）。
- **行为**：`ctx.has("tools")` 时 `ctx.tools.schemas(scope)`，否则 `[]`。

```python
def _tool_schemas(self, scope: Any) -> List[Dict[str, Any]]:
```
- **参数**：`scope`（`Any`）。
- **返回值**：`List[Dict[str, Any]]`（去重后的工具 schema）。
- **行为**：默认 provider 结果 → 追加全局 tool_providers（`provider(None)`）→ 追加作用域 tool_providers（`provider(scope)`）；按 `schema["function"]["name"]` 去重（同名后者覆盖前者）。

```python
def _resolve_variables(self, text: str, scope: Any, assemble_ctx: Optional[Dict[str, Any]]) -> str:
```
- **参数**：`text`（`str`，含 `{{name}}` 的模板）、`scope`（`Any`）、`assemble_ctx`（`Optional[Dict]`）。
- **返回值**：插值后的 `str`。
- **行为**：合并全局+作用域变量（作用域遮蔽）；`_VARIABLE_RE.sub(replace, text)`：provider 为 `None`（未定义）→ `raise ValueError(f"prompt references undefined variable {name!r}")`；provider 返回 `None` → 空串；否则 `str(value)`。

```python
def _build(self, scope: Any, assemble_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
```
- **参数**：`scope`（`Any`）、`assemble_ctx`（`Optional[Dict]`）。
- **返回值**：`{"text": str, "tools": List[Dict]}`。
- **行为**：取分节；`complete` 分节多于一个 → `raise ValueError("more than one effective complete section")`；有 complete → 仅其 `resolve` 文本；否则按 `order` 升序 `resolve` 后用 `"\n\n"` 连接（空结果剔除）；再 `_resolve_variables`；若 `scope is not None` 且未 suppress：追加 `_collect(contexts)` 按 `order` 升序的 resolve 结果（每个非空追加 `"\n\n" + resolved`）。返回 `{"text", "tools": _tool_schemas(scope)}`。
- **边界**：多个 complete 抛错；空文本保留为空串；变量插值发生在上下文追加之前。

```python
async def assemble(self, scope: Any = None, signal: Any = None) -> Dict[str, Any]:
```
- **参数**：`scope`（`Any`，默认 `None`）、`signal`（`Any`，默认 `None`，透传给监听者/变量 provider）。
- **返回值**：`{"text": str, "tools": [模型 schema]}`（waterfall 返回值为权威）。
- **行为**：`assemble_ctx = {"scope": scope, "signal": signal}`；`await ctx.events.waterfall("system-prompt/assemble", assemble_ctx, default=lambda: self._build(scope, assemble_ctx))`。

```python
def close(self) -> None:
```
- 清空 `_global_sections`/`_global_contexts`/`_global_variables`/`_scoped`。**注意**：未清 `_global_tool_providers` 与 `_suppressed_scopes`（代码现状）。

### 4.2 `dsh/prompt/sections.py`

#### `PersonaPlugin`（`Service`，`inject=("systemPrompt",)`）

```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```
- `self._disposers: List[Any] = []`。

```python
def apply(self, ctx) -> None:
```
- 无参数（`ctx` 由 Loader 传入）。构造两个 `PromptSection` 并 `ctx.systemPrompt.section(...)` 注册，disposer 存入 `_disposers`：
  - `harness:identity`（order=-100，文本 `config["identity"]` 覆盖，默认「你是 dsh-python（DeepSeek Harness 的 Python 实现）驱动的编码智能体。当前工作目录用 pwd 获取，不要从其它路径推断。」）。
  - `persona`（order=0，文本 `config["persona"]` 覆盖，默认「你是一个严谨、可靠的智能体助手。」）。
- **返回值**：`cleanup`（逐个调用 disposer 并清空 `_disposers`）。

## 5. 关键流程

### 5.1 prompt 组装流程（伪代码，`assemble`/`_build`）

```
assemble(scope, signal):
  assemble_ctx = {scope, signal}
  return await waterfall("system-prompt/assemble", assemble_ctx,
                         default=lambda: _build(scope, assemble_ctx))

_build(scope, assemble_ctx):
  sections = _collect(scope, "_global_sections")     # 全局 + 作用域（同名遮蔽）
  complete = [s for s in sections if s.complete]
  if len(complete) > 1: raise ValueError("more than one effective complete section")
  if complete: text = complete[0].resolve(assemble_ctx)
  else: text = "\n\n".join(s.resolve(assemble_ctx) for s in sorted(sections, key=order) if 非空)
  text = _resolve_variables(text, scope, assemble_ctx)      # 未定义变量抛 ValueError
  if scope is not None and not _scope_state(scope)["suppress"]:
      for c in sorted(_collect(scope, "_global_contexts"), key=order):
          resolved = c.resolve(assemble_ctx)
          if resolved: text += "\n\n" + resolved
  return {"text": text, "tools": _tool_schemas(scope)}      # tools 去重按 function.name
```

### 5.2 工具 schema 聚合流程（伪代码，`_tool_schemas`）

```
_tool_schemas(scope):
  schemas = _default_tool_provider(scope)          # ctx.tools.schemas(scope) 或 []
  for provider in _global_tool_providers: schemas += list(provider(None) or [])
  if scope is not None:
      for provider in _scope_state(scope)["tool_providers"]: schemas += list(provider(scope) or [])
  seen = {}                                          # 按 function.name 去重，后者覆盖
  for schema in schemas:
      name = schema.get("function", {}).get("name")
      if name: seen[name] = schema
  return list(seen.values())
```

### 5.3 agent-loop 消费方式
`AgentLoopService._run_step` 里：`assembly = await waterfall("system-prompt/assemble", {"scope": agent.ctx_name, "signal": ...}, default=_default_assemble)`；`_default_assemble` 直接 `ctx.systemPrompt._build(agent.ctx_name, {"scope":..., "signal":...})`。组装结果 `{"text","tools"}` 分别进入 `LlmRequest.system` 与 `LlmRequest.tools`。

## 6. 事件与扩展点

| 事件名 | 派发方式 | 载荷 | 含义 |
|---|---|---|---|
| `system-prompt/change` | emit | 无 | 分节注册/注销后通知（缓存失效提示） |
| `system-prompt/assemble` | waterfall | `{"scope", "signal"}` | 组装入口；监听者返回值为权威（可替换/短路默认组装） |

## 7. 常见改动指引

**如何新增一个提示分节**：在插件 `apply` 里 `ctx.systemPrompt.section(PromptSection(name=..., order=..., text=...))`；order 约定 `-100`=身份、`0`=persona、工具指引 `100–199`；重名会 `ValueError`。返回的 disposer 需登记（`ctx.effect` 或自持）。

**如何新增一个插值变量**：`ctx.systemPrompt.variable("name", provider, scope=None)`；`provider(assemble_ctx)` 每次组装求值，`None` → 空串；模板里写 `{{name}}`。未定义变量会在组装时抛 `ValueError`。变量名须匹配 `[a-z][a-z0-9_]*`。

**如何追加动态上下文**：`ctx.systemPrompt.context(PromptContext(name=..., order=..., text=callable))`；`suppress_runtime_context(scope)` 可整体抑制动态上下文而不拆服务。

**如何换一套完整 prompt**：在 `system-prompt/assemble` 上注册 waterfall 监听器并直接返回 `{"text","tools"}`（不调 `next()`）即短路默认组装。

**如何让某分节成为唯一 prompt**：设 `complete=True`，且保证生效集合里只有一个 complete 分节（否则组装抛 `ValueError`）。

**作用域隔离**：子代理（per-agent scope）在 `agent.ctx` 上注册局部分节/变量/工具 provider，不影响其它 agent；`scope is None` 注册为全局。

## 8. 相关测试

- `tests/test_e2e_smoke.py`：`build_context` 里 `SystemPromptService` + `PromptSection(name="persona", order=0)` 装配并跑通整条脊柱（间接覆盖 `assemble`/`_build` 的默认分节拼接与 tool schema 聚合）。
- 当前 `tests/` 无针对 `variable`/`context`/`complete`/`suppress_runtime_context` 分支的独立单测。
