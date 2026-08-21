# 18 · Code Mode（代码模式）开发手册

> 对应 TS 版 dsh-tools 的 Code Mode（`presentAs`/`run_code`/`tools/code-dispatch-log`）
> 与 dsh-code-runtime 的代码执行 seam（Python 后端内建）。本手册覆盖
> `dsh/code/runtime.py`、`dsh/code/sdk.py`、`dsh/code/mode.py` 与
> `dsh/tools/registry.py` 的 Code Mode 部分。所有签名经 `inspect` 核验。

## 1. 总表

| 模块 | 入口 | 文件 | 职责 | 测试 |
|---|---|---|---|---|
| 代码执行 seam | `CodeRuntime`（ctx.codeRuntime） | `dsh/code/runtime.py` | 执行 async Python 程序体，捕获输出/返回值，失败为结果字段 | test_runtime_* 全组 |
| SDK 渲染 | `render_tools_sdk_py` / `json_schema_to_py` | `dsh/code/sdk.py` | 可见工具 → Python 声明块（TypedDict + Tools Protocol） | test_sdk_* |
| run_code 传输 | `build_run_code_tool` / `_DispatchScheduler` | `dsh/code/mode.py` | 保留传输 + 子调用派发桥 + dispatch 事件 | test_run_code_* 全组 |
| 注册表 Code Mode | `ToolRuntime.mode/presentAs/schemas/prepare/dispatch/finalize` | `dsh/tools/registry.py` | wire 坍缩 / code-only 强制 / 三段调度 | test_mode_*、test_collapsed_* 等 |

## 2. 定位与语义（对应 TS 版 README 的契约）

- **mode**：tools 行的 `config.mode ∈ native | code | both`（默认 native），
  决定模型**看到什么**（wire 投影），目录（能力集合）不变。
  - `native`：原生 schema，无 run_code，无 SDK 分节（无需 code runtime）。
  - `code`：wire 坍缩为 **`[run_code]`**；SDK 分节声明其余工具；
    `tools:code-only` 规则（order 99，先于 100–199 工具指引带）声明只有
    run_code 可直接调用；**执行器同谓词强制**：模型直呼任何可见工具在策略
    管线**之前**以 `UNKNOWN_TOOL` 确定性拒绝，并携带回到程序的路线提示
    （`only `run_code` is callable directly — call `x` from inside a `run_code`
    program instead`）。
  - `both`：原生 schema + run_code + SDK 分节；无 code-only 规则（原生调用会执行）。
- **presentAs(mode)**：agent 作用域声明遮蔽部署默认（最近层胜出）。只接受
  **局部运行时**调用（preset 挂载的 `ToolRuntime(parent=根运行时)`）；
  进程全局展示是 tools 行的 `mode` 配置字段（全局运行时调用抛 ToolError）。
  一个作用域一次声明（二次抛冲突）。返回还原 disposer。
- **保留名**：`run_code` 无条件保留——任何模式/任何层注册、遮蔽、restrict
  一律拒绝（`ToolError`）；不在任何可过滤层中（allow/deny 对它无效）。
- **无递归**：SDK/绑定枚举不含 run_code 自身，程序里 `tools.run_code`
  得到 `ToolCallError("run_code", "no such tool: run_code")`。

## 3. CodeRuntime 分节（`dsh/code/runtime.py`）

### 3.1 类型

| 类型 | 字段 | 说明 |
|---|---|---|
| `CodeRunFailure` | `kind`、`message` | 失败类 ∈ exception/timeout/abort/worker-exit/invalid-output/output-limit（正交，绝不互相替代） |
| `CodeRunResult` | `logs: List[str]`、`value`、`error?` | 一次 run 的结果；**error 是字段**，不是异常路径 |
| `CodeToolCallError(ToolError)` | `toolName`、`message` | 程序可见的工具调用失败（TS 版 ToolCallError 契约） |

模块常量：`_PORTABLE_IDENTIFIER`（`^[A-Za-z_][A-Za-z0-9_]*$`）、
`RESERVED_BINDING_GLOBALS = {"console", "__dsh_main__"}`、
`_MAX_ERROR_TEXT = 4000`。

### 3.2 函数/方法详解

