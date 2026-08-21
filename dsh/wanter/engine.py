"""
dsh.wanter.engine —— WanterEngine（ctx.wanter）：四法则的统一引擎。

职责：

- 持有 Terrain + TraceField + StagnationDetector + Eroder；
- 后台蒸发循环（evaporate tick）——法则 ② 的实时衰减；
- 快照/恢复（经 ctx.storage domain "wanter"）——地形跨会话共享（法则 ③ 全局性）；
- 对 harness 暴露：deposit / phi / grad / softmin / step / report_and_maybe_erode。

纯数学实现，零 LLM 依赖；harness 集成见 plugin.py。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence

from ..kernel import Service
from .erosion import Eroder, StagnationDetector
from .flow import gradient_step, langevin_step, softmin_weights
from .terrain import Terrain
from .trace import TraceField

STORAGE_DOMAIN = "wanter"


class WanterEngine(Service):
    """wanter 引擎（ctx.wanter）。"""

    provides = "wanter"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        config = config or {}
        self.dim = int(config.get("dim", 2))
        self.decay_rate = float(config.get("decay_rate", 0.05))   # λ
        self.deposit_beta = float(config.get("deposit_beta", 1.0))  # β
        self.alpha = float(config.get("alpha", 1.0))              # 迹刻蚀 α
        self.sigma = float(config.get("sigma", 1.0))              # 核宽
        self.tau = float(config.get("tau", 1.0))                  # 温度
        self.D = float(config.get("D", 0.05))                     # 扩散
        self.lr = float(config.get("lr", 0.1))                    # 步长
        self.erosion_depth = float(config.get("erosion_depth", 0.5))  # η
        self.erosion_anneal = float(config.get("erosion_anneal", 0.98))  # γ
        self.erosion_radius = float(config.get("erosion_radius",
                                               config.get("sigma", 1.0)))
        self.stagnation_window = int(config.get("stagnation_window", 30))
        self.stagnation_delta = float(config.get("stagnation_delta", 0.2))
        self.evaporate_interval = float(config.get("evaporate_interval", 1.0))
        self.complete_radius = float(config.get("complete_radius", 0.1))
        self.trace_max_events = int(config.get("trace_max_events", 5000))

        self.trace = TraceField(decay_rate=self.decay_rate,
                                max_events=self.trace_max_events)
        self.terrain = Terrain(dim=self.dim,
                               sigma=self.sigma, trace_alpha=self.alpha,
                               trace_field=self.trace)
        # 目标配置：goal=[x,y,…]（单目标坐标）；goals 为坐标列表，
        # 条目可为 {"point": [...], "strength": n} 指定强度。
        default_strength = float(config.get("goal_strength", 1.0))
        goals = config.get("goals")
        if goals:
            for entry in goals:
                if isinstance(entry, dict):
                    self.terrain.add_goal(
                        entry["point"], float(entry.get("strength",
                                                        default_strength)))
                else:
                    self.terrain.add_goal(entry, default_strength)
        elif config.get("goal"):
            self.terrain.add_goal(config["goal"], default_strength)
        from .coordinator import build_coordinator
        self.coordinator = build_coordinator(
            str(config.get("coordinator", "hash")), self.dim,
            config.get("coordinator_config") or {})
        self.detector = StagnationDetector(window=self.stagnation_window,
                                           delta=self.stagnation_delta)
        self.eroder = Eroder(depth=self.erosion_depth,
                             anneal=self.erosion_anneal,
                             radius=self.erosion_radius)
        self._evaporate_task: Optional[asyncio.Task] = None
        self._dirty = False
        """脏标志：地形/迹有变更才需要落盘（蒸发 tick 去抖）。"""
        self._positions: Dict[str, tuple] = {}
        """当前水滴位置（WanterPlugin 维护；goal_complete 等消费方读取）。"""

    def _mark_dirty(self) -> None:
        self._dirty = True

    @property
    def goal(self):
        """主目标（第一个势阱中心；无目标为 None）。"""
        return self.terrain.goal

    def set_position(self, session_id: str, point: Any) -> None:
        """记录某会话水滴的当前位置（WanterPlugin 在每次移动后调用）。"""
        self._positions[session_id] = tuple(point)

    def get_position(self, session_id: str) -> Optional[tuple]:
        """取某会话水滴当前位置（无记录 → None，调用方自行回退）。"""
        return self._positions.get(session_id)

    def apply(self, ctx) -> None:
        ctx.set("wanter", self)
        self._restore()
        loop = asyncio.get_running_loop()
        self._evaporate_task = loop.create_task(self._evaporate_loop())

    # ---- 法则 ②：沉积 / 蒸发 ----

    def deposit(self, point: Any, weight: Optional[float] = None,
                at: Optional[float] = None) -> None:
        """水滴访问处沉积/淤积水迹（β 加权，权重可负）。"""
        self.trace.deposit(point, weight=(
            self.deposit_beta if weight is None else weight), at=at)
        self._mark_dirty()

    async def _evaporate_loop(self) -> None:
        """后台蒸发 tick：仅在变更时落盘（dirty 去抖）。"""
        try:
            while True:
                await asyncio.sleep(self.evaporate_interval)
                removed = self.trace.evaporate_to()
                if removed > 0:
                    self._mark_dirty()
                self._persist()
        except asyncio.CancelledError:
            raise

    # ---- 法则 ③：势能/流动 ----

    def phi(self, x: Any) -> float:
        return self.terrain.phi(x)

    def grad(self, x: Any) -> Any:
        return self.terrain.grad_phi(x)

    def softmin(self, candidates: Sequence[Any]) -> Dict[int, float]:
        return softmin_weights(candidates, self.terrain, tau=self.tau)

    def gradient_step(self, x: Any) -> Any:
        return gradient_step(x, self.terrain, lr=self.lr)

    def langevin_step(self, x: Any) -> Any:
        return langevin_step(x, self.terrain, lr=self.lr, D=self.D)

    def completed(self, x: Any) -> bool:
        """
        水滴是否到达任一目标势阱（法则 ③ 完成判据；多目标 = 子任务分解）。
        """
        nearest = self.terrain.nearest_goal(x)
        if nearest is None:
            return False
        return sum((xi - gi) ** 2 for xi, gi in zip(x, nearest)) \
            <= self.complete_radius ** 2

    # ---- 多目标（子任务分解） ----

    def add_goal(self, goal: Any, strength: float = 1.0) -> None:
        """新增目标势阱（子任务）。"""
        self.terrain.add_goal(goal, strength)
        self._mark_dirty()

    def remove_goal(self, goal: Any) -> bool:
        """移除目标势阱（子任务完成）。"""
        removed = self.terrain.remove_goal(goal)
        if removed:
            self._mark_dirty()
        return removed

    def nearest_goal(self, x: Any) -> Optional[Any]:
        """距 x 最近的目标势阱中心。"""
        return self.terrain.nearest_goal(x)

    async def embed(self, summary: str) -> tuple:
        """语义状态摘要 → 坐标（经 coordinator 缝；兼容同步/异步实现）。"""
        import asyncio
        result = self.coordinator.embed(summary)
        if asyncio.iscoroutine(result):
            result = await result
        return tuple(result)

    # ---- 法则 ④：驻留 + 侵蚀 ----

    def observe_energy(self, x: Any) -> None:
        """观测一次水滴位置（停滞检测基于位置漂移）。"""
        self.detector.observe(x)

    def stagnated(self) -> bool:
        return self.detector.stagnated()

    def report_and_maybe_erode(self, x: Any) -> Dict[str, Any]:
        """
        harness 侧每个 turn 结束时调用：观测位置 → 停滞则侵蚀。

        :return: {"stagnated": bool, "eroded": bool, "depth": float|None,
                  "energy": float}。
        """
        energy = self.phi(x)
        self.detector.observe(x)
        if self.detector.stagnated():
            depth = self.eroder.erode(self.terrain, x)
            self._mark_dirty()
            self.detector.reset()
            return {"stagnated": True, "eroded": True, "depth": depth,
                    "energy": energy}
        return {"stagnated": False, "eroded": False, "depth": None,
                "energy": energy}

    # ---- 持久化（全局地形跨会话共享） ----

    def _persist(self) -> None:
        """落盘（dirty 去抖：无变更直接返回）。"""
        if not self._dirty:
            return
        if self.ctx.has("storage"):
            self.ctx.storage.put(STORAGE_DOMAIN, "terrain",
                                 self.terrain.snapshot())
            self.ctx.storage.put(STORAGE_DOMAIN, "trace",
                                 self.trace.snapshot())
            self.ctx.storage.put(STORAGE_DOMAIN, "erosion_count",
                                 self.eroder.erosion_count)
        self._dirty = False

    def _restore(self) -> None:
        if not self.ctx.has("storage"):
            return
        terrain_snapshot = self.ctx.storage.get(STORAGE_DOMAIN, "terrain")
        trace_snapshot = self.ctx.storage.get(STORAGE_DOMAIN, "trace")
        if trace_snapshot is not None:
            self.trace = TraceField.from_snapshot(trace_snapshot)
            self.terrain.trace_field = self.trace
        if terrain_snapshot is not None:
            self.terrain = Terrain.from_snapshot(terrain_snapshot,
                                                 trace_field=self.trace)
        count = self.ctx.storage.get(STORAGE_DOMAIN, "erosion_count")
        if count is not None:
            self.eroder.erosion_count = int(count)

    def close(self) -> None:
        """关闭引擎（幂等：重复调用安全）。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if self._evaporate_task is not None:
            self._evaporate_task.cancel()
            self._evaporate_task = None
        removed = self.trace.evaporate_to()
        if removed > 0:
            self._mark_dirty()
        self._persist()
