"""test_lab_scenarios —— 靶场业务场景 API（电商/教育/金融/SaaS）漏洞行为。"""
from target_lab.app import LabApp
from target_lab.guards import HARDENED_GUARDS


def _api(app: LabApp, method: str, path: str, params=None, body=None,
         role: str = "customer"):
    return app.handle("api", method=method, path=path, params=params or {},
                      body=body or {}, payload="",
                      headers={"x-role": role})


def test_ecommerce_price_tamper():
    app = LabApp()
    result = _api(app, "POST", "/api/checkout",
                  body={"cart_id": "C-1001", "amount": "1"})
    assert "已按提交金额结算" in result["text"]
    app.guards.price_server_side = True
    result = _api(app, "POST", "/api/checkout",
                  body={"cart_id": "C-1001", "amount": "1"})
    assert "已按服务端价格结算" in result["text"]
    assert "已按提交金额结算" not in result["text"]


def test_ecommerce_coupon_stacking():
    app = LabApp()
    result = _api(app, "POST", "/api/coupons/apply",
                  body={"codes": ["FULL-50", "FULL-30"]})
    assert "优惠已叠加" in result["text"]
    app.guards.coupon_stacking = True
    result = _api(app, "POST", "/api/coupons/apply",
                  body={"codes": ["FULL-50", "FULL-30"]})
    assert "优惠已叠加" not in result["text"]


def test_ecommerce_order_state_machine():
    app = LabApp()
    # 订单 1002 是待发货：直接跳"已完成"是非法流转
    result = _api(app, "POST", "/api/orders/1002/status",
                  body={"status": "已完成"})
    assert "订单状态已更新" in result["text"]
    app.guards.order_state_machine = True
    result = _api(app, "POST", "/api/orders/1002/status",
                  body={"status": "已完成"})
    assert "非法状态流转" in result["text"]


def test_ecommerce_pay_callback_forgery():
    app = LabApp()
    result = _api(app, "POST", "/api/pay/callback",
                  body={"order_id": "1001", "status": "success"})
    assert "回调已处理" in result["text"]
    app.guards.pay_callback_verify = True
    result = _api(app, "POST", "/api/pay/callback",
                  body={"order_id": "1001", "status": "success"})
    assert "回调验签失败" in result["text"]


def test_ecommerce_dup_refund():
    app = LabApp()
    _api(app, "POST", "/api/orders/1001/refund", body={"amount": "299"})
    result = _api(app, "POST", "/api/orders/1001/refund",
                  body={"amount": "299"})
    assert "再次退款已受理" in result["text"]
    app.guards.refund_idempotency = True
    result = _api(app, "POST", "/api/orders/1001/refund",
                  body={"amount": "299"})
    assert "重复退款被拦截" in result["text"]


def test_education_score_idor():
    app = LabApp()
    result = _api(app, "GET", "/api/scores/1", role="student")
    assert result["status"] == 200 and "张三" in result["text"]
    app.guards.score_scope_check = True
    result = _api(app, "GET", "/api/scores/1", role="student")
    assert result["status"] == 403


def test_education_answer_leak():
    app = LabApp()
    result = _api(app, "GET", "/api/exams/101/answers")
    assert "标准答案" in result["text"]
    app.guards.answer_leak_guard = True
    result = _api(app, "GET", "/api/exams/101/answers")
    assert result["status"] == 403


def test_education_score_tamper():
    app = LabApp()
    result = _api(app, "POST", "/api/exams/101/submit", body={"score": "100"})
    assert "客户端提交" in result["text"]
    app.guards.score_server_grade = True
    result = _api(app, "POST", "/api/exams/101/submit", body={"score": "100"})
    assert "服务端判分" in result["text"]


def test_education_exam_time():
    app = LabApp()
    result = _api(app, "POST", "/api/exams/101/time",
                  body={"minutes": "999"})
    assert "考试时间已延长" in result["text"]
    app.guards.exam_time_check = True
    result = _api(app, "POST", "/api/exams/101/time",
                  body={"minutes": "999"})
    assert "服务端控制" in result["text"]


def test_finance_negative_transfer():
    app = LabApp()
    result = _api(app, "POST", "/api/transfer",
                  body={"to": "u-2", "amount": "-100"})
    assert "转账已受理" in result["text"]
    app.guards.amount_validation = True
    result = _api(app, "POST", "/api/transfer",
                  body={"to": "u-2", "amount": "-100"})
    assert "金额必须为正数" in result["text"]


def test_finance_overdraw():
    app = LabApp()
    result = _api(app, "POST", "/api/withdraw", body={"amount": "100000"})
    assert "提现已受理" in result["text"]
    app.guards.withdraw_limit_check = True
    result = _api(app, "POST", "/api/withdraw", body={"amount": "100000"})
    assert "余额不足" in result["text"]


def test_finance_balance_tamper():
    app = LabApp()
    result = _api(app, "POST", "/api/wallet/balance", body={"value": "999"})
    assert "余额已更新" in result["text"]
    app.guards.balance_server_side = True
    result = _api(app, "POST", "/api/wallet/balance", body={"value": "999"})
    assert "不可由客户端修改" in result["text"]


def test_saas_tenant_isolation():
    app = LabApp()
    result = _api(app, "GET", "/api/tenants/t-2/data", role="customer")
    assert result["status"] == 200 and '"tenant_id": "t-2"' in result["text"]
    app.guards.tenant_isolation = True
    result = _api(app, "GET", "/api/tenants/t-2/data", role="customer")
    assert result["status"] == 403


def test_saas_plan_downgrade():
    app = LabApp()
    result = _api(app, "POST", "/api/billing/plan", body={"plan": "basic"})
    assert "高级功能保留" in result["text"]
    app.guards.plan_enforcement = True
    result = _api(app, "POST", "/api/billing/plan", body={"plan": "basic"})
    assert "高级功能已回收" in result["text"]


def test_meta_reports_scenarios():
    app = LabApp()
    result = app.handle("api", method="GET", path="/api/meta/business",
                        params={}, body={}, payload="", headers={})
    scenarios = result["json"]["scenarios"]
    assert {"ecommerce", "education", "finance", "saas"} <= set(scenarios)


def test_hardened_covers_scenario_guards():
    assert HARDENED_GUARDS.price_server_side is True
    assert HARDENED_GUARDS.tenant_isolation is True
    assert HARDENED_GUARDS.score_scope_check is True
    assert HARDENED_GUARDS.amount_validation is True
    assert HARDENED_GUARDS.refund_idempotency is True
