# 插件内核 开发手册

> 对应 TS 版概念：Cordis 式插件内核（`Service` / `Context` / 事件总线 `emit/waterfall/parallel/serial`）、`packages/boot/app-boot` 的 boot 契约、`composeEntries` / `boot`。
> 源码文件：`dsh/kernel/events.py`、`context.py`、`service.py`、`loader.py`、`tree.py`。
> 生成方式：本文档由源码逐函数人工核对生成，所有签名均以 `inspect.signature` 验证为准。

## 1. 模块定位与架构位置

`dsh.kernel` 是整个框架的插件内核：它提供「服务仓库 + 类型化事件总线 + 可逆副作用」三件套，并在此之上实现配置树加载与插件的拓扑挂载（boot）。

- **职责**：管理服务实例（惰性注册/解析/作用域）、事件派发（4 种模式）、插件解析与拓扑挂载、以及整树的可逆卸载（HMR 的基础）。
- **ctx.\<key\> 服务名**：本模块自身不注册任何 `ctx.<key>` 服务；它只提供 `Service` 基类与 `PluginTree`，由上层插件声明 `provides`（如 tools 域提供 `ctx.tools`、session 域提供 `ctx.sessions`）。
- **provides/inject 依赖关系**：`Service` 基类声明类属性 `provides`（本插件提供的服务名）与 `inject`（依赖的服务名元组）。`_topo_sort` 据此做 Kahn 拓扑排序，保证依赖先挂载。
- **与其他模块的调用关系**：
  - `..errors`：`ContextError`、`ServiceNotFoundError`、`LoaderError`（错误体系见 `dsh/errors.py`）。
  - `session/tools/agent/llm` 等域模块都继承 `Service` 或通过 `Context` 注册/取用服务，并把各自事件挂到 `ctx.events` 上。
  - `dsh.boot`（框架入口）组合 `Context` + `PluginTree` 完成启动。

### 1.1 `dsh/errors.py` 错误体系总表（全框架 14 个公开异常类）

| 异常类 | 基类 | 抛出场景 |
|---|---|---|
| `DshError` | `Exception` | **框架根异常**（一切域异常的基类） |
| `ContextError` | DshError | Context/事件派发域错误 |
| `ServiceNotFoundError` | ContextError | `ctx.<key>` 服务不存在（key 存属性） |
| `LoaderError` | DshError | 配置树加载/挂载失败（entry_id/stage 属性，消息带 `[id]` 前缀） |
| `SessionError` | DshError | 会话域错误（append 校验失败等） |
| `SessionFormatError` | SessionError | 无法忠实解读的日志格式（direction：版本过新/过旧） |
| `ToolError` | DshError | 工具调用结构化失败（`code`：TOOL_ERROR/UNKNOWN_TOOL/DENIED/GUARDED/BLOCKED/ABORTED/TIMEOUT/MCP_*/CODE_RUN_FAILED…；`message` 属性） |
| `ToolNotFoundError` | ToolError | 未知工具（code=UNKNOWN_TOOL，`name` 属性） |
| `ToolArgsError` | ToolError | 参数不符合 schema（code=INVALID_ARGS） |
| `ToolOutputError` | ToolError | 输出不符合 canonical schema（code=INVALID_TOOL_OUTPUT） |
| `LlmFailure` | DshError | 模型请求结构化失败（code/provider 属性） |
| `LlmTimeoutError` | LlmFailure | 请求超时（code=TIMEOUT） |
| `AgentError` | DshError | Agent 域错误（如 run_maintenance 时 busy/disposed） |
| `ApprovalDeniedError` | DshError | 审批被拒绝 |

## 2. 文件清单表

| 文件 | 职责 |
| --- | --- |
| `dsh/kernel/events.py` | `EventBus`：按名字登记监听器，支持 emit / waterfall / parallel / serial 四种派发模式。 |
| `dsh/kernel/context.py` | `Context`：服务仓库（provide/set/get）+ 事件门面 + 作用域（scoped）+ 可逆效应（effect/dispose）。 |
| `dsh/kernel/service.py` | `Service` 插件基类与插件元数据解析（provides/inject/name）。 |
| `dsh/kernel/loader.py` | `Entry` 数据类、`resolve_target` 目标解析、`row_disabled` 平台条件、`apply_patch` Patch 应用。 |
| `dsh/kernel/tree.py` | `PluginTree`：组合（add_bundle_rows/apply_patch_rows）→ 拓扑排序 → 挂载 → 失败逆序卸载。 |

