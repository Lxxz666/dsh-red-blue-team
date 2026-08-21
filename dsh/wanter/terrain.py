"""
dsh.wanter.terrain —— 势能地形 Φ(x,t)（法则 ③：全局势能场，最低点=目标）。

Φ(x,t) = Φ_goal + Φ_base + Φ_trace + Φ_erosion

- Φ_goal(x) = ½·k_g·‖x−g‖²（谐波目标势阱）；
- Φ_base：用户定义的高斯「山丘」列表（静态障碍/偏好）；
- Φ_trace = −α·(K⊛T)(x)：水迹刻蚀的河道（负贡献，降低地形）；
- Φ_erosion = −Σ ηⱼ·K(x−cⱼ)：侵蚀挖出的洼地（法则 ④）。
"""
from __future__ import annotations

import math
from typing import Any, Callable, List, Optional, Sequence, Tuple

Bump = Tuple[Any, float]  # (中心, 高度/深度)


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _kernel(distance: float, sigma: float) -> float:
    return math.exp(-(distance * distance) / (2.0 * sigma * sigma))


class Terrain:
    """连续势能地形。"""

    def __init__(self, dim: int = 2, goal: Optional[Any] = None,
                 goal_strength: float = 1.0, sigma: float = 1.0,
                 trace_alpha: float = 1.0, trace_field: Any = None,
                 erosion_max_depth: float = 3.0) -> None:
        """
        :param dim: 坐标维度。
        :param goal: 目标点（任务完成势阱中心）。
        :param goal_strength: k_g。
        :param sigma: 高斯核宽度。
        :param trace_alpha: α（迹刻蚀强度）。
        :param trace_field: TraceField（迹项来源）。
        :param erosion_max_depth: 单点最大累计挖深——河床有底：挖到上限后
            继续侵蚀会改向周边（创建新 bump），通道因此拓宽而非无限加深。
        """
        self.dim = dim
        self.sigma = sigma
        self.trace_alpha = trace_alpha
        self.trace_field = trace_field
        self.erosion_max_depth = erosion_max_depth
        self.goals: List[Tuple[Any, float]] = []  # (目标点, 强度)
        if goal is not None:
            self.goals.append((tuple(goal), float(goal_strength)))
        self.base_bumps: List[Bump] = []     # 静态山丘
        self.erosion_bumps: List[Bump] = []  # 侵蚀洼地（负数高度）

    @property
    def goal(self):
        """主目标（第一个势阱中心；无目标为 None）。"""
        return self.goals[0][0] if self.goals else None

    # ---- 地形改造 ----

    def add_goal(self, goal: Any, strength: float = 1.0) -> None:
        """加一个目标势阱（多目标 = 子任务分解，Σ 谐波阱）。"""
        self.goals.append((tuple(goal), float(strength)))

    def remove_goal(self, goal: Any) -> bool:
        """移除一个目标势阱（子任务完成）。"""
        target = tuple(goal)
        for index, (point, _strength) in enumerate(self.goals):
            if point == target:
                self.goals.pop(index)
                return True
        return False

    def nearest_goal(self, x: Any) -> Optional[Any]:
        """距 x 最近的目标势阱中心（无目标 → None）。"""
        if not self.goals:
            return None
        return min(self.goals, key=lambda g: _distance(x, g[0]))[0]

    def add_bump(self, center: Any, height: float) -> None:
        """加一座静态山丘（height>0 升高 / <0 降低）。"""
        self.base_bumps.append((tuple(center), float(height)))

    def erode(self, center: Any, depth: float) -> None:
        """
        侵蚀：在被困点周边挖低（法则 ④）。depth 为负贡献幅度（正数）。

        近邻聚合（σ/2 内合并加深）受 `erosion_max_depth` 限制：挖到上限后
        改为新增 bump——通道拓宽而非无限加深（河床有底的物理直觉）。
        """
        center = tuple(center)
        threshold = self.sigma / 2.0
        for index, (point, height) in enumerate(self.erosion_bumps):
            if _distance(point, center) < threshold:
                merged = height - float(depth)
                if merged >= -self.erosion_max_depth:
                    self.erosion_bumps[index] = (point, merged)
                    return
                break  # 该处已到底：改为周边新 bump
        self.erosion_bumps.append((center, -float(depth)))

    def reset_erosion(self) -> None:
        """清空侵蚀改造（实验对照组用）。"""
        self.erosion_bumps.clear()

    # ---- 势能与梯度 ----

    def _bump_contribution(self, x: Any, bumps: List[Bump]) -> float:
        total = 0.0
        for center, height in bumps:
            total += height * _kernel(_distance(x, center), self.sigma)
        return total

    def phi(self, x: Any, now: Optional[float] = None) -> float:
        """总势能 Φ(x)。"""
        x = tuple(x)
        value = 0.0
        for goal, strength in self.goals:
            value += 0.5 * strength * _distance(x, goal) ** 2
        value += self._bump_contribution(x, self.base_bumps)
        value += self._bump_contribution(x, self.erosion_bumps)
        if self.trace_field is not None:
            value -= self.trace_alpha * self.trace_field.density_at(
                x, self.sigma, now=now)
        return value

    def grad_phi(self, x: Any, now: Optional[float] = None) -> Any:
        """梯度 ∇Φ(x)。"""
        x = tuple(x)
        grad = [0.0] * self.dim
        for goal, strength in self.goals:
            diff = [x[i] - goal[i] for i in range(self.dim)]
            for i in range(self.dim):
                grad[i] += strength * diff[i]
        for center, height in self.base_bumps + self.erosion_bumps:
            diff = [x[i] - center[i] for i in range(self.dim)]
            k = _kernel(_distance(x, center), self.sigma)
            for i in range(self.dim):
                grad[i] += height * (-diff[i] / self.sigma ** 2) * k
        if self.trace_field is not None:
            trace_grad = self.trace_field.grad_at(x, self.sigma, now=now)
            for i in range(self.dim):
                grad[i] -= self.trace_alpha * trace_grad[i]
        return tuple(grad)

    # ---- 快照 ----

    def snapshot(self) -> dict:
        return {"dim": self.dim,
                "goals": [[list(g), s] for g, s in self.goals],
                "sigma": self.sigma, "trace_alpha": self.trace_alpha,
                "base_bumps": [[list(c), h] for c, h in self.base_bumps],
                "erosion_bumps": [[list(c), h] for c, h in self.erosion_bumps]}

    @staticmethod
    def from_snapshot(snapshot: dict, trace_field: Any = None) -> "Terrain":
        terrain = Terrain(dim=snapshot["dim"], goal=None,
                          sigma=snapshot.get("sigma", 1.0),
                          trace_alpha=snapshot.get("trace_alpha", 1.0),
                          trace_field=trace_field)
        for goal, strength in snapshot.get("goals") or []:
            terrain.add_goal(goal, strength)
        if not terrain.goals and snapshot.get("goal"):
            # 兼容旧快照（单 goal 字段）
            terrain.add_goal(snapshot["goal"],
                             snapshot.get("goal_strength", 1.0))
        terrain.base_bumps = [(tuple(c), h) for c, h in
                              snapshot.get("base_bumps") or []]
        terrain.erosion_bumps = [(tuple(c), h) for c, h in
                                 snapshot.get("erosion_bumps") or []]
        return terrain
