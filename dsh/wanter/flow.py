"""
dsh.wanter.flow —— 流动动力学（法则 ①+③：水向低处流）。

- ``gradient_step``：纯梯度步 x ← x − lr·∇Φ；
- ``langevin_step``：带扩散噪声 x ← x − lr·∇Φ + √(2D·lr)·ξ；
- ``softmin_weights``：候选点的 Boltzmann 选择权重 P ∝ exp(−Φ/τ)
  （离散「水流选择下游方向」，迹刻蚀使旧路径 Φ 更低 → 偏好旧径）。
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence

from .terrain import Terrain


def gradient_step(x: Any, terrain: Terrain, lr: float = 0.1,
                  now: Optional[float] = None) -> Any:
    """纯梯度下降一步（确定性下坡）。"""
    grad = terrain.grad_phi(x, now=now)
    return tuple(xi - lr * gi for xi, gi in zip(x, grad))


def langevin_step(x: Any, terrain: Terrain, lr: float = 0.1, D: float = 0.05,
                  rng: Optional[random.Random] = None,
                  now: Optional[float] = None) -> Any:
    """
    带噪梯度步（随机梯度流）：dx = −lr·∇Φ + √(2D·lr)·ξ。

    :param D: 扩散系数（探索强度，与温度 τ 同源）。
    :param rng: 随机源（测试可注入固定种子）。
    """
    rng = rng or random.Random()
    grad = terrain.grad_phi(x, now=now)
    out = []
    for xi, gi in zip(x, grad):
        noise = math.sqrt(2.0 * D * lr) * rng.gauss(0.0, 1.0)
        out.append(xi - lr * gi + noise)
    return tuple(out)


def softmin_weights(candidates: Sequence[Any], terrain: Terrain,
                    tau: float = 1.0, now: Optional[float] = None
                    ) -> Dict[int, float]:
    """
    离散软最小选择：P(i) ∝ exp(−Φ(candidateᵢ)/τ)。

    :param candidates: 候选点列表。
    :param tau: 温度（越大越随机，等价于 D 增大）。
    :return: {index: 概率}。
    """
    energies = [terrain.phi(c, now=now) for c in candidates]
    min_energy = min(energies)
    weights = [math.exp(-(e - min_energy) / tau) for e in energies]
    total = sum(weights)
    if total <= 0:
        n = len(candidates)
        return {i: 1.0 / n for i in range(n)}
    return {i: w / total for i, w in enumerate(weights)}


def descend(x: Any, terrain: Terrain, steps: int = 200, lr: float = 0.1,
            now: Optional[float] = None) -> List[Any]:
    """纯梯度下降模拟整条水滴路径，返回轨迹。"""
    path = [tuple(x)]
    for _ in range(steps):
        x = gradient_step(x, terrain, lr=lr, now=now)
        path.append(tuple(x))
    return path