## 3. 类型与数据结构

### 3.1 `Handler`（events.py，类型别名）

```python
Handler = Callable[..., Any]
```

事件监听器：同步函数或协程函数均可。派发时由 `_is_coro` 区分。

### 3.2 `EventBus`（events.py，类）

字段（实例属性）：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `_listeners` | `Dict[str, List[Handler]]` | `{}` | 事件名 → 监听器列表（注册序）。 |

关键方法：`on` / `listener_count` / `snapshot` / `emit` / `_contained` / `parallel` / `waterfall` / `serial`（见第 4 节）。

### 3.3 `Context`（context.py，类）

字段（实例属性）：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | `"root"` | 作用域名（根为 root，子作用域为 agent 名等）。 |
| `parent` | `Optional[Context]` | `None` | 父作用域（服务解析回退链）。 |
| `events` | `EventBus` | 继承或新建 | 事件总线；子作用域与父共享。 |
| `_providers` | `Dict[str, tuple]` | `{}` | key → `(factory, deps)`，惰性工厂。 |
| `_instances` | `Dict[str, Any]` | `{}` | 已实例化服务。 |
| `_effects` | `List[Disposer]` | `[]` | 可逆副作用列表（dispose 逆序执行）。 |
| `_disposed` | `bool` | `False` | 是否已销毁。 |

### 3.4 `Service`（service.py，类）

类属性：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `ClassVar[str]` | `""` | 插件名（配置行 id 未给定时用）。 |
| `provides` | `ClassVar[Optional[str]]` | `None` | 提供的 `ctx.<key>` 服务名。 |
| `inject` | `ClassVar[Tuple[str, ...]]` | `()` | 依赖的服务 key。 |

实例属性：`ctx`、`config`、`_disposer`（见 `__init__`）。

### 3.5 `PluginTarget` / `PluginFunction`（service.py，类型别名）

```python
PluginFunction = Callable[[Context], Optional[Disposer]]
PluginTarget = Union[type, Service, PluginFunction]
```

插件目标可以是 `Service` 子类、`Service` 实例、或函数插件 `apply(ctx) -> Disposer|None`。

### 3.6 `Entry`（loader.py，dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `id` | `str` | — | 配置行 id。 |
| `target` | `PluginTarget` | — | 待挂载插件对象。 |
| `config` | `Dict[str, Any]` | `field(default_factory=dict)` | 插件配置（整体替换语义）。 |
| `disabled` | `bool` | `False` | 是否禁用。 |

### 3.7 `MountedPlugin`（tree.py，dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `entry` | `Entry` | — | 对应配置行。 |
| `instance` | `Any` | — | 实例（类插件实例或函数插件本体）。 |
| `disposer` | `Optional[Any]` | `None` | `apply` 返回的清理函数。 |
| `started` | `bool` | `False` | 是否已调用异步 `start()`。 |

### 3.8 `PluginTree`（tree.py，类）

字段：`ctx`、`_entries: Dict[str, Entry]`、`_mounted: List[MountedPlugin]`（见 `__init__`）。

## 4. 函数与类方法详解

### 4.1 `dsh/kernel/events.py`

#### 模块级 `_is_coro`

```python
def _is_coro(fn: Callable[..., Any]) -> bool:
```

- 参数：`fn`（监听器）。
- 返回：`bool`。
- 行为：`inspect.iscoroutinefunction(fn)` 或对象带 `_is_coroutine` 属性（兼容 `functools.partial` / 装饰器包装）。仅用于「是否需 `create_task` 派发」的判断。

#### 模块级 `_maybe_await`

```python
async def _maybe_await(fn: Callable[..., Any], args: tuple) -> Any:
```

