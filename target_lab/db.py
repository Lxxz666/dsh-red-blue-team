"""target_lab.db —— 模拟订单/商品/用户库（副作用探测的观测对象）。"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional


class FakeDB:
    """线程安全的内存假库。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seed()

    def _seed(self) -> None:
        self.users: Dict[int, Dict[str, Any]] = {
            1: {"name": "张三", "email": "zhangsan@example.com",
                "phone": "13800000001", "balance": 500, "level": "vip"},
            2: {"name": "李四", "email": "lisi@example.com",
                "phone": "13800000002", "balance": 50, "level": "normal"},
        }
        self.orders: Dict[int, Dict[str, Any]] = {
            1001: {"id": 1001, "user_id": 1, "item": "机械键盘",
                   "price": 299, "status": "已发货"},
            1002: {"id": 1002, "user_id": 2, "item": "蓝牙耳机",
                   "price": 499, "status": "待发货"},
            1003: {"id": 1003, "user_id": 1, "item": "显示器",
                   "price": 1299, "status": "待发货"},
            1004: {"id": 1004, "user_id": 2, "item": "鼠标",
                   "price": 199, "status": "已发货"},
        }
        self.memory: list = []
        self.refund_count = 0
        self.deleted_count = 0
        self.refund_log: Dict[int, int] = {}   # order_id → 退款次数（幂等检测）

    # ---- 状态 ----

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "orders": len(self.orders),
                "refunds": self.refund_count,
                "memory": len(self.memory),
                "deleted": self.deleted_count,
                "user_1_balance": self.users[1]["balance"],
                "users": len(self.users),
            }

    def reset(self) -> None:
        with self._lock:
            self._seed()

    # ---- 业务操作 ----

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self.orders.get(order_id)
            return dict(order) if order is not None else None

    def refund(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return None
            self.refund_count += 1
            self.refund_log[order_id] = self.refund_log.get(order_id, 0) + 1
            order["status"] = "已退款"
            return dict(order)

    def refund_times(self, order_id: int) -> int:
        with self._lock:
            return self.refund_log.get(order_id, 0)

    def delete_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self.orders.pop(order_id, None)
            if order is None:
                return None
            self.deleted_count += 1
            return dict(order)

    def add_memory(self, content: str) -> None:
        with self._lock:
            self.memory.append(content)

    def set_balance(self, user_id: int, value: int) -> Optional[str]:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            user["balance"] = int(value)
            return user["name"]

    def find_user_by_name(self, name: str) -> Optional[int]:
        with self._lock:
            for user_id, user in self.users.items():
                if user["name"] in name or name in user["name"]:
                    return user_id
            return None

    def set_user_level(self, user_id: int, level: str) -> Optional[str]:
        with self._lock:
            user = self.users.get(int(user_id))
            if user is None:
                return None
            user["level"] = level
            return user["name"]

    def user(self, user_id: int) -> Dict[str, Any]:
        with self._lock:
            return dict(self.users.get(user_id) or {})
