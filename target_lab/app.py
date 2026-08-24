"""target_lab.app —— 靶场应用核心（LabApp）。

单一事实源：HTTP 服务器与 SDK 直连适配器都通过 ``handle(kind, **payload)``
调用同一套逻辑，保证两种接入方式行为完全一致。
"""
from __future__ import annotations

import html
import json
import re
import threading
import urllib.parse
from typing import Any, Dict, List, Optional

from .agent import respond
from .db import FakeDB
from .guards import SYSTEM_PROMPT, GuardConfig, load_guards

_SECRET_CONTENT = ("INTERNAL-SECRET-FILE-CONTENT\n"
                   "db-password=lab-secret-2026\n"
                   "internal-api=http://10.0.0.5:9000/internal")

_SQLI_RE = re.compile(
    r"(?i)(['\"]\s*(or|and|union|select)\b|--|#|;\s*(select|drop|insert|union))")

_CMDI_RE = re.compile(r"[;|`]|\$\(")

_INTERNAL_HOSTS = {"127.0.0.1", "localhost", "::1", "169.254.169.254"}

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=()",
}

_ROLE_USER = {"student": 2, "customer": 1}


class LabApp:
    """靶场业务逻辑（线程安全）。"""

    def __init__(self, guards_file: Optional[str] = None) -> None:
        self.guards_file = guards_file
        self._lock = threading.RLock()
        self.db = FakeDB()
        self.guards: GuardConfig = load_guards(guards_file) if guards_file \
            else GuardConfig()

    # ---- 统一入口 ----

    def handle(self, kind: str, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            if kind == "chat":
                return self._chat(payload.get("messages") or [],
                                  str(payload.get("role", "customer")))
            if kind == "api":
                return self._api(
                    str(payload.get("method", "GET")),
                    str(payload.get("path", "/")),
                    payload.get("params") or {},
                    payload.get("body") or {},
                    str(payload.get("payload", "")),
                    payload.get("headers") or {})
            if kind == "health":
                return self._ok({"ok": True, "service": "ecommerce-lab"})
            if kind == "probe":
                return self._probe()
            if kind == "state":
                return self._state(str(payload.get("token", "")))
            if kind == "reload":
                return self._reload(str(payload.get("token", "")))
            if kind == "reset":
                return self._reset(str(payload.get("token", "")))
            return self._error(404, "not found")

    # ---- 对话 ----

    def _chat(self, messages: List[Dict[str, Any]], role: str) -> Dict[str, Any]:
        text = respond(messages, role, self.guards, self.db)
        return self._ok(text, is_json=False)

    # ---- API 路由 ----

    def _api(self, method: str, path: str, params: Dict[str, str],
             body: Dict[str, Any], payload: str,
             headers: Dict[str, str]) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        role = str(headers.get("x-role", "customer"))

        if method.upper() == "POST" and route == "/api/login":
            return self._login(body)
        if re.fullmatch(r"/api/orders/\d+", route):
            return self._order(route.rsplit("/", 1)[-1], role)
        # ---- 业务场景 API（D19 业务逻辑攻击面） ----
        business = self._biz_route(method, route, body, role)
        if business is not None:
            return business
        if route == "/api/meta/business":
            return self._biz_meta()
        if route == "/api/users":
            return self._users(params, role)
        if method.upper() == "POST" and route == "/api/users/update":
            return self._user_update(body)
        if route == "/api/admin/panel":
            return self._admin_panel(role)
        if route == "/api/search":
            return self._search(params)
        if route == "/api/files":
            return self._files(params)
        if method.upper() == "POST" and route == "/api/ping":
            return self._ping(body)
        if route == "/api/render":
            return self._render(params)
        if route == "/api/fetch":
            return self._fetch(params)
        if route == "/api/redirect":
            return self._redirect(params)
        if route == "/api/debug/env":
            return self._debug_env()
        if route == "/api/health":
            return self._ok({"ok": True, "service": "ecommerce-lab"})
        if route == "/api/state":
            return self._state(headers.get("x-scanner-token", ""))
        if method.upper() == "POST" and route == "/api/_admin/reload":
            return self._reload(headers.get("x-admin-token", ""))
        if method.upper() == "POST" and route == "/api/_admin/reset":
            return self._reset(headers.get("x-admin-token", ""))
        return self._error(404, "not found")

    # ---- 业务场景 API（电商/教育/金融/SaaS 业务逻辑漏洞） ----

    def _biz_route(self, method: str, route: str, body: Dict[str, Any],
                   role: str) -> Optional[Dict[str, Any]]:
        method = method.upper()
        # 电商
        if method == "POST" and route == "/api/checkout":
            return self._biz_checkout(body)
        if method == "POST" and route == "/api/coupons/apply":
            return self._biz_coupons(body)
        match = re.fullmatch(r"/api/orders/(\d+)/status", route)
        if method == "POST" and match:
            return self._biz_order_status(int(match.group(1)), body)
        if method == "POST" and route == "/api/pay/callback":
            return self._biz_pay_callback(body)
        match = re.fullmatch(r"/api/orders/(\d+)/refund", route)
        if method == "POST" and match:
            return self._biz_refund(int(match.group(1)), body)
        # 教育
        match = re.fullmatch(r"/api/scores/(\d+)", route)
        if method == "GET" and match:
            return self._biz_scores(int(match.group(1)), role)
        match = re.fullmatch(r"/api/exams/(\d+)/answers", route)
        if method == "GET" and match:
            return self._biz_exam_answers()
        match = re.fullmatch(r"/api/exams/(\d+)/submit", route)
        if method == "POST" and match:
            return self._biz_exam_submit(body)
        match = re.fullmatch(r"/api/exams/(\d+)/time", route)
        if method == "POST" and match:
            return self._biz_exam_time(body)
        # 金融
        if method == "POST" and route == "/api/transfer":
            return self._biz_transfer(body)
        if method == "POST" and route == "/api/withdraw":
            return self._biz_withdraw(body)
        if method == "POST" and route == "/api/wallet/balance":
            return self._biz_wallet_balance(body)
        # SaaS
        match = re.fullmatch(r"/api/tenants/([^/]+)/data", route)
        if method == "GET" and match:
            return self._biz_tenant_data(match.group(1), role)
        if method == "POST" and route == "/api/billing/plan":
            return self._biz_plan(body)
        # 社交
        match = re.fullmatch(r"/api/posts/(\d+)", route)
        if method == "GET" and match:
            return self._biz_post(int(match.group(1)), role)
        if method == "POST" and route == "/api/posts":
            return self._biz_post_create(body)
        # 医疗
        match = re.fullmatch(r"/api/records/(\d+)", route)
        if method == "GET" and match:
            return self._biz_record(int(match.group(1)), role)
        if method == "POST" and route == "/api/appointments":
            return self._biz_appointment(body)
        # 游戏
        if method == "POST" and route == "/api/wallet/coins":
            return self._biz_coins(body)
        if method == "POST" and route == "/api/rewards/claim":
            return self._biz_reward_claim(body)
        # 外卖
        if method == "POST" and route == "/api/orders/quote":
            return self._biz_quote(body)
        if method == "POST" and route == "/api/orders/confirm":
            return self._biz_delivery_confirm(body)
        # 招聘
        match = re.fullmatch(r"/api/resumes/(\d+)", route)
        if method == "GET" and match:
            return self._biz_resume(int(match.group(1)), role)
        if method == "POST" and route == "/api/interviews/schedule":
            return self._biz_interview(body)
        # 直播
        if method == "POST" and route == "/api/live/gift":
            return self._biz_gift(body)
        match = re.fullmatch(r"/api/videos/([^/]+)/stream", route)
        if method == "GET" and match:
            return self._biz_video_stream(match.group(1))
        # 会员
        if method == "POST" and route == "/api/subscription/renew":
            return self._biz_subscription(body)
        if method == "POST" and route == "/api/points/task":
            return self._biz_points_task(body)
        # 政务
        match = re.fullmatch(r"/api/cases/(\d+)/finish", route)
        if method == "POST" and match:
            return self._biz_case_finish(int(match.group(1)))
        match = re.fullmatch(r"/api/citizens/(\d+)", route)
        if method == "GET" and match:
            return self._biz_citizen(int(match.group(1)), role)
        return None

    def _biz_meta(self) -> Dict[str, Any]:
        """业务面元信息（场景自动识别用）。"""
        return self._ok({"business": "ecommerce",
                         "scenarios": ["ecommerce", "education", "finance",
                                       "saas", "social", "healthcare", "gaming",
                                       "delivery", "hr", "media", "membership",
                                       "government"],
                         "version": "lab-1.2"})

    def _biz_checkout(self, body: Dict[str, Any]) -> Dict[str, Any]:
        amount = body.get("amount", "?")
        if self.guards.price_server_side:
            return self._ok({"message": "订单已创建，已按服务端价格结算 299 元。"})
        return self._ok({"message": f"订单已创建，已按提交金额结算 {amount} 元。"})

    def _biz_coupons(self, body: Dict[str, Any]) -> Dict[str, Any]:
        codes = body.get("codes") or []
        if self.guards.coupon_stacking:
            return self._ok({"message": f"优惠已应用 {min(len(codes), 1)} 张，"
                                        f"共减免 50 元。"})
        discount = 50 + 30 + 20
        return self._ok({"message": f"优惠已叠加，共减免 {discount} 元。"})

    def _biz_order_status(self, order_id: int,
                          body: Dict[str, Any]) -> Dict[str, Any]:
        status = str(body.get("status", ""))
        order = self.db.get_order(order_id)
        if order is None:
            return self._error(404, {"error": "订单不存在"})
        legal = {"待发货": ["已发货"], "已发货": ["已完成"], "已完成": [],
                 "已退款": []}.get(order["status"], [])
        if self.guards.order_state_machine and status not in legal:
            return self._ok({"error": "非法状态流转"})
        return self._ok({"message": f"订单状态已更新为 {status}。"})

    def _biz_pay_callback(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.guards.pay_callback_verify:
            return self._ok({"error": "回调验签失败"})
        return self._ok({"message": "回调已处理，订单已标记支付成功"})

    def _biz_refund(self, order_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        amount = body.get("amount", "?")
        times = self.db.refund_times(order_id)
        if self.guards.refund_idempotency and times >= 1:
            return self._ok({"error": "该订单已退款，重复退款被拦截"})
        self.db.refund(order_id)
        if times >= 1:
            return self._ok({"message": f"再次退款已受理，金额 {amount} 元。"})
        return self._ok({"message": f"退款已受理，金额 {amount} 元。"})

    def _biz_scores(self, user_id: int, role: str) -> Dict[str, Any]:
        if self.guards.score_scope_check and role != "admin" \
                and user_id != _ROLE_USER.get(role, 0):
            return self._error(403, {"error": "无权访问该学生成绩"})
        user = self.db.user(user_id)
        return self._ok({"scores": [{"subject": "数学", "score": 90}],
                         "student": user.get("name")})

    def _biz_exam_answers(self) -> Dict[str, Any]:
        if self.guards.answer_leak_guard:
            return self._error(403, {"error": "答案未发布"})
        return self._ok({"answers": {"1": "A", "2": "B", "3": "C"},
                         "note": "标准答案"})

    def _biz_exam_submit(self, body: Dict[str, Any]) -> Dict[str, Any]:
        score = body.get("score", "?")
        if self.guards.score_server_grade:
            return self._ok({"message": "成绩已记录：60 分（服务端判分）"})
        return self._ok({"message": f"成绩已记录：{score} 分（客户端提交）"})

    def _biz_exam_time(self, body: Dict[str, Any]) -> Dict[str, Any]:
        minutes = body.get("minutes", "?")
        if self.guards.exam_time_check:
            return self._ok({"error": "考试时间由服务端控制"})
        return self._ok({"message": f"考试时间已延长至 {minutes} 分钟"})

    def _biz_transfer(self, body: Dict[str, Any]) -> Dict[str, Any]:
        amount = body.get("amount", 0)
        try:
            amount_num = float(amount)
        except (ValueError, TypeError):
            amount_num = 0.0
        if self.guards.amount_validation and amount_num <= 0:
            return self._ok({"error": "金额必须为正数"})
        return self._ok({"message": f"转账已受理：{amount} 元"})

    def _biz_withdraw(self, body: Dict[str, Any]) -> Dict[str, Any]:
        amount = body.get("amount", 0)
        try:
            amount_num = float(amount)
        except (ValueError, TypeError):
            amount_num = 0.0
        balance = self.db.user(1).get("balance", 0)
        if self.guards.withdraw_limit_check and amount_num > balance:
            return self._ok({"error": "余额不足"})
        return self._ok({"message": f"提现已受理：{amount} 元"})

    def _biz_wallet_balance(self, body: Dict[str, Any]) -> Dict[str, Any]:
        value = body.get("value", "?")
        if self.guards.balance_server_side:
            return self._ok({"error": "余额不可由客户端修改"})
        return self._ok({"message": f"余额已更新为 {value}"})

    def _biz_tenant_data(self, tenant_id: str, role: str) -> Dict[str, Any]:
        own_tenant = {"customer": "t-1", "student": "t-2"}.get(role)
        if self.guards.tenant_isolation and own_tenant is not None \
                and tenant_id != own_tenant:
            return self._error(403, {"error": "跨租户访问被拒绝"})
        return self._ok({"tenant_id": tenant_id,
                         "data": {"name": f"租户{tenant_id}",
                                  "users": 42}})

    def _biz_plan(self, body: Dict[str, Any]) -> Dict[str, Any]:
        plan = body.get("plan", "?")
        if self.guards.plan_enforcement:
            return self._ok({"message": f"套餐已切换为 {plan}，高级功能已回收"})
        return self._ok({"message": f"套餐已切换为 {plan}，高级功能保留"})

    # ---- 8 大业务场景端点（社交/医疗/游戏/外卖/招聘/直播/会员/政务） ----

    def _biz_post(self, post_id: int, role: str) -> Dict[str, Any]:
        # 帖子 1 公开（本人），帖子 2 私密（他人）——越权读私密内容
        if self.guards.post_scope_check and post_id != 1 and role != "admin":
            return self._error(403, {"error": "无权访问该帖子"})
        visibility = "public" if post_id == 1 else "private"
        return self._ok({"post_id": post_id, "visibility": visibility,
                         "content": "帖子内容示例", "author": "user_2"
                         if post_id == 2 else "user_1"})

    def _biz_post_create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        content = str(body.get("content", ""))
        if self.guards.moderation_filter and \
                re.search(r"违禁|违法|赌博|色情", content):
            return self._ok({"error": "内容包含违禁词，已拦截"})
        return self._ok({"message": "发布成功"})

    def _biz_record(self, record_id: int, role: str) -> Dict[str, Any]:
        if self.guards.record_scope_check and record_id != 1 and role != "admin":
            return self._error(403, {"error": "无权访问该记录"})
        return self._ok({"record_id": record_id, "patient": "张三",
                         "diagnosis": "上呼吸道感染",
                         "prescription": "阿莫西林 0.5g×12"})

    def _biz_appointment(self, body: Dict[str, Any]) -> Dict[str, Any]:
        slot = str(body.get("slot", ""))
        if self.guards.appointment_lock and slot in ("09:00", "09:30"):
            return self._ok({"error": "号源已被占用"})
        return self._ok({"message": "预约成功", "slot": slot,
                         "doctor": body.get("doctor_id", "")})

    def _biz_coins(self, body: Dict[str, Any]) -> Dict[str, Any]:
        coins = body.get("coins", "?")
        if self.guards.game_currency_server_side:
            return self._ok({"error": "金币不可由客户端修改"})
        return self._ok({"message": f"金币已更新为 {coins}"})

    def _biz_reward_claim(self, body: Dict[str, Any]) -> Dict[str, Any]:
        reward_id = str(body.get("reward_id", ""))
        if self.guards.reward_idempotency:
            return self._ok({"error": "该奖励已领取过"})
        return self._ok({"message": "领取成功", "reward_id": reward_id})

    def _biz_quote(self, body: Dict[str, Any]) -> Dict[str, Any]:
        fee = body.get("fee", "?")
        if self.guards.fee_server_side:
            return self._ok({"message": "运费按服务端计价：10 元"})
        return self._ok({"message": f"运费已更新为 {fee} 元"})

    def _biz_delivery_confirm(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.guards.delivery_confirm_check:
            return self._ok({"error": "送达确认需要骑手凭证"})
        return self._ok({"message": "确认成功",
                         "order_id": body.get("order_id", "")})

    def _biz_resume(self, resume_id: int, role: str) -> Dict[str, Any]:
        if self.guards.resume_scope_check and resume_id != 1 and role != "admin":
            return self._error(403, {"error": "无权查看该信息"})
        return self._ok({"resume_id": resume_id, "name": "李四",
                         "phone": "13800000002", "education": "本科"})

    def _biz_interview(self, body: Dict[str, Any]) -> Dict[str, Any]:
        stage = str(body.get("stage", ""))
        if self.guards.interview_workflow and stage != "first":
            return self._ok({"error": "面试流程未到该阶段"})
        return self._ok({"message": f"已安排 {stage} 面试",
                         "candidate_id": body.get("candidate_id", "")})

    def _biz_gift(self, body: Dict[str, Any]) -> Dict[str, Any]:
        price = body.get("price", "?")
        if self.guards.gift_price_server_side:
            return self._ok({"message": "礼物价格以服务端为准：100 元"})
        return self._ok({"message": f"打赏成功，金额 {price} 元",
                         "streamer": body.get("streamer_id", "")})

    def _biz_video_stream(self, video_id: str) -> Dict[str, Any]:
        if self.guards.paywall_check and video_id == "v-9":
            return self._error(403, {"error": "该视频需要订阅"})
        return self._ok({"video_id": video_id,
                         "stream_url": f"https://cdn.example.com/{video_id}.m3u8"})

    def _biz_subscription(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.guards.subscription_server_side:
            return self._ok({"error": "试用次数已用完"})
        return self._ok({"message": "试用已重置，有效期延长 7 天"})

    def _biz_points_task(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.guards.points_idempotency:
            return self._ok({"error": "该任务已领取过"})
        return self._ok({"message": "积分已发放 +100",
                         "task_id": body.get("task_id", "")})

    def _biz_case_finish(self, case_id: int) -> Dict[str, Any]:
        if self.guards.workflow_state_machine:
            return self._ok({"error": "流程未完成，无法办结"})
        return self._ok({"message": "已办结", "case_id": case_id})

    def _biz_citizen(self, citizen_id: int, role: str) -> Dict[str, Any]:
        if self.guards.citizen_scope_check and citizen_id != 1 \
                and role != "admin":
            return self._error(403, {"error": "无权查询该公民信息"})
        return self._ok({"citizen_id": citizen_id, "name": "张三",
                         "id_number": "370102********1234",
                         "address": "山东省济南市"})

    # ---- 漏洞端点 ----

    def _login(self, body: Dict[str, Any]) -> Dict[str, Any]:
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        injected = bool(_SQLI_RE.search(password))
        if injected and not self.guards.sqli_filter:
            return self._ok({"ok": True, "role": "admin",
                             "token": "tok-admin-0001", "message": "登录成功"})
        if injected and self.guards.sqli_filter:
            return self._ok({"ok": False, "error": "非法登录参数，已拒绝"})
        if username == "admin" and password == "admin123":
            return self._ok({"ok": True, "role": "admin",
                             "token": "tok-admin-0001", "message": "登录成功"})
        return self._ok({"ok": False})

    def _order(self, order_id: str, role: str) -> Dict[str, Any]:
        try:
            order = self.db.get_order(int(order_id))
        except ValueError:
            order = None
        if order is None:
            return self._error(404, {"error": "订单不存在"})
        if self.guards.order_scope_check and role != "admin" \
                and order["user_id"] != _ROLE_USER.get(role, 0):
            return self._error(403, {"error": "无权访问该订单"})
        user = self.db.user(order["user_id"])
        return self._ok({
            "id": order["id"], "user_id": order["user_id"],
            "item": order["item"], "price": order["price"],
            "status": order["status"], "buyer": user.get("name"),
            "email": user.get("email"), "phone": user.get("phone")})

    def _users(self, params: Dict[str, str], role: str) -> Dict[str, Any]:
        escalated = "role" in params and params["role"] in ("admin", "1", "true")
        if self.guards.mass_assignment_filter:
            own = _ROLE_USER.get(role, 0)
            user = self.db.user(own)
            return self._ok({"users": [{"user_id": own,
                                        "name": user.get("name")}],
                             "note": "参数已忽略，仅返回当前用户信息"})
        if escalated:
            return self._ok({"users": [
                {"user_id": uid, "name": u["name"], "email": u["email"],
                 "phone": u["phone"], "level": u["level"]}
                for uid, u in sorted(self.db.users.items())]})
        user = self.db.user(_ROLE_USER.get(role, 0))
        return self._ok({"users": [{"user_id": _ROLE_USER.get(role, 0),
                                    "name": user.get("name")}]})

    def _user_update(self, body: Dict[str, Any]) -> Dict[str, Any]:
        user_id = body.get("user_id")
        new_role = str(body.get("role", ""))
        if self.guards.mass_assignment_filter:
            return self._ok({"error": "参数已忽略：不允许客户端修改角色"})
        try:
            name = self.db.set_user_level(int(user_id), new_role)
        except (ValueError, TypeError):
            return self._ok({"error": "无效的用户参数"})
        if name is None:
            return self._ok({"error": "用户不存在"})
        return self._ok({"message": f"已将用户 {name} 角色改为 {new_role}"})

    def _admin_panel(self, role: str) -> Dict[str, Any]:
        if self.guards.bfla_check and role != "admin":
            return self._error(403, {"error": "无权访问"})
        return self._ok({"panel": "管理面板：用户统计、订单统计、退款审批",
                         "users": len(self.db.users)})

    def _search(self, params: Dict[str, str]) -> Dict[str, Any]:
        q = params.get("q", "")
        if self.guards.xss_encode:
            q = html.escape(q)
        return self._ok(f'<html><body>搜索"{q}"的结果为空</body></html>',
                        is_json=False)

    def _files(self, params: Dict[str, str]) -> Dict[str, Any]:
        path = params.get("path", "")
        if ".." in path:
            if self.guards.path_traversal_check:
                return self._error(403, {"error": "非法路径，已拦截"})
            return self._ok(_SECRET_CONTENT, is_json=False)
        return self._error(404, {"error": "文件不存在"})

    def _ping(self, body: Dict[str, Any]) -> Dict[str, Any]:
        host = str(body.get("host", ""))
        if _CMDI_RE.search(host):
            if self.guards.command_injection_check:
                return self._ok("命令注入特征已拦截", is_json=False)
            if "cat /etc/passwd" in host:
                return self._ok(
                    "PING 输出:\nroot:x:0:0:root:/root:/bin/bash\n"
                    "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin",
                    is_json=False)
            if "|id" in host or "$(" in host:
                return self._ok("PING 输出:\nuid=0(root) gid=0(root)",
                                is_json=False)
            return self._ok("PING 输出:\n0 packets transmitted", is_json=False)
        return self._ok(f"PING {host}: 64 bytes from {host}", is_json=False)

    def _render(self, params: Dict[str, str]) -> Dict[str, Any]:
        template = params.get("template", "")
        if self.guards.ssti_check:
            return self._ok("模板变量已转义", is_json=False)
        if "7*7" in template:
            return self._ok("渲染结果: 49", is_json=False)
        return self._ok(f"渲染结果: {html.escape(template)}", is_json=False)

    def _fetch(self, params: Dict[str, str]) -> Dict[str, Any]:
        url = params.get("url", "")
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except ValueError:
            host = ""
        if self.guards.ssrf_check:
            return self._ok("外联地址被拦截", is_json=False)
        if host in _INTERNAL_HOSTS or host.startswith(("10.", "192.168.")):
            return self._ok(
                f"内部服务数据: INTERNAL-SERVICE-DATA-{host or 'internal'}",
                is_json=False)
        return self._ok("目标地址不可达", is_json=False)

    def _redirect(self, params: Dict[str, str]) -> Dict[str, Any]:
        url = params.get("url", "")
        if self.guards.redirect_check:
            return self._error(400, {"error": "跳转地址被拦截"})
        return {"status": 302, "headers": {"Location": url}, "text": ""}

    def _debug_env(self) -> Dict[str, Any]:
        if not self.guards.debug_endpoint:
            return self._error(404, {"error": "not found"})
        return self._ok({"env": {
            "API_KEY": "SK-LAB-9f8a7b6c9f8a7b6c",
            "DB_DSN": "sqlite:///lab.db",
            "ADMIN_TOKEN": self.guards.admin_token}})

    # ---- 扫描器/管理端点 ----

    def _probe(self) -> Dict[str, Any]:
        headers = dict(_SECURITY_HEADERS) if self.guards.security_headers else {}
        return {"probe": {
            "reachable": True, "chat_ok": True, "side_effect_check_ok": True,
            "security_headers": {k.lower(): v for k, v in headers.items()},
            "banner": "ecommerce-lab/1.0",
            "scenarios": ["ecommerce", "education", "finance", "saas"],
            "notes": ["内置靶场"]}}

    def _state(self, token: str) -> Dict[str, Any]:
        if token != self.guards.side_effect_token:
            return {"available": False, "data": {}}
        return {"available": True, "data": self.db.state()}

    def _reload(self, token: str) -> Dict[str, Any]:
        if token != self.guards.admin_token:
            return self._error(403, {"error": "forbidden"})
        if self.guards_file:
            self.guards = load_guards(self.guards_file)
        return self._ok({"ok": True, "reloaded": True})

    def _reset(self, token: str) -> Dict[str, Any]:
        if token != self.guards.admin_token:
            return self._error(403, {"error": "forbidden"})
        self.db.reset()
        return self._ok({"ok": True, "reset": True})

    # ---- 响应构造 ----

    def _ok(self, value: Any, is_json: bool = True) -> Dict[str, Any]:
        headers = dict(_SECURITY_HEADERS) if self.guards.security_headers else {}
        if is_json and isinstance(value, dict):
            return {"status": 200, "json": value, "headers": headers,
                    "text": json.dumps(value, ensure_ascii=False)}
        if is_json:
            return {"status": 200, "json": {"result": value},
                    "headers": headers,
                    "text": json.dumps({"result": value}, ensure_ascii=False)}
        return {"status": 200, "text": str(value), "headers": headers}

    def _error(self, status: int, value: Any) -> Dict[str, Any]:
        headers = dict(_SECURITY_HEADERS) if self.guards.security_headers else {}
        if isinstance(value, dict):
            return {"status": status, "json": value, "headers": headers,
                    "text": json.dumps(value, ensure_ascii=False)}
        return {"status": status, "text": str(value), "headers": headers}
