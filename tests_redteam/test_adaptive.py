"""test_adaptive —— 自适应攻击地形：沉积/淤积、优先级排序、持久化、bench 指标。"""
from dsh.kernel import Context

from redteam.adaptive.terrain import AttackTerrain
from redteam.models import AttackSample, ConcreteSample, Severity


def _sample(sample_id: str, category: str, severity: str = "high",
            role: str = "student") -> ConcreteSample:
    base = AttackSample(id=sample_id, category=category, name=category,
                        severity=severity, surface="chat", role_context=[role])
    return ConcreteSample(uid=f"{sample_id}-{role}-v0", sample=base,
                          role=role, payload="p")


class FakeStorage:
    """内存地形存储（测试用）。"""

    def __init__(self):
        self.data = {}

    def save_terrain(self, domain, snapshot):
        self.data[domain] = snapshot

    def load_terrain(self, domain):
        return self.data.get(domain)


def _terrain(storage=None) -> AttackTerrain:
    ctx = Context("test-terrain")
    ctx.set("storage", storage or FakeStorage())
    terrain = AttackTerrain(ctx, {"domain": "lab:test"})
    terrain.apply(ctx)
    return terrain


def test_success_erodes_failed_raises():
    terrain = _terrain()
    good = _sample("a-001", "direct_injection")
    bad = _sample("b-001", "hallucination")
    terrain.record(good, "success")
    terrain.record(bad, "failed")
    assert terrain.phi(good) < terrain.phi(bad), \
        "成功向量应比失败向量更优先（地形更低）"


def test_priority_untried_first_then_phi():
    terrain = _terrain()
    tried_good = _sample("a-001", "direct_injection")
    tried_bad = _sample("b-001", "hallucination")
    untried = _sample("c-001", "xss")
    terrain.record(tried_good, "success")
    terrain.record(tried_bad, "failed")
    ordered = terrain.priority([tried_bad, untried, tried_good],
                               seen_uids=terrain.seen_uids())
    assert ordered[0].uid == untried.uid, "未尝试样本优先探索"
    assert ordered[1].uid == tried_good.uid, "已尝试样本按势能升序（成功优先）"
    assert ordered[2].uid == tried_bad.uid


def test_priority_severity_order_for_untried():
    terrain = _terrain()
    low = _sample("a-001", "xss", severity="low")
    critical = _sample("b-001", "sqli", severity="critical")
    ordered = terrain.priority([low, critical], seen_uids=set())
    assert ordered[0].uid == critical.uid, "未尝试样本按危害度降序"


def test_persistence_roundtrip(tmp_path):
    class FakeStorage:
        def __init__(self, path):
            self.path = path
            self.data = {}

        def save_terrain(self, domain, snapshot):
            self.data[domain] = snapshot

        def load_terrain(self, domain):
            return self.data.get(domain)

    storage = FakeStorage(str(tmp_path))
    terrain = _terrain(storage)
    sample = _sample("a-001", "direct_injection")
    terrain.record(sample, "success")
    terrain.save()
    assert "lab:test" in storage.data
    # 恢复：phi 一致（JSON 往返的浮点误差容忍 1e-6）
    ctx = Context("test-terrain-2")
    ctx.set("storage", storage)
    restored = AttackTerrain(ctx, {"domain": "lab:test"})
    restored.apply(ctx)
    assert abs(restored.phi(sample) - terrain.phi(sample)) < 1e-6


def test_domain_isolation():
    a = _terrain()
    ctx = Context("test-terrain")
    ctx.set("storage", FakeStorage())
    b = AttackTerrain(ctx, {"domain": "lab:shop-b"})
    b.apply(ctx)
    assert a.coord(_sample("x-001", "sqli")) != b.coord(_sample("x-001", "sqli")), \
        "不同 domain 的地形分区应隔离"


def test_coord_deterministic():
    a = _terrain()
    ctx = Context("test-terrain")
    ctx.set("storage", FakeStorage())
    b = AttackTerrain(ctx, {"domain": "lab:test"})
    b.apply(ctx)
    sample = _sample("x-001", "sqli")
    assert a.coord(sample) == b.coord(sample), "坐标投影必须确定性可复现"
