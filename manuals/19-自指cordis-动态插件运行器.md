# 19 · 自指 cordis（动态 Cordis Plugin 运行器）开发手册

> 对应 TS 版 cordis-host-runner + tool-cordis 的 **host-only 子集**：
> 不可变 Package 定义、每 Plugin 单活动运行、审批门控的 Client 激活、
> Host 方法调用（Client RPC 对应物）与四个生命周期事件
> （cordis/request-run、cordis/request-run-resolved、cordis/dynamic-package、
> cordis/dynamic-retract）。本手册覆盖 `dsh/cordis/` 全包。签名经
> `inspect` 核验。

## 1. 总表

| 模块 | 入口 | 文件 | 职责 | 测试 |
|---|---|---|---|---|
| 线协议词汇 | dataclass + 收据助手 | `dsh/cordis/types.py` | 运行尝试/半状态/诊断/收据（camelCase 线形状） | 全组 |
| 注册表 | `DynamicCordisRegistry` | `dsh/cordis/registry.py` | 身份铸造 + 不可变包 + 生命周期指针 + 挂起请求 | test_define_new_and_existing_immutable 等 |
| 沙箱 | `precheck_code` / `evaluate_host_code` | `dsh/cordis/sandbox.py` | host 半：async 函数体 + ctx/harness/console + AST 危险导入拒绝 | test_define_precheck_syntax_and_imports 等 |
| 检查目录 | `CordisInspectRegistryService` | `dsh/cordis/inspect.py` | 只读提供者目录（内建 harness + 可注册） | test_inspect_registry_* |
| 运行器 | `DynamicCordisRunnerService` | `dsh/cordis/runner.py` | define/undefine/run/stop/自省/invoke/审批/事件 | test_run_* 全组 |
| 工具 | 7 个 cordis_* + `ToolCordisPlugin` | `dsh/cordis/tools.py` | 模型可调用的动态插件面 | test_cordis_tools_*、test_e2e_* |

## 2. 定位与语义（与 TS 版对齐的契约）

- **不可变 Package**：一次 cordis_define 铸一个 packageId（`dyn-<n>`），
  定义后不可变；给已有 Plugin 追加版本用 `plugin.kind="existing"`。
- **每 Plugin 单活动运行**：run（无成功版本或重跑当前）/ update（切换版本）
  二态意图；激活新版本**先回收旧运行**（替换语义）。
- **会话所有权**：Plugin 归定义它的 session 所有；一切操作校验
  `plugin.session_id == agent.id`，否则 plugin-missing（消息说明「不存在或
  非本会话所有」）。
- **事件四件套**（emit，根总线）：
  `cordis/request-run` {requestId, agentId, pluginId, packageId, mode, name,
  purpose, requiresApproval}、`cordis/request-run-resolved` {requestId,
  outcome: approved|rejected|cancelled}、`cordis/dynamic-package` {pluginId,
  packageId, pluginRunId, name}、`cordis/dynamic-retract` {pluginId,
  packageId, pluginRunId}。
- **文档化边界**：无浏览器 client 运行时——client 半只存与检查
  （precheck），带 client 代码的包 run 返回 `client-half-failed`（指引把能力
  移入 code.host）；host 半在进程内执行（与 run_code 同信任立场，非容器；
  AST 级危险导入拒绝 + 协作式预算）。

## 3. 词汇表分节（`dsh/cordis/types.py`）

| 类型 | 字段 | 说明 |
|---|---|---|
| `CordisHalfState` | `status`（absent/pending/stopped/running/waiting/failed）、`waiting_for`、`error?` | 一平台半状态 |
| `CordisRunDiagnostic` | `phase`、`message`、`plugin_id`、`package_id`、`plugin_run_id` | 绑定精确尝试的结构化失败；phase ∈ approval/host-load/host-apply/client-load/... |
| `CordisRunAttempt` | `plugin_run_id`、`package_id`、`mode`、`status`、`approval_request_id?`、`requires_approval`、`host`、`client`、`error?` | 最新激活尝试（独立于物理运行保留）；status ∈ awaiting-approval/starting-host/running/stopped/rejected/failed/cancelled |

