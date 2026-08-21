"""test_scenarios —— 业务场景层：指纹识别与场景样本映射。"""
import os

from redteam.scenarios import (SCENARIOS, detect_scenario_endpoints,
                               detect_scenario_folder, detect_scenario_text,
                               get_scenario, sample_categories_for)


def test_scenario_catalog_complete():
    assert len(SCENARIOS) == 12
    ids = {s.id for s in SCENARIOS}
    assert {"ecommerce", "finance", "education", "saas", "social",
            "healthcare", "gaming", "delivery", "hr", "media", "membership",
            "government"} <= ids
    for scenario in SCENARIOS:
        assert scenario.sample_categories, f"{scenario.id} 缺少专属样本类别"


def test_detect_scenario_folder(tmp_path):
    ecommerce = tmp_path / "shop"
    ecommerce.mkdir()
    (ecommerce / "order_service.py").write_text("", encoding="utf-8")
    (ecommerce / "cart_api.py").write_text("", encoding="utf-8")
    (ecommerce / "refund_handler.py").write_text("", encoding="utf-8")
    assert detect_scenario_folder(str(ecommerce)) == "ecommerce"

    edu = tmp_path / "edu"
    edu.mkdir()
    (edu / "exam_service.py").write_text("", encoding="utf-8")
    (edu / "score_repo.py").write_text("", encoding="utf-8")
    assert detect_scenario_folder(str(edu)) == "education"

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.md").write_text("", encoding="utf-8")
    assert detect_scenario_folder(str(empty)) is None


def test_detect_scenario_endpoints():
    assert detect_scenario_endpoints(
        {"/api/checkout", "/api/cart", "/api/coupons"}) == "ecommerce"
    assert detect_scenario_endpoints(
        {"/api/wallet", "/api/transfer", "/api/withdraw"}) == "finance"
    assert detect_scenario_endpoints({"/api/health", "/api/login"}) is None


def test_detect_scenario_text():
    assert detect_scenario_text("订单管理 购物车 退款 优惠券 秒杀系统") == "ecommerce"
    assert detect_scenario_text("考试 成绩 题库 答题系统") == "education"
    assert detect_scenario_text("普通说明文档") is None


def test_sample_categories_for():
    cats = sample_categories_for("ecommerce")
    assert "ecom_price_tamper" in cats and "ecom_dup_refund" in cats
    multi = sample_categories_for(["ecommerce", "finance"])
    assert "ecom_price_tamper" in multi and "fin_negative_transfer" in multi
    assert sample_categories_for(None) == []
    assert sample_categories_for("unknown") == []


def test_get_scenario():
    scenario = get_scenario("saas")
    assert scenario.name == "SaaS/多租户"
    assert "saas_tenant_isolation" in scenario.sample_categories
