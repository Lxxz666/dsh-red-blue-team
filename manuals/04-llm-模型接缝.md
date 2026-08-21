# LLM 模型接缝 开发手册

> 对应 TS 版概念：`llm-streaming.md` 的 `StreamChunk` 判别联合、`ContentBlockMap`/`MessageSourceMap` 对话词汇、LLM 能力缝（Definition + Provider + Consumer）。
> 源码文件清单：`dsh/llm/messages.py`、`dsh/llm/stream.py`、`dsh/llm/adapters.py`、`dsh/llm/deepseek.py`、`dsh/llm/mock.py`、`dsh/llm/plugins.py`。
> 生成方式：人工完整读取上述源码，并用 `python -c "import inspect; ..."` 逐个验证签名后撰写；本手册仅记录代码真实行为。

## 1. 模块定位与架构位置

**职责**：把「LLM 调用」抽象成一条能力缝——统一的消息词汇（`ContentBlock`/`Message`）、原始流块协议（`StreamChunk`）与组装器、适配器抽象与注册表、以及两个开箱即用的 provider（DeepSeek 真实现、Mock 确定性实现）。

**ctx 服务名与 provides/inject**：

| 类 | provides | inject | ctx.<key> |
|---|---|---|---|
| `LlmRuntime` | `"llm"` | — | `ctx.llm` |
| `MockAdapterPlugin` | `None` | `("llm",)` | 仅消费 `ctx.llm` |
| `DeepSeekAdapterPlugin` | `None` | `("llm",)` | 仅消费 `ctx.llm` |

**与其他模块的调用关系**：
- `dsh.agent.loop.AgentLoopService` 是主要 Consumer：经 `ctx.llm.stream(request)` 拉取流、用 `AssistantAssembler` 折叠为 assistant 消息、把原始块照录 `assistant/chunk` 日志。
- `dsh.llm.messages.messages_to_openai` 被 `DeepSeekAdapter._payload` 调用，把派生历史投影成 OpenAI 兼容 `messages`。
- `dsh.session.session.derive_event_message` 延迟导入 `ContentBlock`/`Message`，用于把日志投影成消息。
- `dsh.errors` 提供 `LlmFailure`/`LlmTimeoutError`（适配器边界的归一化失败）。
- `dsh.tools.pipeline.AbortSignal` 用于跨层取消。

**能力缝三角色分析**（`dsh.llm.adapters` 是标准实现）：
- **Definition（服务定义）**：`LlmAdapter`（抽象 Provider 契约）+ `LlmRequest`/`LlmCallConfig`（请求词汇）。
- **Provider（实现）**：`DeepSeekAdapter`、`MockAdapter`（实现 `stream`，注册进注册表）。
- **Registry（注册表）**：`LlmRuntime`（`ctx.llm`），`register_adapter` 同名覆盖 = 换 provider 换产品行为。
- **Consumer**：agent-loop 经 `llm/stream` waterfall 派发；`llm/stream` 是 waterfall，默认 `next` 走适配器流，监听者（重放/指标/checkpoint）可在两侧包装。

## 2. 文件清单表

| 文件 | 职责 |
|---|---|
| `dsh/llm/messages.py` | 对话词汇：`ContentBlock`/`Message`、block/source 目录、OpenAI 投影 |
| `dsh/llm/stream.py` | `StreamChunk` 原始块协议 + `AssistantAssembler` 组装器 |
| `dsh/llm/adapters.py` | `LlmCallConfig`/`LlmRequest`、`LlmAdapter` 抽象、`LlmRuntime` 注册表 |
| `dsh/llm/deepseek.py` | DeepSeek OpenAI 兼容适配器（httpx + SSE 解析） |
| `dsh/llm/mock.py` | Mock 适配器（无密钥确定性回复，回显/脚本两模式） |
| `dsh/llm/plugins.py` | 两个插件：把 mock/deepseek 适配器注册进 `ctx.llm` |

## 3. 类型与数据结构

**模块级目录**（`messages.py`）：

| 常量 | 类型 | 内容 |
|---|---|---|
| `BLOCK_CATALOG` | `Dict[str, str]` | block 类型目录：`text`/`reasoning`/`image`/`tool-call`/`tool-result` → 中文描述 |
| `SOURCE_CATALOG` | `Dict[str, str]` | 消息来源目录：`user`/`plugin`/`model`/`tool`/`goal`/`compaction` → 描述 |

