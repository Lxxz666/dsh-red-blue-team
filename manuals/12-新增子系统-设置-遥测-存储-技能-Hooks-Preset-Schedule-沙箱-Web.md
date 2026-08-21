# 新增子系统开发手册（补齐批次）

> 背景：本批子系统对应 TypeScript 版 `settings`/`settings-file`、`session-telemetry`/`session-telemetry-otel`、
> `storage`/`storage-json`/`storage-domain`、`session-title`、`time-context`、`tool-web`、`user-questions`/`tool-ask-user`、
> `skill`/`skill-filesystem`/`tool-skill`、`hook-protocol`（claude/codex 桥的 YAML 化）、`agent-presets`、
> `schedule`/`cron`、`ctx.sandbox`（landlock/sandbox-exec/Windows ACL）、`session-persistence-sqlite`。
>
> 补齐原因与逐条对照见 `manuals/13-与TS版差异对照与补齐记录.md`（第 1.6/1.7/1.8 节能力缝与事件矩阵、第 2 节实施记录）。
> 简言之：上一轮审计发现这些「能力缝」在 Python 版缺位，导致 Web 侧栏取不到标题、设置/遥测/非会话存储无处落地、
> 技能与定时任务不可用、权限审批之外没有文本问答通道、hooks 与 preset 无桥、SQLite 后端缺失。本批一次性补齐。
>
> 本文档覆盖 13 个文件里的**每一个类、每一个函数/方法**（含私有 `_xxx` 与内层工具闭包）。所有签名均以
> `python -c "import inspect; ..."` 核验通过（见正文各签名）。

## 1. 总表：每个子系统一行

| 服务 key（ctx.\<key\>） | 对应 TS 包 | 文件 | 一句话职责 |
|---|---|---|---|
| `settings` | settings + settings-file | `dsh/settings/service.py` | 用户设置 JSON 后端，写入落盘并广播 `settings/updated` |
| `sessionTelemetry` | session-telemetry + session-telemetry-otel | `dsh/telemetry/service.py` | 会话遥测分发（record waterfall）+ JSONL 后端 |
| `storage` | storage + storage-json + storage-domain | `dsh/storage/service.py` | 非会话分区 JSON 存储，广播 `domain/changed` |
| `sessionTitle` | session-title | `dsh/session/title.py` | 会话标题 provider 注册表（首条 user 截断） |
| —（注入 `systemPrompt`） | time-context | `dsh/context/time_context.py` | 当前日期/时间注入模型上下文（order 10） |
| —（注入 `tools`） | tool-web | `dsh/web/tool.py` | `web_fetch`/`web_search`（DDG lite 尽力而为） |
| `userQuestions` | user-questions + tool-ask-user | `dsh/interaction/user_questions.py` | 文本问答通道 + `ask_user` 工具 |
| `skills` | skill + skill-filesystem + tool-skill | `dsh/skill/skill.py` | 技能发现（SKILL.md）与加载注入 |
| —（注入 `subprocess`） | hook-protocol + 桥 | `dsh/hooks/hooks.py` + `compat.py` | hooks.yml 最小桥 + Claude Code/Codex 文件格式兼容桥（五监听点 + 日志事件） |
| `agentPresets` | agent-presets | `dsh/preset/presets.py` | preset yml 行挂进 agent 作用域（isolate 等价物） |
| `schedule` | schedule / cron | `dsh/schedule/schedule.py` | 间隔任务 → agent.inject 通知 |
| `sandbox` | ctx.sandbox（landlock/sandbox-exec/ACL） | `dsh/sandbox/sandbox.py` | 进程限制缝：Windows Job Object（kill-on-close）+ Linux Landlock（只读 FS + 工作区可写）；不可用如实降级 local |
| `sessionPersistence`（后端之一） | session-persistence-sqlite | `dsh/persistence/sqlite.py` | SQLite 单库两表会话后端 |

> 服务 key 列为 `—` 的子系是**插件**（无 `provides`），它们把工具/监听器/分节挂到既有服务上，不新增 `ctx.<key>`。

---

## 2. 逐子系统分节

约定：`Service` 基类（`dsh/kernel/service.py`）提供 `__init__(ctx, config=None)`（置 `self.ctx`、`self.config = config or {}`、`self._disposer=None`）、
`apply(ctx)`（默认返回 None）、`start()`（异步钩子）、`close()`（调用 apply 返回的 disposer）。
`provides` 声明本插件提供的服务名；`inject` 声明依赖的服务名（Loader 据此拓扑排序）。
所有子类都继承这些，下文只写子类**新增/覆写**的行为。

### 2.1 settings —— SettingsService

- **定位与依赖**：`provides = "settings"`，无 `inject`。后端 = 单个 JSON 文件，默认 `~/.dsh/settings.json`（`config.path` 可换）。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `path` | `str` | `os.path.expanduser("~/.dsh/settings.json")` | 后端文件路径 |
  | `_lock` | `threading.Lock` | 新建 | 写盘互斥锁 |
  | `_data` | `Dict[str, Any]` | `{}` | 内存中的设置字典 |

- **函数/方法详解**

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：`super().__init__`；`self.path = os.path.expanduser((config or {}).get("path", "~/.dsh/settings.json"))`；建 `_lock`；`_data={}`；调用 `_load()`。
  - `apply(self, ctx) -> None`：`ctx.set("settings", self)`。
  - `_load(self) -> None`：`try: open(path, "r", encoding="utf-8") → json.load`，异常 `(OSError, json.JSONDecodeError)` 时 `_data = {}`。**文件不存在/损坏 → 空字典，不抛错**。
  - `_save(self) -> None`：`with self._lock:`；`os.makedirs(os.path.dirname(path), exist_ok=True)`（父目录非空时）；`open(path, "w", encoding="utf-8")` → `json.dump(_data, ensure_ascii=False, indent=2)`。**同步全量覆写落盘，无异常捕获**。
  - `get(self, key: str, default: Any = None) -> Any`：`return self._data.get(key, default)`。**读不加锁**（字典 `.get` 原子性足够，弱一致读）。
  - `set(self, key: str, value: Any) -> None`：`_data[key] = value` → `_save()` → `try:` emit `settings/updated` `{"key","value"}` + emit `settings/document-updated` `{"key","value"}`；`except Exception: pass`。**副作用：同步落盘 + 两个事件；事件派发异常被吞掉**。
  - `delete(self, key: str) -> None`：`if key in _data:` → `del _data[key]` → `_save()` → emit `settings/updated` `{"key", "value": None}`。**注意：不广播 `settings/document-updated`，且此处 emit 无 try/except 包裹**（与 `set` 不同）。
  - `all(self) -> Dict[str, Any]`：`return dict(self._data)`（浅拷贝快照）。

- **关键流程伪代码**

  ```
  set(key, value):
      _data[key] = value
      _save()                          # 加锁、makedirs、json.dump(indent=2)
      emit "settings/updated" {key, value}        # 异常被吞
      emit "settings/document-updated" {key, value}
  ```

- **事件**：发布 `settings/updated`、`settings/document-updated`（payload = `{"key","value"}`）。无订阅。
- **TS 对应**：`settings` + `settings-file`；`agent-default-model` 的 `current_selection` 实时读取此处的 `agent_default_model`（本文件只负责存取，读取方在 agent 循环内）。