- 参数：`fn`（调用对象）、`args`（位置参数元组）。
- 返回：调用结果（异步则 await 后返回）。
- 行为：先同步调用 `fn(*args)`，若结果为 awaitable 则 `await`，否则原样返回。是 waterfall/serial/parallel 的统一调用入口。

#### `EventBus.__init__`

```python
def __init__(self) -> None:
```

初始化 `_listeners = {}`。无副作用。

#### `EventBus.on`

```python
def on(self, name: str, handler: Handler, *, prepend: bool = False) -> Callable[[], None]:
```

- 参数：`name`（事件名，如 `tools/pre-execute`）、`handler`（同步或协程）、`prepend`（True 插到队首）。
- 返回：注销函数（幂等，重复调用无副作用）。
- 行为：`setdefault` 建列表；`prepend` 用 `insert(0, ...)`，否则 `append`。注销时 `lst.remove(handler)`，`ValueError` 被吞掉。
- 边界：允许同一 handler 重复注册（会被调用多次）。

#### `EventBus.listener_count`

```python
def listener_count(self, name: str) -> int:
```

返回某事件监听器数量（调试/测试用）。无副作用。

#### `EventBus.snapshot`

```python
def snapshot(self, name: str) -> List[Handler]:
```

返回监听器列表的浅拷贝快照，派发与注册解耦（派发期间的新注册不影响本次派发）。无副作用。

#### `EventBus.emit`

```python
def emit(self, name: str, *args: Any) -> List[asyncio.Task]:
```

- 参数：`name`、`*args`。
- 返回：异步监听器对应的后台 `asyncio.Task` 列表（调用者可选择 await 或忽略）。
- 行为：遍历快照；协程监听器用 `asyncio.get_running_loop().create_task(self._contained(...))` 调度为后台任务；同步监听器立即调用，异常被 `log.exception` 隔离记录。
- 边界：**必须在运行中的事件循环内调用**（否则 `get_running_loop()` 抛 `RuntimeError`）。异常永不向调用者传播（fire-and-forget）。

#### `EventBus._contained`

```python
async def _contained(self, handler: Handler, name: str, args: tuple) -> None:
```

在 task 内执行异步监听器并隔离异常（`log.exception`）。私有方法，emit 专用。

#### `EventBus.parallel`

```python
async def parallel(self, name: str, *args: Any) -> List[Any]:
```

- 参数：`name`、`*args`。
- 返回：各监听器返回值列表（同步监听器返回值原样保留）。
- 行为：对每个 handler 构造 `_run`，内部 `_maybe_await`，异常隔离后返回 `None`；`asyncio.gather(*(...))` 并发执行并等待全部结束。

#### `EventBus.waterfall`

```python
async def waterfall(self, name: str, *args: Any,
                    default: Union[Any, Callable[[], Any]] = None) -> Any:
```

- 参数：`name`、`*args`、`default`（链末端返回值；callable 则调用生成，用于「默认放行」等动态决策）。
- 返回：链最终返回值（最外层监听器返回值）。
- 行为（洋葱中间件）：每个监听器签名 `handler(*args, next)`；调用 `await next()` 委派给下一个监听器，其返回值向上传播；不调用 `next()` 直接返回即短路。末端 `next()` 返回 `default`（callable 时求值，awaitable 则 await）。
- 边界：**监听器异常原样向上传播**（waterfall 不隔离异常）；短路语义由监听器决定。

#### `EventBus.serial`

```python
async def serial(self, name: str, *args: Any) -> List[Any]:
```

- 返回：各监听器返回值列表。
- 行为：按注册序依次 `await _maybe_await(handler, args)`（**无 `next` 参数**），异常向上传播（不隔离）。

### 4.2 `dsh/kernel/context.py`

#### `Context.__init__`

```python
def __init__(self, name: str = "root", parent: Optional["Context"] = None,
             bus: Optional[EventBus] = None) -> None:
```

- 行为：`events` 取 `bus`（若给）→ 否则 `parent.events`（共享）→ 否则新建 `EventBus()`。初始化各空容器。