**`ContentBlock`（`@dataclass(frozen=True)`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `kind` | `str` | （必填） | block 语义类型，决定携带哪些语义字段 |
| `text` | `Optional[str]` | `None` | text/reasoning 块的文本 |
| `call_id` | `Optional[str]` | `None` | tool-call 块的调用 id |
| `name` | `Optional[str]` | `None` | tool-call 块的函数名 |
| `arguments` | `Optional[str]` | `None` | tool-call 块的原始 JSON 参数字符串 |
| `tool_call_id` | `Optional[str]` | `None` | tool-result 块回填的调用 id |
| `content` | `Any` | `None` | tool-result 块的内容（任意可 JSON 值） |
| `is_error` | `bool` | `False` | tool-result 是否错误 |
| `extra` | `Dict[str, Any]` | `field(default_factory=dict)` | 前向兼容的未知字段（`to_json` 不序列化它） |

**`Message`（`@dataclass(frozen=True)`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | `str` | （必填） | 消息 id（`new_message_id()`，`msg-xxxx`） |
| `role` | `str` | （必填） | `user`/`assistant`/`system` |
| `content` | `List[ContentBlock]` | （必填） | 类型化内容块列表 |
| `source` | `Dict[str, Any]` | （必填） | 必含 `kind`（见 `SOURCE_CATALOG`），可选 `form`/`plugin`/`provider`/`model` |

**`StreamChunk`（`@dataclass(frozen=True)`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `type` | `str` | （必填） | `text-delta`/`reasoning-delta`/`tool-call`/`usage`/`finish` |
| `text` | `Optional[str]` | `None` | delta 文本 |
| `tool_call` | `Optional[Dict[str, Any]]` | `None` | `{index, call_id, name, arguments}` |
| `usage` | `Optional[Dict[str, Any]]` | `None` | token 用量 |
| `finish_reason` | `Optional[str]` | `None` | 结束原因 |

**`LlmCallConfig`（`@dataclass(frozen=True)`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `provider` | `str` | （必填） | provider 路由名 |
| `model` | `str` | （必填） | 模型名 |
| `max_tokens` | `Optional[int]` | `None` | 最大 token |
| `temperature` | `Optional[float]` | `None` | 采样温度 |
| `reasoning_effort` | `Optional[str]` | `None` | 推理强度 |
| `extra` | `Dict[str, Any]` | `field(default_factory=dict)` | 额外透传参数 |

**`LlmRequest`（`@dataclass(frozen=True)`）字段表**：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `config` | `LlmCallConfig` | （必填） | 调用配置 |
| `messages` | `List[Message]` | （必填） | 派生历史 |
| `tools` | `List[Dict[str, Any]]` | `field(default_factory=list)` | 工具 schema |
| `system` | `Optional[str]` | `None` | system prompt 文本 |
| `signal` | `Optional[AbortSignal]` | `None` | 取消信号 |

## 4. 函数与类方法详解

### 4.1 `dsh/llm/messages.py`

#### `ContentBlock`（frozen dataclass）

```python
def to_json(self) -> Dict[str, Any]:
```
- 参数：无。返回：无损失 JSON dict。
- 行为：`kind` 恒写入；`text`/`call_id`/`name`/`arguments`/`tool_call_id` 仅在非 `None` 时写入；`content` 非 `None` 时写入；`is_error` 为真才写 `True`。`extra` 不序列化。

```python
def to_openai(self) -> Dict[str, Any]:
```
- 参数：无。返回：OpenAI 兼容 content 片段。
- 行为：`text`→`{"type":"text","text":...}`；`reasoning`→同样投影为 text（思维链并入文本）；`tool-call`→`{"type":"tool_call","id":call_id,"function":{name,arguments}}`（缺省用 `""`/`"{}"`）；其它类型回退为 `{"type":"text","text":str(content or "")}`。

```python
@staticmethod
def text_block(text: str) -> "ContentBlock":
```
- 便捷构造文本块 `ContentBlock(kind="text", text=text)`。

```python
@staticmethod
def tool_call_block(call_id: str, name: str, arguments: str) -> "ContentBlock":
```
- 便捷构造工具调用块。

```python
@staticmethod
def tool_result_block(tool_call_id: str, content: str, is_error: bool = False) -> "ContentBlock":
```
- 便捷构造工具结果块。

#### `Message`（frozen dataclass）

```python
@staticmethod
def user(text: str, source: Optional[Dict[str, Any]] = None) -> "Message":
```
- 构造用户消息：`id=new_message_id()`、`role="user"`、单文本块、`source` 缺省 `{"kind":"user"}`。

