# 会话日志 开发手册

> 对应 TS 版概念：`SessionEventMap`、session.md 的事件溯源契约、`SessionStore`。
> 源码文件：`dsh/session/events.py`、`session.py`、`store.py`、`title.py`。
> 生成方式：本文档由源码逐函数人工核对生成，所有签名均以 `inspect.signature` 验证为准。

## 1. 模块定位与架构位置

`dsh.session` 实现事件溯源会话：只追加日志是唯一真相，LLM 历史是派生的。

- **职责**：事件词汇表（`SessionEvent` + catalog）、会话本体（append 校验 / surface 投影 / 派生历史 / 重建）、会话存储与发布枢纽 `SessionStore`、以及会话标题 provider `SessionTitleService`。
- **ctx.\<key\> 服务名**：`SessionStore.provides = "sessions"`（`ctx.sessions`）；`SessionTitleService.provides = "sessionTitle"`（`ctx.sessionTitle`），`apply` 中各自 `ctx.set(..., self)`。
- **provides/inject 依赖关系**：`SessionStore` 提供 `sessions`，无 `inject` 依赖；继承 `dsh.kernel.Service`。
- **与其他模块的调用关系**：
  - 依赖 `..errors`（`SessionError`、`SessionFormatError`）、`..ids.new_message_id`。
  - `derive_event_message` 延迟导入 `..llm.messages`（`ContentBlock`/`Message`）做投影。
  - 持久化由订阅 `session/event` 的插件（如 `dsh/persistence`）负责，本模块只广播。
- **`dsh/ids.py` 身份铸造**（会话域用到的两个）：`new_session_id() -> str`
  （`session-xxxx`，10 位 hex）、`new_message_id() -> str`（`msg-xxxx`）、
  `new_call_id() -> str`（`call-xxxx`，工具调用 id，配对 `tool/call` 与
  `tool/result`；注册表/循环用它做 call_id）。

## 2. 文件清单表

| 文件 | 职责 |
| --- | --- |
| `dsh/session/events.py` | 事件词汇表：`SESSION_FORMAT_VERSION`、`SURFACE_EVENT_TYPES`、`EVENT_CATALOG`、`SessionEvent`、`is_json_value`、`register_event_type`。 |
| `dsh/session/session.py` | `SessionHeader`、`SurfaceManager`、`derive_event_message`、`Session`（append/派生/重建）。 |
| `dsh/session/store.py` | `SessionStore`（ctx.sessions）：生命周期（create/prepare/enter/announce）与发布钩子、flush/fork/remove。 |
| `dsh/session/title.py` | `SessionTitleService`（ctx.sessionTitle）：会话标题 provider（`set_provider`/`title_for`）。 |

`dsh/session/__init__.py` 额外导出 `SessionTitleService`（`__all__` 含之）。

## 3. 类型与数据结构

### 3.1 常量与全局表（events.py）

| 名称 | 类型 | 值 | 说明 |
| --- | --- | --- | --- |
| `SESSION_FORMAT_VERSION` | `int` | `1` | 磁盘格式版本，后端拒绝其它版本（无迁移路径）。 |
| `SURFACE_EVENT_TYPES` | `Set[str]` | `{user/message, assistant/message, tool/result, compaction/summary}` | 产生 LLM 消息、可出现在有序 surface 上的事件类型（可扩展）。 |
| `EVENT_CATALOG` | `Dict[str, str]` | 13 个内置类型描述 | 事件类型 → 描述（供 catalog 与手册生成）。 |
| `TurnEndReason` | `Dict[str, Any]` | — | turn 结束原因 `{'kind': 'completed'|'aborted'|'blocked'|'error'|'max-tokens'|'interrupted', ...}`。 |
| `SurfaceOp` | `Union[str, Dict[str, int]]` | — | `'append'` 或 `{'op':'replace','start':..,'end':..}`。 |

`EVENT_CATALOG` 内置类型：`turn/start`、`turn/end`、`step/start`、`step/end`、`user/message`、`assistant/chunk`、`assistant/message`、`tool/call`、`tool/result`、`todo/write`、`request/header`、`session/end-seed`、`compaction/summary`。

