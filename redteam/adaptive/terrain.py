"""redteam.adaptive.terrain —— 自适应攻击地形（复用 dsh.wanter）。

原理（对齐 wanter 法则）：
- 攻击空间 = 攻击向量 × 角色，经哈希投影为连续坐标（确定性、可复现）；
- 水迹场 TraceField：攻击成功 → 正沉积（刻蚀河道，地形降低 → 下次优先）；
  攻击失败 → 负沉积（淤积抬高，系统避开）；
- 优先级 = 未尝试样本（按危害度）优先探索 + 已尝试样本按势能 Φ 升序
  （软最小选择：Φ 越低越优先）；
- 按"目标类型 × 业务域"分区（domain 隔离），跨目标泛化互不污染；
- 持久化到 SQLite（terrain_state），跨进程/跨扫描复现。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from dsh.kernel import Service
from dsh.wanter import HashCoordinator, Terrain, TraceField

from ..models import ConcreteSample, Severity

log = logging.getLogger("redteam.adaptive")

_SUCCESS_WEIGHT = 1.0
_FAIL_WEIGHT = -0.6
_SUSPICIOUS_WEIGHT = -0.2


def _stable_salt(domain: str) -> int:
    """域名 → 稳定盐值（hashlib，不受 PYTHONHASHSEED 影响）。"""
    digest = hashlib.sha256(domain.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class AttackTerrain(Service):
    """攻击地形服务（ctx.terrain）。"""

    provides = "terrain"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.domain = str((config or {}).get("domain", "default"))
        self.temperature = float((config or {}).get("temperature", 0.5))
        self.coordinator = HashCoordinator(dim=2, salt=_stable_salt(self.domain),
                                           scale=2.0)
        self.trace = TraceField(decay_rate=0.01, aggregate=True)
        self.terrain = Terrain(dim=2, sigma=0.8, trace_field=self.trace,
                               trace_alpha=1.0)
        self._seen: Set[str] = set()
        self._loaded = False

    def apply(self, ctx) -> None:
        ctx.set("terrain", self)
        self._load()

    # ---- 坐标 ----

    def coord(self, sample: ConcreteSample) -> tuple:
        return self.coordinator.embed(f"{sample.category}:{sample.sample.id}")

    def coord_of(self, category: str, sample_id: str) -> tuple:
        return self.coordinator.embed(f"{category}:{sample_id}")

    # ---- 记录与查询 ----

    def record(self, sample: ConcreteSample, verdict: str) -> None:
        """攻击结果沉积：success → 刻蚀（优先），failed → 淤积（避开）。"""
        weight = {"success": _SUCCESS_WEIGHT, "failed": _FAIL_WEIGHT,
                  "suspicious": _SUSPICIOUS_WEIGHT}.get(verdict, 0.0)
        if weight == 0.0:
            return
        self.trace.deposit(self.coord(sample), weight)
        self._seen.add(sample.uid)

    def seen_uids(self) -> Set[str]:
        """本次运行已记录过的样本 uid（跨进程场景回退为"全部未尝试"安全默认）。"""
        return set(self._seen)

    def priority(self, samples: Sequence[ConcreteSample],
                 seen_uids: Optional[Set[str]] = None) -> List[ConcreteSample]:
        """自适应优先级排序：
        1) 未尝试样本在前（按危害度降序，同危害按 uid 稳定序）；
        2) 已尝试样本按势能 Φ 升序（成功过的向量 Φ 更低 → 更优先）。
        """
        seen = seen_uids or set()
        untried = [s for s in samples if s.uid not in seen]
        tried = [s for s in samples if s.uid in seen]
        untried.sort(key=lambda s: (-Severity(s.sample.severity).rank, s.uid))
        tried.sort(key=lambda s: (self.terrain.phi(self.coord(s)), s.uid))
        return untried + tried

    def phi(self, sample: ConcreteSample) -> float:
        return self.terrain.phi(self.coord(sample))

    def stats(self) -> Dict[str, Any]:
        """地形统计：事件数 / 有效权重分布（bench 指标用）。"""
        weights = self.trace.weights_at()
        positives = [w for w in weights if w > 0]
        negatives = [w for w in weights if w < 0]
        return {"domain": self.domain, "events": len(weights),
                "success_traces": len(positives),
                "failed_traces": len(negatives),
                "net_depth": round(sum(weights), 3)}

    # ---- 持久化 ----

    def save(self) -> None:
        storage = self.ctx.storage
        snapshot = {"domain": self.domain,
                    "trace": self.trace.snapshot(),
                    "temperature": self.temperature}
        storage.save_terrain(self.domain, snapshot)

    def _load(self) -> None:
        if self._loaded:
            return
        storage = self.ctx.storage
        snapshot = storage.load_terrain(self.domain)
        if snapshot:
            self.trace = TraceField.from_snapshot(snapshot.get("trace") or {})
            self.terrain.trace_field = self.trace
            log.info("攻击地形已恢复：%s（%d 条历史沉积）", self.domain,
                     self.trace.event_count())
        self._loaded = True

    def reset(self) -> None:
        """清空地形（bench 对照组用）。"""
        self.trace = TraceField(decay_rate=0.01, aggregate=True)
        self.terrain.trace_field = self.trace
        self._seen.clear()