#### `Context.provide`

```python
def provide(self, key: str, factory: Callable[["Context"], Any],
            *deps: str) -> None:
```

- 参数：`key`（服务名）、`factory`（工厂，接收 ctx）、`deps`（依赖的服务 key，用于文档与校验，实例化时先解析）。
- 行为：`_check_open()` 后写入 `_providers[key] = (factory, deps)`，并 `_instances.pop(key, None)` 清除旧实例。
- 边界：已销毁时抛 `ContextError`。惰性：首次 `get` 才调用工厂。

#### `Context.set`

```python
def set(self, key: str, instance: Any) -> None:
```

直接注入已构造实例（替代工厂注册）。`_check_open()` 后写入 `_instances`。

#### `Context.get`

```python
def get(self, key: str) -> Any:
```

- 返回：服务实例。
- 行为（解析序）：本层 `_instances` → 本层 `_providers`（先 `get(dep)` 解析依赖，再 `factory(self)`，缓存到 `_instances`）→ 父层 `get` → 抛 `ServiceNotFoundError`。
- 边界：工厂只被调用一次（结果缓存）；依赖解析不校验返回值。

#### `Context.has`

```python
def has(self, key: str) -> bool:
```

服务是否可解析（本层实例/工厂 → 父层递归）。无副作用。

#### `Context.__getattr__`

```python
def __getattr__(self, key: str) -> Any:
```

- 行为：`ctx.tools` 语法糖。`key.startswith("_")` 直接抛 `AttributeError`（保护私有属性）；否则 `get(key)`；`ServiceNotFoundError` 转为 `AttributeError`。

#### `Context.on` / `emit` / `parallel` / `waterfall` / `serial`

```python
def on(self, name: str, handler: Handler, *, prepend: bool = False) -> Disposer:
def emit(self, name: str, *args: Any):
async def parallel(self, name: str, *args: Any):
async def waterfall(self, name: str, *args: Any, default=None):
async def serial(self, name: str, *args: Any):
```

均为 `self.events` 的转发门面（`on` 返回注销函数；waterfall 的 `default` 透传）。无额外逻辑。

#### `Context.scoped`

```python
def scoped(self, name: str) -> "Context":
```

- 返回：子 `Context`（`parent=self`、`bus=self.events` 共享总线）。
- 行为：`_check_open()` 后创建；子作用域可注册局部服务，不影响父/其他作用域。对应 per-agent scope。

#### `Context.effect`

```python
def effect(self, disposer: Disposer) -> Disposer:
```

- 参数：`disposer`（`Callable[[], Any]`）。
- 返回：传入的 disposer（可单独调用作为注销函数）。
- 行为：`_check_open()` 后 `_effects.append(disposer)`。dispose 时逆序执行。

#### `Context._check_open`

```python
def _check_open(self) -> None:
```

已销毁则抛 `ContextError`。私有守卫，供 provide/set/scoped/effect 调用。

#### `Context.dispose`

```python
async def dispose(self) -> None:
```

- 行为：幂等（`_disposed` 已置位直接返回）；置位后**逆序**执行 `_effects` 中的 disposer（awaitable 的 await，异常 `log.exception` 隔离）；再逆序遍历 `_instances` 调 `close()`（存在且 callable 时，同样 await/隔离）。
- 边界：disposer 或 close 抛异常不影响其余清理。

### 4.3 `dsh/kernel/service.py`

#### `Service.__init__`

```python
def __init__(self, ctx: Context, config: Optional[dict] = None) -> None:
```

- 行为：保存 `ctx`、`config = config or {}`、`_disposer = None`。

#### `Service.apply`

```python
def apply(self, ctx: Context) -> Optional[Disposer]:
```

- 返回：可选额外清理函数（Loader 记录，卸载时调用）。默认返回 `None`。
- 行为：子类覆写，完成注册（服务、事件监听器、工具、prompt 分节……）。内部 `ctx.effect` 注册的副作用由 Context 统一回滚。

#### `Service.start`

```python
async def start(self) -> None:
```

可选的异步启动钩子（apply 之后调用）。默认空实现。

