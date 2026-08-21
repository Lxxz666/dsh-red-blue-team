# 智能体与循环 开发手册

> 对应 TS 版概念：`Agent` 公共接口（`followup`/`steer`/`inject`/`cancel`/`whenIdle`/`runMaintenance`）、`agent-loop` 包的 turn/step 状态机、`Inbox`、`user-approval`、`agent-default-model`。
> 源码文件清单：`dsh/agent/agent.py`、`dsh/agent/inbox.py`、`dsh/agent/approval.py`、`dsh/agent/loop.py`、`dsh/agent/plugins.py`。
> 生成方式：人工完整读取上述源码，并用 `python -c "import inspect; ..."` 逐个验证签名后撰写；仅记录代码真实行为。

## 1. 模块定位与架构位置

**职责**：把「一个活跃 agent」抽象成公共句柄（`Agent`）、两队列收件箱（`Inbox`）、人机审批通道（`ApprovalService`）、具体驱动循环（`AgentLoopService`）与默认模型选项插件。驱动循环把排队输入逐批消费成 turn/step，串起 prompt 组装、LLM 流、工具执行。

**ctx 服务名与 provides/inject**：

| 类 | provides | inject | ctx.<key> |
|---|---|---|---|
| `AgentRegistry` | `"agents"` | — | `ctx.agents` |
| `AgentLoopService` | `"agentLoop"` | —（消费 `sessions`/`agents`/`llm`/`tools`/`systemPrompt`/`sessionPersistence`） | `ctx.agentLoop` |
| `ApprovalService` | `"approval"` | — | `ctx.approval` |
| `DefaultOptionsPlugin` | `None` | —（`apply` 里 `ctx.set` 到 `agentDefaultModel`） | `ctx.agentDefaultModel` |

**与其他模块的调用关系**：
- `AgentLoopService` 依赖 `ctx.sessions`（`SessionStore`）、`ctx.agents`、`ctx.llm`、`ctx.tools`、`ctx.systemPrompt`，恢复时用 `ctx.sessionPersistence`。
- `Agent` 构造时持有 `Inbox` 与作用域 `ctx`（`ctx.scoped(f"agent:{session.id}")`），事件经 `AgentLoopService.emit_agent_event` 广播。
- `Inbox`/`ApprovalService` 是 `Agent`/工具管线消费的独立服务。
- `AbortSignal`（`dsh.tools.pipeline`）承载 turn 级取消信号。

**能力缝三角色分析**：
- **Definition**：`AgentLoopBase`（`_spawn` 传给 `Agent` 的 factory，`Agent.__init__` 的类型标注 `"AgentLoopBase"`，实际是 `AgentLoopService`）。
- **Provider**：`AgentLoopService`（实现 create/resume/teardown/driver 循环）。
- **Registry**：`AgentRegistry`（`ctx.agents`）——创建由 factory（agent-loop）提供。
- **Consumer**：UI/hook/编排器经 `Agent` 句柄投递 `followup/steer/inject`。

## 2. 文件清单表

| 文件 | 职责 |
|---|---|
| `dsh/agent/agent.py` | `Agent` 公共句柄 + `AgentHandle` + `AgentRegistry`（含发起者作用域） |
| `dsh/agent/inbox.py` | `Inbox`：`next-turn`/`next-step` 两有序待办列表（线程安全） |
| `dsh/agent/approval.py` | `ApprovalService`：人机审批通道（`tools/pre-execute` 的 ask / ask_user / 权限） |
| `dsh/agent/loop.py` | `AgentLoopService`：具体驱动循环（turn/step 状态机） |
| `dsh/agent/plugins.py` | `DefaultOptions`/`DefaultOptionsPlugin`：注册 `ctx.agentDefaultModel` |

## 3. 类型与数据结构

