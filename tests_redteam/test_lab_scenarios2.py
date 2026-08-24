"""test_lab_scenarios2 —— 靶场 8 大业务场景端点（社交/医疗/游戏/外卖/招聘/直播/会员/政务）。"""
from target_lab.app import LabApp
from target_lab.guards import HARDENED_GUARDS


def _api(app: LabApp, method: str, path: str, params=None, body=None,
         role: str = "customer"):
    return app.handle("api", method=method, path=path, params=params or {},
                      body=body or {}, payload="",
                      headers={"x-role": role})


def test_social_post_idor_and_moderation():
    app = LabApp()
    # 私密帖子越权
    result = _api(app, "GET", "/api/posts/2")
    assert result["status"] == 200 and '"visibility": "private"' in result["text"]
    app.guards.post_scope_check = True
    result = _api(app, "GET", "/api/posts/2")
    assert result["status"] == 403
    # 审核绕过
    app2 = LabApp()
    result = _api(app2, "POST", "/api/posts",
                  body={"content": "违禁内容变体 违法词_1"})
    assert "发布成功" in result["text"]
    app2.guards.moderation_filter = True
    result = _api(app2, "POST", "/api/posts",
                  body={"content": "违禁内容变体 违法词_1"})
    assert "已拦截" in result["text"] and "发布成功" not in result["text"]


def test_healthcare_record_idor_and_appointment_lock():
    app = LabApp()
    result = _api(app, "GET", "/api/records/2")
    assert result["status"] == 200 and "diagnosis" in result["text"]
    app.guards.record_scope_check = True
    assert _api(app, "GET", "/api/records/2")["status"] == 403
    # 号源锁（09:00 预占）
    app2 = LabApp()
    result = _api(app2, "POST", "/api/appointments",
                  body={"doctor_id": "d-1", "slot": "09:00"})
    assert "预约成功" in result["text"]
    app2.guards.appointment_lock = True
    result = _api(app2, "POST", "/api/appointments",
                  body={"doctor_id": "d-1", "slot": "09:00"})
    assert "号源已被占用" in result["text"]


def test_gaming_currency_and_reward():
    app = LabApp()
    result = _api(app, "POST", "/api/wallet/coins", body={"coins": "999999"})
    assert "金币已更新" in result["text"]
    app.guards.game_currency_server_side = True
    result = _api(app, "POST", "/api/wallet/coins", body={"coins": "999999"})
    assert "不可由客户端修改" in result["text"]
    # 道具重复领取
    app2 = LabApp()
    assert "领取成功" in _api(app2, "POST", "/api/rewards/claim",
                              body={"reward_id": "r-1"})["text"]
    app2.guards.reward_idempotency = True
    result = _api(app2, "POST", "/api/rewards/claim",
                  body={"reward_id": "r-1"})
    assert "已领取过" in result["text"] and "领取成功" not in result["text"]


def test_delivery_fee_and_confirm():
    app = LabApp()
    result = _api(app, "POST", "/api/orders/quote",
                  body={"distance_km": "0.1", "fee": "0"})
    assert "运费已更新为 0" in result["text"]
    app.guards.fee_server_side = True
    result = _api(app, "POST", "/api/orders/quote",
                  body={"distance_km": "0.1", "fee": "0"})
    assert "服务端计价" in result["text"]
    # 送达确认绕过
    app2 = LabApp()
    assert "确认成功" in _api(app2, "POST", "/api/orders/confirm",
                              body={"order_id": "1"})["text"]
    app2.guards.delivery_confirm_check = True
    result = _api(app2, "POST", "/api/orders/confirm",
                  body={"order_id": "1"})
    assert "骑手凭证" in result["text"] and "确认成功" not in result["text"]


def test_hr_resume_and_interview():
    app = LabApp()
    result = _api(app, "GET", "/api/resumes/2")
    assert result["status"] == 200 and '"phone"' in result["text"]
    app.guards.resume_scope_check = True
    assert _api(app, "GET", "/api/resumes/2")["status"] == 403
    # 面试跳步
    app2 = LabApp()
    result = _api(app2, "POST", "/api/interviews/schedule",
                  body={"candidate_id": "1", "stage": "final"})
    assert "已安排 final 面试" in result["text"]
    app2.guards.interview_workflow = True
    result = _api(app2, "POST", "/api/interviews/schedule",
                  body={"candidate_id": "1", "stage": "final"})
    assert "流程未到该阶段" in result["text"]


def test_media_gift_and_paywall():
    app = LabApp()
    result = _api(app, "POST", "/api/live/gift",
                  body={"streamer_id": "s-1", "gift_id": "g-1",
                        "price": "-999"})
    assert "打赏成功，金额 -999" in result["text"]
    app.guards.gift_price_server_side = True
    result = _api(app, "POST", "/api/live/gift",
                  body={"streamer_id": "s-1", "gift_id": "g-1",
                        "price": "-999"})
    assert "服务端为准" in result["text"] and "打赏成功" not in result["text"]
    # 付费墙
    app2 = LabApp()
    result = _api(app2, "GET", "/api/videos/v-9/stream")
    assert result["status"] == 200 and '"stream_url"' in result["text"]
    app2.guards.paywall_check = True
    assert _api(app2, "GET", "/api/videos/v-9/stream")["status"] == 403


def test_membership_subscription_and_points():
    app = LabApp()
    result = _api(app, "POST", "/api/subscription/renew",
                  body={"trial": "true"})
    assert "试用已重置" in result["text"]
    app.guards.subscription_server_side = True
    result = _api(app, "POST", "/api/subscription/renew",
                  body={"trial": "true"})
    assert "试用次数已用完" in result["text"]
    # 积分刷量
    app2 = LabApp()
    assert "积分已发放" in _api(app2, "POST", "/api/points/task",
                                body={"task_id": "t-1"})["text"]
    app2.guards.points_idempotency = True
    result = _api(app2, "POST", "/api/points/task", body={"task_id": "t-1"})
    assert "已领取过" in result["text"] and "积分已发放" not in result["text"]


def test_government_workflow_and_citizen():
    app = LabApp()
    result = _api(app, "POST", "/api/cases/1/finish")
    assert "已办结" in result["text"]
    app.guards.workflow_state_machine = True
    result = _api(app, "POST", "/api/cases/1/finish")
    assert "流程未完成" in result["text"]
    # 公民信息越权
    app2 = LabApp()
    result = _api(app2, "GET", "/api/citizens/2")
    assert result["status"] == 200 and '"id_number"' in result["text"]
    app2.guards.citizen_scope_check = True
    assert _api(app2, "GET", "/api/citizens/2")["status"] == 403


def test_meta_reports_all_12_scenarios():
    app = LabApp()
    result = app.handle("api", method="GET", path="/api/meta/business",
                        params={}, body={}, payload="", headers={})
    scenarios = set(result["json"]["scenarios"])
    assert len(scenarios) == 12


def test_hardened_covers_all_new_guards():
    for key in ("post_scope_check", "moderation_filter", "record_scope_check",
                "appointment_lock", "game_currency_server_side",
                "reward_idempotency", "fee_server_side",
                "delivery_confirm_check", "resume_scope_check",
                "interview_workflow", "gift_price_server_side",
                "paywall_check", "subscription_server_side",
                "points_idempotency", "workflow_state_machine",
                "citizen_scope_check"):
        assert getattr(HARDENED_GUARDS, key) is True, f"{key} 应加固"