#### `Service.close`

```python
def close(self) -> None:
```

卸载钩子：调用 apply 返回的 `_disposer`（若存在）并置空。**注意**：同步调用、不 await（若 disposer 是协程需调用方自行处理）。

#### `Service.__repr__`

```python
def __repr__(self) -> str:
```

返回 `<Service {name 或类名}>`。

#### 模块级 `is_service_class`

```python
def is_service_class(target: PluginTarget) -> bool:
```

判断目标是否为 `Service` 子类（`isinstance(target, type) and issubclass(target, Service)`）。

#### 模块级 `plugin_inject`

```python
def plugin_inject(target: PluginTarget) -> Tuple[str, ...]:
```

读取声明的依赖服务名；函数插件返回 `()`。

#### 模块级 `plugin_provides`

```python
def plugin_provides(target: PluginTarget) -> Optional[str]:
```

读取提供的服务名；函数插件返回 `None`。

#### 模块级 `plugin_name`

```python
def plugin_name(target: PluginTarget) -> str:
```

类插件取 `name` 或类名；函数插件取 `__name__` 或 `repr`。

### 4.4 `dsh/kernel/loader.py`

#### `Entry.__repr__`

```python
def __repr__(self) -> str:
```

返回 `<Entry {id} ({enabled|disabled}) plugin={plugin_name}>`。

#### 模块级 `resolve_target`

```python
def resolve_target(spec: str) -> PluginTarget:
```

- 参数：`spec`（`"module.path:Attr"`，Attr 支持点分多级 `a.b.c`）。
- 返回：插件对象（类/实例/函数）。
- 行为：不含 `:` 抛 `LoaderError`；`importlib.import_module(module_path)` 失败抛 `LoaderError`；逐级 `getattr` 失败抛 `LoaderError`。

#### 模块级 `_platform_condition_met`

```python
def _platform_condition_met(condition: dict) -> bool:
```

- 行为：取 `sys.platform`，若 condition 含 `"platform"` 列表则返回 `platform in list(...)`，否则返回 `False`。私有。

#### 模块级 `row_disabled`

```python
def row_disabled(config: Dict[str, Any]) -> bool:
```

- 参数：`config`（**插件配置 dict**，即行内 `config` 字段内容）。
- 返回：该行是否禁用。
- 行为：`disabled` 优先——值为 bool 直接返回；为 dict 走 `_platform_condition_met`。否则看 `enabled`——bool 返回 `not cond`；dict 返回 `not _platform_condition_met`。二者皆无返回 `False`。
- 边界：只读 `config` 内部的条件（config 级）。行级条件由 `entry_from_row`/`_row_disabled_state` 优先处理。

#### 模块级 `entry_from_row`

```python
def entry_from_row(row: dict) -> Entry:
```

- 行为：取 `row["id"]`、`row["plugin"]`（缺 plugin 抛 `LoaderError`）；`plugin` 为 str 则 `resolve_target`，否则直接用作 target。
- **禁用条件两级**（行级优先，config 级次之）：
  - 行级 `disabled`/`enabled`（bool 或 `{platform: [...]}` dict）；
  - 否则 config 级（`row_disabled(config)`）。
- 无论哪级，`disabled`/`enabled` 都是控制元数据，会从 config 中剥离，**不混入插件 config**。

#### 模块级 `apply_patch`

```python
def apply_patch(entries: Dict[str, Entry], patch_rows: Sequence[dict],
                layer_name: str) -> None:
```

- 参数：`entries`（有序 id→Entry，**原地修改**）、`patch_rows`（YAML 顶层列表）、`layer_name`（错误信息用）。
- 返回：`None`。
- 行为（逐行分派）：
  1. 非 dict 行 → 抛 `LoaderError`；
  2. 含 `disable`：对每个 id 置 `entries[id].disabled = True`，未知 id 仅 `log.warning`；
  3. 含 `insert`：对每个 insert_row `entry_from_row`；id 已存在抛 `LoaderError`（duplicate id），否则加入；
  4. 含 `id`：**整体替换** `config`（非深合并，对应 dsh「restate unchanged fields」）；`_row_disabled_state` 按两级条件重算 `disabled` 并剥离控制键；未知 id 仅 `log.warning`；
  5. 其余 → 抛 `LoaderError`（需 `id`+`config`、`disable` 或 `insert`）。

