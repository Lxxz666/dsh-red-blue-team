# 服务端 Web 界面开发手册

> 对应 TS 版概念：`dsh-web-app`（FastAPI Web 服务 + SSE 广播中枢 + 审批通道）。
> 源码文件：`dsh/server/app.py`、`dsh/server/static/index.html`、`dsh/server/static/app.js`、`dsh/server/static/style.css`。
> 生成方式：本文按代码逐函数撰写；服务端函数签名以 `python -c "import inspect; ..."` 核验；前端 app.js 逐函数覆盖。

## 1. 模块定位与架构位置

`dsh.server.app` 是从已 boot 的根 `Context` 构建 FastAPI 应用，提供：

- **SSE 广播中枢**（`SseHub`）：订阅 `session/event` 与 `agent/status`，按会话把事件推给各订阅者。
- **审批通道**（bool）：`ctx.approval.set_channel(_approval_channel)` 注册人工应答通道；`tools/pre-execute` 的 `ask` 决策 → `ApprovalService.request` → 通道广播问题 → 浏览器 POST 答案 → future 完成。
- **文本问答通道**（ask_user）：`ctx.userQuestions.set_channel(_question_channel)` 注册文本问答通道；`ask_user` 工具 → `UserQuestionsService.ask` → SSE 推送 `user-question` → 浏览器文本输入 → `POST /api/questions/{qid}` → future 完成。
- **消息反馈**：`POST /api/sessions/{id}/feedback`（点赞/点踩，经 `ctx.messageFeedback`）与 `GET /api/sessions/{id}/feedback`（读取，`ctx.has` 守卫）。
- **REST**：会话 CRUD、消息投递（斜杠命令直派发）、取消、事件回放、providers 列表。
- **静态文件**：`/` 返回 `index.html`；`app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")` 把 CSS/JS 挂到 `/static/*`；`index.html` 用绝对路径 `/static/style.css`、`/static/app.js` 引用。

依赖关系：`run_server` 调 `boot`（手册 09）得到 `ctx`/`tree`，再 `build_app(ctx)`。`build_app` 依赖 `ctx.sessions/ctx.agents/ctx.llm/ctx.goals/ctx.commands/ctx.approval/ctx.userQuestions/ctx.sessionTitle/ctx.messageFeedback`（后几者经 `ctx.has` 守卫，可选）。

## 2. 文件清单表

| 文件 | 职责 |
|---|---|
| `dsh/server/app.py` | `SseHub` SSE 中枢 + FastAPI 应用构建 + 全部 REST/SSE 端点 + `run_server` 启动 |
| `dsh/server/static/index.html` | 页面骨架：侧栏、主区、输入区、审批/问答弹窗 |
| `dsh/server/static/app.js` | 前端逻辑：会话列表、流式渲染、工具卡片、审批/问答弹窗、SSE 消费 |
| `dsh/server/static/style.css` | 深色主题样式与布局 |

## 3. 类型与数据结构

### 3.1 `SseHub`（普通类）

| 字段 | 类型 | 说明 |
|---|---|---|
| `ctx` | `Context` | 根上下文 |
| `_subscribers` | `Dict[str, List[asyncio.Queue]]` | 会话 id → 订阅者队列列表 |
| `_disposers` | `List[Disposer]` | `session/event` 与 `agent/status` 两个监听器的注销函数 |

### 3.2 Pydantic 模型

```python
class MessageIn(BaseModel):
    content: str
```
```python
class ApprovalAnswer(BaseModel):
    allow: bool
```
```python
class QuestionAnswer(BaseModel):
    text: str
```
```python
class FeedbackIn(BaseModel):
    seq: int
    kind: str
```

> 四个模型都定义在模块级（`build_app` 之外）：`app.py` 顶部 `from __future__ import annotations` 启用 postponed annotations，FastAPI 无法解析函数内定义的 Pydantic 模型，故 `QuestionAnswer`/`FeedbackIn` 从函数内移出。

### 3.3 `app.state`

`build_app` 在 `app.state` 上存放：`ctx`、`hub`（`SseHub`）、`approval_requests: Dict[str, asyncio.Future]`、`approval_seq: int = 0`、`question_requests: Dict[str, asyncio.Future]`。

### 3.4 前端 `state`（app.js）

```js
const state = {
  sessionId: null,
  eventSource: null,
  rendered: new Map(),      // seq -> element（已渲染事件）
  pendingTools: new Map(),  // callId -> 卡片元素
  statusEl: null,
  messagesEl: null,
};
```