**`Agent` 状态字段**（非 dataclass，`__init__` 手工赋值）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | `str` | `session.id` | agent id = 会话 id |
| `session` | `Session` | 入参 | 事件溯源会话 |
| `options` | `Dict[str, Any]` | `dict(options)` | 创建选项（provider/model/max_tokens 等） |
| `inbox` | `Inbox` | 新实例 | 两队列收件箱 |
| `ctx` | `Context` | `scope_ctx` | per-agent 作用域 |
| `ctx_name` | `Optional[str]` | `scope_ctx.name` | 作用域名（`agent:<id>`） |
| `status` | `AgentStatus` | `"idle"` | `'idle' \| 'running'` |
| `_wakeup`/`_idle`/`_disposed` | `asyncio.Event` | 各自初始化（`_idle` 初始 set） | 驱动唤醒/静默/销毁信号 |
| `_turn_signal` | `Optional[AbortSignal]` | `None` | 当前 turn 的取消信号 |
| `_turn_number`/`_step_number` | `int` | `0` | 计数 |
| `_cancel_cause` | `Optional[Dict]` | `None` | 首个取消原因胜出 |
| `_driver_task` | `Optional[asyncio.Task]` | `None` | 驱动 task |
| `_maintenance` | `Optional[asyncio.Task]` | `None` | true idle 阶段的维护任务（`run_maintenance` 启动，`cancel` 中止） |

**`Inbox` 字段**：`next_turn: List[Dict]`、`next_step: List[Dict]`、`_lock: threading.Lock`。每条 pending 消息是 `{"id","content","source"}`（`make_message` 生成，对应 TS 版 UserMessage）。

**`AnswerCallback`**：`Callable[[str, str], Any]` —— `(question, detail) -> bool | awaitable`。

**`_TurnFailed`**：内部异常，携带 `failure: Dict[str, Any]`（`{"message","code"}` 等），由 `_run_turn` 捕获转成 `turn/end` 的 error 原因。

## 4. 函数与类方法详解

### 4.1 `dsh/agent/agent.py`

#### `Agent`
```python
def __init__(self, session: Session, options: Dict[str, Any], scope_ctx: Any, factory: "AgentLoopBase") -> None:
```
- 见字段表；`_idle.set()` 表示初始静默。

```python
@property
def running(self) -> bool:
```
- `status == "running"`。

```python
def _set_status(self, status: AgentStatus) -> None:
```
- 仅在状态变化时赋值并 `emit_agent_event("agent/status", {"agent":..., "status":...})`。

```python
def send(self, message: Dict[str, Any], target: str, wakeup: bool) -> None:
```
- 已销毁则直接返回；`inbox.append(target, message)`；`emit_agent_event("agent/inbox/inserted", ...)`；`wakeup` 为真则 `_wakeup.set()`。

```python
def followup(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
```
- `send(make_message(text, source or {"kind":"user"}), "next-turn", wakeup=True)`。

```python
def steer(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
```
- 同上，target=`"next-step"`，source 缺省 `{"kind":"steer"}`。

```python
def inject(self, text: str, source: Optional[Dict[str, Any]] = None) -> None:
```
- 同上，target=`"next-step"`，`wakeup=False`（不唤醒），source 缺省 `{"kind":"plugin"}`。

```python
def cancel(self, cause: Optional[Dict[str, Any]] = None, keep_inbox: bool = False) -> None:
```
- 已销毁则返回；首个 `_cancel_cause` 胜出（`cause or {"kind":"user"}`）；`keep_inbox=False` 时对 `next_turn + next_step` 逐条 `emit("agent/inbox/discarded")` 后 `inbox.clear()`；`_turn_signal.abort(_cancel_cause)`；`_maintenance` 非空则 `_maintenance.cancel()`（中止维护任务）；`_wakeup.set()`。

```python
def run_maintenance(self, task) -> "asyncio.Task":
```
- 在 true idle 阶段同步启动一个非 turn 维护任务（对应 TS 版 runMaintenance）：`status == "running"` 或已有 `_maintenance` → `raise AgentError(f"agent {self.id} busy")`；已 disposed → `raise AgentError(f"agent {self.id} disposed")`；否则新建 `AbortSignal`，`create_task(self._run_maintenance(task, signal))` 存入 `_maintenance` 并返回该 task。任务同步启动、公共状态保持 idle；后续唤醒输入留在收件箱直到任务结束；`when_idle` 跟随任务完成；`cancel` 中止它。

```python
async def _run_maintenance(self, task, signal) -> Any:
```
- `try: return await task(signal)`；`finally: self._maintenance = None`（任务结束后清空维护槽）。

```python
def dispose(self, cause: Optional[Dict[str, Any]] = None) -> None:
```
- `cancel(cause or {"kind":"disposed"})`、`_disposed.set()`、`_wakeup.set()`。

