# 15 · MCP 与 Cron 开发手册（第三批）

> 对应 TS 版：MCP（一个插件一个服务器：discover tools → `ctx.tools.register()`）
> 与 schedule/cron。
> 本手册覆盖 `dsh/mcp/mcp.py`、`dsh/schedule/cron.py`、`dsh/schedule/schedule.py`
> 与测试夹具 `tests/fixtures/mock_mcp_server.py`。所有签名经 `inspect` 核验。

## 1. 总表

| 子系统 | 服务/入口 | 文件 | 职责 | 测试 |
|---|---|---|---|---|
| MCP 客户端 | `McpClient` | `dsh/mcp/mcp.py` | stdio JSON-RPC 2.0 握手/发现/调用 | test_mcp_client_discover_and_call、test_mcp_client_stop_kills_process |
| MCP 插件 | `McpServerPlugin`（bundle 行） | `dsh/mcp/mcp.py` | 配置行启停服务器、工具注册进 ctx.tools | test_mcp_client_discover_and_call |
| schema 降级 | `safe_schema` | `dsh/mcp/mcp.py` | 超子集输入 schema 降级开放对象 | test_mcp_safe_schema |
| cron 解析 | `parse_cron`/`CronSpec` | `dsh/schedule/cron.py` | 5/6 字段表达式 → 匹配/下次触发 | test_cron_parse_and_matches、test_cron_next_after、test_cron_invalid |
| Schedule cron 条目 | `ScheduleService` | `dsh/schedule/schedule.py` | interval 与 cron 两种条目 | test_schedule_cron_entry_fires、test_schedule_register_tool_cron_and_validation |
| mock MCP 服务器 | （测试夹具） | `tests/fixtures/mock_mcp_server.py` | stdio JSON-RPC 夹具（echo/lenient） | 上述 MCP 用例 |

## 2. MCP 分节

### 2.1 定位与依赖

- 对应 TS 版「MCP: one plugin per server: discover tools → `ctx.tools.register()`」。
- `McpServerPlugin.inject = ("tools",)`；`McpClient` 是纯客户端类（无 ctx）。
- 协议：stdio、**newline-delimited** JSON-RPC 2.0（每行一个消息；JSON 不得含裸换行）。

### 2.2 关键类型

| 类 | 字段/常量 | 说明 |
|---|---|---|
| `McpClient` | `command: List[str]`、`env`、`timeout=60.0`、`process`、`_next_id`、`_pending: Dict[int, Future]`、`_reader_task` | 客户端实例 |
| `PROTOCOL_VERSION = "2024-11-05"` | 模块常量 | initialize 协商版本 |
| `MAX_TOOL_NAME = 128` | 模块常量 | 工具名上限（prefix 拼接后截断） |

### 2.3 函数/方法详解

#### `McpClient.__init__(self, command, env=None, timeout=60.0)`

- 参数：`command`（argv 列表）、`env`（子进程附加环境）、`timeout`（请求默认超时秒）。
- 行为：仅存字段；进程在 `start()` 才创建。

#### `McpClient.start(self) -> None`

- `asyncio.create_subprocess_exec(*command, stdin/stdout=PIPE, stderr=DEVNULL, env={**os.environ, **env})`；启动失败抛 `ToolError(MCP_SPAWN_FAILED)`；
- 建 `_reader_task`（`_read_loop`）；
- `request("initialize", {protocolVersion, capabilities:{}, clientInfo})`；版本非 2024-/2025- 前缀仅 warning；
- `await notify("notifications/initialized")`（无 id 通知）。

#### `McpClient.stop(self) -> None`

- 取消 `_reader_task`；进程仍活则 `terminate()` 等 5s，超时 `kill()`；
- 对全部未决 future `set_exception(ToolError(MCP_CLOSED))`（不 cancel——cancel 会污染调用方语义）。

#### `McpClient._write(self, message) -> None`（async）

- `json.dumps` 成一行（ensure_ascii=False）+ `\n`，`stdin.write` 后 **`await drain()`**——asyncio stdin 缓冲必须 drain 才到达对端（第一批修复的缺陷）。

#### `McpClient.notify(self, method, params=None) -> None`（async）

- 发送无 id 通知（不等待响应）。

#### `McpClient.request(self, method, params=None, timeout=None) -> Any`（async）