### 3.2 `SessionEvent`（events.py，frozen dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | `str` | — | 事件类型名。 |
| `seq` | `int` | — | 序号（= 入日志时的长度，单调连续）。 |
| `time` | `int` | — | 毫秒时间戳。 |
| `data` | `Dict[str, Any]` | — | 载荷（源头已校验为无损失 JSON）。 |
| `surface_op` | `Optional[SurfaceOp]` | `None` | surface 事件必填。 |
| `source_event_seqs` | `Optional[List[int]]` | `None` | 引用的更早事件 seq。 |
| `ignorable` | `bool` | `False` | 读者遇未知类型可安全跳过。 |

### 3.3 `SessionHeader`（session.py，dataclass）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `version` | `int` | `1`（`SESSION_FORMAT_VERSION`） | 格式版本。 |
| `id` | `str` | `""` | 会话 id。 |
| `created_at` | `int` | `0` | 创建毫秒时间戳。 |
| `cwd` | `Optional[str]` | `None` | 工作目录。 |
| `parent_session` | `Optional[str]` | `None` | 父会话 id（fork）。 |
| `seed_length` | `Optional[int]` | `None` | 种子前缀长度。 |
| `origin` | `Optional[str]` | `None` | 来源。 |
| `delegation_depth` | `int` | `0` | 委派深度。 |
| `agent_preset` | `Optional[str]` | `None` | agent 预设名。 |

（存储元数据：不进事件日志、不进派生历史。）

### 3.4 `SurfaceManager`（session.py，类）

字段：`nodes: List[int]`（有序 surface 节点 seq）、`replace_generation: int`（replace 次数，作派生缓存 key）。方法 `validate` / `commit` 见第 4 节。

### 3.5 `Session`（session.py，类）

字段：`id`、`header`、`_events`、`_publish`、`_derive_cache`、`_seeded`、`surface`、`first_live_seq`（见 `__init__`）。

## 4. 函数与类方法详解

### 4.1 `dsh/session/events.py`

#### 模块级 `register_event_type`

```python
def register_event_type(event_type: str, description: str,
                        surface: bool = False) -> None:
```

- 参数：`event_type`（如 `hook/invoked`）、`description`（单句）、`surface`（True 表示产生 LLM 消息，须带 surface_op）。
- 行为：写入 `EVENT_CATALOG`；`surface=True` 时加入 `SURFACE_EVENT_TYPES`。对应 TS 版 declaration merging。

#### 模块级 `is_json_value`

```python
def is_json_value(value: Any) -> bool:
```

- 返回：该值能否被 JSON 原样持久化。
- 行为：实现 `to_json()` 协议的对象（如 `ContentBlock`/`Message`）递归校验其返回值；`None`/`str`/`bool`/`int` 为 True；`float` 需 `math.isfinite`；`list` 逐项递归；`dict` 要求键为 str 且值递归；其余 False。
- 边界：`float("nan")` / `inf` 判 False；非 str 键 dict 判 False。

#### `SessionEvent.__init__`

```python
def __init__(self, type: str, seq: int, time: int, data: Dict[str, Any],
             surface_op: Optional[SurfaceOp] = None,
             source_event_seqs: Optional[List[int]] = None,
             ignorable: bool = False) -> None:
```

frozen dataclass，构造即不可变。

#### `SessionEvent.is_surface`

```python
def is_surface(self) -> bool:
```

`type in SURFACE_EVENT_TYPES`。

#### `SessionEvent.to_json`

```python
def to_json(self) -> Dict[str, Any]:
```

序列化为 JSONL 行：始终含 `type/seq/time/data`；`surface_op`、`source_event_seqs` 非 None 时输出；`ignorable=True` 时输出 `"ignorable": True`。

#### `SessionEvent.from_json`（staticmethod）

```python
@staticmethod
def from_json(raw: Dict[str, Any]) -> "SessionEvent":
```