```python
@staticmethod
def assistant(blocks: List[ContentBlock], provider: Optional[str] = None, model: Optional[str] = None) -> "Message":
```
- 构造助手消息，`source={"kind":"model","provider":...,"model":...}`。

```python
def plain_text(self) -> str:
```
- 拼接 `kind in ("text","reasoning")` 且 `text` 非空的块，用 `"\n"` 连接（UI/标题用）。

#### `messages_to_openai`（模块级函数）

```python
def messages_to_openai(messages: List[Message]) -> List[Dict[str, Any]]:
```
- 把派生历史投影为 OpenAI `messages`。`tool-result` 块被折叠成 `role="tool"` 消息（`tool_call_id` + `content`）；其余块按消息角色分组（非 tool-result 块为空则该条不产出普通消息）。tool-result 消息追加在其宿主消息之后。

### 4.2 `dsh/llm/stream.py`

#### `StreamChunk`（frozen dataclass）
- 静态构造器：`text_delta(text)`、`reasoning_delta(text)`、`tool_call_chunk(index, call_id, name, arguments)`（组包 `tool_call` dict）、`usage_chunk(usage)`、`finish(reason)`。

#### `AssistantAssembler`

```python
def __init__(self) -> None:
```
- 初始化 `text_parts`/`reasoning_parts`（`List[str]`）、`tool_calls`（`Dict[int, Dict]`）、`usage`/`finish_reason`（`Optional`）。

```python
def feed(self, chunk: StreamChunk) -> None:
```
- 按 `chunk.type` 分派累积：`text-delta`→`text_parts`；`reasoning-delta`→`reasoning_parts`；`tool-call`→按 `index` 建槽（缺省 `call_id/name/arguments`），`name` 覆盖、`arguments` 累加拼接；`usage`→浅拷贝；`finish`→记录 `finish_reason`。

```python
def blocks(self) -> List[Any]:
```
- 组装：合并 text→`text_block`、reasoning→`ContentBlock(kind="reasoning")`，`tool_calls` 按 index 升序产出 `tool_call_block`（`call_id` 缺省 `call-{index}`，`arguments` 缺省 `"{}"`）。返回 `List[ContentBlock]`。

```python
def finish(self) -> Dict[str, Any]:
```
- 返回 `{"blocks": [...], "finish_reason": ..., "usage": ...}`（`usage` 可能为 `None`）。

### 4.3 `dsh/llm/adapters.py`

#### `LlmCallConfig`
```python
def to_json(self) -> Dict[str, Any]:
```
- 序列化：恒写 `provider`/`model`；`max_tokens`/`temperature`/`reasoning_effort` 仅在非 `None` 写入；`extra` 非空才写。

#### `LlmRequest`
```python
def tool_schemas(self) -> List[Dict[str, Any]]:
```
- 返回 `self.tools` 的浅拷贝列表（OpenAI tools 数组）。

#### `LlmAdapter`（`ABC`）
```python
async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
```
- 抽象方法（`@abstractmethod`）：以块流产出回复，必须以 `finish` 块收尾；失败抛 `LlmFailure`。类属性 `name="adapter"`、`supports_reasoning=False`。

```python
async def _watch_signal(self, request: LlmRequest) -> None:
```
- 空实现（适配器可选地观察取消信号）。

#### `LlmRuntime`（`Service`，`provides="llm"`）
```python
def __init__(self, ctx, config: Optional[dict] = None) -> None:
```
- `self._adapters: Dict[str, LlmAdapter] = {}`。

```python
def apply(self, ctx) -> None:
```
- `ctx.set("llm", self)`。

```python
def register_adapter(self, adapter: LlmAdapter):
```
- 以 `adapter.name` 覆盖注册；`emit("llm/adapters-updated")`；返回 `ctx.effect(unregister)`（注销时若仍指向该 adapter 则弹出并再次 emit）。

```python
def get_adapter(self, provider: str) -> Optional[LlmAdapter]:
```
- 按路由名取适配器（无则 `None`）。

```python
def providers(self) -> List[str]:
```
- 返回已注册 provider 名列表。

```python
async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
```
- 先取适配器，无则 `raise LlmFailure(..., code="NO_ADAPTER", provider=...)`；构造 `default_stream()`（包装 `adapter.stream`）；经 `ctx.events.waterfall("llm/stream", request, default=default_stream)` 得到 `agen` 并逐块 `yield`。监听者签名 `(request, next)`，`await next()` 返回下游异步生成器。

