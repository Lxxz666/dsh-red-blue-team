"""
dsh.session.events —— 会话事件词汇表（对应 SessionEventMap）。

设计契约（与 TS 版 session.md 一致）:

- 日志是只追加、无损失 JSON 的：``append`` 在源头拒绝不可序列化数据；
- ``seq`` 单调连续（``seq = log.length``）；
- surface 事件（产生 LLM 消息的三种：user/message、assistant/message、tool/result，
  以及插件扩展如 compaction/summary）必须声明 ``surface_op``；
- 未知必填事件在回放时必须导致拒绝（除非带 ``ignorable=True``）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union

from ..errors import SessionError

SESSION_FORMAT_VERSION = 1
"""磁盘格式版本。后端拒绝其它版本（无迁移路径，与 TS 版一致）。"""

#: 产生 LLM 消息、可出现在有序 surface 上的事件类型（可扩展）
SURFACE_EVENT_TYPES: Set[str] = {
    "user/message",
    "assistant/message",
    "tool/result",
    "compaction/summary",
}

#: 事件注册表: 类型名 -> 描述（供 catalog 与手册生成）
EVENT_CATALOG: Dict[str, str] = {
    "turn/start": "在循环认领排队输入或运行 pre-step 之前打开 turn。",
    "turn/end": "以 TurnEndReason 关闭 turn（completed/aborted/blocked/error/max-tokens/interrupted）。",
    "step/start": "打开一步：一次模型调用 + 其工具执行。",
    "step/end": "关闭一步。",
    "user/message": "用户角色消息（真人 prompt / inject 上下文 / goal 续轮）。",
    "assistant/chunk": "原始流式块（token 级回放保真，不进派生历史）。",
    "assistant/message": "一步组装好的助手消息（派生历史用它；含 usage）。",
    "tool/call": "模型请求一次工具调用（arguments 为原始 JSON 字符串）。",
    "tool/result": "工具调用的模型可见结果（error/meta 可选）。",
    "todo/write": "todo 列表全量快照（最新写入胜出，仅日志不进历史）。",
    "request/header": "下一次请求的完整信封（config+system prompt+tools），仅变化时全量快照。",
    "request/context": "下一次请求的路由容量元数据（provider/model/context_window），仅变化时快照。",
    "session/end-seed": "构造器种子结束标记（firstLiveSeq 的持久化投影）。",
    "compaction/summary": "上下文压缩摘要（surface replace 事件，投影为 user 消息）。",
}

TurnEndReason = Dict[str, Any]
"""turn 结束原因：{'kind': 'completed'|'aborted'|'blocked'|'error'|'max-tokens'|'interrupted', ...}"""


def register_event_type(event_type: str, description: str,
                        surface: bool = False) -> None:
    """
    插件扩展事件词汇（对应 TS 版 declaration merging）。

    :param event_type: 事件类型名（如 ``hook/invoked``）。
    :param description: 单句描述（进 catalog 与手册）。
    :param surface: True 表示该事件产生 LLM 消息（须带 surface_op）。
    """
    EVENT_CATALOG[event_type] = description
    if surface:
        SURFACE_EVENT_TYPES.add(event_type)


def is_json_value(value: Any) -> bool:
    """
    无损失 JSON 校验：str/int/float(有限)/bool/None/list/dict(str 键)。

    额外接受实现了 ``to_json()`` 协议的对象（如 ContentBlock/Message），
    其返回值会被递归校验——这样日志既能持有类型化值，又保证无损失持久化。

    :return: False 表示该值无法被 JSON 原样持久化。
    """
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return is_json_value(to_json())
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_value(v) for k, v in value.items())
    return False


SurfaceOp = Union[str, Dict[str, int]]
"""'append' 或 {'op':'replace','start':..,'end':..}"""


@dataclass(frozen=True)
class SessionEvent:
    """日志中一条不可变事件。``data`` 已在源头校验为无损失 JSON。"""

    type: str
    seq: int
    time: int
    data: Dict[str, Any]
    surface_op: Optional[SurfaceOp] = None
    source_event_seqs: Optional[List[int]] = None
    ignorable: bool = False

    def is_surface(self) -> bool:
        """是否 surface（消息产生型）事件。"""
        return self.type in SURFACE_EVENT_TYPES

    def to_json(self) -> Dict[str, Any]:
        """序列化为可持久化的 dict（JSONL 行格式）。"""
        out: Dict[str, Any] = {
            "type": self.type,
            "seq": self.seq,
            "time": self.time,
            "data": self.data,
        }
        if self.surface_op is not None:
            out["surface_op"] = self.surface_op
        if self.source_event_seqs is not None:
            out["source_event_seqs"] = self.source_event_seqs
        if self.ignorable:
            out["ignorable"] = True
        return out

    @staticmethod
    def from_json(raw: Dict[str, Any]) -> "SessionEvent":
        """从持久化行还原事件（校验见 Session.from_seed）。"""
        try:
            return SessionEvent(
                type=str(raw["type"]),
                seq=int(raw["seq"]),
                time=int(raw["time"]),
                data=dict(raw.get("data") or {}),
                surface_op=raw.get("surface_op"),
                source_event_seqs=raw.get("source_event_seqs"),
                ignorable=bool(raw.get("ignorable", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"malformed session event: {exc}") from exc
