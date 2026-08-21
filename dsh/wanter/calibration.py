"""
dsh.wanter.calibration —— 语义坐标校准实验（离线、确定性、可复现）。

回答「真实 embedding 比哈希伪嵌入好多少」：多目标语义匹配场景——
K 个任务各有一个目标势阱；坐标提供者把任务标签映射为起点：

- **hash**：HashCoordinator（确定性哈希伪嵌入，与目标布局无关 → 起点
  散布，每个任务落入最近势阱，匹配率 ≈ 机会水平）；
- **oracle**：模拟**训练好的语义嵌入**——标签确定性映射到其目标 ± 小抖动
  （与目标布局对齐 → 匹配率 ≈ 100%、步数更少）。

每次实验 = 种子 × 任务的水滴 Langevin 下降；度量：
`matching`（落入**自己**目标的比率）、`steps`（匹配者的平均步数）、
`per_task`（逐任务明细）。全部离线可复现（无网络/无密钥）。

消费方：`examples/wanter_embedding_calibration.py`（产出 metrics JSON +
SVG 图表）与 `tests/test_wanter_calibration.py`（回归守护）。
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .coordinator import HashCoordinator
from .flow import langevin_step
from .terrain import Terrain

DEFAULT_GOALS: List[Tuple[float, float]] = [
    (5.0, 0.0), (-5.0, 0.0), (0.0, 5.0), (0.0, -5.0)]
DEFAULT_LABELS = ["写后端接口", "修前端样式", "数据库调优", "部署流水线"]
WELL_DEPTH = 3.0
"""目标势阱深度（作为负高度高斯 bump 实现）。

设计注：Terrain 的目标模型是二次势阱（0.5·strength·|x−goal|²，长程），
多目标时远阱干扰主导（φ≈105 量级），会把人造中心鞍点当成物理陷阱。
校准实验改用**短程高斯洼地**（add_bump 负高度）——与真实语义嵌入的
「局部盆地」直觉一致，且相邻任务不串扰。
"""


def build_calibration_terrain(goals: Optional[Sequence[Any]] = None,
                              sigma: float = 1.0) -> Terrain:
    """多目标高斯洼地地形（每目标一个 WELL_DEPTH 深的短程势阱）。"""
    terrain = Terrain(dim=2, sigma=sigma)
    for goal in (goals or DEFAULT_GOALS):
        terrain.add_bump(tuple(goal), height=-WELL_DEPTH)
    return terrain


class OracleEmbeddingProvider:
    """模拟「训练好的语义嵌入」：标签确定性映射到其目标 ± 小抖动。

    抖动由标签哈希决定（复现性），幅度控制在「仍在自家盆地内」。
    """

    def __init__(self, goals: Optional[Sequence[Any]] = None,
                 jitter: float = 0.2) -> None:
        self._goals = [tuple(g) for g in (goals or DEFAULT_GOALS)]
        self._jitter = jitter

    def _jitter_for(self, label: str, axis: int) -> float:
        digest = hashlib.sha256(
            f"{label}:{axis}".encode("utf-8")).digest()
        unit = (digest[0] / 255.0) * 2.0 - 1.0  # [-1, 1]
        return unit * self._jitter

    def embed(self, label: str) -> tuple:
        index = (DEFAULT_LABELS + [label]).index(label) % len(self._goals) \
            if label in DEFAULT_LABELS else \
            int(hashlib.sha256(label.encode()).hexdigest(), 16) \
            % len(self._goals)
        goal = self._goals[index]
        return (goal[0] + self._jitter_for(label, 0),
                goal[1] + self._jitter_for(label, 1))


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def run_matching_experiment(provider, goals: Optional[Sequence[Any]] = None,
                            labels: Optional[Sequence[str]] = None,
                            seeds: Sequence[int] = (0, 1, 2, 3, 4, 5, 6, 7),
                            step_cap: int = 1200, lr: float = 0.1,
                            D: float = 0.005,
                            complete_radius: float = 0.5
                            ) -> Dict[str, Any]:
    """跑一次多目标匹配实验（种子 × 任务的水滴）。"""
    goals = [tuple(g) for g in (goals or DEFAULT_GOALS)]
    labels = list(labels or DEFAULT_LABELS)
    terrain = build_calibration_terrain(goals)
    per_task: List[Dict[str, Any]] = []
    matched = 0
    total_steps = 0
    completed = 0
    for index, label in enumerate(labels):
        task_matched = 0
        task_steps = 0
        for seed in seeds:
            rng = random.Random(1000 * index + seed)
            x = tuple(provider.embed(label))
            hit_own = False
            for step in range(step_cap):
                x = langevin_step(x, terrain, lr=lr, D=D, rng=rng)
                landed = next((j for j, g in enumerate(goals)
                               if _distance(x, g) < complete_radius), None)
                if landed is not None:
                    hit_own = landed == index
                    task_steps += step + 1
                    break
            if hit_own:
                matched += 1
                task_matched += 1
            completed += 1
            total_steps += step + 1
        per_task.append({"label": label, "matched": task_matched,
                         "runs": len(seeds)})
    return {
        "matching": matched / completed if completed else 0.0,
        "mean_steps": total_steps / completed if completed else float(step_cap),
        "runs": completed,
        "per_task": per_task,
    }


def run_calibration(seeds: Sequence[int] = (0, 1, 2, 3, 4, 5, 6, 7)
                    ) -> Dict[str, Any]:
    """hash 伪嵌入 vs oracle 语义嵌入 的对照实验。"""
    goals = DEFAULT_GOALS
    labels = DEFAULT_LABELS
    hash_provider = HashCoordinator(dim=2, salt=7, scale=2.0)
    oracle = OracleEmbeddingProvider(goals)
    return {
        "goals": [list(g) for g in goals],
        "labels": labels,
        "hash": run_matching_experiment(hash_provider, goals, labels,
                                        seeds=seeds),
        "oracle": run_matching_experiment(oracle, goals, labels,
                                          seeds=seeds),
    }