- `CodeRuntime.__init__(self, ctx, config=None)`：
  `max_output_bytes = int(config.get("max_output_bytes", 64*1024*1024))`；
  `timeout_ms = float(config.get("timeout_ms", 30000))`；`_run_lock = asyncio.Lock()`。
- `apply(self, ctx)`：`ctx.set("codeRuntime", self)`。
- `async run(self, program, bindings, signal=None) -> CodeRunResult`
  1. **绑定校验**：每个 `{global, functions, error_class?}`——`global` 必须匹配
     可移植标识符子集且非保留名；`error_class.name` 同校验且不与全局名冲突；
     失败 → `CodeRunFailure("exception", ...)`（结果路径）。
  2. **signal 已中止** → `abort` 失败。
  3. **编译**：`program` 逐行缩进 4 格，包裹为
     `async def __dsh_main__(<globals>, <error names>):`；`SyntaxError` →
     `exception` 失败（消息含 "syntax error:"）。
  4. **执行**（`async with self._run_lock`——redirect_stdout 是进程全局的，
     以锁串行化输出捕获）：`contextlib.redirect_stdout(StringIO)` 下运行；
     - 有 signal：`asyncio.wait({task, waiter})`（`signal.wait()`），先完成者胜；
       waiter 先 → `task.cancel()` → `abort` 失败（保留已捕获 logs）；
     - 预算：`asyncio.wait_for(..., timeout=timeout_ms/1000)` → `timeout` 失败
       （保留已捕获 logs）；
     - 程序抛出 `CodeToolCallError`（未被捕获的绑定失败）→ `exception`
       失败（`"{toolName}: {message}"`）；其他异常 → `exception`
       （`"Type: 消息"`，截断 4000 字符）。
  5. **完成值物化**：`is_json_value(value)` 不通过 → `invalid-output`；
     `json.dumps([logs, value])` 字节数超 `max_output_bytes` → `output-limit`
     （`_fit_logs_prefix` 保留能容纳的 logs 前缀）；`value is None` = 缺省
     result 键（与 TS 版 undefined 语义一致）。
- `_ToolsNamespace`：`__getattr__`（`__` 前缀走 AttributeError；未知名 →
  `CodeToolCallError(name, f"no such tool: {name}")`）+ `__getitem__`
  （异名/保留名下标访问）。

### 3.3 文档化边界（in-process 后端）

- signal 中止是**协作式**的（await 点生效；同步阻塞代码无法硬停）；
- stdout 捕获经全局 redirect，run 之间以锁串行；
- 绑定函数与程序跑在同一进程（无 worker 隔离——TS 版 worker-thread 的
  隔离语义留作「未来工作」）。

## 4. SDK 渲染分节（`dsh/code/sdk.py`）

- `_is_bare_identifier(name)`：`str.isidentifier()`（渲染器与执行器同解释器，
  无 TS 版的 Unicode 表版本偏移）+ `NFKC(name) == name` + 非
  `keyword.kwlist ∪ {True,False,None,__debug__}`。
- `_describe(schema)`：折叠 `\s+`、C0/C1 控制字符转义 `\xNN`、trim；空 → None。
- `_doc_lines(description, indent)`：反斜杠与双引号转义后包一行 `"""..."""`。
- `_camel_case`/`_cap_name_base(120)`/`_child_name`/`_RenderState.allocate`
  （碰撞计数 `2,3,…`）：类名分配与传播（同 TS 版线性保证）。
- `_render_type(schema, class_name, state, list_depth=0)`：
  oneOf → `A | B`；const/enum → `Literal[...]`（`_py_scalar` 用 `json.dumps`
  双引号字符串）；integer/int、number/float、string/str、boolean/bool、
  null/None；array → `list[T]`（`items` 缺失 → `list[Any]`；`_MAX_LIST_NESTING=180`
  超深 → `Any`）；object：字段名全部裸标识符且无 `__` 前缀才声明
  `class X(TypedDict)`（required 直接字段、非 required 包 `NotRequired`、
  开放对象加注释行、空体加 `pass`），否则 `dict[str, Any]`；异常一律退化
  `Any`（trusted-after-validation 立场）。