```python
async def when_idle(self) -> None:
```
- 循环：`await self._idle.wait()` 后检查「`_wakeup` 未置位 且 next_turn/next_step 皆空」；此时若 `_maintenance` 非空则 `await asyncio.gather(self._maintenance, return_exceptions=True)` 等待维护任务完成后再 `continue` 重查，否则返回；不满足则 `await asyncio.sleep(0)` 再等。
  意义：idle 标志初始为 set，仅凭 `_idle.wait()` 会在「驱动尚未开始处理刚排队的消息」时提前返回；
  同时检查收件箱与唤醒位保证「无活跃 turn 且无排队工作」才 resolve（子代理等待场景的关键保证）；
  维护任务分支保证「维护任务也完成」才 resolve（对应 `runMaintenance`）。

```python
async def _wait_disposed(self) -> None:
```
- `await self._disposed.wait()`。

```python
def __repr__(self) -> str:
```
- `<Agent <id> (<status>)>`。

#### `AgentHandle`
```python
def __init__(self, agent: Agent) -> None:
async def dispose(self) -> None:
```
- `dispose`：`agent.dispose()` 后 `await agent._factory.teardown(agent)`（停驱动、注销、移除会话）。

#### `AgentRegistry`（`Service`，`provides="agents"`）
```python
def apply(self, ctx) -> None:
```
- `ctx.set("agents", self)`。

```python
def set_factory(self, factory: Any) -> None:
```
- 已注册则 `raise AgentError("agent factory already registered")`；否则存工厂并 `return ctx.effect(clear)`。

```python
@property
def factory(self) -> Any:
```
- 无工厂则 `raise AgentError("no agent factory registered")`。

```python
async def create(self, options=None, meta=None, session_id=None) -> Agent:
async def resume(self, session_id: str, options=None) -> Agent:
```
- 委托 `factory.create(...)` / `factory.resume(...)`。

```python
def get(self, agent_id: str) -> Optional[Agent]:
def list(self) -> List[Agent]:
```
- 按 id 取 / 全部活跃 agent。

```python
def register(self, agent: Agent) -> None:
```
- 已存在同 id 则 `raise AgentError`；否则登记并 `emit("agent/created")`。

```python
def remove(self, agent: Agent) -> None:
```
- `pop` 失败（不存在）直接返回；成功则 `emit("agent/disposed")`。

```python
def current_initiator(self) -> Optional[Agent]:
def require_initiator(self) -> Agent:
```
- 读/强制读进程内发起者（`_initiator_var`）；无则 `raise AgentError("no initiator boundary active")`。

```python
def with_initiator(self, agent: Agent, operation: Callable[[], Any]) -> Any:
def without_initiator(self, operation: Callable[[], Any]) -> Any:
```
- 用 `contextvars` 设/清发起者边界，`finally` 里 `reset(token)`，返回值原样。

```python
def close(self) -> None:
```
- 对每个 agent `dispose()`，清空 `_agents`。

### 4.2 `dsh/agent/inbox.py`

#### `make_message`（模块级函数）
```python
def make_message(text: str, source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
```
- 返回 `{"id": new_message_id(), "content": text, "source": source or {"kind":"user"}}`。

#### `Inbox`
```python
def __init__(self) -> None:
```
- `threading.Lock()` + 两个空列表。

```python
def _find(self, message_id: str) -> Tuple[Optional[List], int]:
```
- 先 `next_step` 后 `next_turn` 线性查找，返回 `(列表, 索引)`；未命中 `(None, -1)`。

```python
def append(self, target: str, message: Dict[str, Any]) -> None:
def prepend(self, target: str, message: Dict[str, Any]) -> None:
```
- 加锁后追加 / `insert(0, ...)`。

```python
def remove(self, message_id: str) -> Optional[Dict[str, Any]]:
```
- 加锁跨两列表删除并返回；未命中 `None`。

```python
def clear(self, target: Optional[str] = None) -> None:
```
- 省略 target 清空两个列表；否则清指定列表。

```python
def _list_for(self, target: str) -> List[Dict[str, Any]]:
```
- `"next-turn"`/`"next-step"` 映射；否则 `raise ValueError("unknown inbox target: ...")`。