### 2.2 telemetry —— SessionTelemetryService + ConsoleTelemetryPlugin

- **定位与依赖**：`SessionTelemetryService.provides = "sessionTelemetry"`；`ConsoleTelemetryPlugin.inject = ("sessionTelemetry",)`。
  分发服务订阅 `session/event` → 转成 `session-telemetry/record` waterfall；后端监听者包装/替换记录。
- **类型与字段表（SessionTelemetryService）**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_disposers` | `List[Any]` | `[]` | 事件注销函数列表 |

- **函数/方法详解（SessionTelemetryService）**

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：`super().__init__`；`_disposers = []`。
  - `apply(self, ctx) -> None`：`ctx.set("sessionTelemetry", self)`；`_disposers.append(ctx.on("session/event", self._on_event))`；`_disposers.append(ctx.on("session/flush", self._on_flush))`。
  - `_on_event(self, session: Any, event: Any) -> None`：`record = {"session": session.id, "event": event.to_json()}`；`try: self.ctx.events.emit("session-telemetry/record", record)`，`except Exception: log.exception(...)`。
  - `_on_flush(self, session: Any) -> bool`（`async def`）：`await self.ctx.events.parallel("session-telemetry/flush", session.id)`；`return True`（表示 flush 已处理，放行 flush 流水线）。
  - `close(self) -> None`：逐个调用 `_disposers` 并 `clear()`。

- **函数/方法详解（ConsoleTelemetryPlugin）**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_disposers` | `List[Any]` | `[]` | 事件注销函数 |
  | `_io_lock` | `threading.Lock` | 新建 | 写文件互斥 |
  | `enabled` | `bool` | `bool(config.get("enabled", False))` | 默认关闭 |
  | `path` | `str` | `~/.dsh/telemetry.jsonl` | JSONL 输出文件 |

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：如字段表。
  - `apply(self, ctx) -> None`：`if not self.enabled: return`（**关闭时完全不订阅**）；否则订阅 `session-telemetry/record → _record`、`session-telemetry/flush → _flush`。
  - `_record(self, record: Dict[str, Any]) -> None`：`with _io_lock:` → makedirs parent → `open(path, "a", encoding="utf-8")` → `fh.write(json.dumps(record, ensure_ascii=False) + "\n")`；`except OSError: log.exception`。**同步追加、异常只记日志不抛**。
  - `_flush(self, session_id: str) -> None`（`async def`）：`return None`（记录是同步追加的，flush 无额外工作）。
  - `close(self) -> None`：注销并清空。

- **关键流程伪代码**

  ```
  session/event(session, event) ──emit──> "session-telemetry/record" {session.id, event.to_json()}
                                       └─ ConsoleTelemetryPlugin._record 追加一行 JSON
  session/flush(session) ──parallel──> "session-telemetry/flush" session.id  （后端落盘钩子）
  ```

- **事件**：发布 `session-telemetry/record`（emit）、`session-telemetry/flush`（parallel）；订阅 `session/event`、`session/flush`。
- **边界**：线程安全 = 文件追加由 `_io_lock` 保护；分发异常隔离（`log.exception`）。record 为 JSON 可重放 trace（`sessions.create(seed)` 重放，见手册 13）。
- **TS 对应**：`session-telemetry`（分发）+ `session-telemetry-otel` 的简化（JSONL 后端，无 OTel 上报）。

### 2.3 storage —— StorageService

- **定位与依赖**：`provides = "storage"`。后端 = `~/.dsh/storage.json`（`config.path` 可换），键空间按 domain 分区。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `path` | `str` | `~/.dsh/storage.json` | 后端文件路径 |
  | `_lock` | `threading.Lock` | 新建 | 写盘互斥 |
  | `_data` | `Dict[str, Dict[str, Any]]` | `{}` | `domain -> {key -> value}` |

- **函数/方法详解**

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：同 settings 模式；`_data` 为两层字典；`_load()`。
  - `apply(self, ctx) -> None`：`ctx.set("storage", self)`。
  - `_load(self) -> None`：`try: json.load`；`except (OSError, json.JSONDecodeError): _data = {}`。
  - `_save(self) -> None`：`with _lock:` makedirs + `json.dump(indent=2, ensure_ascii=False)` 全量覆写。
  - `get(self, domain: str, key: str, default: Any = None) -> Any`：`return self._data.get(domain, {}).get(key, default)`。
  - `put(self, domain: str, key: str, value: Any) -> None`：`_data.setdefault(domain, {})[key] = value` → `_save()` → `try: emit "domain/changed" {"domain"}`；`except Exception: pass`。
  - `delete(self, domain: str, key: str) -> None`：`table = _data.get(domain, {})`；`if key in table:` → `del table[key]` → `_save()` → `emit "domain/changed" {"domain"}`（此处无 try/except）。**删除后若域变空，空表保留在 `_data` 中（domain 列表仍含该域）**。
  - `domain(self, domain: str) -> Dict[str, Any]`：`return dict(self._data.get(domain, {}))`（浅拷贝）。
  - `domains(self) -> List[str]`：`return list(self._data.keys())`。

- **事件**：发布 `domain/changed`（payload = `{"domain": ...}`）。无订阅。
- **边界**：写盘加锁；`get`/`domain`/`domains` 读不加锁。供 goal/plan 等域存元数据的最小形态。
- **TS 对应**：`storage` + `storage-json` + `storage-domain`。

### 2.4 session-title —— SessionTitleService

- **定位与依赖**：`provides = "sessionTitle"`。注册表里唯一 provider 决定标题；默认策略 = 首条 user 消息截断 60 字符（`session-title-first-prompt` 简化）。Web UI 侧栏与恢复列表经此取标题。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_provider` | `Any` | `None` | 自定义标题 provider（签名 `(session, messages) -> str|None`） |
  | `_max_len` | `int` | `int(config.get("max_len", 60))` | 截断长度 |

- **函数/方法详解**

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：如字段表。
  - `apply(self, ctx) -> None`：`ctx.set("sessionTitle", self)`。
  - `set_provider(self, provider) -> None`：`self._provider = provider`。**注册即覆盖，非追加**；返回 None 表示放弃（回退默认策略）。
  - `title_for(self, session: Any, messages: Optional[List[Any]] = None) -> str`：
    1. `messages is None` → `messages = session.derive_messages()`；
    2. 若 `_provider` 非 None：`try: title = provider(session, messages); if title: return title`；`except Exception: pass`（provider 异常回退）；
    3. 遍历 messages，第一条 `role == "user"` 且 `message.plain_text().strip()` 非空 → `return text[:self._max_len]`（截断到 max_len；文本已 strip 非空故必有内容）；
    4. 兜底 `return session.id[:16]`。

- **事件**：无。
- **边界**：provider 抛异常被静默吞掉（回退默认策略）；无并发状态（单 `_provider`）。
- **TS 对应**：`session-title` 缝。

### 2.5 time-context —— TimeContextPlugin

- **定位与依赖**：`inject = ("systemPrompt",)`（无 provides）。注册动态 `PromptContext`（order 10，persona 之后、工具指引之前），每次组装注入当前时间。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_disposer` | `Any` | `None` | context 注销函数 |

