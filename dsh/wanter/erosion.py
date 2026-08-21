"""
dsh.wanter.erosion —— 洼地逃逸（法则 ④：缓慢降低洼地周边地形）。

- ``StagnationDetector``：滑动窗口内势能下降不足 δ 即判定「困于局部洼地」；
- ``Eroder``：在被困点周边用高斯核挖低地形（depth η，可退火 γ<1），
  持续侵蚀 → 洼地周边被挖出下坡通道 → 水滴流出继续向更低处探索。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional

from .terrain import Terrain


class StagnationDetector:
    """驻留（停滞）检测器：窗口内位置漂移不足 = 困于局部洼地。"""

    def __init__(self, window: int = 30, delta: float = 0.2) -> None:
        """
        :param window: 观察窗口（步数）。
        :param delta: 窗口首尾位置漂移阈值（低于此值判停滞——
            水滴位置不再前进，等价于势能不再下降）。
        """
        self.window = window
        self.delta = delta
        self._history: List[Any] = []

    def observe(self, position: Any) -> None:
        """记录一次位置观测（坐标序列）。"""
        self._history.append(tuple(float(v) for v in position))
        if len(self._history) > self.window:
            self._history.pop(0)

    def stagnated(self) -> bool:
        """当前是否被困（窗口满且首尾位置漂移 < delta）。"""
        if len(self._history) < self.window:
            return False
        first = self._history[0]
        last = self._history[-1]
        drift = math.sqrt(sum((a - b) ** 2 for a, b in zip(first, last)))
        return drift < self.delta

    def reset(self) -> None:
        self._history.clear()


class Eroder:
    """地形侵蚀器（法则 ④：降低洼地**周边**地形高度，开辟下坡通道）。"""

    def __init__(self, depth: float = 0.5, anneal: float = 0.98,
                 radius: float = 1.0) -> None:
        """
        :param depth: η（单次侵蚀挖深幅度）。
        :param anneal: γ（每次侵蚀后的退火系数，<1 缓慢减小挖深，避免挖穿）。
        :param radius: 侵蚀环半径 r（挖低「周边」而非洼底）。
        """
        self.depth = depth
        self.anneal = anneal
        self.radius = radius
        self.erosion_count = 0

    def erode(self, terrain: Terrain, center: Any) -> float:
        """
        在被困点周边挖低地形一次，开辟「通向下游」的下坡通道。

        - 有目标（nearest_goal）：在朝向目标的 rim 点全深开挖（主通道），
          垂直各轴 rim 点半深开挖（拓宽）；**不挖反向（洼地内侧）**——
          否则只会把陷阱挖得更深（对称环形侵蚀的教训，实验记录于手册 16）；
        - 无目标（纯探索）：各轴 rim 点半深开挖（对称拓宽）。

        :return: 本次实际挖深（退火后，全深值）。
        """
        depth = self.depth * (self.anneal ** self.erosion_count)
        dim = terrain.dim
        center = tuple(center)
        seen = set()

        def dig_at(point: tuple, amount: float) -> None:
            key = tuple(round(v, 6) for v in point)
            if key in seen:
                return
            seen.add(key)
            terrain.erode(point, amount)

        nearest = terrain.nearest_goal(center)
        if nearest is not None:
            diff = [nearest[i] - center[i] for i in range(dim)]
            norm = math.sqrt(sum(d * d for d in diff))
            if norm > 1e-9:
                goal_point = [center[i] + (diff[i] / norm) * self.radius
                              for i in range(dim)]
                dig_at(tuple(goal_point), depth)  # 主通道：全深
            # 垂直/同向方向：半深拓宽；反向（洼地内侧）不开挖
            for axis in range(dim):
                for sign in (+1.0, -1.0):
                    if sign * diff[axis] < 0:
                        continue  # 反向 rim 点 = 陷阱内侧，挖它只会加深陷阱
                    point = list(center)
                    point[axis] = center[axis] + sign * self.radius
                    dig_at(tuple(point), depth * 0.5)
        else:
            for axis in range(dim):
                for sign in (+1.0, -1.0):
                    point = list(center)
                    point[axis] = center[axis] + sign * self.radius
                    dig_at(tuple(point), depth * 0.5)
        self.erosion_count += 1
        return depth