```python
def has_next_step(self) -> bool:
def has_next_turn(self) -> bool:
```
- 对应列表非空。

```python
def claim_turn_batch(self) -> Optional[List[Dict[str, Any]]]:
```
- 加锁：`next_step` 非空则全量清空返回；否则 `next_turn.pop(0)` 单条；都空返回 `None`。

```python
def claim_next_step(self) -> List[Dict[str, Any]]:
```
- 加锁认领全部 `next_step` 并清空。

```python
def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
```
- 加锁返回两列表浅拷贝。

### 4.3 `dsh/agent/approval.py`

#### `ApprovalService`（`Service`，`provides="approval"`）
```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```
- `_channel=None`、`_pending=[]`、`_default=bool(config.get("default_allow", False))`。

```python
def apply(self, ctx) -> None:
```
- `ctx.set("approval", self)`。

```python
def set_channel(self, callback: AnswerCallback) -> None:
def clear_channel(self) -> None:
```
- 注册/清空人工应答通道。

```python
async def request(self, question: str, detail: str = "") -> bool:
```
- 挂起记录入 `_pending`；应答顺序（先到先得）：
  1. **`approval/request` waterfall 事件**（对应 TS 事件矩阵）：监听者收到
     `(payload, next)`，payload={question, detail}；返回 bool 即短路应答，
     `await next()` 委派；`waterfall(..., default=None)` 无应答 → None；
  2. 人工通道：有通道 → 调 callback（协程则 await），`bool(result)` 定
     `allowed/denied`；无通道 → `state="denied"` 返回 `_default`；
  3. callback 抛异常 → 记日志、`denied`、返回 `False`；
  `finally` 移除记录（`ValueError` 忽略）。

```python
def pending_questions(self) -> List[Dict[str, Any]]:
```
- 返回挂起问题快照。

### 4.4 `dsh/agent/loop.py`

#### `_TurnFailed(Exception)`
```python
def __init__(self, failure: Dict[str, Any]) -> None:
```
- `super().__init__(failure.get("message","turn failed"))`；`self.failure = failure`。

#### `AgentLoopService`（`Service`，`provides="agentLoop"`）
```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```
- `self._drivers: Dict[str, asyncio.Task] = {}`。

```python
def apply(self, ctx) -> None:
```
- `ctx.set("agentLoop", self)`；`ctx.has("agents")` 时 `ctx.agents.set_factory(self)`。

```python
def emit_agent_event(self, name: str, payload: Dict[str, Any]) -> None:
```
- fire-and-forget `ctx.events.emit`，异常隔离记录。

```python
async def create(self, options=None, meta=None, session_id=None,
                 scope_parent=None) -> Agent:
```
- `store.prepare`→`enter`→`_spawn(..., scope_parent=scope_parent)`→`announce`→`agents.register`（失败回滚 `store.remove`）→`emit("agent/session-start", source="startup")`→`_start_driver`。`scope_parent`：父 agent 的作用域 ctx（子代理继承父作用域注册，composeFrom/join 语义）。

```python
async def resume(self, session_id: str, options=None) -> Agent:
```
- 无 `sessionPersistence` → `LlmFailure("session persistence is not configured", code="NO_PERSISTENCE")`；`persistence.load`→`Session.from_seed(..., publish=store._publish)`→`enter`→`_spawn`→`announce`→`register`→`emit(source="resume")`→`_start_driver`。

```python
async def _spawn(self, session: Session, options: Dict[str, Any],
                 meta: Dict[str, Any], scope_parent: Any = None) -> Agent:
```
- `scope_ctx = ctx.scoped(f"agent:{session.id}", parent=scope_parent)`（`scope_parent` 存在时作用域以其为父层，继承父作用域注册）+ `Agent(session, options, scope_ctx, self)`。
- `meta.agent_preset` 存在时：无 `agentPresets` 服务抛 `LlmFailure(code="NO_PRESETS")`；否则 `await ctx.agentPresets.mount(scope_ctx, preset_id)`（发布前挂 preset，对应 TS 版 setup 回调的时序保证）。

```python
def _start_driver(self, agent: Agent) -> None:
```
- `create_task(self._drive(agent))`，存入 `agent._driver_task` 与 `_drivers`。

