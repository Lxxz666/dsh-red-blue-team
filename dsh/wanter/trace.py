"""
dsh.wanter.trace —— 水迹场 T（法则 ②：路径留痕 + 指数蒸发）。

- 事件模型：每次沉积记录 (坐标 c, 初始权重 w, 时间 t₀)；
- 蒸发：读取/查询时按 `w(t) = w·e^(−λ(t−t₀))` 惰性折算；
  权重低于阈值 ε 的事件被遗忘（丢弃）；
- 密度/梯度：`density_at` / `grad_at` 按高斯核 `K(x)=exp(−‖x‖²/2σ²)` 加权求和，
  供 Terrain 组装「迹刻蚀通道」项。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

Kernel = Tuple[Any, float, float]  # (坐标, 初始权重, 沉积时间)


class TraceField:
    """水迹场：事件列表 + 惰性蒸发 + 同点聚合 + 容量上限。"""

    def __init__(self, decay_rate: float = 0.05,
                 epsilon: float = 1e-4, max_events: int = 5000,
                 aggregate: bool = True) -> None:
        """
        :param decay_rate: λ（指数蒸发率，1/秒）。
        :param epsilon: 遗忘阈值（|有效权重|低于此值的事件被丢弃）。
        :param max_events: 事件容量上限（超限按 |有效权重| 最小者淘汰）。
        :param aggregate: 同点沉积聚合（多次冲刷同一处 = 河道加深）。
        """
        self.decay_rate = decay_rate
        self.epsilon = epsilon
        self.max_events = max_events
        self.aggregate = aggregate
        self._events: List[Kernel] = []

    # ---- 事件 ----

    def deposit(self, point: Any, weight: float = 1.0,
                at: Optional[float] = None) -> None:
        """
        沉积一次水迹。

        - weight > 0：正沉积（成功路径 → 刻蚀成河道，降低地形）；
        - weight < 0：**淤积**（失败路径 → 泥沙堆积抬高地形，反向刻蚀）；
        - weight == 0：跳过；
        - 同点重复沉积（aggregate=True）：先把旧权重蒸发折算到当前时刻，
          再与新权重相加——物理上「多次冲刷同一处 = 河道更深」。
        """
        if weight == 0:
            return
        point = tuple(point)
        stamp = time.time() if at is None else float(at)
        if self.aggregate:
            for index, event in enumerate(self._events):
                if event[0] == point:
                    merged = self._weight_at(event, stamp) + float(weight)
                    self._events[index] = (point, merged, stamp)
                    self._enforce_cap()
                    return
        self._events.append((point, float(weight), stamp))
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        """容量上限：超限时按 |有效权重| 升序淘汰最弱事件。"""
        if len(self._events) <= self.max_events:
            return
        now = time.time()
        self._events.sort(key=lambda e: abs(self._weight_at(e, now)))
        self._events = self._events[len(self._events) - self.max_events:]

    def evaporate_to(self, now: Optional[float] = None) -> int:
        """
        蒸发到当前时刻：丢弃 |有效权重| 低于 ε 的事件。

        :return: 遗忘的事件数。
        """
        stamp = time.time() if now is None else float(now)
        kept: List[Kernel] = []
        for event in self._events:
            if abs(self._weight_at(event, stamp)) >= self.epsilon:
                kept.append(event)
        removed = len(self._events) - len(kept)
        self._events = kept
        return removed

    def _weight_at(self, event: Kernel, now: float) -> float:
        _point, weight, deposited = event
        # 时钟回拨保护：deposited 在未来时按 0 流逝处理（防 exp 爆炸）
        elapsed = max(0.0, now - deposited)
        return weight * math.exp(-self.decay_rate * elapsed)

    # ---- 查询 ----

    def event_count(self) -> int:
        return len(self._events)

    def weights_at(self, now: Optional[float] = None) -> List[float]:
        """全部事件在当前时刻的有效权重（蒸发后）。"""
        stamp = time.time() if now is None else float(now)
        return [self._weight_at(e, stamp) for e in self._events]

    def density_at(self, point: Any, sigma: float = 1.0,
                   now: Optional[float] = None) -> float:
        """迹密度：Σ wᵢ(t)·K(x − cᵢ)。"""
        stamp = time.time() if now is None else float(now)
        x = tuple(point)
        total = 0.0
        for event in self._events:
            weight = self._weight_at(event, stamp)
            if abs(weight) < self.epsilon:
                continue
            total += weight * _kernel(_distance(x, event[0]), sigma)
        return total

    def grad_at(self, point: Any, sigma: float = 1.0,
                now: Optional[float] = None) -> Any:
        """迹密度梯度：Σ wᵢ(t)·∇K(x − cᵢ)。"""
        stamp = time.time() if now is None else float(now)
        x = tuple(point)
        grad = [0.0] * len(x)
        for event in self._events:
            weight = self._weight_at(event, stamp)
            if abs(weight) < self.epsilon:
                continue
            center = event[0]
            diff = [x[i] - center[i] for i in range(len(x))]
            k = _kernel(_distance(x, center), sigma)
            for i in range(len(x)):
                grad[i] += weight * (-diff[i] / (sigma * sigma)) * k
        return tuple(grad)

    def snapshot(self) -> Dict[str, Any]:
        """持久化快照（含蒸发后有效权重）。"""
        now = time.time()
        self.evaporate_to(now)
        return {"decay_rate": self.decay_rate,
                "events": [{"point": list(e[0]), "weight": e[1],
                            "time": e[2]} for e in self._events]}

    @staticmethod
    def from_snapshot(snapshot: Dict[str, Any]) -> "TraceField":
        """从快照恢复。"""
        field = TraceField(decay_rate=snapshot.get("decay_rate", 0.05))
        for event in snapshot.get("events") or []:
            field._events.append((tuple(event["point"]), event["weight"],
                                  event["time"]))
        return field


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _kernel(distance: float, sigma: float) -> float:
    return math.exp(-(distance * distance) / (2.0 * sigma * sigma))