- 分配自增 id → 建 future 入 `_pending` → 写请求 → `asyncio.wait_for(asyncio.shield(future), timeout or self.timeout)`；
- 超时 → `ToolError(MCP_TIMEOUT)`；响应信封含 `error` → `ToolError(MCP_ERROR)`；
- **返回解包的 `result` 字段**（第二批修复：之前误返回整个信封）。

#### `McpClient._read_loop(self) -> None`（async）

- 逐行读 stdout：非 JSON 行忽略；有 id → 分发给 `_pending[id]` future（`set_result(整信封)`）；无 id → 通知仅 debug 日志；退出/异常 → log。

#### `McpClient.list_tools(self) -> List[Dict]`（async）

- `request("tools/list")` → 返回 `(响应或 {}).get("tools") or []`。

#### `McpClient.call_tool(self, name, arguments) -> str`（async）

- `request("tools/call", {name, arguments})`；result 的 `isError` → `ToolError(MCP_TOOL_ERROR)`；
- 拼接 `content` 块：type=text 取 text，其余 json 序列化。

#### 模块级 `safe_schema(schema) -> Dict`

- schema None → `{"type": "object"}`；`assert_supported_schema` 通过 → 原样；抛 ToolError（如 anyOf 超出子集）→ 降级 `{"type": "object"}`（参数校验交由远端）。

#### 模块级 `build_mcp_tool_definition(name, description, input_schema, client, prefix="") -> ToolDefinition`

- 全名 = `(prefix + name)[:128]`；描述加 `[MCP:name]` 前缀；
- `parameters = safe_schema(input_schema)`；`output = ToolOutputDefinition(schema=None, render=_default_render)`；
- `execute(args, run_ctx)` → `client.call_tool(name, dict(args or {}))`。

#### `McpServerPlugin`（Service，inject=("tools",)）

- `apply(ctx)`：校验 `config.command`（缺则 `ToolError(MCP_BAD_CONFIG)`）；读取 prefix/timeout/env；
  定义 `mount()`（异步：建 client → start → 逐个 list_tools 包装注册，disposer 记录）；
  **apply 返回 cleanup**（逆序注销工具 + `client.stop()`）；`self._start_hook = mount`。
- `start()`：`await self._start_hook()`——PluginTree.mount 在 apply 后 await start，因此挂载是异步安全时序。
- `close()`：兜底调度 `client.stop()`（防 rollback 之外的泄漏；rollback 现在也执行 apply 返回的 disposer）。

### 2.4 关键流程（握手时序）

```
插件 start
 └─ McpClient.start
     ├─ spawn 服务器进程（stderr=DEVNULL）
     ├─ 启动读循环 task
     ├─ request initialize（id=1，60s 超时）
     ├─ 校验 protocolVersion（warning 级别）
     └─ notify notifications/initialized
 └─ list_tools（id=2）→ 逐个 safe_schema 包装 → ctx.tools.register
运行时: ctx.tools.execute("echo", ...) → execute(args) → call_tool（id=n）
卸载: 逆序注销工具 → stop()：cancel 读循环 → terminate（5s）→ kill → pending 全部 MCP_CLOSED
```

### 2.5 语义差异（如实标注）

- 传输：stdio（第三批）+ **Streamable HTTP（第十批，`dsh/mcp/http.py`）**；
  通知仅记录日志不派发事件；
- `safe_schema` 降级策略：超子集 schema 不拒绝而开放透传（远端负责校验）；
- `request` 超时用 shield+wait_for；服务器响应迟到会被丢弃（pending 已弹）。

### 2.6 Streamable HTTP 客户端（第十批）

- `McpHttpClient(url, token?, timeout)`：单端点 POST JSON-RPC 2.0；
  与 stdio 客户端同接口（`start/stop/notify/request/list_tools/call_tool`），
  插件复用同一套 `build_mcp_tool_definition`（safe_schema 降级）。
- 响应双形态：`application/json`（单信封）与 `text/event-stream`
  （按行解析 `data:`，按 id 匹配响应，其余事件 = 通知记日志；流无响应 →
  `MCP_SSE_EOF`）。
- `Mcp-Session-Id` 会话头：initialize 响应带回后自动回传每个后续请求。
- 错误结构化：`MCP_NOT_FOUND`（404）/ `MCP_HTTP_ERROR`（≥400 或传输错误）/
  `MCP_BAD_RESPONSE`（非法 JSON）/ `MCP_ERROR`（错误信封）/ `MCP_CLOSED`。