```python
async def teardown(self, agent: Agent) -> None:
```
- `agent.dispose()`；取出 driver task，非自身则 `await wait_for(shield(task), timeout=10)`（超时/取消则 `task.cancel()`）；`agents.remove`、`sessions.remove`、`await agent.ctx.dispose()`。

```python
async def _drive(self, agent: Agent) -> None:
```
- 每 agent 一个驱动 task：`while not disposed`：等 `_wakeup`→清；设 `running`、清 `_idle`；内层 `while not disposed`：`claim_turn_batch()` 为 `None` 则 break，否则 `_run_turn`；`finally` 设 `idle`、`_idle.set()`。`CancelledError` 重抛，其余异常记日志。

```python
async def _run_turn(self, agent: Agent, batch: List[Dict[str, Any]]) -> None:
```
- `_turn_number += 1`；新建 `AbortSignal` 存 `_turn_signal`；`append("turn/start", {"turn": turn})`；`reason={"kind":"completed"}`、`step=1`；循环（见 5.1）；捕获 `_TurnFailed`→error 原因、`Exception`→`{"kind":"error","error":{"message":..., "code":"UNKNOWN"}}`；两个 except 分支均 `emit_agent_event("agent/error", {"agent", "turn", "step", "error"})`；若信号已 abort 覆盖为 `_abort_reason`；`append("turn/end", {"turn","reason"})`。

```python
def _abort_reason(self, agent: Agent) -> Dict[str, Any]:
```
- `{"kind":"aborted","reason": dict(_cancel_cause or {"kind":"user"})}`。

```python
async def _run_step(self, agent, turn: int, step: int, batch: List[Dict[str, Any]], first: bool) -> str:
```
- 详见 5.1；返回 `'continue'|'stop'|'retry'|'abort'`。
- 组装好 `config` 后记录路由容量元数据：`adapter = ctx.llm.get_adapter(config.provider)`，取 `adapter.context_window`（可选）；构造 `new_context = {"provider", "model"}`（`context_window` 非 None 时补 `"context_window"`）；与 `session.request_context()` 比较，缺失或不同才 `append("request/context", new_context)`。

```python
def _default_assemble(self, agent: Agent) -> Dict[str, Any]:
```
- `self.ctx.systemPrompt._build(agent.ctx_name, {"scope":..., "signal":...})`。

```python
def _default_config(self, agent: Agent) -> LlmCallConfig:
```
- `agentDefaultModel.current_selection()`（有则取 defaults）；provider/model 依次回退 agent options → defaults；provider 仍空 → 无 provider 抛 `LlmFailure("no LLM provider registered", "NO_PROVIDER")`，否则按 `DEEPSEEK_API_KEY`+deepseek 已注册 → deepseek，否则 mock（已注册）→ mock，否则首个 provider；model 空 → `"mock" if provider=="mock" else "deepseek-chat"`；组装 `LlmCallConfig`（max_tokens/temperature/reasoning_effort 用 `or` 回退）。

```python
@staticmethod
def _chunk_json(chunk: StreamChunk) -> Dict[str, Any]:
```
- StreamChunk → 日志 JSON（`type` 恒写，`text`/`tool_call`/`usage`/`finish_reason` 非 None 才写）。

```python
@staticmethod
def _parse_args(arguments: Optional[str]) -> Any:
```
- 空 → `{}`；`json.loads` 失败（`JSONDecodeError`）→ `{}`。

```python
def close(self) -> None:
```
- 取消所有 driver task 并清 `_drivers`（**幂等**：`done()` 的 task 跳过 cancel，重复调用无副作用）。

### 4.5 `dsh/agent/plugins.py`

#### `DefaultOptions`
```python
def __init__(self, config: Optional[dict] = None) -> None:
def current_selection(self) -> Dict[str, Any]:
def to_json(self) -> Dict[str, Any]:
```
- `current_selection` 返回 `dict(self._config)` 拷贝。

#### `DefaultOptionsPlugin`（`Service`，`provides=None`）
```python
def apply(self, ctx) -> None:
```
- `ctx.set("agentDefaultModel", DefaultOptions(self.config))`；返回空 `cleanup`。

## 5. 关键流程

### 5.1 驱动循环 turn/step 状态机（伪代码，`_drive`/`_run_turn`/`_run_step` 完整流转）