- **函数/方法详解**

  - `__init__(self, ctx, config: Optional[dict] = None) -> None`：`super().__init__`；`_disposer = None`。
  - `apply(self, ctx) -> None`：
    `context = PromptContext(name="time", order=10, text=lambda _ac: "当前时间：" + time.strftime("%Y-%m-%d %H:%M:%S %Z"))`；
    `self._disposer = ctx.systemPrompt.context(context)`；
    返回内层 `cleanup()`：若 `_disposer` 非 None 则调用并置 None。**返回值为 Loader 记录的 Disposer**（`ctx.systemPrompt.context` 内部也已 `ctx.effect(unregister)`，双保险）。

- **事件**：无（依赖 `system-prompt/assemble` 时解析 `text` callable）。
- **TS 对应**：`time-context`。

### 2.6 web-tool —— web_fetch / web_search / WebPlugin

- **定位与依赖**：`WebPlugin.inject = ("tools",)`。模块级常量 `DDG_LITE = "https://html.duckduckgo.com/html/"`，`_LINK_RE`（抓 `<a class="result__a" ... href=...>`）、`_SNIPPET_RE`（抓 `<a class="result__snippet">`），均 `re.S`。
- **类型与字段表（WebPlugin）**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_disposers` | `List[Any]` | `[]` | 工具注销函数 |

- **函数/方法详解**

  - `build_web_tools() -> List[Any]`：构造并返回 `[web_fetch, web_search]`（两个 `ToolDefinition`）。内部两个 `@define_tool` 闭包：
    - `web_fetch`（`async def web_fetch(args, run_ctx)`）：`name="web_fetch"`，`description="抓取一个 URL 的文本内容（http/https）。"`，`parameters={"url":{"type":"string","required":True}, "max_chars":{"type":"integer","description":"正文截断长度（默认 8000）"}}`，`output={"type":"string"}`，`timeout_ms=60_000`，`present_result=lambda args, result: web_result(title=f"抓取 {args['url']}", kind="fetch", url=args["url"], status_code=200, truncated=False)`。执行体：
      1. `url = args["url"]`；非 `http://`/`https://` 前缀 → `raise ToolError(..., code="BAD_URL")`；
      2. `max_chars = int(args.get("max_chars") or 8000)`；
      3. `httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent":"dsh-python/0.1"})` GET；`httpx.HTTPError` → `ToolError(code="FETCH_FAILED")`；
      4. `stripped = _strip_html(response.text)`；`truncated = len(stripped) > max_chars`；`body = stripped[:max_chars]`；
      5. 返回 `f"status: {response.status_code}\nurl: {str(response.url)}\n" + body + ("\n[truncated]" if truncated else "")`。
    - `web_search`（`async def web_search(args, run_ctx)`）：`name="web_search"`，`parameters={"query":{"type":"string","required":True}, "max_results":{"type":"integer","description":"最多结果数（默认 5）"}}`，`output={"type":"array","items":{"type":"object"}}`，`timeout_ms=60_000`，`render=lambda args, value: _render_results(value)`。执行体：
      1. `max_results = int(args.get("max_results") or 5)`；
      2. GET `DDG_LITE`（`params={"q": query}`），同 AsyncClient 配置；`httpx.HTTPError` → `SEARCH_FAILED`；`status_code != 200` → `ToolError(code="SEARCH_FAILED")`；
      3. `links = _LINK_RE.findall(response.text)`、`snippets = _SNIPPET_RE.findall(response.text)`；
      4. 对 `links[:max_results]` 逐条：`url=_resolve_url(href)`、`title=_strip_html(title).strip()`、`snippet = _strip_html(snippets[index]).strip()[:300]`（无对应 snippet 时为空串）；
      5. 无结果 → `ToolError(code="NO_RESULTS", "no results (search endpoint may be rate-limited)")`；返回结果列表。
  - `_strip_html(text: str) -> str`：先 `re.sub` 删除 `<script>...</script>`、`<style>...</style>`（`re.I`），再删 `<[^>]+>`，`html.unescape` 后把 `\s+` 折叠成单空格并 `strip()`。
  - `_resolve_url(href: str) -> str`：DDG lite 的 href 形如 `//duckduckgo.com/l/?uddg=<url>`；`re.search(r"uddg=([^&]+)", href)` 命中则 `html.unescape(组1)`；否则 `//` 开头 → `"https:" + href`；否则原样返回。
  - `_render_results(value: List[Dict[str, str]]) -> str`：空 → `"(无结果)"`；否则 `"{i}. {title}\n   {url}\n   {snippet}"` 逐行 join。
  - `WebPlugin.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_disposers = []`。
  - `WebPlugin.apply(self, ctx) -> None`：`for tool in build_web_tools(): _disposers.append(ctx.tools.register(tool))`；返回 `cleanup()` 逐个注销并清空。

- **事件**：无。
- **边界**：网络异常统一转 `ToolError`（`FETCH_FAILED`/`SEARCH_FAILED`/`NO_RESULTS`），不崩溃。**已知简化**：`web_fetch` 的 `present_result` 硬编码 `status_code=200, truncated=False`（UI 卡片不反映真实状态码/截断）；`web_search` 无 `present_result`，只靠 `render` 文本化。
- **TS 对应**：`tool-web`（web card：`kind=fetch/search`）。

### 2.7 interaction —— UserQuestionsService + ask_user + ToolAskUserPlugin