## 4. 函数与类方法详解

### 4.1 `dsh/server/app.py`

#### 类 `SseHub`

```python
def __init__(self, ctx) -> None
```
初始化订阅表，注册两个监听器：`ctx.on("session/event", self._on_session_event)`、`ctx.on("agent/status", self._on_agent_status)`。

```python
def subscribe(self, session_id: str) -> asyncio.Queue
```
创建 `asyncio.Queue(maxsize=1000)`，加入 `_subscribers[session_id]` 列表，返回该队列（调用者持有）。

```python
def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None
```
从对应列表移除队列；列表不存在或队列不存在（`ValueError`）均静默忽略。

```python
def push(self, session_id: str, payload: Dict[str, Any]) -> None
```
向该会话全部订阅者 `put_nowait`；`QueueFull` 时先 `get_nowait()` 丢弃最旧一条再 `put_nowait`（`QueueEmpty/QueueFull` 一并捕获忽略）。

```python
def _on_session_event(self, session: Any, event: Any) -> None
```
`self.push(session.id, {"kind": "event", "event": event.to_json()})`。

```python
def _on_agent_status(self, payload: Dict[str, Any]) -> None
```
`agent = payload.get("agent")`；为 `None` 返回；否则 `self.push(agent.id, {"kind": "status", "status": payload["status"]})`。

```python
def close(self) -> None
```
调用全部 disposer、`clear()` disposer 与订阅表。

#### 函数 `build_app`

```python
def build_app(ctx: Any) -> FastAPI
```
先定义 `lifespan(app)`（`@asynccontextmanager`）：`yield` 后取 `app.state.hub`（存在则 `hub.close()`）。然后创建 `FastAPI(title="dsh-python", version="0.1.0", lifespan=lifespan)`，初始化 `app.state`（见 3.3），并 `app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")` 挂载静态资源。随后：

**文本问答通道 `_question_channel(question, detail)`（内部 async）**：`ctx.has("userQuestions")` 时定义；`approval_seq += 1` → `qid = f"question-{seq}"` → `loop.create_future()` 存入 `question_requests[qid]` → 对 `ctx.sessions.list()` 每个会话 `hub.push(session.id, {"kind":"user-question","qid","question","detail"})` → `await future`；`finally` 弹出 qid。最后 `ctx.userQuestions.set_channel(_question_channel)`。

**审批通道 `_approval_channel(question, detail)`（内部 async）**：`approval_seq += 1` → `qid = f"approval-{seq}"` → `loop.create_future()` 存入 `approval_requests[qid]` → 对 `ctx.sessions.list()` 每个会话 `hub.push(session.id, {"kind":"approval","qid","question","detail"})`（广播给所有活跃会话）→ `await future`；`finally` 弹出 qid。最后 `ctx.approval.set_channel(_approval_channel)`。

**页面 `index()`（`@app.get("/")`）**：返回 `FileResponse(STATIC_DIR / "index.html")`。

