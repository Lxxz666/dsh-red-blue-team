"""test_vectors —— 样本库加载、确定性变体展开、模板渲染。"""
from dsh.kernel import Context

from redteam.models import render_template
from redteam.vectors.registry import VectorRegistry, _expand_filler
from target_lab import planted_categories


def _registry() -> VectorRegistry:
    ctx = Context("test-vectors")
    registry = VectorRegistry(ctx, {})
    registry.apply(ctx)
    return registry


def test_bank_loads_all_categories():
    registry = _registry()
    assert len(registry.samples) >= 60, "基础样本数不足（含 12 大业务场景）"
    planted = planted_categories()
    missing = planted - set(registry.categories())
    assert not missing, f"埋入漏洞类别缺少攻击样本: {missing}"
    assert "all" not in registry.categories()
    # 场景专属样本类别齐备
    from redteam.scenarios import SCENARIOS
    for scenario in SCENARIOS:
        for category in scenario.sample_categories:
            assert category in registry.categories(), \
                f"场景 {scenario.id} 的样本类别 {category} 未在样本库中"


def test_expansion_deterministic():
    registry = _registry()
    roles = ["student", "customer", "admin"]
    first = registry.samples_for(roles, ["all"], variants_per_sample=2)
    second = registry.samples_for(roles, ["all"], variants_per_sample=2)
    assert [s.uid for s in first] == [s.uid for s in second]
    assert [s.payload for s in first] == [s.payload for s in second]


def test_variant_budget_respected():
    registry = _registry()
    samples = registry.samples_for(["student", "customer", "admin"],
                                   ["direct_injection"], variants_per_sample=2)
    # di-001/di-002 两个基础样本 × 2 角色 × 2 变体 = 8
    # + di-001 静态攻击链 1 条 × 2 角色 = 2 → 共 10
    assert len(samples) == 8 + 2


def test_variant_uid_stable_and_rebuildable():
    registry = _registry()
    samples = registry.samples_for(["student"], ["sqli"], variants_per_sample=1)
    assert samples, "sqli 样本应存在"
    concrete = samples[0]
    assert concrete.uid == "sqli-001-student-v0"
    rebuilt = registry.concrete_for_uid(concrete.uid)
    assert rebuilt is not None
    assert rebuilt.payload == concrete.payload
    assert rebuilt.body == concrete.body


def test_payload_slot_flows_into_params_and_body():
    registry = _registry()
    samples = registry.samples_for(["student"], ["sqli", "xss"],
                                   variants_per_sample=3)
    sqli = [s for s in samples if s.category == "sqli"]
    assert len(sqli) == 3, "3 个密码变体"
    assert all("{payload}" not in s.body["password"] for s in sqli)
    assert all(s.body["password"] == s.payload for s in sqli)
    xss = [s for s in samples if s.category == "xss"]
    assert len(xss) == 2, "2 个 XSS 载荷变体"
    assert all(s.params["q"] == s.payload for s in xss)


def test_template_render_preserves_unknown_braces():
    assert render_template("{{7*7}}", {}) == "{{7*7}}"
    assert render_template("${7*7}", {}) == "${7*7}"
    assert render_template("a {x} b {unknown}", {"x": "1"}) == "a 1 b {unknown}"


def test_filler_expansion():
    assert _expand_filler("__filler(10)__") == "A" * 10
    assert _expand_filler("plain") == "plain"


def test_negative_control_samples_present():
    registry = _registry()
    negatives = [s for s in registry.samples if "negative" in s.tags]
    assert {s.category for s in negatives} >= {"hallucination", "model_dos",
                                               "graphql", "http_smuggling",
                                               "directory_listing"}


def test_repeat_and_stateful_fields():
    registry = _registry()
    dup = registry.sample_by_id("ecom-005")
    assert dup is not None
    assert dup.repeat == 2
    assert dup.stateful is True