#### 模块级 `_row_disabled_state`

```python
def _row_disabled_state(row: dict):
```

- 返回：`(disabled: bool, config: dict)`——行级条件优先，config 级次之；`disabled`/`enabled` 从 config 剥离。私有。

### 4.5 `dsh/kernel/tree.py`

#### 模块级 `_topo_sort`

```python
def _topo_sort(entries: List[Entry]) -> List[Entry]:
```

- 返回：按 `provides/inject` 拓扑排序后的条目。
- 行为（Kahn）：建 `provides_map`（服务名→Entry）；对每个 entry 的 `inject` 名在 `provides_map` 中查依赖（**查不到则忽略**，允许主程序手工 `set` 的服务）；构建入度表与 dependents 表；`queue` 从入度 0 开始 BFS。排序结果不全 → 抛 `LoaderError`（依赖环）。
- 边界：无 `provides` 的插件不参与图，视作叶子（最后挂载）。

#### 模块级 `_row_entry`

```python
def _row_entry(row: dict) -> Entry:
```

直接转发 loader 的 `entry_from_row`（延迟导入避免环），保证 bundle 行与 patch 行走同一入口、语义不漂移。

#### `PluginTree.__init__`

```python
def __init__(self, ctx: Context) -> None:
```

初始化 `ctx`、`_entries={}`、`_mounted=[]`。

#### `PluginTree.add_bundle_rows`

```python
def add_bundle_rows(self, rows: Sequence[dict], layer_name: str = "bundle") -> None:
```

- 行为：对每个 row 调 `_row_entry`（即 `entry_from_row`）直写 `_entries[row.id]`（同名覆盖）。

#### `PluginTree.apply_patch_rows`

```python
def apply_patch_rows(self, rows: Sequence[dict], layer_name: str) -> None:
```

转发 `apply_patch(self._entries, rows, layer_name)`，应用一个用户 patch 层。

#### `PluginTree.enabled_entries`

```python
def enabled_entries(self) -> List[Entry]:
```

按插入序返回未禁用条目。

#### `PluginTree.entries`

```python
def entries(self) -> List[Entry]:
```

按插入序返回全部条目（含禁用，供 dump 用）。

#### `PluginTree.mount`

```python
async def mount(self) -> List[MountedPlugin]:
```

- 返回：已挂载列表。
- 行为：`_topo_sort(enabled_entries())` 后逐个挂载：
  - 类插件：`entry.target(self.ctx, entry.config)` 实例化 → `instance.apply(self.ctx)` → `await instance.start()` → `mounted.started = True`；
  - `Service` 实例：`instance.apply(self.ctx)`；
  - 函数插件：`entry.target(self.ctx)` 作为 disposer，`instance` 记函数本身。
  任一异常 → `await self._rollback()` 后抛 `LoaderError(f"plugin failed: ...", entry_id=entry.id)`（fail loud）。

#### `PluginTree.mounted` / `is_mounted` / `get_entry` / `set_disabled`

```python
def mounted(self, entry_id: str) -> Optional[MountedPlugin]:
def is_mounted(self, entry_id: str) -> bool:
def get_entry(self, entry_id: str) -> Optional[Entry]:
def set_disabled(self, entry_id: str, disabled: bool) -> bool:
```

HMR 支撑原语：`mounted` 按 id 取已挂载记录（`_mounted_by_id` 索引）；`get_entry`
取条目（含禁用）；`set_disabled` 标记禁用状态。

#### `PluginTree.unmount_entry`

```python
async def unmount_entry(self, entry_id: str) -> bool:
```

卸载一条已挂载插件（HMR disable 用）：`instance.close()` + apply 返回的 disposer
（可 await）；从 `_mounted`/`_mounted_by_id` 移除。返回是否确实卸载。

#### `PluginTree.mount_additional`