**会话端点**（详见 REST 表）：
- `list_sessions()`（`GET /api/sessions`）：若 `ctx.has("sessionTitle")` → `preview = ctx.sessionTitle.title_for(session)`；否则回退取 `derive_messages()` 最后一条 `plain_text()[:60]`（无消息则空串）。返回 `[{id, created_at, preview}]`。
- `create_session()`（`POST /api/sessions`）：`agent = await ctx.agents.create()`，返回 `{"id": agent.id}`。
- `get_events(session_id, since=0)`（`GET /api/sessions/{id}/events`）：`ctx.sessions.get` 为空返回 404 `{"error":"not found"}`；否则返回 `{"events": [e.to_json() for e in session.events if e.seq >= since], "todos": session.todos(), "goal": ctx.goals.get(f"agent:{session_id}").to_json() 或 None（ctx.has("goals") 时）, "surface": list(session.surface.nodes)}`。
- `post_message(session_id, body)`（`POST /api/sessions/{id}/messages`）：`ctx.agents.get` 为空返回 404 `{"error":"agent not live"}`；`content = body.content.strip()`，空返回 `{"ok": True, "ignored": True}`；`content.startswith("/")` 且 `ctx.has("commands")` → `ctx.commands.dispatch(agent, content)` 返回 `{"ok": True, "command": result}`；否则 `agent.followup(content)` 返回 `{"ok": True, "enqueued": True}`。
- `cancel_session(session_id)`（`POST /api/sessions/{id}/cancel`）：`ctx.agents.get` 为空返回 404 `{"error":"agent not live"}`；`agent.cancel({"kind": "user"})` 返回 `{"ok": True}`。
- `post_feedback(session_id, body)`（`POST /api/sessions/{id}/feedback`）：`ctx.has("messageFeedback")` 为假返回 404 `{"error":"feedback unavailable"}`；否则 `ctx.messageFeedback.put(session_id, body.seq, body.kind)`，`ValueError`（非法 kind）返回 400 `{"error": str(exc)}`；成功返回 `{"ok": True, "record": record}`。
- `get_feedback(session_id)`（`GET /api/sessions/{id}/feedback`）：`ctx.has("messageFeedback")` 为假返回 `{"feedback": []}`；否则返回 `{"feedback": ctx.messageFeedback.get(session_id)}`。
- `answer_approval(qid, body)`（`POST /api/approval/{qid}`）：`approval_requests.get(qid)` 为空或已 done 返回 404 `{"error":"unknown approval"}`；`future.set_result(body.allow)` 返回 `{"ok": True}`。
- `answer_question(qid, body)`（`POST /api/questions/{qid}`）：`question_requests.get(qid)` 为空或已 done 返回 404 `{"error":"unknown question"}`；`future.set_result(body.text)` 返回 `{"ok": True}`。
- `providers()`（`GET /api/providers`）：返回 `{"providers": ctx.llm.providers()}`。

**SSE `stream(session_id, request)`（`GET /api/sessions/{id}/stream`）**：`queue = hub.subscribe(session_id)`。内部 `gen()`：
1. `yield "event: hello\ndata: {}\n\n"`。
2. 循环：`await request.is_disconnected()` 为真 break；`payload = await asyncio.wait_for(queue.get(), timeout=15)`，`yield f"event: {payload['kind']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"`；`asyncio.TimeoutError` 时 `yield ": keep-alive\n\n"`（注释保活）。
3. `finally`：`hub.unsubscribe(session_id, queue)`。
返回 `StreamingResponse(gen(), media_type="text/event-stream")`。

**关闭**：无 `@app.on_event("shutdown")`；改为 `lifespan` 上下文管理器（见上）在应用退出后 `hub.close()`。

返回 `app`。

#### 函数 `run_server`

```python
async def run_server(profile: str = "web", workspace: Optional[str] = None,
                     mock: bool = False, provider: Optional[str] = None,
                     model: Optional[str] = None, host: str = "127.0.0.1",
                     port: int = 3080) -> None
```
`setup_logging()` → `ctx, tree = await boot(profile=..., workspace=..., mock_llm=mock, provider=..., model=...)` → `app = build_app(ctx)` → `uvicorn.Config(app, host=host, port=port, log_level="info")` → `server.serve()` → `await tree.dispose()`。

### 4.2 `dsh/server/static/app.js`

```js
const $ = (sel) => document.querySelector(sel);
```
DOM 查询简写。

```js
async function api(path, options = {})
```
`fetch(path, {headers: {"Content-Type": "application/json"}, ...options})`；`!resp.ok` 抛 `Error`；返回 `resp.json()`。

```js
function escapeHtml(text)
```
转义 `&` `<` `>`（注意：不转义引号）。

```js
function renderMarkdown(text)
```
微 markdown：先 `escapeHtml` 后把 ```` ```lang\n...``` ```` 替换为占位符（code 块进 `codeBlocks`），再处理行内 `` `code` `` 与 `**bold**`，最后还原 code 块（`<pre><code>...</code></pre>`）。返回 HTML 字符串。

```js
function el(tag, cls, text)
```
创建元素；`cls` 非空设 `className`；`text !== undefined` 时设 `textContent`。

```js
function renderUserMessage(event)
```
返回 `.msg.user` 包裹，含 `.role-tag`「用户」与 `.bubble`（`innerHTML = renderMarkdown(event.data.content ?? "")`）。

```js
function renderAssistantChunk(event)
```
增量流式：取 `state.messagesEl.lastElementChild`；若 `chunk.type === "text-delta"` 且当前气泡 `dataset.streaming === "1"`，把 `chunk.text` 追加到 `.bubble`（用 `textContent + chunk.text` 重渲染 markdown），滚动到底部；否则返回 `null`。