- 行为：从持久化行还原，`type=str`、`seq=int`、`time=int`、`data=dict(...)`、其余字段原样透传。
- 边界：`KeyError/TypeError/ValueError` 统一转为 `SessionError("malformed session event: ...")`。深校验见 `Session.from_seed`。

### 4.2 `dsh/session/session.py`

#### `SurfaceManager.__init__`

```python
def __init__(self, seed_nodes: Optional[List[int]] = None) -> None:
```

`nodes = list(seed_nodes or [])`、`replace_generation = 0`。

#### `SurfaceManager.validate`

```python
def validate(self, seq: int, surface_op: Optional[Any],
             source_event_seqs: Optional[List[int]],
             is_surface: bool) -> None:
```

- 行为（校验规则）：
  - surface 事件：`surface_op` 为 None → 抛 `SessionError`；`"append"` → 通过；`{"op":"replace"}` 需 `start <= end` 且 `start/end ∈ nodes`（否则抛），覆盖区间 `[start,end]` 非空（否则抛），且 `source_event_seqs` 必须包含全部被遮蔽节点（否则抛）；其它 → 抛非法 surface_op。
  - 非 surface 事件：携带 surface_op → 抛（必须不带）。
- 边界：纯校验，无副作用（修改在 `commit`）。

#### `SurfaceManager.commit`

```python
def commit(self, seq: int, surface_op: Optional[Any]) -> None:
```

- 行为：`None` 直接返回；`"append"` → `nodes.append(seq)`；`replace` → 删除 `[start,end]` 内节点后**把 `seq` 插入到被替换区间所在位置**（start 节点原位置；压缩最旧前缀时摘要仍在剩余消息之前——审计批次修复：早期实现 append 到末尾会把摘要排到最后），`replace_generation += 1`。

#### 模块级 `derive_event_message`

```python
def derive_event_message(event: SessionEvent) -> Optional["Message"]:
```

