"""
dsh.agent.inbox —— Inbox：agent 的两个有序待办列表（next-turn / next-step）。

对应 TS 版 Inbox。每条 pending 消息是 dict: ``{"id", "content", "source"}``
（对应 UserMessage）。所有变更由 agent-loop 记录为 ``agent/inbox/*`` 事件。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from ..ids import new_message_id


def make_message(text: str, source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条 pending 消息。"""
    return {"id": new_message_id(), "content": text, "source": source or {"kind": "user"}}


class Inbox:
    """两个有序 pending 列表 + 线程安全守卫。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.next_turn: List[Dict[str, Any]] = []
        self.next_step: List[Dict[str, Any]] = []

    # ---- 基础操作 ----

    def _find(self, message_id: str) -> Tuple[Optional[List], int]:
        for lst in (self.next_step, self.next_turn):
            for index, message in enumerate(lst):
                if message["id"] == message_id:
                    return lst, index
        return None, -1

    def append(self, target: str, message: Dict[str, Any]) -> None:
        """向指定列表追加（target ∈ 'next-turn'|'next-step'）。"""
        with self._lock:
            self._list_for(target).append(message)

    def prepend(self, target: str, message: Dict[str, Any]) -> None:
        with self._lock:
            self._list_for(target).insert(0, message)

    def remove(self, message_id: str) -> Optional[Dict[str, Any]]:
        """跨两个列表删除一条消息。"""
        with self._lock:
            lst, index = self._find(message_id)
            if lst is None:
                return None
            return lst.pop(index)

    def clear(self, target: Optional[str] = None) -> None:
        """清空列表（省略 target = 全部）。"""
        with self._lock:
            if target is None:
                self.next_turn.clear()
                self.next_step.clear()
            else:
                self._list_for(target).clear()

    def _list_for(self, target: str) -> List[Dict[str, Any]]:
        if target == "next-turn":
            return self.next_turn
        if target == "next-step":
            return self.next_step
        raise ValueError(f"unknown inbox target: {target}")

    # ---- 认领 ----

    def has_next_step(self) -> bool:
        return bool(self.next_step)

    def has_next_turn(self) -> bool:
        return bool(self.next_turn)

    def claim_turn_batch(self) -> Optional[List[Dict[str, Any]]]:
        """
        认领一个 turn 批次：next-step 全量优先；否则一条 next-turn。

        :return: None 表示没有可认领的工作（驱动保持 idle）。
        """
        with self._lock:
            if self.next_step:
                batch = list(self.next_step)
                self.next_step.clear()
                return batch
            if self.next_turn:
                return [self.next_turn.pop(0)]
            return None

    def claim_next_step(self) -> List[Dict[str, Any]]:
        """认领全部 next-step 输入（turn 内下一步）。"""
        with self._lock:
            batch = list(self.next_step)
            self.next_step.clear()
            return batch

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        """只读快照。"""
        with self._lock:
            return {"next_turn": list(self.next_turn),
                    "next_step": list(self.next_step)}
