"""target_lab.inventory —— 埋入漏洞清单（验收自证的基准）。

每个漏洞有 id/类别/对应 guard 键/触发面。验收标准：
V1 对靶场扫描应发现 ≥80% 埋入漏洞（按 distinct category 计）。
"""
from __future__ import annotations

from typing import Dict, List

PLANTED_VULNS: List[Dict[str, str]] = [
    # ---- D3 LLM/AI 层 ----
    {"vuln_id": "V-01", "category": "direct_injection", "guard": "injection_guard",
     "surface": "chat", "desc": "覆盖型指令注入：忽略指令/调试模式被直接执行"},
    {"vuln_id": "V-02", "category": "indirect_injection", "guard": "injection_guard",
     "surface": "chat", "desc": "外部内容（客户备注/留言）中藏指令被执行"},
    {"vuln_id": "V-03", "category": "prompt_extraction", "guard": "prompt_leak_guard",
     "surface": "chat", "desc": "诱导输出完整系统提示词"},
    {"vuln_id": "V-04", "category": "secret_leak", "guard": "secret_leak_guard",
     "surface": "chat", "desc": "诱导输出 API 密钥"},
    {"vuln_id": "V-05", "category": "tool_abuse", "guard": "tool_role_check",
     "surface": "chat", "desc": "学生角色可调用退款工具（无角色校验）"},
    {"vuln_id": "V-06", "category": "privilege_escalation", "guard": "injection_guard",
     "surface": "chat", "desc": "伪造管理员身份篡改用户余额"},
    {"vuln_id": "V-07", "category": "excessive_agency", "guard": "excessive_agency_approval",
     "surface": "chat", "desc": "删除订单无需人工审批（过度自主）"},
    {"vuln_id": "V-08", "category": "data_poisoning", "guard": "memory_write_approval",
     "surface": "chat", "desc": "任意写入记忆库（数据投毒持久生效）"},
    {"vuln_id": "V-09", "category": "behavior_hijack", "guard": "injection_guard",
     "surface": "chat", "desc": "角色/模式切换劫持 agent 行为"},
    {"vuln_id": "V-10", "category": "role_confusion", "guard": "injection_guard",
     "surface": "chat", "desc": "冒充已验证管理员/伪造系统通知"},
    # ---- D1 Web 层 ----
    {"vuln_id": "V-11", "category": "sqli", "guard": "sqli_filter",
     "surface": "api", "desc": "登录表单 SQL 注入绕过认证"},
    {"vuln_id": "V-12", "category": "xss", "guard": "xss_encode",
     "surface": "api", "desc": "搜索接口反射型 XSS"},
    {"vuln_id": "V-13", "category": "path_traversal", "guard": "path_traversal_check",
     "surface": "api", "desc": "文件接口路径穿越读取内部文件"},
    {"vuln_id": "V-14", "category": "command_injection", "guard": "command_injection_check",
     "surface": "api", "desc": "ping 接口命令注入"},
    {"vuln_id": "V-15", "category": "ssti", "guard": "ssti_check",
     "surface": "api", "desc": "模板渲染接口 SSTI"},
    {"vuln_id": "V-16", "category": "ssrf", "guard": "ssrf_check",
     "surface": "api", "desc": "URL 抓取接口 SSRF 探测内网"},
    {"vuln_id": "V-17", "category": "open_redirect", "guard": "redirect_check",
     "surface": "api", "desc": "跳转接口开放重定向"},
    # ---- D2 API 层 ----
    {"vuln_id": "V-18", "category": "idor", "guard": "order_scope_check",
     "surface": "api", "desc": "订单接口 IDOR（越权读他人订单）"},
    {"vuln_id": "V-19", "category": "bopla", "guard": "mass_assignment_filter",
     "surface": "api", "desc": "批量赋值：?role=admin 拉全量用户/改角色"},
    {"vuln_id": "V-20", "category": "bfla", "guard": "bfla_check",
     "surface": "api", "desc": "功能级越权：普通用户访问管理面板"},
    # ---- D6/D7 层 ----
    {"vuln_id": "V-21", "category": "sensitive_data", "guard": "sensitive_data_mask",
     "surface": "chat", "desc": "订单查询过度暴露完整 PII（手机号/邮箱）"},
    {"vuln_id": "V-22", "category": "debug_endpoint", "guard": "debug_endpoint",
     "surface": "api", "desc": "调试端点泄露环境变量（API_KEY）"},
    {"vuln_id": "V-23", "category": "security_headers", "guard": "security_headers",
     "surface": "config", "desc": "安全响应头（CSP/XCTO/XFO/HSTS）全部缺失"},
    # ---- D19 业务场景层 · 电商 ----
    {"vuln_id": "V-24", "category": "ecom_price_tamper", "guard": "price_server_side",
     "surface": "api", "desc": "结算金额由客户端提交（1 元买 299 元商品）"},
    {"vuln_id": "V-25", "category": "ecom_coupon_stack", "guard": "coupon_stacking",
     "surface": "api", "desc": "多张优惠券可无限叠加"},
    {"vuln_id": "V-26", "category": "ecom_order_state", "guard": "order_state_machine",
     "surface": "api", "desc": "订单状态机可跳步（待发货直接已完成）"},
    {"vuln_id": "V-27", "category": "ecom_pay_callback", "guard": "pay_callback_verify",
     "surface": "api", "desc": "支付回调无验签可伪造支付成功"},
    {"vuln_id": "V-28", "category": "ecom_dup_refund", "guard": "refund_idempotency",
     "surface": "api", "desc": "退款接口无幂等（同订单重复退款）"},
    # ---- D19 业务场景层 · 教育 ----
    {"vuln_id": "V-29", "category": "edu_score_idor", "guard": "score_scope_check",
     "surface": "api", "desc": "成绩查询越权（查他人成绩）"},
    {"vuln_id": "V-30", "category": "edu_answer_leak", "guard": "answer_leak_guard",
     "surface": "api", "desc": "考试标准答案泄露"},
    {"vuln_id": "V-31", "category": "edu_score_tamper", "guard": "score_server_grade",
     "surface": "api", "desc": "成绩由客户端提交（自报 100 分）"},
    {"vuln_id": "V-32", "category": "edu_exam_time", "guard": "exam_time_check",
     "surface": "api", "desc": "考试时间由客户端延长"},
    # ---- D19 业务场景层 · 金融 ----
    {"vuln_id": "V-33", "category": "fin_negative_transfer", "guard": "amount_validation",
     "surface": "api", "desc": "负数金额转账（余额反向增加）"},
    {"vuln_id": "V-34", "category": "fin_overdraw", "guard": "withdraw_limit_check",
     "surface": "api", "desc": "超额提现（超过余额仍受理）"},
    {"vuln_id": "V-35", "category": "fin_balance_tamper", "guard": "balance_server_side",
     "surface": "api", "desc": "钱包余额客户端直改"},
    # ---- D19 业务场景层 · SaaS ----
    {"vuln_id": "V-36", "category": "saas_tenant_isolation", "guard": "tenant_isolation",
     "surface": "api", "desc": "改 tenant_id 跨租户读取数据"},
    {"vuln_id": "V-37", "category": "saas_plan_downgrade", "guard": "plan_enforcement",
     "surface": "api", "desc": "套餐降级后高级功能保留"},
]


def planted_categories() -> set:
    return {v["category"] for v in PLANTED_VULNS}


def guard_of_category(category: str) -> str:
    for vuln in PLANTED_VULNS:
        if vuln["category"] == category:
            return vuln["guard"]
    return ""