```
_drive(agent):
  while not disposed:
      await _wakeup.wait(); _wakeup.clear()
      if disposed: break
      _set_status("running"); _idle.clear()
      try:
          while not disposed:
              batch = inbox.claim_turn_batch()   # next-step 全量优先，否则一条 next-turn
              if batch is None: break
              await _run_turn(agent, batch)
      finally:
          if not disposed: _set_status("idle")
          _idle.set()

_run_turn(agent, batch):
  turn = ++_turn_number
  _turn_signal = AbortSignal()
  append("turn/start", {turn})
  reason = {"kind":"completed"}; step = 1; retries = 0
  try:
      while True:
          if _turn_signal.aborted: reason=_abort_reason(); break
          outcome = _run_step(agent, turn, step, batch, first=(step==1 and retries==0))
          if outcome=="retry":                      # 同一步重试，step 不增
              retries += 1
              if retries >= 3:                      # 重试上限 3 次
                  reason = {"kind":"error","error":agent._last_failure or RETRY_LIMIT}
                  break
              batch=[]; continue
          if outcome=="abort": reason=_abort_reason(); break
          step += 1
          batch = inbox.claim_next_step()
          if outcome=="stop":
              await serial("agent/turn-stopping", {agent, turn})
              if inbox.has_next_step() and not _turn_signal.aborted:
                  batch = inbox.claim_next_step(); continue  # 监听者 steer 出新一步
              break
          # outcome=="continue"：模型欠一个对工具结果的回复
  except _TurnFailed as f:
      reason = {"kind":"error","error":f.failure}
      emit("agent/error", {agent, turn, step, error: f.failure})
  except Exception as e:
      reason = {"kind":"error","error":{"message":...,"code":"UNKNOWN"}}
      emit("agent/error", {agent, turn, step, error: reason["error"]})
  if _turn_signal.aborted: reason = _abort_reason()
  append("turn/end", {turn, reason})

_run_step(agent, turn, step, batch, first) -> 'continue'|'stop'|'retry'|'abort':
  for m in batch: emit("agent/inbox/claimed", {agent, m, turn})
  # 1) pre-step waterfall（首步拒绝 = 无 step 关闭 turn）
  decision = await waterfall("agent/pre-step", {agent, messages=batch, turn, step, signal},
                             default=lambda: {"kind":"enter","messages":batch})
  if not dict 或 kind!="enter" 或 (first and 无 messages): return "stop"
  entered = decision["messages"]
  append("step/start", {turn, step})
  for m in entered: append("user/message", {content, source}, surface_op="append")
  if _turn_signal.aborted: append("step/end"); return "abort"
  # 2) prompt 组装 + 请求配置
  assembly = await waterfall("system-prompt/assemble", {scope, signal},
                             default=_default_assemble(agent))
  system_text = assembly["text"]; tool_schemas = assembly["tools"] or []
  config = await waterfall("agent/request", {agent, turn, step, signal},
                           default=_default_config(agent))
  append("request/header", {header: config.to_json(), reason:"initial"})
  # 路由容量元数据：路由/容量变化时才落日志（request/context）
  adapter = ctx.llm.get_adapter(config.provider)
  new_context = {provider, model} + (context_window 若 adapter.context_window 非 None)
  if session.request_context() 缺失或不同: append("request/context", new_context)
  # 3) 模型请求（流式）
  request = LlmRequest(config, session.derive_messages(), tools=tool_schemas,
                       system=system_text, signal=_turn_signal)
  assembler = AssistantAssembler(); chunk_seqs=[]
  try:
      for chunk in ctx.llm.stream(request):
          chunk_seqs.append(append("assistant/chunk", {...}).seq)
          assembler.feed(chunk)
  except LlmFailure as failure:
      append("step/end")
      agent._last_failure = {message, code, provider}   # 供 retry 上限触发的 turn/end 载荷
      action = await waterfall("agent/request-error", {...failure...}, default=None)
      if action.kind=="retry": return "retry"
      raise _TurnFailed({message, code})
  finished = assembler.finish(); blocks = finished["blocks"]
  append("assistant/message", {blocks: [...], provider, model, usage},
         surface_op="append", source_event_seqs=chunk_seqs)
  tool_calls = [b for b in blocks if b.kind=="tool-call"]
  if not tool_calls: append("step/end"); return "stop"
  # 4) 工具执行（顺序）
  stop_after = False
  for block in tool_calls:
      if _turn_signal.aborted: stop_after=True; break
      args = _parse_args(block.arguments)
      append("tool/call", {turn, step, call_id, name, arguments})
      result = await ctx.tools.execute(call_id, name, args, agent, signal, scope=ctx_name)
      data = {turn, step, call_id, name, content, is_error}
      if result.is_error: data["error"]={code, message}
      append("tool/result", data, surface_op="append")
      if not result.is_error:
          for extra in result.additional_contexts: append("user/message", ..., surface_op="append")
          if result.concludes_turn: stop_after=True
  append("step/end")
  return "abort" if (stop_after or _turn_signal.aborted) else "continue"
```