收据/响应助手（线形状 camelCase）：`define_receipt`、`run_response_ok/fail`
（ok 含 status ∈ awaiting-approval|starting|running + current/nextPackageId）、
`stop_response_ok/fail`（reason ∈ plugin-missing|not-running）、
`undefine_receipt`（ok + wasRunning）、`invoke_result_ok/fail`（code ∈
plugin-not-running|stale-run|method-not-found|handler-error）、
`inspect_resolution_ok/fail`（reason ∈ provider-missing|method-missing|
invalid-input|provider-error）。

## 4. 沙箱分节（`dsh/cordis/sandbox.py`）

- `_DENIED_IMPORTS`：os/subprocess/socket/shutil/ctypes/importlib/sys/pathlib
  → AST 级拒绝 + cordis 服务替代指引（与 TS 版 require/fetch 陷阱同构）。
- `precheck_code(code, half)`：定义期预检——先 AST 扫危险导入（拒绝消息含
  ``imports `os``` 与指引），再按 `async def __dsh_plugin__():` 包裹编译；
  `SyntaxError` → ``failed to parse`` + 源码行 + 插入号 + `_PRELUDE`
  （函数体/缩进/括号配平教学提示）。抛 `ToolError`。
- `evaluate_host_code(code, plugin_id, vm_timeout_ms, scope_ctx, harness,
  console)`：exec 包裹源码（namespace 注入 `ctx`/`harness`/`console`）→
  `await` 插件函数体（`asyncio.wait_for` 协作式预算）→ 返回插件对象；
  失败归一化 `ToolError`（failed to load / failed / budget 消息）。
- `_TaggedConsole`：logging 通道（`dsh.cordis.pkg.<pluginId>` logger），
  log/info/warn/debug/error。
- `HOST_BUILTIN_INSPECTION`：ctx/harness/console 三个签名文档条目
  （内建 inspect 提供者的数据源）。

## 5. 运行器分节（`dsh/cordis/runner.py`）

### 5.1 配置与字段

`DynamicCordisRunnerService.provides = "dynamicCordisRunner"`；
config：`vm_timeout_ms`（默认 5000，<1 抛 ToolError）、`requires_approval`
（默认 False——True 时每次 run 经 ctx.approval 审批门控）。
字段：`root_ctx`、`_registry`、`inspect_registry`（`apply` 时
`ctx.set("cordisInspect")` 若未挂载）、`_starting`（激活去重表）、
`_retract_task`、`_closed`。

### 5.2 define / undefine

- `define(request, session_id) -> receipt`：
  1. name/purpose trim 非空、`code.host`/`code.client` 至少其一、两半各
     `precheck_code`；
  2. kind=new：`idPrefix` 全匹配 `[a-z]{3,6}` → `mint_plugin_id`（`<prefix>-<n>`
     每前缀计数）→ 注册 plugin 记录（session_id/packages/approval 集合/
     生命周期指针全 None）；kind=existing：所有权校验；
  3. `mint_package_id` → 不可变 definition {package_id, name, purpose,
     host_code?, client_code?} 入 plugin["packages"]。
- `undefine(agent, plugin_id) -> receipt`：所有权 → 撤销挂起审批 → 回收
  运行 → 删除（含全部版本）；`{ok: true, wasRunning}`。

### 5.3 run（核心状态机）

1. `_resolve_plan`：plugin-missing / package-missing / mode 校验
   （update 需有 current 且 target ≠ current；run 需 target == current 或
   current 为空；非法 mode 字符串拒绝）→ 明确指引消息；
2. signal 已中止 → cancelled；`pending_for(plugin_id)` 非空 →
   transition-in-flight；
3. client 半存在 → 铸造失败尝试（phase=client-load）→
   `client-half-failed`（边界消息：无浏览器运行时，移入 code.host）；
4. 铸造尝试（`mint_run_id` → `run-<n>`，host 半 pending）；next_package_id
   = target；latest_run = attempt；
5. `requires_approval`：铸 requestId（`req-<n>`）→ arm（registry）→
   attempt.status = awaiting-approval → emit `cordis/request-run` →
   `ctx.approval.request(...)`（无 approval 服务 = 拒）→ **状态再校验**
   （`attempt.status != "awaiting-approval"` = 已被 stop/undefine/close 撤销，
   返回 cancelled 且不再发 resolved）→ claim + emit
   `cordis/request-run-resolved`（approved/rejected）→ 拒 → fail_attempt
   （phase=approval）→ `rejected`；允 → starting-host；