```js
function renderAssistantMessage(event)
```
返回 `.msg.assistant` 包裹：`.role-tag`「助手」+ `.bubble`（取 `event.data.blocks` 中 `kind === "text"` 的 `text` 拼接，`renderMarkdown`）。若 `text` 非空，追加 `.feedback` 容器：两个 `.fb-btn` 按钮（「👍」「👎」），`onclick` 分别绑 `sendFeedback(event.seq, "up"/"down")`。

```js
async function sendFeedback(seq, kind)
```
未选会话（`!state.sessionId`）则返回；`POST /api/sessions/{id}/feedback`（body `{seq, kind}`）；异常仅 `console.warn("feedback failed", err)`，不打断渲染。

```js
function renderToolCall(event)
```
创建 `.tool-card`，`tool-head` 显示工具名与「执行中…」；`state.pendingTools.set(event.data.call_id, card)`；返回卡片。

```js
function renderToolResult(event)
```
按 `call_id` 取 `pendingTools` 里的卡片，无则返回 `null`；追加 `.tool-body`（`textContent = String(event.data.content ?? "")`）；`event.data.is_error` 为真给卡片加 `.error` 并把头部改为「失败」，否则改「完成」；`pendingTools.delete(call_id)`；返回卡片。

```js
function renderEvent(event)
```
以 `event.seq` 去重（`state.rendered.has(seq)` 则返回）；按 `event.type` 分派：`user/message`→`renderUserMessage`、`assistant/chunk`→`renderAssistantChunk`、`assistant/message`→`renderAssistantMessage`、`tool/call`→`renderToolCall`、`tool/result`→`renderToolResult`、`compaction/summary`→构造 `.msg.user`「上下文压缩」气泡（`textContent = event.data.summary ?? ""`）。若有节点则 `appendChild`、`rendered.set(seq, node)`、滚动到底部。

```js
function beginStreaming()
```
创建 `.msg.assistant` 占位（`dataset.streaming = "1"`，含 `.role-tag`「助手」与空 `.bubble`）并追加，供 `renderAssistantChunk` 增量填充。

```js
async function loadSessions()
```
`api("/api/sessions")`；清空 `#session-list`；逐项创建 `.session-item`（当前会话加 `.active`），文本为 `preview || session.id.slice(0, 16)`，`onclick` 绑 `selectSession(session.id)`。

```js
async function createSession()
```
`POST /api/sessions` → `selectSession(data.id)`。

```js
async function selectSession(sessionId)
```
设置 `state.sessionId`、清空 `rendered`/`pendingTools`/`#messages`、设标题、`await loadSessions()`、`await replay()`、`connectStream()`。

```js
async function replay()
```
`GET /api/sessions/{id}/events`，对 `data.events` 逐个 `renderEvent`。

```js
function connectStream()
```
先关旧 `EventSource`；新建 `EventSource(/api/sessions/{id}/stream)`；监听 `event`（解析 `payload.event` 走 `renderEvent`）、`status`（`setStatus(payload.status)`）、`approval`（`showApproval(...)`）、`user-question`（`showQuestion(...)`）；`onerror` 留空（浏览器自动重连）。

```js
function setStatus(status)
```
更新 `#status` 文本与 class（`running` → `.running`，否则 `.idle`）；`#btn-cancel` 按 `status === "running"` 切 `.visible`。

```js
async function sendMessage()
```
取输入、`trim`；空或未选会话则返回；清空输入框；本地回显用户气泡 + `beginStreaming()`；`POST /api/sessions/{id}/messages`；若返回 `data.command && data.command.reply`（斜杠命令），追加「命令」气泡显示 `reply` 并移除末尾多余的流式占位。

```js
function showApproval(payload)
```
填充 `#modal-question` / `#modal-detail`，显示 `#modal`；定义 `answer(allow)`：隐藏弹窗 + `POST /api/approval/{qid}`（body `{allow}`）；绑定 `#modal-allow`→`answer(true)`、`#modal-deny`→`answer(false)`。

```js
function showQuestion(payload)
```
复用审批弹窗做文本问答：填充 `#modal-question`/`#modal-detail`；取 `#modal-input` 清空并 `classList.remove("hidden")`、`#modal-deny` 隐藏（`style.display="none"`）、`#modal-allow` 文案改「提交回答」、显示 `#modal`；`submit()`：隐藏弹窗、恢复按钮态与「允许」文案、隐藏输入框、`POST /api/questions/{qid}`（body `{text: 输入或"(无回答)"}`）；`#modal-allow` 与 `#modal-deny` 均绑定 `submit`。