- 返回：`Message` 或 `None`（纯函数，单节点投影）。
- 投影规则（延迟导入 `ContentBlock`/`Message`）：
  - `user/message` → user 消息（content 为 str 生成 text 块；为 list 直接用为 blocks；source 取 `data["source"]` 或 `{"kind":"user"}`）。
  - `assistant/message` → assistant 消息（blocks 由 dict 重建 `ContentBlock`；`text` 兜底；空 blocks 返回 `None`；source `{"kind":"model", provider, model}`）。
  - `tool/result` → user 消息携带 tool-result 块（`call_id`/`content`/`is_error`；source `{"kind":"tool", name}`）。
  - `compaction/summary` → user 消息携带摘要文本（source `{"kind":"plugin","plugin":"compaction"}`）。
  - 其余（turn/*、step/*、assistant/chunk、todo/write、request/header、request/context、session/end-seed）→ `None`。

#### `Session.__init__`

```python
def __init__(self, session_id: str, header: Optional[SessionHeader] = None,
             publish: Optional[PublishHook] = None,
             seed: Optional[Sequence[SessionEvent]] = None) -> None:
```

- 行为：`header` 缺省用 `SessionHeader(id=session_id, created_at=毫秒)`；`surface = SurfaceManager()`；`first_live_seq = 0`；若给 `seed`，逐条 `_append_event(event, publish=False)` 回放，`_seeded = True`，`first_live_seq = len(_events)`，最后追加一条 `session/end-seed`（seq 为当前长度）。
- 边界：seed 回放**不触发 publish**；end-seed 是 `firstLiveSeq` 的持久化投影。

#### `Session.events`（property）

```python
@property
def events(self) -> List[SessionEvent]:
```

返回事件日志**快照**（`list(self._events)`），调用者可安全遍历。

#### `Session.seq`（property）

```python
@property
def seq(self) -> int:
```

下一条事件的序号 = 当前日志长度（连续性契约）。

#### `Session.has_seed`（property）

```python
@property
def has_seed(self) -> bool:
```

返回 `_seeded`。

#### `Session.append`

```python
def append(self, event_type: str, data: Dict[str, Any],
           surface_op: Optional[Any] = None,
           source_event_seqs: Optional[List[int]] = None,
           ignorable: bool = False) -> SessionEvent:
```

- 参数：`event_type`、`data`（str 键 dict，值无损失 JSON）、`surface_op`、`source_event_seqs`、`ignorable`。
- 返回：已入日志的 `SessionEvent`。
- **校验顺序**：① `data` 必须是 str 键 dict（否则 `SessionError`）→ ② `is_json_value(data)`（否则 `SessionError`）→ ③ `surface.validate(...)` → ④ 构造事件 → ⑤ `_append_event(event, publish=True)`（同步通知 publish hook）。
- 边界：校验失败事件**不入日志**（seq 不前进）。

#### `Session._append_event`

```python
def _append_event(self, event: SessionEvent, publish: bool) -> SessionEvent:
```

追加事件、`surface.commit`、清空 `_derive_cache`、`publish` 且 `_publish` 非 None 时调用 `_publish(self, event)`。私有。

#### `Session.derive_messages`

```python
def derive_messages(self) -> List[Any]:
```

- 返回：模型可见 `Message` 列表（每次新数组，Message 对象共享）。
- 行为：缓存 key `(replace_generation, len(nodes))`；命中返回 `list(cached)`；否则按 `surface.nodes` 序逐个 `derive_event_message`，非 None 入列表并写缓存。

#### `Session.request_header`

```python
def request_header(self) -> Optional[Dict[str, Any]]:
```

折叠最新 `request/header` 快照（下一次请求将与之比较）：遍历 `_events`，取最后一个 `request/header` 事件的 `data["header"]`。

#### `Session.request_context`

```python
def request_context(self) -> Optional[Dict[str, Any]]:
```

折叠最新 `request/context` 快照（路由容量元数据，仅变化时记录）：遍历 `_events`，取最后一个 `request/context` 事件的 `dict(event.data)`；返回 `{'provider','model','context_window'?}` 或 `None`。

#### `Session.todos`

```python
def todos(self) -> List[Dict[str, str]]:
```

折叠最新 `todo/write` 快照：取最后一个 `todo/write` 的 `data["todos"]`（缺省 `[]`）。

#### `Session.from_seed`（staticmethod）

```python
@staticmethod
def from_seed(session_id: str, header: SessionHeader,
              seed: Sequence[Dict[str, Any]],
              publish: Optional[PublishHook] = None,
              known_types: Optional[set] = None) -> "Session":
```

- 参数：`seed`（持久化行 dict 列表）、`known_types`（本 build 认识的事件类型集合；缺省 `set(EVENT_CATALOG)`）。
- 返回：重建的 `Session`。
- 校验序（逐条）：版本不符 → `SessionFormatError`（带 direction newer/older）；`SessionEvent.from_json` 失败 → `SessionError`；`seq != index` → `SessionFormatError`（seq 断裂）；未知必填类型（不在 known 且 `not ignorable`）→ `SessionFormatError`；`data` 非 JSON → `SessionFormatError`。
- 边界：`ignorable=True` 的未知事件被接受。

### 4.3 `dsh/session/store.py`

#### `SessionStore`（类属性）

`provides = "sessions"`。

#### `SessionStore.__init__`

```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```

`_live: Dict[str, Session]`、`_pending_tasks: List[asyncio.Task]`、`_flush_listeners = 0`。

#### `SessionStore.apply`

```python
def apply(self, ctx) -> None:
```

`ctx.set("sessions", self)` + `ctx.on("session/flush", self._on_flush)`。

#### `SessionStore._publish`

```python
def _publish(self, session: Session, event: SessionEvent) -> None:
```

- 行为：append 后的同步通知。`self.ctx.events.emit("session/event", session, event)`（同步监听器立即入缓冲；异步监听器成 task）；把返回的 task 记入 `_pending_tasks` 并挂 `add_done_callback` 从列表移除。整体异常 `log.exception` 隔离。

#### `SessionStore._drain_pending`

```python
async def _drain_pending(self) -> None:
```

`gather` 尚未完成的异步 `session/event` 广播（`return_exceptions=True`）。

#### `SessionStore._on_flush`

```python
async def _on_flush(self, session: Session) -> Optional[bool]:
```

`session/flush` 汇聚监听器，返回 **None**（不声称持久化参与——「是否有持久化
监听器参与」由真实持久化插件返回 True 决定；审计批次修复：早期恒返回 True
使无持久化插件时 `flush()` 仍报 True）。

#### `SessionStore.create`

```python
def create(self, session_id: Optional[str] = None,
           meta: Optional[Dict[str, Any]] = None,
           seed: Optional[Sequence[SessionEvent]] = None) -> Session:
```

- 返回：已进入存储并公告的会话。
- 行为：`prepare` → `enter` → `announce`。
- 边界：id 已存在抛 `ValueError`。

#### `SessionStore.prepare`

```python
def prepare(self, session_id: Optional[str] = None,
            meta: Optional[Dict[str, Any]] = None,
            seed: Optional[Sequence[SessionEvent]] = None) -> Session:
```

- 行为：构造但不进存储。`session_id = session_id or new_session_id()`（`session-xxxx`）；id 已存在抛 `ValueError`；`meta.cwd` 非 str 抛 `ValueError`；组装 `SessionHeader`（`created_at` 缺省当前毫秒、`delegation_depth=int(... or 0)`）后 `Session(..., publish=self._publish, seed=seed)`。

#### `SessionStore.enter`

```python
def enter(self, session: Session) -> None:
```

把 prepared 会话加入 `_live`（发布钩子已随 Session 注入）。id 已存在抛 `ValueError`。

#### `SessionStore.announce`

```python
def announce(self, session: Session) -> None:
```

`emit("session/created", session)`；同步监听器 throw 会 `_live.pop(session.id)` 回滚后重抛。

#### `SessionStore.get`

```python
def get(self, session_id: str) -> Optional[Session]:
```

按 id 取活跃会话（不存在返回 None）。

#### `SessionStore.list`

```python
def list(self) -> List[Session]:
```

全部活跃会话（创建序）。

#### `SessionStore.flush`

```python
async def flush(self, session: Session) -> bool:
```

- 返回：是否有持久化监听器参与（`any(result for result in results)`，None 结果不计）。
- 行为：先 `await _drain_pending()`（保证持久化插件已看到全部事件），再 `parallel("session/flush", session)`。

#### `SessionStore.remove`

```python
def remove(self, session: Session) -> None:
```

`_live.pop(session.id)` 成功才 `emit("session/disposed", session)`；不存在则直接返回。

#### `SessionStore.fork`

```python
def fork(self, source: Session, boundary: Optional[int] = None,
         child_session_id: Optional[str] = None) -> Session:
```

- 参数：`boundary`（含端点源 seq；省略 = 当前最后一条）。
- 行为：`boundary = len(source._events) - 1 if None else boundary`；`< 0` 归为 `-1`；`prefix = source._events[:boundary+1]`；**turn 深度平衡校验**——扫描前缀内 `turn/start`/`turn/end` 计数，深度 > 0 抛 `ValueError`（不得结束于开放 turn 内；审计批次修复：早期只查最后一条是否为 `turn/start`，检不出「结束于 turn 中段」）；`create(child_session_id, meta={parent_session, seed_length, cwd, delegation_depth+1}, seed=prefix)`。
- 边界：子会话经 `Session(seed=...)` 再补 `session/end-seed`，且 seed 回放不广播。

#### `SessionStore.close`

```python
def close(self) -> None:
```

对所有活跃会话 `remove`（触发 `session/disposed`），并 `cancel` 全部 `_pending_tasks`。

### 4.4 `dsh/session/title.py`

#### `SessionTitleService`（`Service`，`provides="sessionTitle"`）

```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```

`_provider = None`、`_max_len = int((config or {}).get("max_len", 60))`。

```python
def apply(self, ctx) -> None:
```

`ctx.set("sessionTitle", self)`。

```python
def set_provider(self, provider) -> None:
```

注册唯一标题 provider，签名 `(session, messages) -> str|None`；provider 返回 `None` 表示放弃（回退默认策略）。

```python
def title_for(self, session: Any, messages: Optional[List[Any]] = None) -> str:
```

计算标题：`messages` 缺省取 `session.derive_messages()`；`_provider` 非空且返回非空标题则用之（provider 异常吞掉回退）；否则取第一条 user 消息 `plain_text().strip()`，非空则截断到 `_max_len`（默认 60 字符）；无 user 消息则回退 `session.id[:16]`。

## 5. 关键流程

### 5.1 append 校验顺序

1. `data` 必须是 str 键 dict，否则 `SessionError`；
2. `is_json_value(data)` 无损校验，否则 `SessionError`；
3. `is_surface = event_type in SURFACE_EVENT_TYPES`，`surface.validate(seq, surface_op, source_event_seqs, is_surface)`；
4. 构造 `SessionEvent`（`seq = 当前长度`）；
5. `_append_event`：入日志 → `surface.commit` → 清派生缓存 → `publish=True` 时同步 `_publish`（`emit("session/event")`）。

### 5.2 derive_messages 投影与缓存

- 缓存 key = `(replace_generation, len(nodes))`；命中 O(1) 返回拷贝。
- 未命中按 `surface.nodes` 序逐节点投影，`None` 跳过。
- `replace` 使 `replace_generation` 递增 → 缓存失效重建。

### 5.3 重建（from_seed）

版本校验 → 逐行 `from_json` → seq 连续性 → 未知必填类型拒绝（ignorable 豁免）→ data JSON 校验 → 用 seed 构造 Session（自动补 end-seed）。

## 6. 事件与扩展点

| 事件名 | 派发方式 | 含义 |
| --- | --- | --- |
| `session/event` | `emit`（`SessionStore._publish`） | 每次 append 后同步广播（session, event），持久化插件据此写日志。 |
| `session/created` | `emit`（`announce`） | 新会话进入存储时公告；同步监听器 throw 会回滚该会话。 |
| `session/disposed` | `emit`（`remove`） | 会话离开存储时公告。 |
| `session/flush` | `parallel`（`flush`） | awaited 持久化 checkpoint；store 自身的 `_on_flush` 返回 None（不声称参与），真实持久化插件返回 True，`flush()` 用 `any(results)` 判定。 |

扩展方式：`register_event_type(name, description, surface=...)` 注册新事件词汇；surface 事件需在 append 时携带合法 `surface_op`。

## 7. 常见改动指引

### 如何新增一个事件类型

1. 调用 `register_event_type("my/event", "描述", surface=False)`（插件初始化时）。
2. 若产生 LLM 消息，传 `surface=True`，并保证 append 时带 `surface_op`（`"append"` 或 replace dict）。
3. 若该类型需要在 `derive_event_message` 里投影为消息，在函数中加一个 `if etype == "my/event":` 分支返回 `Message`。

### 如何扩展 surface 事件（replace 语义）

- 追加普通消息用 `surface_op="append"`。
- 遮蔽一段旧节点用 `surface_op={"op":"replace","start":s,"end":e}`，并**必须**在 `source_event_seqs` 中声明 `[s..e]` 全部节点，否则 `validate` 抛错。

### 如何新增一个持久化后端

订阅 `ctx.on("session/event", handler)`（同步 handler 会在 append 时同步入缓冲），并在 `session/flush`（parallel）中执行真实写盘；`SessionStore.flush` 会先排空广播再派发 flush。

## 8. 相关测试

`tests/test_session.py`：覆盖 append/seq 连续性、非 JSON 拒绝、surface 必填 surface_op、derive_messages 投影（含空助手消息跳过）、replace 遮蔽与全量遮蔽校验、`is_json_value` 边界、`from_seed` 校验（版本/未知必填/ignorable/seq 断裂）、`SessionStore` 的 create 广播与 fork。
`tests/test_seams.py`：`test_session_title_default`（sessionTitle 默认标题与自定义 provider）、`test_request_context_logged`（request/context 变化时落日志，经 `request_context()` 折叠）。
