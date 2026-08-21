"""
dsh.session.session —— Session：事件溯源会话 + Surface 投影 + derive_messages。

核心契约（对应 TS 版 session.md）:

- 只追加日志是唯一真相，LLM 历史是派生的；
- 每个 append 在源头做无损失 JSON 校验（坏事件进不了日志）；
- ``derive_messages()`` 走 surface 节点序，``replace`` 删除被遮蔽节点；
- 观察者通过 store 注入的 publish hook 同步接到通知（异步派发由 store 负责）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .events import (EVENT_CATALOG, SESSION_FORMAT_VERSION, SURFACE_EVENT_TYPES,
                     SessionEvent, is_json_value)
from ..errors import SessionError, SessionFormatError
from ..ids import new_message_id

PublishHook = Callable[["Session", SessionEvent], None]


@dataclass
class SessionHeader:
    """日志旁的存储元数据（不进事件日志、不进派生历史）。"""

    version: int = SESSION_FORMAT_VERSION
    id: str = ""
    created_at: int = 0
    cwd: Optional[str] = None
    parent_session: Optional[str] = None
    seed_length: Optional[int] = None
    origin: Optional[str] = None
    delegation_depth: int = 0
    agent_preset: Optional[str] = None


class SurfaceManager:
    """surface 增量管理器：校验 append 候选并维护节点序。"""

    def __init__(self, seed_nodes: Optional[List[int]] = None) -> None:
        self.nodes: List[int] = list(seed_nodes or [])
        self.replace_generation: int = 0

    def validate(self, seq: int, surface_op: Optional[Any],
                 source_event_seqs: Optional[List[int]],
                 is_surface: bool) -> None:
        """
        校验一次 append 的 surface 元数据。

        :raises SessionError: 契约违反（surface 事件缺 surface_op、replace 区间非法、
            遮蔽节点未完整声明等）。
        """
        if is_surface:
            if surface_op is None:
                raise SessionError(f"surface event at seq {seq} missing surface_op")
            if surface_op == "append":
                return
            if isinstance(surface_op, dict) and surface_op.get("op") == "replace":
                start, end = surface_op["start"], surface_op["end"]
                if start > end or start not in self.nodes or end not in self.nodes:
                    raise SessionError(
                        f"invalid replace range {start}..{end} at seq {seq}")
                covered = [n for n in self.nodes if start <= n <= end]
                if not covered:
                    raise SessionError(f"replace range {start}..{end} covers nothing")
                if source_event_seqs is None or not set(covered).issubset(
                        set(source_event_seqs)):
                    raise SessionError(
                        f"replace at seq {seq} must declare all shadowed nodes")
            else:
                raise SessionError(f"invalid surface_op at seq {seq}: {surface_op!r}")
        else:
            if surface_op is not None:
                raise SessionError(
                    f"non-surface event at seq {seq} must not carry surface_op")

    def commit(self, seq: int, surface_op: Optional[Any]) -> None:
        """提交一条已通过校验的事件。"""
        if surface_op is None:
            return
        if surface_op == "append":
            self.nodes.append(seq)
        elif isinstance(surface_op, dict) and surface_op.get("op") == "replace":
            start, end = surface_op["start"], surface_op["end"]
            # 新节点插入「被替换区间所在位置」（start 之前元素之后），
            # 而不是追加到末尾——压缩最旧前缀时摘要必须仍在剩余消息之前。
            index = next((i for i, n in enumerate(self.nodes)
                          if n == start), len(self.nodes))
            kept = [n for n in self.nodes if not (start <= n <= end)]
            kept.insert(index, seq)
            self.nodes = kept
            self.replace_generation += 1


def derive_event_message(event: SessionEvent) -> Optional["Message"]:
    """
    单节点投影（纯函数）：事件 → Message 或 None。

    投影规则（与 TS 版一致）：

    - ``user/message`` → user 消息（content 原样）；
    - ``assistant/message`` → assistant 消息（空 content 跳过）；
    - ``tool/result`` → user 消息携带 tool-result 块；
    - ``compaction/summary`` → user 消息携带摘要文本；
    - 其余（turn/*、step/*、assistant/chunk、todo/write…）→ None。
    """
    from ..llm.messages import ContentBlock, Message  # 延迟导入避免环

    etype = event.type
    if etype == "user/message":
        data = event.data
        content = data.get("content", "")
        blocks: List[ContentBlock] = []
        if isinstance(content, str):
            blocks.append(ContentBlock(kind="text", text=content))
        elif isinstance(content, list):
            blocks = list(content)
        return Message(id=new_message_id(), role="user", content=blocks,
                       source=data.get("source") or {"kind": "user"})
    if etype == "assistant/message":
        data = event.data
        raw_blocks = list(data.get("blocks") or [])
        blocks: List[ContentBlock] = []
        for raw in raw_blocks:
            if isinstance(raw, ContentBlock):
                blocks.append(raw)
            elif isinstance(raw, dict):
                blocks.append(ContentBlock(
                    kind=raw.get("kind", "text"), text=raw.get("text"),
                    call_id=raw.get("call_id"), name=raw.get("name"),
                    arguments=raw.get("arguments"),
                    tool_call_id=raw.get("tool_call_id"),
                    content=raw.get("content"),
                    is_error=bool(raw.get("is_error"))))
        text = data.get("text")
        if not blocks and text is not None:
            blocks = [ContentBlock(kind="text", text=text)]
        if not blocks:
            return None  # 空 content 的助手消息不进历史
        return Message(id=new_message_id(), role="assistant", content=blocks,
                       source={"kind": "model",
                               "provider": data.get("provider"),
                               "model": data.get("model")})
    if etype == "tool/result":
        data = event.data
        return Message(
            id=new_message_id(), role="user",
            content=[ContentBlock(kind="tool-result",
                                  tool_call_id=data.get("call_id", ""),
                                  content=data.get("content", ""),
                                  is_error=bool(data.get("is_error")))],
            source={"kind": "tool", "name": data.get("name", "")})
    if etype == "compaction/summary":
        return Message(id=new_message_id(), role="user",
                       content=[ContentBlock(kind="text",
                                             text=event.data.get("summary", ""))],
                       source={"kind": "plugin", "plugin": "compaction"})
    return None


class Session:
    """事件溯源会话：只追加日志 + surface 投影 + 派生历史缓存。"""

    def __init__(self, session_id: str, header: Optional[SessionHeader] = None,
                 publish: Optional[PublishHook] = None,
                 seed: Optional[Sequence[SessionEvent]] = None) -> None:
        self.id = session_id
        self.header = header or SessionHeader(id=session_id, created_at=int(time.time() * 1000))
        self._events: List[SessionEvent] = []
        self._publish: Optional[PublishHook] = publish
        self._derive_cache: Dict[Tuple[int, int], List[Any]] = {}
        self._seeded = False
        self.surface = SurfaceManager()
        self.first_live_seq = 0
        if seed:
            for event in seed:
                self._append_event(event, publish=False)
            self._seeded = True
            self.first_live_seq = len(self._events)
            # 种子结束后追加 end-seed 标记
            self._append_event(SessionEvent(
                type="session/end-seed", seq=len(self._events),
                time=int(time.time() * 1000), data={}), publish=False)

    # ---- 属性 ----

    @property
    def events(self) -> List[SessionEvent]:
        """事件日志快照（不可变事件，调用者可安全遍历）。"""
        return list(self._events)

    @property
    def seq(self) -> int:
        """下一条事件的序号（= 当前日志长度，连续性契约）。"""
        return len(self._events)

    @property
    def has_seed(self) -> bool:
        return self._seeded

    # ---- append ----

    def append(self, event_type: str, data: Dict[str, Any],
               surface_op: Optional[Any] = None,
               source_event_seqs: Optional[List[int]] = None,
               ignorable: bool = False) -> SessionEvent:
        """
        追加一条事件（唯一写入口）。

        校验顺序：JSON 无损性 → surface 契约 → 入日志 → 同步通知 publish hook。

        :param event_type: 事件类型名。
        :param data: 载荷（必须是 str 键 dict，值无损失 JSON）。
        :param surface_op: surface 事件必填（'append' 或 replace dict）。
        :param source_event_seqs: 本事件引用的更早事件 seq。
        :param ignorable: 读者遇到未知类型时可安全跳过。
        :return: 已入日志的事件（深冻结快照）。
        :raises SessionError: 校验失败（事件不入日志）。
        """
        if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
            raise SessionError("event data must be a str-keyed dict")
        if not is_json_value(data):
            raise SessionError(f"event data not lossless-JSON at type {event_type!r}")
        is_surface = event_type in SURFACE_EVENT_TYPES
        self.surface.validate(len(self._events), surface_op, source_event_seqs, is_surface)
        event = SessionEvent(type=event_type, seq=len(self._events),
                             time=int(time.time() * 1000), data=data,
                             surface_op=surface_op,
                             source_event_seqs=list(source_event_seqs)
                             if source_event_seqs is not None else None,
                             ignorable=ignorable)
        return self._append_event(event, publish=True)

    def _append_event(self, event: SessionEvent, publish: bool) -> SessionEvent:
        self._events.append(event)
        self.surface.commit(event.seq, event.surface_op)
        self._derive_cache.clear()
        if publish and self._publish is not None:
            self._publish(self, event)
        return event

    # ---- 派生 ----

    def derive_messages(self) -> List[Any]:
        """
        派生模型可见消息历史（缓存：节点未变时 O(1)，replace 时重建）。

        :return: Message 列表（每次调用新数组，Message 对象共享）。
        """
        key = (self.surface.replace_generation, len(self.surface.nodes))
        cached = self._derive_cache.get(key)
        if cached is not None:
            return list(cached)
        out: List[Any] = []
        for seq in self.surface.nodes:
            message = derive_event_message(self._events[seq])
            if message is not None:
                out.append(message)
        self._derive_cache[key] = out
        return list(out)

    # ---- 其它投影 ----

    def request_header(self) -> Optional[Dict[str, Any]]:
        """折叠最新 request/header 快照（下一次请求将与之比较）。"""
        header: Optional[Dict[str, Any]] = None
        for event in self._events:
            if event.type == "request/header":
                header = event.data.get("header")
        return header

    def request_context(self) -> Optional[Dict[str, Any]]:
        """
        折叠最新 request/context 快照（路由容量元数据，仅变化时记录）。

        :return: {'provider', 'model', 'context_window'?} 或 None。
        """
        context: Optional[Dict[str, Any]] = None
        for event in self._events:
            if event.type == "request/context":
                context = dict(event.data)
        return context

    def todos(self) -> List[Dict[str, str]]:
        """折叠最新 todo/write 快照。"""
        todos: List[Dict[str, str]] = []
        for event in self._events:
            if event.type == "todo/write":
                todos = list(event.data.get("todos") or [])
        return todos

    # ---- 重建（persistence 用） ----

    @staticmethod
    def from_seed(session_id: str, header: SessionHeader,
                  seed: Sequence[Dict[str, Any]],
                  publish: Optional[PublishHook] = None,
                  known_types: Optional[set] = None) -> "Session":
        """
        从持久化行重建会话（严格校验）。

        :param known_types: 本 build 认识的事件类型集合；未知必填类型 → 拒绝。
        :raises SessionFormatError: 版本不符 / seq 断裂 / 未知必填事件。
        """
        if header.version != SESSION_FORMAT_VERSION:
            direction = "newer" if header.version > SESSION_FORMAT_VERSION else "older"
            raise SessionFormatError(
                f"session {session_id} format v{header.version} "
                f"({direction} than v{SESSION_FORMAT_VERSION})", direction)
        known = known_types or set(EVENT_CATALOG.keys())
        events: List[SessionEvent] = []
        for index, raw in enumerate(seed):
            event = SessionEvent.from_json(raw)
            if event.seq != index:
                raise SessionFormatError(
                    f"session {session_id} seq gap at {index}: got {event.seq}")
            if event.type not in known and not event.ignorable:
                raise SessionFormatError(
                    f"session {session_id}: unknown required event {event.type!r}")
            if not is_json_value(event.data):
                raise SessionFormatError(
                    f"session {session_id}: non-JSON data at seq {index}")
            events.append(event)
        return Session(session_id, header=header, publish=publish, seed=events)