```python
def close(self) -> None:
```
- `self._adapters.clear()`。

### 4.4 `dsh/llm/deepseek.py`

#### `DeepSeekAdapter`（`LlmAdapter`，`name="deepseek"`，`supports_reasoning=True`）
```python
def __init__(self, api_key=None, base_url=None, model=None, timeout=300.0, client=None) -> None:
```
- `api_key` 缺省读 `DEEPSEEK_API_KEY`；`base_url` 缺省读 `DEEPSEEK_BASE_URL` 或 `DEFAULT_BASE_URL`（`https://api.deepseek.com`，末尾去 `/`）；`default_model`；`timeout`；`_client`；`_owns_client = client is None`。

```python
@property
def client(self) -> httpx.AsyncClient:
```
- 惰性创建 `httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))`。

```python
def _endpoint(self) -> str:
```
- 返回 `{base_url}/chat/completions`。

```python
def _headers(self) -> Dict[str, str]:
```
- `Authorization: Bearer <key>` + `Content-Type: application/json`。

```python
def _payload(self, request: LlmRequest) -> Dict[str, Any]:
```
- 组装 body：`model = config.model or default_model or "deepseek-chat"`、`stream=True`、`messages=[]`；`system` 非空先插入 system 消息；`messages_to_openai(...)` 扩展；`tools` 非空写 `tools`；`max_tokens`/`temperature` 按需写入（`max_tokens` 用真值判断，`temperature` 用 `is not None`）。

```python
def _parse_line(self, line: str) -> Optional[StreamChunk]:
```
- 解析一行 SSE：`strip` 后非 `data:` 前缀返回 `None`；`[DONE]`→`StreamChunk.finish("stop")`；JSON 解析失败返回 `None`；取 `choices[0]`；`finish_reason` 优先→`finish`；`delta.content`→`text_delta`；`delta.reasoning_content`→`reasoning_delta`；`delta.tool_calls[0]`→`tool_call_chunk`；`obj.usage`→`usage_chunk`；否则 `None`。

```python
async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
```
- 无 key → `LlmFailure(code="NO_API_KEY")`；`client.stream("POST", ...)`；非 200 → 读 body、`LlmFailure(code=f"HTTP_{status}")`；`aiter_lines()` 逐行：空行跳过、`request.signal.aborted` 时 `break`；解析出块则（finish 置 `saw_finish`）`yield`。异常映射：`httpx.TimeoutException`→`LlmTimeoutError`、`httpx.HTTPError`→`LlmFailure(code="HTTP_ERROR")`。末尾 `saw_finish` 为假则补 `yield StreamChunk.finish("stop")`。

```python
async def close(self) -> None:
```
- 若 `_owns_client` 且 `_client` 非空：`await _client.aclose()` 并置空。

### 4.5 `dsh/llm/mock.py`

#### `MockAdapter`（`LlmAdapter`，`name="mock"`）
```python
def __init__(self, script: Optional[List[Dict[str, Any]]] = None, text: Optional[str] = None) -> None:
```
- `self.script = list(script or [])`（按调用次序消费）；`fallback_text`；`calls: List[LlmRequest]` 记录每次请求。

```python
def _last_user_text(self, request: LlmRequest) -> str:
```
- 倒序遍历 `request.messages`，返回第一条 `role=="user"` 的 `plain_text()`；无则 `""`。

```python
async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
```
- 先 `calls.append(request)`；`script` 非空则 `pop(0)`：`chunks` 键→逐块 yield + `finish("stop")` 返回；`tool` 键→`tool_call_chunk(index=0, call_id=工具 dict 的 call_id 或 "mock-call-0", name, arguments=json.dumps(...))` + `finish("tool_calls")` 返回；否则取 `turn["text"]`。`script` 空且 `fallback_text` 非空→用 fallback；否则回显 `[mock] 回显: {last_user_text}`。最后逐字符 `text_delta` + `finish("stop")`。

### 4.6 `dsh/llm/plugins.py`

#### `MockAdapterPlugin`（`Service`，`inject=("llm",)`）
```python
def apply(self, ctx) -> None:
```
- 用 `config["script"]`/`config["text"]` 构造 `MockAdapter` 并 `ctx.llm.register_adapter`；返回 `cleanup`（调用 disposer 注销）。

#### `DeepSeekAdapterPlugin`（`Service`，`inject=("llm",)`）
```python
def apply(self, ctx) -> None:
```
- 用 `config` 的 `api_key`/`base_url`/`model`/`timeout`（缺省 300）构造 `DeepSeekAdapter` 并注册；返回 `cleanup`。