- **定位与依赖**：`UserQuestionsService.provides = "userQuestions"`；`ToolAskUserPlugin.inject = ("tools", "userQuestions")`。文本问答通道（区别于权限审批的 bool 通道）。
- **类型与字段表（UserQuestionsService）**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_channel` | `Any` | `None` | 问答回调 `async def(question, detail) -> str|None` |

- **函数/方法详解**

  - `UserQuestionsService.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_channel = None`。
  - `UserQuestionsService.apply(self, ctx) -> None`：`ctx.set("userQuestions", self)`。
  - `UserQuestionsService.set_channel(self, callback) -> None`：`self._channel = callback`。**注册即覆盖**；返回 None = 用户未作答。
  - `UserQuestionsService.ask(self, question: str, detail: str = "") -> str`（`async def`）：
    1. `_channel is None` → `raise ToolError(..., code="NO_CHANNEL")`（headless）；
    2. `answer = self._channel(question, detail)`；`asyncio.iscoroutine(answer)` → `await`；
    3. `except Exception` → `log.exception` + `raise ToolError(..., code="CHANNEL_ERROR")`；
    4. `answer is None` → `raise ToolError(..., code="UNANSWERED")`；否则 `return str(answer)`。
  - `build_ask_user_tool() -> Any`：返回 `ask_user` 的 `ToolDefinition`。`@define_tool(name="ask_user", parameters={"question": required string, "detail": optional string}, output={"type":"string"}, timeout_ms=300_000)`；执行体（`async def ask_user(args, run_ctx)`）：`agent = run_ctx.execution.agent`；`ctx = agent.ctx if agent is not None else run_ctx.root_ctx`；`not ctx.has("userQuestions")` → `ToolError(code="NO_CHANNEL")`；`return await ctx.userQuestions.ask(args["question"], args.get("detail", ""))`。
  - `ToolAskUserPlugin.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_disposer = None`。
  - `ToolAskUserPlugin.apply(self, ctx) -> None`：`_disposer = ctx.tools.register(build_ask_user_tool())`；返回 `cleanup()` 注销并置 None。

- **事件**：无。
- **边界**：channel 未挂载（headless）→ `NO_CHANNEL`；用户不答 → `UNANSWERED`；channel 抛错 → `CHANNEL_ERROR`（异常被转译，不裸抛）。
- **TS 对应**：`user-questions` 缝 + `tool-ask-user`。

### 2.8 skill —— Skill / SkillService / FilesystemSkillProvider / SkillsPlugin

- **定位与依赖**：`SkillService.provides = "skills"`；`SkillsPlugin.inject = ("skills", "tools")`。`Skill` 是数据类。技能源 = `~/.dsh/skills/<id>/SKILL.md`（YAML front matter: name/description）。
- **类型与字段表（Skill）**：`@dataclass`，字段 `id: str`、`name: str`、`description: str = ""`、`instructions: str = ""`。
- **类型与字段表（SkillService）**：`_providers: List[Any] = []`。
- **类型与字段表（FilesystemSkillProvider）**：`paths: Optional[List[str]]`（默认 `[~/.dsh/skills]`）。
- **类型与字段表（SkillsPlugin）**：`_disposers: List[Any] = []`；`_provider = FilesystemSkillProvider(paths=config.get("paths"))`。

- **函数/方法详解**

  - `Skill.to_json(self) -> Dict[str, Any]`：返回 `{"id","name","description"}`（**不含 instructions**）。
  - `SkillService.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_providers = []`。
  - `SkillService.apply(self, ctx) -> None`：`ctx.set("skills", self)`。
  - `SkillService.register_provider(self, provider) -> None`：`_providers.append(provider)`；`emit "skills/change"`（无参数）。provider 契约 `list() -> [Skill]`。
  - `SkillService.list(self) -> List[Skill]`：按 provider 顺序拼接，`merged[skill.id] = skill`（**按 id 去重，后者覆盖**），返回 `list(merged.values())`。
  - `SkillService.get(self, skill_id: str) -> Optional[Skill]`：对 `self.list()` 线性扫描，`id == skill_id` 返回，否则 None。
  - `FilesystemSkillProvider.__init__(self, paths: Optional[List[str]] = None) -> None`：`self.paths = paths or [os.path.join(os.path.expanduser("~"), ".dsh", "skills")]`。
  - `FilesystemSkillProvider.list(self) -> List[Skill]`：遍历每个 root（非目录跳过）→ `sorted(os.listdir(root))` → 仅目录且含 `SKILL.md` 者 → `try: append(self._parse(entry, markdown))`，`except: log.exception`。
  - `FilesystemSkillProvider._parse(skill_id: str, path: str) -> Skill`（`@staticmethod`）：`text = open(path, "r", encoding="utf-8").read()`；`name, description, body = skill_id, "", text`；若 `text.startswith("---")`：`parts = text.split("---", 2)`，`len(parts) >= 3` 时 `meta = yaml.safe_load(parts[1]) or {}`，`name = str(meta.get("name") or skill_id)`，`description = str(meta.get("description") or "")`，`body = parts[2]`；`yaml.YAMLError` 时 `pass`（保留默认）。返回 `Skill(id=skill_id, name, description, instructions=body.strip())`。
  - `build_skill_tools() -> List[Any]`：返回 `[skill_list, skill_load]`。内部：
    - `_skills_of(run_ctx)`（闭包，非 async）：`agent = run_ctx.execution.agent`；`ctx = agent.ctx if agent else run_ctx.root_ctx`；`not ctx.has("skills")` → `ToolError(code="NO_SKILLS")`；返回 `ctx.skills`。
    - `skill_list`（`async def`，`name="skill_list"`，`parameters={}`，`output={"type":"array","items":{"type":"object"}}`）：`return [s.to_json() for s in _skills_of(run_ctx).list()]`。
    - `skill_load`（`async def`，`name="skill_load"`，`parameters={"id": required string}`，`output={"type":"string"}`）：`skill = get(args["id"])`；None → `ToolError(code="UNKNOWN_SKILL")`；`run_ctx.defer_context({"content": f"[skill:{skill.id}] {skill.instructions}", "source": {"kind":"plugin","plugin":"skill","skill":skill.id}})`；返回 `f"loaded skill {skill.id}: {skill.name}"`。
  - `SkillsPlugin.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_disposers=[]`；`_provider = FilesystemSkillProvider(paths=config.get("paths"))`。
  - `SkillsPlugin.apply(self, ctx) -> None`：`ctx.skills.register_provider(self._provider)`；`for tool in build_skill_tools(): _disposers.append(ctx.tools.register(tool))`；返回 `cleanup()` 注销工具并清空。

- **关键流程伪代码**

  ```
  skill_load(args):
      skill = skills.get(args["id"])            # 失败 → UNKNOWN_SKILL
      defer_context({content: "[skill:id] instructions",
                     source: {kind: plugin, plugin: skill, skill: id}})
      return "loaded skill <id>: <name>"        # 指令下一轮注入上下文
  ```

- **事件**：发布 `skills/change`（`register_provider` 时）。无订阅。
- **边界**：`_parse` 用同步 `open`（无锁）；解析失败按技能跳过（不中断整体发现）；`get` 每次全量 list（O(n)）。
- **TS 对应**：`skill` + `skill-filesystem` + `tool-skill`。

### 2.9 hooks —— HooksPlugin + HooksCompatPlugin（第十一批重构）

- **定位与依赖**：`inject = ("subprocess",)`。配置 `~/.dsh/hooks.yml`（`config.path` 可换）。模块导入时即注册两个事件类型：`hook/invoked`、`hook/result`（log-only）。
- **模块级常量**：`HOOK_EVENTS = ("session-start", "pre-step", "pre-tool", "post-tool", "turn-stopping")`。
- **`_NoBoolLoader(yaml.SafeLoader)`**：类体内 `_NoBoolLoader.add_constructor("tag:yaml.org,2002:bool", lambda loader, node: loader.construct_scalar(node))`——**禁用 YAML 1.1 布尔解析**，否则裸键 `on` 会被解析成 `True`。
- **第十一批重构**：运行器抽为模块级函数（`hooks_for` / `run_hook` / `_substitute_vars`），
  HooksPlugin 与 HooksCompatPlugin（Claude/Codex 兼容桥，`dsh/hooks/compat.py`）共用同一套。

**类型与字段表（HooksPlugin / HooksCompatPlugin 共用形状）**

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `path` / `paths` | `str` / `List[str]` | `~/.dsh/hooks.yml` / 发现清单 | 配置文件路径 |
| `_disposers` | `List[Any]` | `[]` | 事件注销函数 |
| `_hooks` | `List[Dict[str, Any]]` | `[]` | 归一化后的 hook 行 |

**行级扩展键（第十一批，兼容桥用）**：`tool_pattern`（正则工具名过滤）、
`stdin_json`（Claude 契约：JSON 写命令 stdin）、`vars`（Codex 契约：
command 内 `$VAR`/`${VAR}` 替换）。

**模块级函数详解**

- `hooks_for(hooks, event, tool_name=None) -> List[dict]`：过滤
  `hook.get("on") == event`；`tools` 精确列表非空且 `tool_name` 不在内 → 跳过；
  `tool_pattern` 存在且 `re.search(pattern, tool_name)` 不命中 → 跳过。
- `_substitute_vars(command, ctx, agent, tool_name) -> str`：从
  `os.environ ∪ {CWD, SESSION_ID, ARGUMENTS}` 替换 `$VAR`/`${VAR}`
  （best-effort，未知名保留原样）。
- `run_hook(ctx, agent, hook, tool_name=None, env=None) -> Dict[str, Any]`
  （`async def`）：
  1. `handler_id = f"hook-{uuid.uuid4().hex[:8]}"`；
  2. agent 非 None → `try: agent.session.append("hook/invoked", {handler_id,
     name, on, tool})`；`except: pass`；
  3. `hook.get("vars")` → `command = _substitute_vars(...)`；
     `run_env = dict(os.environ, **(env or {}))`；tool_name 非空 →
     `run_env["DSH_HOOK_TOOL"] = tool_name`；
  4. `hook.get("stdin_json")` → `stdin_data = json.dumps({session_id,
     hook_event_name, tool_name})`；
  5. `["pwsh","-NoProfile","-Command",command]`（Windows）/ `["bash","-lc",
     command]`（POSIX）→ `ctx.subprocess.run(..., timeout=60, env=run_env,
     stdin_data=stdin_data)`；`outcome = {exit_code, output: (stdout+stderr)
     .strip()[:2000]}`；
  6. `except Exception → outcome = {"exit_code": -1, "output": "hook error:
     ..."}`（**执行异常不抛，转为 exit_code=-1 → fail-closed**）；
  7. agent 非 None → `try: append("hook/result", {handler_id, name,
     exit_code, output})`；`except: pass`；`return outcome`。

**HooksPlugin 方法**：`__init__`（如字段表）；`apply`（`_load()` + 五监听点
订阅 + `cleanup()`）；`_load`（yaml 解析失败 → `_hooks = []`，只保留
`on ∈ HOOK_EVENTS` 的行）；五个监听点
（`_on_session_start` fire-and-forget / `_on_pre_step` 非零退出 →
`{"kind": "reject"}` / `_on_pre_tool` 非零退出 → `AskDecision`（decision=ask）
或 `DenyDecision`（默认） / `_on_post_tool` 非零退出 → `BlockDecision` /
`_on_turn_stopping`）——全部经 `hooks_for`/`run_hook`。

**HooksCompatPlugin（Claude Code / Codex 兼容桥，`dsh/hooks/compat.py`）**

- `discover_paths(config)`：默认发现顺序 `~/.claude/settings.json`、
  `~/.claude/settings.local.json`、`./.claude/settings.json`、
  `./.claude/settings.local.json`、`~/.codex/config.toml`、
  `./.codex/config.toml`（`config.paths` 可覆盖）。
- `load_claude_hooks(path)`：JSON 解析 `hooks` 键；事件映射
  `PreToolUse→pre-tool`、`PostToolUse→post-tool`、`UserPromptSubmit→pre-step`、
  `SessionStart→session-start`、`Stop→turn-stopping`；其余（Notification/
  SubagentStop/PreCompact）不映射（记日志）；每行产出 `{name, on, command,
  decision: "deny", tool_pattern: matcher, stdin_json: True}`。
- `load_codex_hooks(path)`：`tomllib` 解析 `[hooks]` 段；映射
  `Command→pre-tool`、`SessionStart→session-start`、`Stop/SessionEnd→
  turn-stopping`；Notification 不映射；每行 `{name, on, command, decision:
  "deny", vars: True}`。
- `apply`：无配置文件时**完全 no-op**（不订阅任何监听点）；有 hook 时挂与
  HooksPlugin 相同的五监听点（复用 `hooks_for`/`run_hook`）。
- **语义差异（如实标注）**：Claude matcher 按正则匹配工具名（非完整 Claude
  输入匹配语义）；Claude exit 2 与普通非零统一视为拦截；Codex `$VAR` 为
  best-effort 环境替换（`$FILE_PATH` 等未实现变量原样保留）。

- **事件**：订阅 `agent/session-start`、`agent/pre-step`（waterfall）、`tools/pre-execute`（waterfall）、`tools/post-execute`（waterfall）、`agent/turn-stopping`；发布 `hook/invoked`、`hook/result`（经 `agent.session.append` 落为 log-only 事件）。
- **决策语义**：pre-tool 的 `decision` 取 `deny`（默认）/`ask`；`allow` 由「不拦截即 `next()`」隐式表达。pre-step 非零退出 = reject。post-tool 非零退出 = block。
- **TS 对应**：`hook-protocol` + claude/codex 桥（YAML 化最小桥 + 文件格式兼容子集）。

### 2.10 preset —— AgentPresets

- **定位与依赖**：`provides = "agentPresets"`。preset = `~/.dsh/presets/<id>.yml`（`config.paths` 可换）里的插件行列表。`mount(agent_ctx, id)` 在 agent 作用域 ctx 上挂一棵 PluginTree（服务/工具/分节只对该 agent 及其后代可见——isolate realm 的作用域化等价物）。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `paths` | `List[str]` | `[~/.dsh/presets]` | preset 搜索目录 |
  | `_mounted` | `Dict[int, Any]` | `{}` | `id(agent_ctx) -> (tree, preset_id)` |

- **函数/方法详解**

  - `AgentPresets.__init__(self, ctx, config: Optional[dict] = None) -> None`：`paths = config.get("paths") or [~/.dsh/presets]`；`_mounted = {}`。
  - `AgentPresets.apply(self, ctx) -> None`：`ctx.set("agentPresets", self)`。
  - `AgentPresets._resolve_path(self, preset_id: str) -> Optional[str]`：按 `paths` 顺序找第一个存在的 `<root>/<preset_id>.yml`，无则 None。
  - `AgentPresets.list(self) -> List[str]`：遍历每个 root（非目录跳过），`sorted(os.listdir)`，`.yml` 结尾 → `name[:-4]`。
  - `AgentPresets.read(self, preset_id: str) -> List[dict]`：`_resolve_path` None → `raise LoaderError("preset ... not found")`；`rows = yaml.safe_load(open(...)) or []`；非 list → `raise LoaderError("preset ... must be a YAML list")`；返回 rows。
  - `AgentPresets.mount(self, agent_ctx: Any, preset_id: str) -> Dict[str, Any]`（`async def`）：
    1. **作用域内工具隔离**：`if agent_ctx.has("tools") and "tools" not in agent_ctx._instances:`（工具是从父层继承而非本地注册时）→ `from ..tools import ToolRuntime`；`local_runtime = ToolRuntime(agent_ctx, {}, parent=agent_ctx.get("tools"))`；`local_runtime.apply(agent_ctx)`（第七批起局部运行时 = 根运行时 `_scoped[ctx.name]` 作用域层的视图：注册/查询/执行全委托父层——preset 工具既对外不可见，又可被循环经根运行时按 scope 正常执行，run_code 程序内亦可调用）；
    2. `rows = self.read(preset_id)`；`tree = PluginTree(agent_ctx)`；
    3. 每行 `tree.add_bundle_rows([row], layer_name=f"preset:{preset_id}")`；
    4. `await tree.mount()`（拓扑挂载 preset 里的服务/工具/分节到 `agent_ctx`）；
    5. `key = id(agent_ctx)`；`_mounted[key] = (tree, preset_id)`；
    6. 注册两个 effect：`dispose_tree()`（`await tree.dispose(dispose_ctx=False)`，**只卸载插件不销毁作用域**）、`self._mounted.pop(key, None)`；
    7. `return {"preset": preset_id, "rows": len(rows)}`。`LoaderError` 在 preset 不存在或组合不可用时向上抛。
  - `AgentPresets.recompose(self, agent_ctx: Any, preset_id: str) -> Dict[str, Any]`（`async def`，第六批补）：
    1. `key = id(agent_ctx)`；`record = self._mounted.pop(key, None)`；
    2. `record` 非空 → `await tree.dispose(dispose_ctx=False)`（卸旧树：close 各插件 + 执行 apply disposer（旧工具注销）+ 清空树索引；**作用域不销毁**）；
    3. `result = await self.mount(agent_ctx, preset_id)`（作用域级局部 ToolRuntime 已存在则复用，新树工具落到同一层）；
    4. `result["recomposed"] = True` 返回。`LoaderError` 向上抛。
    - **契约**：**调用方**负责「agent 尚未产生任何对外输出」检查（对应 TS 版 recompose 的前置约束）；旧树 dispose 幂等（`_rollback` 清空 `_mounted` 索引），作用域销毁时旧 effect 重放无害。
  - `AgentPresets.composed_preset(self, agent_ctx: Any) -> Optional[str]`：`record = _mounted.get(id(agent_ctx))`；`record[1] if record else None`。
  - `AgentPresets.close(self) -> None`：`_mounted.clear()`。

- **事件**：无（挂载走 PluginTree / ToolRuntime 自身事件）。
- **边界**：`_mounted` 以 `id(agent_ctx)` 为键（对象生命周期内有效）；preset 卸载经 `ctx.effect` 逆序回滚。
- **TS 对应**：`agent-presets`（基础版 = isolate realm 的作用域化等价物；`join(parent)`/`composeFrom` 简化见 13 号手册）。

### 2.11 schedule —— ScheduleService / build_schedule_tools / ToolSchedulePlugin

- **定位与依赖**：`ScheduleService.provides = "schedule"`；`ToolSchedulePlugin.inject = ("tools", "schedule")`。条目两种：`{id, interval_seconds, prompt, last_fired}`（间隔型）或 `{id, schedule, next_time, prompt, last_fired}`（cron 型，表达式见手册 15），后台循环每秒检查。`STORAGE_DOMAIN = "schedule"`。
- **类型与字段表（ScheduleService）**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_entries` | `Dict[str, Dict[str, Any]]` | `{}` | `id -> entry` |
  | `_task` | `Optional[asyncio.Task]` | `None` | 后台循环 task |

- **函数/方法详解**

  - `ScheduleService.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_entries={}`；`_task=None`。
  - `ScheduleService.apply(self, ctx) -> None`：`ctx.set("schedule", self)`；`_restore()`；`loop = asyncio.get_running_loop()`；`self._task = loop.create_task(self._loop())`。
  - `ScheduleService._restore(self) -> None`：`if self.ctx.has("storage")`：对 `storage.domain("schedule")` 每项 `entry.setdefault("last_fired", 0.0)` 后复制进 `_entries`。**未挂载 storage 则仅内存（不恢复）**。
  - `ScheduleService._persist(self) -> None`：`if has("storage")`：对每项 `storage.put("schedule", entry_id, dict(entry))`。
  - `ScheduleService.register(self, prompt: str, interval_seconds: Optional[float] = None, schedule: Optional[str] = None) -> str`：`interval_seconds` 与 `schedule` **恰好一个**（都缺/都给 → `ValueError`）；`entry_id = new_job_id()`（`job-<10 hex>`）；interval 型存 `{id, interval_seconds, prompt, last_fired}`；cron 型 `parse_cron(schedule)` 校验（非法 → `CronError`）并算 `next_time = spec.next_after(datetime.now()).timestamp()` 存 `{id, schedule, next_time, prompt, last_fired}`；`_persist()`；返回 id。
  - `ScheduleService.remove(self, entry_id: str) -> bool`：不在 → `False`；`del` → `if has("storage"): storage.delete("schedule", entry_id)` → `True`。
  - `ScheduleService.list(self) -> List[Dict[str, Any]]`：`[dict(e) for e in _entries.values()]`。
  - `ScheduleService._loop(self) -> None`（`async def`）：`while True:` `await asyncio.sleep(1)`；`now = time.time()`；对 `list(_entries.values())` 每项：`if self._due(entry, now):` → `entry["last_fired"] = now`；`self._fire(entry)`。`except asyncio.CancelledError: raise`（正常取消传播）。
  - `ScheduleService._due(self, entry, now: float) -> bool`：interval 型 `now - last_fired >= interval_seconds`；cron 型 `next_time` 未到 → False，否则触发并用 `parse_cron(schedule).next_after(datetime.fromtimestamp(max(now, next_time)))` **重算 next_time** 后返回 True。
  - `ScheduleService._fire(self, entry: Dict[str, Any]) -> None`：`if not self.ctx.has("agents"): return`；`message = {"content": f"[定时任务] {entry['prompt']}", "source": {"kind":"cron","schedule": entry["id"]}}`；对 `ctx.agents.list()` 每个 agent：`if not agent._disposed.is_set(): agent.inject(message["content"], source=message["source"])`。
  - `ScheduleService.close(self) -> None`：`_task.cancel()` 并置 None；`_entries.clear()`。
  - `build_schedule_tools() -> List[Any]`：返回 `[schedule_register, schedule_list, schedule_remove]`。内部 `_schedule_of(run_ctx)`（闭包）：解析 agent.ctx/root_ctx，`not has("schedule")` → `ToolError(code="NO_SCHEDULE")`，返回 `ctx.schedule`。三个工具均为 `async def`：
    - `schedule_register`（`prompt` string required、`interval_seconds` number 可选、`schedule` string 可选（cron 5/6 字段），output string）→ `register(...)`；`CronError`/`ValueError` → `ToolArgsError`；成功 `return f"scheduled: {entry_id}"`。
    - `schedule_list`（无参数，output array of objects）→ `return _schedule_of(...).list()`。
    - `schedule_remove`（`id` string required，output string）→ `removed ? f"removed {id}" : f"not found: {id}"`。
  - `ToolSchedulePlugin.__init__(self, ctx, config: Optional[dict] = None) -> None`：`_disposers=[]`。
  - `ToolSchedulePlugin.apply(self, ctx) -> None`：注册三个工具，返回 `cleanup()`。

- **关键流程伪代码**

  ```
  _loop():
      loop:
          sleep(1)
          now = time.time()
          for entry in entries:
              if now - last_fired >= interval:
                  last_fired = now
                  _fire(entry)   # → 对每个未 disposed 的活跃 agent 注入 "[定时任务] <prompt>"
  ```

- **事件**：无（注入走 `agent.inject`，busy 时排队到下一步、idle 时等下一次唤醒）。
- **边界**：`_loop` 仅在 `CancelledError` 时退出；`_fire` 同步、无 await；条目持久化依赖 storage，无 storage 时仅内存（重启丢失）。
- **TS 对应**：`schedule`/`cron`（间隔任务 + cron 表达式，第三批补齐）。

### 2.12 sandbox —— SandboxService

- **定位与依赖**：`provides = "sandbox"`。进程限制缝（第十批起有真后端）。`confine(argv, cwd)` 在生成子进程**前**改写 argv（消费者 = bash 工具）；`attach(pid)` 在生成**后**把子进程挂入沙箱（消费者 = subprocess 服务，`dsh/subprocess/local.py` 已接入）。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `_mode` | `str` | `config.get("mode", "auto")` | auto（win32→jobobject，linux→landlock，否则 local）/ jobobject / landlock / local |
  | `_job` | `WindowsJobObject \| None` | `None` | Job Object 后端（`dsh/sandbox/jobobject.py`，ctypes） |
  | `_job_error` | `str \| None` | `None` | Job 不可用时的降级原因（describe 如实标注） |
  | `_landlock_ok` | `bool` | `False` | Landlock 可用性（`available()` 探针，ABI v1+） |
  | `_landlock_error` | `str \| None` | `None` | Landlock 不可用时的降级原因 |

- **函数/方法详解**

  - `SandboxService.__init__`：mode 校验（未知抛 `ToolError`）；jobobject 模式在非 Windows 抛 `ToolError`；`CreateJobObjectW + SetInformationJobObject(KILL_ON_JOB_CLOSE[, JOB_MEMORY])` 失败 → **降级 local 并记录原因**（不装死，如实报告）；landlock 模式经 `available()` 探针（非 Linux / 内核无 ABI v1+ → `_landlock_error` + 降级）。
  - `SandboxService.attach(self, pid: int) -> None`：`job.assign(pid)`（OpenProcess + AssignProcessToJobObject；进程已退出返回 False 不抛错）；identity/landlock 后端 no-op。
  - `SandboxService.confine(self, argv, cwd)`：landlock 可用时返回 `wrapper_argv(cwd, list(argv))`（包装器先应用「只读 FS + cwd 可写」再 exec 目标命令）；否则 `return list(argv)`（jobobject/local 无需改写，cwd 由调用方限定）。
  - `SandboxService.describe()`：`{"mode", "confinement", "active", "degraded"?}`——active=False 时如实标注。
  - `SandboxService.close()`：关闭 Job 句柄 → **kill-on-close** 终止全部挂入子进程（幂等）。
- **WindowsJobObject**：`assign(pid) -> bool`；`close()`。注意：Windows 对经 Job 终止的进程常报告**退出码 0**——判定「被杀」看远早于自然结束。
- **事件**：无。
- **边界**：Job Object 限制**生命周期与内存**（可选 `memory_limit_mb`，仅 64 位），
  **不限制文件/网络**；Landlock（第十一批，Linux）限制**文件系统**（只读 FS +
  工作区可写，包装器进程先应用再 exec，不可逆）；两者均非容器。不可用一律
  降级 local 并如实标注。
- **TS 对应**：`ctx.sandbox`（landlock / sandbox-exec / Windows ACL）——
  Windows ACL 对应物 = Job Object，landlock 同名对齐。

### 2.13 persistence-sqlite —— SqlitePersistence

- **定位与依赖**：继承 `SessionPersistence`（`dsh/persistence/service.py`，`provides="sessionPersistence"`）。单文件 `~/.dsh/sessions.db`（`config.path` 可换）。`SCHEMA_VERSION = 1`。base bundle 中默认禁用，patch 开启。
- **类型与字段表**

  | 字段 | 类型 | 默认 | 说明 |
  |---|---|---|---|
  | `path` | `str` | `~/.dsh/sessions.db` | SQLite 文件 |
  | `_io_lock` | `threading.Lock` | 新建 | 跨线程共享单连接的 IO 互斥 |
  | `_conn` | `Optional[sqlite3.Connection]` | `None` | 惰性连接 |

- **函数/方法详解**

  - `SqlitePersistence.__init__(self, ctx, config: Optional[dict] = None) -> None`：`super().__init__(ctx, config)`（继承缓冲/写锁/失败集）；`path`、`_io_lock`、`_conn=None`。
  - `SqlitePersistence.apply(self, ctx) -> None`：`super().apply(ctx)`（`ctx.set("sessionPersistence", self)` + 订阅 `session/event`、`session/flush`）；`os.makedirs(os.path.dirname(path), exist_ok=True)`（父目录非空时）。
  - `SqlitePersistence._connect(self) -> sqlite3.Connection`：`_conn is None` 时惰性创建：`sqlite3.connect(path, check_same_thread=False)`（`check_same_thread=False` + `_io_lock`：跨 `to_thread` 线程共享单连接）；`execute("PRAGMA user_version = 1")`；`CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, header TEXT NOT NULL)`；`CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(session_id, seq))`；`commit()`；返回 conn。
  - `SqlitePersistence.locate(self, session: Session) -> Optional[str]`：`return None`（会话共享一库，无逐会话工件）。
  - `SqlitePersistence._write_batch(self, session: Session, events: List[SessionEvent]) -> None`（`async def`）：`header_json = json.dumps(_header_to_json(session.header), ensure_ascii=False)`；内层同步 `_write()`：`conn = self._connect()`；`with _io_lock:` → `INSERT OR REPLACE INTO sessions (session_id, header) VALUES (?, ?)` + `executemany("INSERT OR REPLACE INTO events (session_id, seq, event) VALUES (?, ?, ?)", [(session.id, e.seq, json.dumps(e.to_json(), ensure_ascii=False)) for e in events])` + `commit()`；`await asyncio.to_thread(_write)`。
  - `SqlitePersistence._load_raw(self, session_id: str) -> Tuple[SessionHeader, List[dict]]`（`async def`）：内层 `_read()`：`with _io_lock:` → `SELECT header FROM sessions WHERE session_id=?`；无行 → `(None, [])`；否则 `_header_from_json(json.loads(row[0]))` + `SELECT seq, event FROM events WHERE session_id=? ORDER BY seq` → `[json.loads(event) for _seq, event in event_rows]`；`header, rows = await asyncio.to_thread(_read)`；`header is None` → `raise SessionError("session ... not found in ...")`；返回 `(header, rows)`。
  - `SqlitePersistence.list_ids(self) -> List[str]`（`async def`）：`to_thread` 执行 `SELECT session_id FROM sessions`，返回 `[row[0] for row in ...]`。
  - `SqlitePersistence.close(self) -> None`：`super().close()`（置 `_closed`、清缓冲）；`_conn` 非 None → `with _io_lock:` 关闭连接并置 None。
  - `_header_to_json(header: SessionHeader) -> Dict[str, Any]`（模块级）：序列化 9 字段：`version, id, created_at, cwd, parent_session, seed_length, origin, delegation_depth, agent_preset`。
  - `_header_from_json(raw: Dict[str, Any]) -> SessionHeader`（模块级）：反序列化并容错默认值：`version=int(raw.get("version",1))`、`id=str(raw.get("id",""))`、`created_at=int(raw.get("created_at",0))`、`cwd/parent_session/seed_length/origin/agent_preset` 原样取、`delegation_depth=int(raw.get("delegation_depth",0))`。

- **关键流程伪代码**

  ```
  flush(session) [基类 SessionPersistence.flush]:
      batch = 缓冲弹出
      if batch: async with _write_lock:
          await _write_batch(session, batch)   # to_thread: INSERT OR REPLACE 两表
          _failed.discard(session.id)
  load(session_id) [基类 SessionPersistence.load]:
      header, rows = await _load_raw(session_id)
      repaired = repair_open_turn(rows)        # 孤儿 turn 补合成 turn/end(interrupted)
      return header, repaired
  ```

- **事件**：订阅 `session/event`、`session/flush`（继承自基类）。无额外发布。
- **边界**：所有 SQLite IO 经 `asyncio.to_thread`（不阻塞事件循环）；`check_same_thread=False` + `_io_lock` 串行化跨线程访问；加载走与 JSONL 相同的校验/崩溃修复路径（`SessionPersistence.load` → `repair_open_turn`，孤儿 turn 以合成 `turn/end {kind:'interrupted'}` 关闭，不截断）；格式不符抛 `SessionError`。
- **TS 对应**：`session-persistence-sqlite`。

---

## 3. 与 TS 版的语义差异说明（stub/简化点）

以下均如实标注，不夸大能力：

| 子系统 | TS 版概念 | Python 实现 | 差异/简化 |
|---|---|---|---|
| sandbox | landlock / sandbox-exec / Windows ACL 真实围栏 | 第十批起：Windows = Job Object（kill-on-close + 可选内存上限，`attach(pid)` 接入 subprocess 服务）；第十一批起：Linux = Landlock（只读 FS + 工作区可写，包装器进程先应用再 exec）；其余 POSIX = local identity | **Job Object 覆盖生命周期/内存、Landlock 覆盖文件系统**，均非容器；不可用时降级 local 并在 `describe()` 如实标注 |
| presets | roster / isolate realm / `mount` / `composeFrom` | preset yml 行挂进 agent 作用域，局部 `ToolRuntime(parent=根运行时)` = 父层作用域视图（第七批全委托） | **基础版 isolate 等价物**：作用域化（服务/工具/分节仅该 agent 及其后代可见），非完整 roster/composeFrom；`join(parent)` 简化为「父作用域为 parent 创建」 |
| hooks | hook-protocol（结构化 handler 协议）+ claude/codex 桥 | hooks.yml：shell 命令 + 五监听点 | **hooks.yml 最小桥**：命令是 shell 字符串，非 JS handler；`pre-tool` 只有 `deny`/`ask`（`allow` 由「不拦截」隐式表达）；`pre-step` 非零退出 = reject |
| schedule | schedule / cron（cron 表达式、日历语义） | interval 与 cron 两种条目 + 每秒轮询 | **interval + cron 表达式**（5/6 字段，见手册 15）；busy 排队 / idle 等下一次唤醒与 TS cron 一致；无 storage 时仅内存 |
| web_search | tool-web 的 web search（可能是结构化 provider） | DuckDuckGo lite HTML 端点 + 正则解析 | **DDG lite 尽力而为**：无密钥；被限流返回 `NO_RESULTS` 结构化错误而非崩溃；`web_fetch` 的 `present_result` 硬编码 `status_code=200, truncated=False`（UI 卡片不反映真实状态） |
| sessionTelemetry | session-telemetry-otel（OpenTelemetry 上报） | ConsoleTelemetryPlugin 写 JSONL | JSONL 后端（replayable trace），默认关闭；无 OTel 导出器 |
| storage | storage-json + storage-domain | 单 JSON 文件按 domain 分区 | 域数据设施的最小形态；无版本迁移/事务 |
| settings | settings-file | 单 JSON 文件同步覆写 | 每次写全量落盘；`set` 吞事件异常、`delete` 不吞（不一致点见 2.1） |
| SQLite | session-persistence-sqlite | 单库两表 | 与 JSONL 后端互斥（base.yml 默认禁用）；`locate()` 恒 None |
| time-context | time-context | `PromptContext(order=10)` 动态注入 | 功能对齐；时区用 `time.strftime("%Z")` |
| userQuestions | user-questions 缝 + tool-ask-user | 单通道回调 | 通道为「注册即覆盖」，非多路复用 |
| skills | skill + skill-filesystem + tool-skill | SKILL.md front matter 解析 | 功能对齐；`skill_load` 经 `defer_context` 注入下一请求 |

---

## 4. 相关测试（tests/test_seams.py 逐用例对应）

共 15 项，与子系统一一对应：

| 测试用例 | 覆盖子系统 | 断言要点 |
|---|---|---|
| `test_session_title_default` | session-title | 首条 user 消息截断 ≤60，前缀匹配 |
| `test_telemetry_record_seam` | telemetry | `session-telemetry/record` payload 的 `session` == 会话 id |
| `test_settings_round_trip` | settings | `set`→`get` 一致；`settings/updated` payload key；重载持久化 |
| `test_storage_domain_changed` | storage | `put`→`get` 一致；`domain/changed` 含 domain；`domains()` |
| `test_web_fetch_bad_scheme` | web | `ftp://` → `ToolError(BAD_URL)` |
| `test_ask_user_channel_and_no_channel` | user-questions | 有通道返回文本；`set_channel(None)` → `NO_CHANNEL` |
| `test_request_context_logged` | （伴随事件，见手册 13） | `session.request_context()` 非空；`provider=="mock"`；`context_window==8192` |
| `test_sqlite_backend_parity` | SQLite | 写读往返 header/末事件 `turn/end(interrupted)`；`list_ids`；`locate is None` |
| `test_subagent_start_end_events` | （伴随事件，见手册 13） | `subagent/start` → `subagent/end {ok:True}` |
| `test_skills_discovery_and_load` | skills | 发现 1 个技能，`id=="my-skill"`；`get(...).instructions` 前缀 |
| `test_hooks_pre_tool_deny` | hooks | pre-tool 非零退出 → `DENIED`；会话含 `hook/invoked`、`hook/result` |
| `test_agent_preset_mount` | presets | `composed_preset=="greet"`；`agent.ctx.tools.get("greet")` 非空；`ctx.tools.get("greet") is None` |
| `test_agent_preset_recompose`（第六批） | presets | 换绑 farewell：`recomposed is True`、新工具可见、旧工具已卸载、全局不可见；再次换绑幂等 |
| `test_agent_request_done_event`（第六批） | agent/request-done | `provider/model=="mock"`、`latency_ms` 非负 int、`usage` None 或 dict、`turn==step==1` |
| `test_schedule_injects_notification` | schedule | 0.2s 间隔 → 1.2s 后 inbox `next_step` ≥1 |
| `test_sandbox_stub_identity` | sandbox | `confine(argv)==argv`；`describe()["confinement"]=="none (stub)"` |
| `test_settings_drive_agent_default_model` | settings→agentDefaultModel | `agent_default_model` 写入后 `current_selection()` 合并用户层 |

> 运行方式：`python -m pytest tests/test_seams.py -q`，预期 15 passed（本批次新增）。