- `McpHttpServerPlugin`（config: url/token/prefix/timeout）：start 钩子发现
  工具注册进 ctx.tools；卸载注销（无进程可停）。
- 夹具：`tests/fixtures/mock_mcp_http_server.py`（FastAPI 单端点：
  initialize 走 SSE 回传会话头，tools/list·tools/call 走 JSON）。

## 3. Cron 分节

### 3.1 语法

- 5 字段：`minute(0-59) hour(0-23) day(1-31) month(1-12) weekday(0-6, 0=周日)`；
- 6 字段：前加 `second(0-59)`；
- 每字段支持：`*`、数字、`a-b` 范围、`*/n` 或 `a-b/n` 步长、`a,b,c` 列表；
- **`L`（仅 day 字段）**：当月最后一天（月感知：闰年 2 月=29 日）；
  `matches` 对 day 字段做 `calendar.monthrange` 语义匹配。

### 3.2 类型

| 类 | 说明 |
|---|---|
| `CronError(ValueError)` | 表达式非法 |
| `_Field`（frozen） | `allowed: frozenset`（步长已展开） |
| `CronSpec`（frozen） | `expr` + `fields`（tuple）；`matches(dt)`、`next_after(dt)` |

### 3.3 函数详解

#### `_parse_field(text, minimum, maximum) -> _Field`

- 按 `,` 拆分；每个 part 处理 `/` 步长、`-` 范围、`*`/数字；
- 校验步长 ≥1、值域与 low≤high，违规抛 CronError；
- 结果展开为 `range(low, high+1, step)` 的 frozenset（`*/5` → {0,5,10,…}）。

#### `parse_cron(expr) -> CronSpec`

- 字段数必须 5 或 6，否则 CronError；按字段名映射值域（second 0-59，其余按 `_RANGES`）。

#### `CronSpec.matches(dt) -> bool`

- 值序列 `(second, minute, hour, day, month, (weekday()+1) % 7)`（0=周日）；按字段顺序全命中才 True。

#### `CronSpec.next_after(dt) -> datetime`

- 从 dt+1 秒逐秒匹配，上限 400 天；超限抛 `CronError("never fires")`。

#### 模块级 `next_fire_time(expr, now=None) -> datetime`

- 便捷入口：parse + next_after（now 默认当前时间）。

### 3.4 ScheduleService cron 条目（读 dsh/schedule/schedule.py）

- `register(prompt, interval_seconds=None, schedule=None) -> str`：二者**恰好一个**，否则 ValueError；cron 条目立即计算 `next_time`（`parse_cron(...).next_after(datetime.now()).timestamp()`）；
- `_due(entry, now)`：interval 型按 `last_fired` 间隔；cron 型 `now >= next_time` 时触发并**重算 next_time**（从 max(now, next_time) 起）；
- `schedule_register` 工具：`prompt` 必填 + `interval_seconds`/`schedule` 可选；CronError/ValueError → `ToolArgsError`。

### 3.5 语义差异

- 无日/月名（mon/tue…）、无 `L`/`W`/`#` 等扩展；weekday 0=周日（含 6 字段时 seconds 在最前）；
- `next_after` 为逐秒扫描（≤400 天），非算法递推——简单、可验证，非性能热点（每秒 tick 只对到期条目调用）。

## 4. 相关测试（tests/test_mcp_cron.py，8 项）

| 用例 | 断言要点 |
|---|---|
| test_cron_parse_and_matches | `*/5 * * * *`、`0 9 * * 1-5`（周一命中/周日不中）、6 字段 `*/2 * * * * *` |
| test_cron_next_after | `0 9 * * *` 下一触发 = 当日 9:00；`* * * * * *` = now+1s |
| test_cron_invalid | `61 * * * *`、字段数错、`a * * * *` → CronError |
| test_schedule_cron_entry_fires | `* * * * * *` 条目 2.4s 内注入 ≥2 次 |
| test_schedule_register_tool_cron_and_validation | 工具注册 cron 条目成功；非法表达式抛错 |
| test_mcp_client_discover_and_call | 发现 echo/lenient；echo 调用回显；anyOf 降级开放对象；cleanup 停进程 |
| test_mcp_client_stop_kills_process | stop 后 pid 不存在（轮询 os.kill） |
| test_mcp_safe_schema | None→开放对象；合法 schema 原样；anyOf→降级 |

> 全量：`python -m pytest tests -q` → 86 passed（78 + 8）。