```js
async function init()
```
缓存 `#status`/`#messages`；绑定 `#btn-new`→`createSession`、`#btn-send`→`sendMessage`、`#btn-cancel`→`POST .../cancel`、`#input` keydown（Enter 且非 Shift 发送）；`GET /api/providers` 填 `#providers`；`GET /api/sessions`：为空则 `createSession()`，否则 `selectSession(最后一条.id)`。

```js
init();
```
脚本加载即执行初始化。

### 4.3 `dsh/server/static/index.html`（结构）

- `#sidebar`：`.brand`（logo `dsh` + `python`）、`#btn-new`「＋ 新会话」、`#session-list`、侧栏底 `.providers`（`#providers`）与提示「斜杠命令: /help /goal /compact /plan」。
- `#main`：`#topbar`（`#session-title` + `#status` + `#btn-cancel`「■ 停止」）、`#messages`、`#inputbar`（`#input` textarea + `#btn-send`）。
- `#modal`：`.modal-box` 含 `.modal-title`「需要人工确认」、`#modal-question`、`#modal-detail`、`#modal-input`（textarea，`class="modal-input hidden"`，问答文本输入）、`#modal-allow`（允许）、`#modal-deny`（拒绝）。
- 第 7 行 `<link rel="stylesheet" href="/static/style.css">`；末尾 `<script src="/static/app.js"></script>`。

### 4.4 `dsh/server/static/style.css`（结构）

`:root` 定义深色主题 CSS 变量（`--bg/--panel/--panel-2/--border/--text/--muted/--accent/--accent-2/--user/--assistant/--tool/--danger/--ok`）。分区：侧栏（`.brand/.logo/.new-btn/.session-list/.session-item(.active)/.sidebar-foot/.providers`）、主区（`#topbar/.status(.running)/.cancel-btn(.visible)/.messages/.msg(.user/.assistant)/.role-tag/.bubble`）、工具卡片（`.tool-card/.tool-head/.tool-body/.error`）、消息反馈（`.feedback/.fb-btn(.hover)`）、markdown 微渲染（`.bubble code/pre/h1-3`）、输入区（`#inputbar/#input/.send-btn`）、审批/问答弹窗（`.modal(.hidden)/.modal-box/.modal-title/.modal-question/.modal-detail/.modal-input(.hidden)/.modal-actions(.primary/.danger)`）、滚动条样式。

## 5. 关键流程

