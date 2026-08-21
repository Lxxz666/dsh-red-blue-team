"""redteam.scenarios —— 业务场景适配层（D19 业务场景逻辑攻击的工程化）。

12 大业务场景，每个场景定义：
- 指纹（文件夹路径/文件名关键词、URL/端点关键词、内容关键词）→ 自动识别目标业务域；
- 场景专属攻击样本类别（sample_bank/scn_*.yaml）→ 场景命中后自动加入扫描；
- 场景专属修复模板 → 蓝队修复报告按业务域给出针对性修复指引。

场景识别支持三种输入：
- 本地项目文件夹（target.type=folder）：扫描文件路径/内容关键词；
- 网址（target.type=http/lab）：侦察探测端点 + 业务元信息；
- 显式指定（target.scenario=ecommerce|auto）：跳过指纹识别。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

log = logging.getLogger("redteam.scenarios")


@dataclass
class BusinessScenario:
    id: str
    name: str
    description: str
    path_keywords: List[str] = field(default_factory=list)     # 文件夹/文件名关键词
    endpoint_keywords: List[str] = field(default_factory=list)  # URL/端点关键词
    content_keywords: List[str] = field(default_factory=list)   # 代码/页面内容关键词
    sample_categories: List[str] = field(default_factory=list)  # 场景专属样本类别
    default_guard_hint: str = ""                                 # 演示说明


SCENARIOS: List[BusinessScenario] = [
    BusinessScenario(
        id="ecommerce", name="电商/零售",
        description="商品、购物车、下单、支付、退款、优惠券、库存",
        path_keywords=["order", "cart", "pay", "refund", "coupon", "sku",
                       "goods", "inventory", "checkout", "订单", "购物车",
                       "支付", "退款", "优惠", "库存"],
        endpoint_keywords=["order", "cart", "checkout", "pay", "refund",
                           "coupon", "goods", "sku"],
        content_keywords=["订单", "购物车", "退款", "优惠券", "秒杀"],
        sample_categories=["ecom_price_tamper", "ecom_coupon_stack",
                           "ecom_order_state", "ecom_pay_callback",
                           "ecom_dup_refund"],
        default_guard_hint="服务端计价/优惠互斥/状态机/回调验签/退款幂等"),
    BusinessScenario(
        id="finance", name="金融/支付/钱包",
        description="转账、提现、充值、余额、红包、风控",
        path_keywords=["wallet", "transfer", "withdraw", "recharge", "balance",
                       "loan", "ledger", "转账", "提现", "余额", "充值"],
        endpoint_keywords=["wallet", "transfer", "withdraw", "balance", "loan"],
        content_keywords=["转账", "提现", "余额", "充值", "红包"],
        sample_categories=["fin_negative_transfer", "fin_overdraw",
                           "fin_balance_tamper"],
        default_guard_hint="金额校验/余额校验/服务端计价"),
    BusinessScenario(
        id="education", name="教育/在线学习",
        description="题库、考试、成绩、课程、证书",
        path_keywords=["exam", "score", "answer", "course", "quiz", "paper",
                       "考试", "成绩", "题库", "课程", "答题"],
        endpoint_keywords=["exam", "score", "answer", "course", "quiz"],
        content_keywords=["考试", "成绩", "题库", "答案", "答题"],
        sample_categories=["edu_score_idor", "edu_answer_leak",
                           "edu_score_tamper", "edu_exam_time"],
        default_guard_hint="成绩属主校验/答案服务端保存/服务端判分/时间服务端校验"),
    BusinessScenario(
        id="saas", name="SaaS/多租户",
        description="租户隔离、成员角色、计费订阅、工作空间",
        path_keywords=["tenant", "workspace", "billing", "plan", "seat",
                       "subscription", "租户", "工作空间"],
        endpoint_keywords=["tenant", "workspace", "billing", "plan"],
        content_keywords=["租户", "工作空间", "套餐", "席位"],
        sample_categories=["saas_tenant_isolation", "saas_plan_downgrade"],
        default_guard_hint="租户 id 属主校验/降级权益回收"),
    BusinessScenario(
        id="social", name="社交/社区",
        description="帖子、关注、点赞、评论、私信、审核",
        path_keywords=["post", "follow", "like", "comment", "dm", "feed",
                       "moment", "帖子", "关注", "点赞", "评论"],
        endpoint_keywords=["post", "follow", "like", "comment", "feed"],
        content_keywords=["帖子", "关注", "点赞", "评论", "私信"],
        sample_categories=["soc_content_idor", "soc_moderation_bypass"],
        default_guard_hint="内容属主校验/审核闭环"),
    BusinessScenario(
        id="healthcare", name="医疗/健康",
        description="预约挂号、病历、处方、健康数据",
        path_keywords=["patient", "medical", "prescription", "appointment",
                       "clinic", "病历", "挂号", "处方"],
        endpoint_keywords=["patient", "medical", "prescription", "appointment"],
        content_keywords=["病历", "挂号", "处方", "就诊"],
        sample_categories=["med_record_idor", "med_appointment_race"],
        default_guard_hint="病历属主校验/号源并发锁"),
    BusinessScenario(
        id="gaming", name="游戏/虚拟资产",
        description="游戏币、道具、抽卡、装备、活动",
        path_keywords=["coin", "gem", "item", "gacha", "skin", "prop", "recharge",
                       "道具", "游戏币", "抽卡"],
        endpoint_keywords=["coin", "gem", "item", "gacha", "skin"],
        content_keywords=["游戏币", "道具", "抽卡", "装备"],
        sample_categories=["game_currency_tamper", "game_item_dup"],
        default_guard_hint="货币服务端记账/道具发放幂等"),
    BusinessScenario(
        id="delivery", name="外卖/物流/出行",
        description="配送费、计价、骑手、送达确认、赔付",
        path_keywords=["delivery", "courier", "ride", "logistics", "ship",
                       "配送", "骑手", "运费"],
        endpoint_keywords=["delivery", "courier", "ride", "ship"],
        content_keywords=["配送", "骑手", "运费", "送达"],
        sample_categories=["dlv_fee_tamper", "dlv_confirm_bypass"],
        default_guard_hint="费用服务端计算/送达确认校验"),
    BusinessScenario(
        id="hr", name="招聘/HR",
        description="简历、职位、面试流程、offer",
        path_keywords=["resume", "candidate", "offer", "interview", "job",
                       "简历", "面试", "职位"],
        endpoint_keywords=["resume", "candidate", "offer", "interview", "job"],
        content_keywords=["简历", "面试", "offer", "候选人"],
        sample_categories=["hr_resume_idor", "hr_offer_bypass"],
        default_guard_hint="简历查看鉴权/offer 审批流"),
    BusinessScenario(
        id="media", name="内容/媒体/直播",
        description="打赏、订阅、版权内容、播放量",
        path_keywords=["live", "gift", "subscribe", "video", "stream", "tip",
                       "直播", "打赏", "订阅"],
        endpoint_keywords=["live", "gift", "subscribe", "video", "stream"],
        content_keywords=["直播", "打赏", "订阅", "播放"],
        sample_categories=["med_gift_amount", "med_paywall_bypass"],
        default_guard_hint="打赏金额服务端校验/付费内容鉴权"),
    BusinessScenario(
        id="membership", name="会员/订阅/积分",
        description="订阅周期、积分、等级、权益",
        path_keywords=["membership", "points", "subscribe", "level", "vip",
                       "积分", "会员", "等级"],
        endpoint_keywords=["membership", "points", "subscribe", "level"],
        content_keywords=["积分", "会员", "等级", "权益"],
        sample_categories=["mem_subscription_bypass", "mem_points_farm"],
        default_guard_hint="权益有效期校验/积分获取幂等"),
    BusinessScenario(
        id="government", name="政务/公共服务",
        description="办事流程、材料、审批、个人信息",
        path_keywords=["gov", "citizen", "approval", "workflow", "certificate",
                       "办事", "审批", "政务"],
        endpoint_keywords=["gov", "citizen", "approval", "workflow"],
        content_keywords=["办事", "审批", "政务", "材料"],
        sample_categories=["gov_workflow_jump", "gov_data_idor"],
        default_guard_hint="流程状态机校验/数据属主校验"),
]

_SCENARIO_BY_ID: Dict[str, BusinessScenario] = {s.id: s for s in SCENARIOS}

#: 忽略目录（文件夹扫描）
_IGNORE_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
                ".idea", ".vscode", "dist", "build", ".pytest_cache"}


def get_scenario(scenario_id: str) -> Optional[BusinessScenario]:
    return _SCENARIO_BY_ID.get(scenario_id)


def scenario_ids() -> List[str]:
    return [s.id for s in SCENARIOS]


def scenario_names() -> Dict[str, str]:
    return {s.id: s.name for s in SCENARIOS}


def detect_scenario_folder(folder: str, max_files: int = 300) -> Optional[str]:
    """文件夹指纹识别：路径关键词计分，得分最高且 ≥2 的场景命中。"""
    scores: Dict[str, int] = {}
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for name in files + dirs:
            lowered = name.lower()
            for scenario in SCENARIOS:
                for keyword in scenario.path_keywords:
                    if keyword in lowered:
                        scores[scenario.id] = scores.get(scenario.id, 0) + 1
            count += 1
            if count >= max_files:
                break
        if count >= max_files:
            break
    if not scores:
        return None
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None


def detect_scenario_endpoints(endpoints: Set[str]) -> Optional[str]:
    """端点指纹识别：URL/端点关键词计分。"""
    scores: Dict[str, int] = {}
    lowered = {e.lower() for e in endpoints}
    for endpoint in lowered:
        for scenario in SCENARIOS:
            for keyword in scenario.endpoint_keywords:
                if keyword in endpoint:
                    scores[scenario.id] = scores.get(scenario.id, 0) + 1
    if not scores:
        return None
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None


def detect_scenario_text(text: str) -> Optional[str]:
    """内容指纹识别（页面/代码/业务元信息）。"""
    lowered = text.lower()
    scores: Dict[str, int] = {}
    for scenario in SCENARIOS:
        for keyword in scenario.content_keywords:
            if keyword in lowered:
                scores[scenario.id] = scores.get(scenario.id, 0) + 1
    if not scores:
        return None
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None


def sample_categories_for(scenario_ids) -> List[str]:
    """场景（str 或 list，None=通用）对应的专属样本类别。"""
    if isinstance(scenario_ids, str):
        scenario_ids = [scenario_ids]
    out: List[str] = []
    for scenario_id in scenario_ids or []:
        scenario = _SCENARIO_BY_ID.get(scenario_id)
        if scenario:
            out.extend(scenario.sample_categories)
    return out