```python
async def mount_additional(self, entries: Sequence[Entry]) -> List[MountedPlugin]:
```

运行期增量挂载（HMR insert 用）：对新增条目 `_topo_sort`（已挂载条目的 provides
视为可用依赖，缺失依赖不参与排序——它们已在 ctx 中激活）；逐个 `_mount_one`；
任一失败 → 逆序卸载本次新增部分并抛 `LoaderError`（last good tree）。

#### `PluginTree._rollback`

```python
async def _rollback(self) -> None:
```

逆序对已挂载 `instance.close()`（异常 `log.exception` 隔离），然后 `_mounted.clear()`。私有。

#### `PluginTree.dispose`

```python
async def dispose(self) -> None:
```

`await _rollback()` 后 `await ctx.dispose()`（卸载整棵树并回滚 context 副作用）。

## 5. 关键流程

### 5.1 boot 组合与挂载（对应 TS `composeEntries` / `boot`）

1. 空条目表 `entries = {}`。
2. 依次 `add_bundle_rows(...)` 应用各 bundle 行（等价 insert）。
3. 依次 `apply_patch_rows(...)` 应用 profile / home / `--patch` 覆盖层（顺序由 boot 决定，本模块只提供原语）。
4. `enabled_entries()` 过滤禁用行。
5. `_topo_sort` 按 provides/inject 拓扑排序。
6. `mount` 逐个挂载（类插件 apply→start）。
7. 任一失败 → `_rollback` 逆序 close 已挂载部分 → 抛带 `entry_id` 的 `LoaderError`。

### 5.2 waterfall 折叠算法（洋葱中间件）

伪代码：

```
next_():
    if index >= len(handlers):
        r = default() if callable(default) else default
        return await r if isawaitable(r) else r
    handler = handlers[index]; index += 1
    return await _maybe_await(handler, (*args, next_))
return await next_()
```

监听器通过 `await next()` 委派、直接返回即短路；异常向上传播。

### 5.3 Patch 行分派（apply_patch）

`disable` → 置禁用；`insert` → 去重后新增；`id`+`config` → 整体替换 config 并重算 disabled；其余 → 报错。

## 6. 事件与扩展点

本模块**不 emit 任何领域事件**；它提供的是事件派发**基础设施**（`EventBus` / `Context.on/emit/parallel/waterfall/serial`）。各域模块（tools/session 等）用这些门面派发自己的事件。扩展方式：调用 `ctx.on(name, handler)` 即可监听任意名字的事件，事件名由各域定义。

## 7. 常见改动指引

### 如何新增一个插件（Service）

1. 继承 `Service`，声明 `provides`（若对外提供服务）与 `inject`（依赖的服务名）。
2. 实现 `apply(self, ctx)`：`ctx.set("your_key", ...)` 或 `ctx.provide(...)`、`ctx.on(...)` 注册监听器，返回可选的 disposer。
3. 在配置（bundle/patch）中加一行 `{"id": "xxx", "plugin": "your.module:YourService", "config": {...}}`。
4. 如需异步初始化，覆写 `async def start(self)`（`mount` 会在 apply 后 await 它）。

### 如何新增一个事件派发模式/监听器

- 监听：`ctx.on("my/event", handler)`；`handler` 可同步或协程。
- 需要洋葱中间件语义用 `waterfall`，需要并发用 `parallel`，需要顺序结果用 `serial`，fire-and-forget 用 `emit`。

### 如何扩展 loader 的 Patch 语义

在 `apply_patch` 的分派链中新增 `elif` 分支（如新的 `disable` 条件、新的操作符）；保持「未知结构 fail loud 抛 `LoaderError`」的风格。若新增行级条件字段，注意 `row_disabled` 目前只读 `config` 内部。

## 8. 相关测试

`tests/test_kernel.py`：覆盖 EventBus 四种派发（waterfall 顺序/短路/default 工厂、parallel/serial、emit 异常隔离）、Context（惰性/作用域/逆序 dispose）、resolve_target、apply_patch（replace/disable/insert/平台条件）、PluginTree 拓扑挂载与失败回滚。