- `json_schema_to_py(schema)`：无上下文入口（object 退化 `dict[str, Any]`）。
- `render_tools_sdk_py(schemas)`：工具按名字典序；每个工具渲染
  `XArgs`/`XOutput` 两个 TypedDict + `async def name(self, args: XArgs) ->
  XOutput:`（裸标识符路径，docstring 来自 description）或下标注释
  `# tools["name"](args: X) -> Y`；`from typing import ...`（`_TYPING_ORDER`
  过滤实际使用）；`class ToolCallError(Exception): toolName: str`；
  `class Tools(Protocol)` + `tools: Tools`；整体 =
  `SDK_INSTRUCTIONS + "\n\n```python\n<declaration>\n```"`。

## 5. run_code 传输与派发桥分节（`dsh/code/mode.py`）

### 5.1 常量与事件

- `RUN_CODE_NAME = "run_code"`；`PYTHON_FLAVOR`（工具 description +
  code 参数 description 的语言感知字符串，python 单语言）；
  `RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION`（description 参数的 UI 标签契约）。
- 模块导入时注册两个会话事件词汇：
  `tool/code-dispatch-start`（派发进入：root/parent/sub_call_id + name +
  arguments 快照）、`tool/code-dispatch`（结算：is_error + content 副本，
  可被 `tools/code-dispatch-log` 监听器替换）。**两者都不进派生历史**
  （非 surface）。
- `CodeRunFailedError(ToolError)`：`code="CODE_RUN_FAILED"`。

### 5.2 build_run_code_tool(registry, require_runtime, max_parallel=10)

- `define_tool`：parameters = code（string required）+ description
  （string required）；output schema = `{object, additionalProperties: false,
  properties: {logs: [string], result: None}, required: [logs]}`；
  `render = _render_run_code`（logs 行 + 结果渲染，皆空 →
  `(run_code completed with no output)`）；`present_call` = generic 卡片
  （title = description，kind = execute，raw_input = code——TS 版 presentCall
  对应物）。
- `execute(args, run_ctx)`：
  1. `description` 空白 → `ToolError("invalid description...")`；
  2. `runtime = require_runtime()`（code 模式挂载校验）；
  3. `run_signal = AbortSignal()`；跟随外层信号（`follow_outer` task：
     `await outer.wait()` → `run_signal.abort("outer abort")`）；
  4. `_DispatchScheduler(registry, max_parallel, run_signal, execution,
     run_ctx, agent, scope)`；
  5. 绑定枚举：`registry.list(scope)` 排除 run_code——**调用 agent 的可见集合**
     （作用域工具并入、受限全局消失），与 SDK 声明同一视图；
  6. `runtime.run(program=code, bindings=[{"global":"tools","functions":...,
     "error_class":{"name":"ToolCallError","memberNameProperty":"toolName"}}],
     signal=run_signal)`；
  7. `finally`：`run_signal.abort("run_code settled")` → `scheduler.drain()`
     → cancel follower；
  8. `result.error` → `CodeRunFailedError("code run failed (<kind>): <msg>"
     + "\nCaptured output:\n<logs>")`（管线转为 isError 结果，模型可自纠）；
  9. 成功 → `{"logs": ...}`（`result` 键仅当有值时）。

### 5.3 _DispatchScheduler（原生并发契约的有序 lane）

| 成员 | 说明 |
|---|---|
| `pending` / `commit_queue` | 提交序启动队列 / 提交序结算队列 |
| `in_flight` / `exclusive_active` | 并发池 / 独占屏障（覆盖 post-execute 的完整结算） |
| `_counter` → `sub_call_id` | `<outer call_id>:code:<n>`（按提交序编号） |

- `binding(name) -> async call(args)`：run 已结束 → `CodeToolCallError`；
  `is_json_value(args)` 不通过 → `CodeToolCallError`（lossless JSON 契约）；
  `copy.deepcopy` 双快照（派发值 + 日志值，工具改参不污染日志）→
  `_PendingDispatch` 入队 → 唤醒 driver → `await pending.future`。
- `_driver()`（单有序 lane 循环）：
  1. **commit 头**：`commit_queue[0].settled` → `await head.commit()`；
     exclusive 条目 commit 完成才释放 `exclusive_active`；
  2. **启动头**：run 已结束 → abandon（未启动不落日志）；否则
     `classify()`（**启动时重读** `execution_mode`，fail-closed）；
     容量 = `not exclusive_active and (parallel → in_flight < max_parallel;
     exclusive → in_flight == 0)`；够 → `await head.start()`（有序 prepare +
     预结算），body 进 `in_flight`（并发段）；
  3. 队列与池全空 → 退出；否则睡 `_wake`。