```python
def close(self) -> None:
```
- 若有适配器，尝试 `asyncio.get_running_loop().create_task(adapter.close())`（无运行循环则忽略）。

## 5. 关键流程

### 5.1 DeepSeek SSE 解析流程（伪代码）
```
stream(request):
  if not api_key: raise LlmFailure(NO_API_KEY)
  POST {base_url}/chat/completions  (json=_payload(request), headers=_headers(), stream=True)
  if status != 200: raise LlmFailure(HTTP_<status>)
  saw_finish = False
  for raw_line in response.aiter_lines():
      if request.signal.aborted: break
      if raw_line 空: continue
      chunk = _parse_line(raw_line)
      if chunk: (chunk.type=="finish" -> saw_finish=True); yield chunk
  if not saw_finish: yield StreamChunk.finish("stop")

_parse_line(line):
  line 非 "data:" 前缀 -> None
  payload == "[DONE]" -> finish("stop")
  obj = json.loads(payload)  失败 -> None
  choice = (obj.choices or [])[0]  空 -> None
  if choice.finish_reason -> finish(...)
  if delta.content -> text_delta
  if delta.reasoning_content -> reasoning_delta
  if delta.tool_calls -> tool_call_chunk(第一个 tool_call)
  if obj.usage -> usage_chunk
  -> None
```

### 5.2 `llm/stream` waterfall 派发（伪代码）
```
LlmRuntime.stream(request):
  adapter = get_adapter(config.provider)  无 -> raise NO_ADAPTER
  default = 异步生成器(逐块 yield adapter.stream(request))
  agen = await events.waterfall("llm/stream", request, default=default)
  for chunk in agen: yield chunk
```
监听者（洋葱中间件）签名 `(request, next)`：`await next()` 得到下游生成器，可包装/替换后返回自己的异步生成器。

### 5.3 Mock 脚本回合（伪代码）
```
MockAdapter.stream(request):
  calls.append(request)
  if script:
      turn = script.pop(0)
      if "chunks" -> yield *chunks; yield finish("stop"); return
      if "tool" -> yield tool_call_chunk(...); yield finish("tool_calls"); return
      text = turn["text"]
  elif fallback_text is not None: text = fallback_text
  else: text = "[mock] 回显: " + last_user_text
  for token in text: yield text_delta(token)
  yield finish("stop")
```

## 6. 事件与扩展点

| 事件名 | 派发方式 | 载荷 | 含义 |
|---|---|---|---|
| `llm/stream` | waterfall | `(request, next)` | 包装/替换一次模型调用流（默认=适配器流） |
| `llm/adapters-updated` | emit | 无 | 适配器注册/注销后通知（provider 集合变化） |

## 7. 常见改动指引

**如何新增一个 LLM provider**：
1. 新建 `dsh/llm/<name>.py`，定义 `class XxxAdapter(LlmAdapter)`：设 `name`（路由名）、`supports_reasoning`，实现 `async def stream(self, request) -> AsyncIterator[StreamChunk]`（块流必须以 `finish` 收尾，失败抛 `LlmFailure`）。
2. 复用 `messages_to_openai` 把 `request.messages` 投影成协议消息；用 `request.config.*` 填参数；`request.signal` 做取消检查。
3. 新建 `XxxAdapterPlugin(Service)`：`inject=("llm",)`，在 `apply` 里 `ctx.llm.register_adapter(XxxAdapter(...))`，返回注销 disposer。
4. 在 bundle 配置里加一行指向该插件；`agent-loop` 的 `_default_config` 会按 `provider` 名路由到它。
5. 覆盖行为测试可参照 `tests/test_e2e_smoke.py` 的 `MockAdapter` 装配方式。

**如何换 provider 的产品行为**：直接 `ctx.llm.register_adapter(...)` 用同名覆盖既有适配器（`register_adapter` 是同名覆盖语义）。

## 8. 相关测试

- `tests/test_e2e_smoke.py`：通过 `LlmRuntime` + `MockAdapter` 装配整条脊柱，覆盖 `llm/stream` 派发、`AssistantAssembler` 折叠、工具回合（`test_tool_round_trip`）、取消（`test_cancel_turn`）。
- `tests/test_session.py`：间接覆盖 `ContentBlock`/`Message` 的投影（`derive_event_message` 使用 `tool-result`/文本块）。
- 当前 `tests/` 无 DeepSeek SSE 解析的独立单测（依赖真实网络/httpx mock 未纳入）。