6. `_activate`（`_starting` 去重表）→ `_start_fresh`：**先回收旧运行** →
   铸 fiber 作用域 `root.scoped(f"cordis:{plugin_id}")` + 包私有局部
   ToolRuntime（注册落根注册表的 `_scoped["cordis:<id>"]` 层，对外不可见）→
   `evaluate_host_code`（注入 ctx=作用域、harness、tagged console）→
   返回值校验（callable / Service 类 / Service 实例，否则
   "must return a plugin"）→ `PluginTree` 单条目挂载（id `cordis:<id>`）→
   运行记录 {plugin_run_id, package_id, handlers, fiber, scope}；
   失败：tree dispose + scope dispose + 运行记录回滚；
7. 成功：current_package_id = target、next_package_id = None、attempt →
   running、emit `cordis/dynamic-package` → run_response_ok("running", ...)。

### 5.4 stop / 自省 / invoke / close

- `stop`：所有权 → 无运行且无挂起 → not-running；撤销挂起（resolved
  outcome=cancelled）→ 回收运行（emit `cordis/dynamic-retract`）→
  latest_run → stopped（host/client 半非 absent 同置 stopped）。
- `snapshot(agent)`（`cordis_inspect_self` 数据源）：pluginId + 包摘要
  （packageId/name/purpose/hasHostHalf/hasClientHalf）+ current/nextPackageId
  + activeRun {pluginRunId, packageId, handlers} + latestRun（clone）。
- `list_plugins` / `inspect_plugin` / `inspect_package`（含源码 code
  {host?, client?}）/ `reference`：无源码→含源码的元数据视图。
- `invoke(plugin_id, plugin_run_id, method, args)`：plugin-not-running /
  stale-run（run id 不符）/ method-not-found / handler 执行（await 协程）→
  结果 lossless JSON 校验（handler-error "not lossless JSON"）/ 异常 →
  handler-error。
- `close()`：**幂等**——撤销全部挂起审批 + 回收全部活动运行（
  `_retract_task` 汇集）+ 清空注册表。

### 5.5 `_Harness`（host 代码的 `harness` 全局）

- `handle(method, handler) -> 注销函数`：包私有 Host 方法（invoke 调用面）；
  非空名字 + 同运行去重（duplicate host method）。
- `defineTool(definition)`：校验 ToolDefinition 实例并原样返回。
- `registerTool(ctx, tool)`：注册进调用者作用域 ctx（落在包自己的层）。

## 6. 检查目录分节（`dsh/cordis/inspect.py`）

- `CordisInspectRegistryService.provides = "cordisInspect"`；
  `register_provider(id, description, methods)`（同名替换，非内建去重）→
  注销函数；内建提供者 `harness`（ctx/harness/console 三方法，返回
  HOST_BUILTIN_INSPECTION 的签名文档）。
- `list()` → `[{platform: "host", id, description, methods: [{name,
  description, inputSchema, outputSchema}]}]`。
- `query(provider_id, method, input)`：provider-missing / method-missing /
  输入按 input_schema 校验（invalid-input）/ call（await 协程）/ 输出按
  output_schema 校验（provider-error）+ lossless JSON 终检。

## 7. 工具分节（`dsh/cordis/tools.py`）

| 工具 | 参数 | 说明 |
|---|---|---|
| `cordis_define` | name/purpose/plugin{kind,idPrefix?,pluginId?}/code{host?,client?} | 定义（预检失败=ToolError 带教学消息） |
| `cordis_run` | pluginId/packageId/mode(run\|update) | 启动/切换（signal 透传） |
| `cordis_stop` | pluginId | 停止 |
| `cordis_undefine` | pluginId | 删除 |
| `cordis_inspect_list` | — | 检查目录（无需 agent） |
| `cordis_inspect_query` | provider/method/input? | 只读查询 |
| `cordis_inspect_self` | — | 本会话插件快照 |

除 `cordis_inspect_list` 外均要求父 agent（无 → `NO_AGENT` ToolError）；
结果 JSON 渲染。`ToolCordisPlugin.inject = ("tools", "dynamicCordisRunner")`。

## 8. 验证

- 本批测试 21 项（define 校验/预检/不可变 4 · run 生命周期 8 · 审批门控 3 ·
  inspect 2 · 工具 3 · e2e 1 · 幂等关闭 1）；全量 **181 passed**；
  headless/compileall 通过。
- 文档化边界：client 半无运行时（存储/检查）；host 沙箱非容器
  （协作式预算 + AST 导入拒绝）；激活替换语义（新版本先回收旧运行）。