- `_PendingDispatch.start()`：append `tool/code-dispatch-start`（有序 lane
  内，先于 prepare）→ `registry.prepare(..., parent=外层 token)`（豁免 code
  坍缩）→ dispatch 类：`ensure_future(registry.dispatch_prepared(...))`，
  完成回调置 `settled`；result 类（拒绝/未知/中止）：直接 settled。
- `_PendingDispatch.commit()`：dispatch 类 → `registry.finalize_prepared`
  （post-execute + finalize_content + tools/result，**提交序有序**）；
  result 类 → `finish_prepared`。然后：非 error → `additional_contexts` 经
  外层 `run_ctx.defer_context` 延迟提交（保持调用/结果相邻）、
  `concludes_turn` 传导外层；**程序先取值**（error → future 抛
  `CodeToolCallError(name, message)`；成功 → future 置 value）；最后
  `schedule_log`。
- `schedule_log(pending)`：`tools/code-dispatch-log` waterfall（载荷 =
  `{exec, agent, sub_call_id, name, is_error, content}`；监听者抛错 →
  记日志并保留原内容）→ `agent.session.append("tool/code-dispatch", ...)`；
  fire-and-forget task + 背压（`len(_log_tasks) > max_parallel` 时等一个完成）。
- `drain()`：`await shield(_driver_task)`（run 结算时 driver 把排队未启动的
  abandon、等在途全部结算）→ 汇集剩余日志任务。

## 6. 注册表 Code Mode 集成（`dsh/tools/registry.py`）

- `_ScopeState.mode`；`mode_for(scope)`（链上最近层声明，否则 `default_mode`）。
- `apply`：根运行时注册两个提示分节（`tools:code-only` order 99、
  `tools:sdk` order 150），text 为 callable（读 `assemble_ctx["scope"]`）：
  code 作用域渲染规则/SDK，native 渲染空（被组装丢弃）。
- `schemas(scope)`：native → 白名单投影；非 native → `require_code_runtime`
  （缺 runtime 或 language ≠ python 均 fail loud）→ code 只 `[run_code]`，
  both = 原生 + run_code。
- `_require_code_transport()`：懒构建共享传输（进程一个，闭包捕获注册表）。
- `prepare(...)` 第 0 段：`definition is not None and parent is None and
  mode_for(scope) == "code" and name != "run_code"` → 信号已中止先
  ABORTED_BEFORE_DISPATCH，否则 UNKNOWN_TOOL（**策略管线之前**）。
- 三段调度：`execute = prepare → dispatch → finalize`；
  `prepare`（有序：冻结 + 校验 + 坍缩 + pre-execute + guards）→
  `dispatch_prepared`（around + body，可并发）→ `finalize_prepared`
  （post-execute + finalize_content + deferred + `tools/result` 广播）→
  `finish_prepared`（预结算：仅广播）。run_code 桥复用同一三段 = 原生
  并发契约（提交序启动 / parallel 重叠 ≤ max_parallel_sub_calls /
  exclusive 独占持障到结算）。
- **作用域委托修复**（本批 bug）：局部 `ToolRuntime(parent=根)` 的全部层
  操作（register/restrict/guard/get/list/execute）委托到父层的
  `_scoped[ctx.name]`。此前局部运行时自持层，循环经根运行时按
  `scope=agent.ctx_name` 执行时查不到 preset 工具（注册可见但执行
  UNKNOWN_TOOL）；委托后 preset 工具既对外不可见、又可在 run_code 程序内
  与原生循环中正常执行。

## 7. 验证

- 本批测试 27 项（wire 坍缩/presentAs 6 · SDK 渲染 3 · runtime 10 · run_code 桥 8）；
  全量 **160 passed**；headless/compileall 通过。
- 已知文档化边界：in-process runtime 无 worker 隔离；code 模式坍缩与
  SDK 语言当前仅 Python（新增语言 = SDK 渲染器 + flavor 表 + runtime 三处并行）。