### 5.1 REST API 表

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/` | — | `text/html`（index.html） |
| GET | `/api/sessions` | — | `[{id, created_at, preview}]` |
| POST | `/api/sessions` | — | `{"id": agent.id}` |
| GET | `/api/sessions/{id}/events` | query `since`(int, 默认 0) | `{events, todos, goal, surface}`；404 `{"error":"not found"}` |
| POST | `/api/sessions/{id}/messages` | `{"content": str}` | 空→`{"ok":true,"ignored":true}`；斜杠命令→`{"ok":true,"command":{handled,reply}}`；否则 `{"ok":true,"enqueued":true}`；404 `{"error":"agent not live"}` |
| POST | `/api/sessions/{id}/cancel` | — | `{"ok":true}`；404 `{"error":"agent not live"}` |
| POST | `/api/sessions/{id}/feedback` | `{"seq": int, "kind": str}` | `{"ok":true,"record":{...}}`；400 `{"error":...}`（非法 kind）；404 `{"error":"feedback unavailable"}` |
| GET | `/api/sessions/{id}/feedback` | — | `{"feedback":[...]}`（无 messageFeedback 时 `[]`） |
| POST | `/api/approval/{qid}` | `{"allow": bool}` | `{"ok":true}`；404 `{"error":"unknown approval"}` |
| POST | `/api/questions/{qid}` | `{"text": str}` | `{"ok":true}`；404 `{"error":"unknown question"}` |
| GET | `/api/providers` | — | `{"providers": [name,...]}` |
| GET | `/api/wanter/terrain` | — | `image/svg+xml`（`dsh/wanter/viz.py::render_terrain_svg`）；wanter 未挂载 404 |
| GET | `/api/wanter/state` | — | `{"dim","goals","sigma","erosion_bumps","trace_events","trace_decay_rate","completed"}`；wanter 未挂载 404 |
| GET | `/static/wanter.html` | — | `text/html`（wanter 地形面板：3s 自动刷新 SVG + 状态表） |
| GET | `/api/sessions/tree` | — | `{"roots": [id], "nodes": {id: {parent, children, live, origin, created_at}}}`（活跃 + 持久化谱系合并） |
| GET | `/static/tree.html` | — | `text/html`（会话节点树：fork 谱系可视化） |
| GET | `/api/sessions/{id}/stream` | SSE | `text/event-stream`（见 5.2） |

### 5.2 SSE 广播与审批/问答的端到端协议

**事件格式**（`event: <kind>` + `data: <json>`，`ensure_ascii=False`）：

| event | data | 触发 |
|---|---|---|
| `hello` | `{}` | 连接建立时首帧 |
| `event` | `{"kind":"event","event":{type,seq,time,data,...}}` | `session/event`（每条会话日志） |
| `status` | `{"kind":"status","status":"running"\|"idle"}` | `agent/status` |
| `approval` | `{"kind":"approval","qid","question","detail"}` | 审批通道广播 |
| `user-question` | `{"kind":"user-question","qid","question","detail"}` | 文本问答通道广播 |
| （注释） | `: keep-alive` | 15 秒无事件保活 |

**审批端到端**：工具守卫返回 `AskDecision` → 工具管线调 `ApprovalService.request(question, detail)` → `_approval_channel` 生成 qid + future 存入 `approval_requests` → `hub.push` 广播 `approval` 到所有会话 → 前端当前会话的 `EventSource` 收到 → `showApproval` 弹窗 → 用户点允许/拒绝 → `POST /api/approval/{qid}` → `answer_approval` `future.set_result(allow)` → `request` 返回布尔 → 守卫据此 allow/deny。

**文本问答端到端**：`ask_user` 工具 → `UserQuestionsService.ask(question, detail)` → `_question_channel` 生成 qid + future 存入 `question_requests` → `hub.push` 广播 `user-question` 到所有会话 → 前端 `EventSource` 收到 → `showQuestion` 弹窗带文本输入框 → 用户输入并提交 → `POST /api/questions/{qid}` → `answer_question` `future.set_result(text)` → `ask` 返回文本（无通道/未作答抛 `ToolError`）。

## 6. 事件与扩展点

- 监听：`session/event`、`agent/status`（`SseHub` 构造时注册）。
- 注册：`ctx.approval.set_channel(_approval_channel)`（审批 bool 通道）、`ctx.userQuestions.set_channel(_question_channel)`（文本问答通道，仅 `ctx.has("userQuestions")` 时）。
- 无自定义 emit；SSE 的 `kind` 是前端协议命名空间，非内核事件名。

## 7. 常见改动指引

- **新增一个 REST 端点**：在 `build_app` 内加 `@app.get/post(...)` 路由函数，复用 `ctx.*` 服务与 `JSONResponse`。需要鉴权时自行加依赖。
- **新增一个 SSE 事件类型**：在 `SseHub` 增加 `ctx.on(...)` 监听器并 `push` 一个带新 `kind` 的 payload；前端 `connectStream` 里加对应的 `addEventListener`，`renderEvent`/专门渲染器处理。
- **改审批广播范围**：把 `_approval_channel` 里的 `for session in ctx.sessions.list()` 改为按需定向（例如只推给发起会话）。
- **改静态资源**：直接改 `index.html` / `app.js` / `style.css`；若引入新静态文件，确保相对路径在 `STATIC_DIR` 下可访问。
- **改端口/主机默认值**：改 `run_server` 默认参数与 `cli/main.py` 的 `--port/--host` 默认值。
- **新增工具卡片样式**：在 `app.js` 的 `renderEvent` switch 加 case，并在 `style.css` 加对应 class。

## 8. 相关测试

- server 无独立测试文件（`tests/` 不含 server 测试）。手动验证：
  - `python run.py web --port 3081 --mock` 启动（`--mock` 禁用 deepseek 适配器，避免无密钥报错），浏览器打开 `http://127.0.0.1:3081`。
  - 观察侧栏会话列表、发送普通消息走流式 `assistant/chunk`→`assistant/message`、发送 `/help` 走命令直派发、`/goal <描述>` 触发续轮、工具调用渲染为工具卡片。
  - 用一个会触发 `AskDecision` 的工具（或审批通道）验证弹窗与 `POST /api/approval/{qid}` 往返。
  - `curl http://127.0.0.1:3081/api/sessions`、`curl http://127.0.0.1:3081/api/providers` 验证 REST。