## 6. 事件与扩展点

本模块 **emit**（经 `emit_agent_event`，异常隔离）与 **waterfall/serial** 的事件：

| 事件名 | 方式 | 载荷 | 含义 |
|---|---|---|---|
| `agent/status` | emit | `{agent, status}` | 状态切换（idle/running） |
| `agent/inbox/inserted` | emit | `{agent, message}` | 消息入收件箱 |
| `agent/inbox/claimed` | emit | `{agent, message, turn}` | 驱动认领一条输入 |
| `agent/inbox/discarded` | emit | `{agent, message}` | cancel 丢弃待办 |
| `agent/created` | emit | `{agent}` | agent 登记 |
| `agent/disposed` | emit | `{agent}` | agent 移除 |
| `agent/session-start` | emit | `{agent, source}` | 会话启动（startup/resume） |
| `agent/pre-step` | waterfall | `{agent, messages, turn, step, signal}` | 进入 step 前的决策（reject / enter(messages)） |
| `agent/request` | waterfall | `{agent, turn, step, signal}` | 决定调用配置（`next` 返回 `LlmCallConfig`） |
| `agent/request-error` | waterfall | 同 request + `failure` | 请求失败后的补救（`{"kind":"retry"}` 触发重试） |
| `agent/request-done` | emit | `{agent, turn, step, provider, model, usage, latency_ms}` | 每次成功模型请求的观测广播（perf_counter 计时；mock 无计量时 usage=None） |
| `agent/turn-stopping` | serial | `{agent, turn}` | turn 自然停止后的终态检查点（可 steer 出新一步） |
| `agent/error` | emit | `{agent, turn, step, error}` | turn 以错误结束时广播（`_TurnFailed`、未知异常、retry 上限三分支） |

## 7. 常见改动指引

**如何新增一个 agent 事件监听点**：
1. 用 `ctx.events.on(name, handler)` 注册（同步或协程均可）。要参与决策用 waterfall（`handler(payload, next)`，`await next()` 委派，直接返回即短路）；要收尾通知用 serial；纯广播用 emit。
2. 若监听点由 `AgentLoopService` 内部派发，加一行 `self.emit_agent_event(...)` 或在驱动里 `await self.ctx.events.waterfall(...)`；注意 waterfall 的异常会向上传播（可用来短路 turn），emit 异常被隔离。
3. 状态变更类监听点建议走 `Agent._set_status`/`send` 已有事件，勿另造。

**如何换 agent 的默认 provider/model**：改 bundle 里 `DefaultOptionsPlugin` 的 config，或经 `agent.options`（`create(options={"provider":...,"model":...})`）覆盖；`_default_config` 的回落顺序见 4.4。

**如何扩展 cancel 原因**：`Agent.cancel` 的 `cause` 为 `{"kind": "user"|"parent"|"hook"|"disposed", ...}`，首个 cause 胜出；`_abort_reason` 会把它写进 `turn/end` 的 reason。

## 8. 相关测试

- `tests/test_e2e_smoke.py`：`test_simple_turn_echo`（turn/step 全流转）、`test_tool_round_trip`（工具回合两次 assistant/message）、`test_pre_step_reject_closes_turn_without_step`（pre-step reject）、`test_cancel_turn`（`agent.cancel` → aborted 原因）。
- `tests/test_domains.py`：`test_boot_base_bundle` 校验 base bundle 里 `agents`/`agentLoop`/`approval` 等 key 均存在。
