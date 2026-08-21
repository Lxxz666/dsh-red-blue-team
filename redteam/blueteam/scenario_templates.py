"""redteam.blueteam.scenario_templates —— 业务场景漏洞修复模板（D19）。

14 个靶场可自动修复类别（guard 键）+ 16 个通用业务场景类别（人工实施）。
在 templates.py 尾部自动注册进 FIX_TEMPLATES。
"""
from __future__ import annotations

from .templates import FixTemplate, register_template

# ---- 电商/零售 ----
register_template(FixTemplate(
    "price-server-side", "ecom_price_tamper", "结算金额服务端计价（禁止客户端传金额）",
    explanation=(
        "【现象】结算接口信任客户端提交的金额，1 元买下 299 元商品。\n"
        "【根因】价格/金额由客户端提交且服务端未重算（WSTG-BUSL-02/10）。\n"
        "【影响】低价购买/资金损失，评级 critical。"),
    rationale="金额必须由服务端按商品/优惠规则重新计算，客户端金额仅作展示。",
    how_to_fix=[
        "结算金额由服务端按购物车商品价格表重新计算，忽略客户端金额",
        "订单金额与支付金额一致性校验（服务端双重记账）",
        "价格变更审计（谁改价/何时/依据）",
    ],
    code_before=(
        "# 修复前：信任客户端金额\n"
        "order = create_order(cart, amount=request.json['amount'])  # 1 元成交"),
    code_after=(
        "# 修复后：服务端计价\n"
        "amount = sum(item.price for item in cart.items)\n"
        "order = create_order(cart, amount=amount)  # 客户端金额被忽略"),
    auto_fixable=True, guard_key="price_server_side", guard_value=True,
    verify_steps=["提交 amount=1 下单，实付金额仍为服务端价格"]))

register_template(FixTemplate(
    "coupon-mutex", "ecom_coupon_stack", "优惠券互斥规则（禁止无限叠加）",
    explanation=(
        "【现象】多张满减券可同时叠加，减免金额超过商品价格。\n"
        "【根因】优惠规则无互斥/上限约束（WSTG-BUSL-05）。\n"
        "【影响】套利/资金损失，评级 high。"),
    rationale="优惠叠加必须按规则引擎互斥（同类券取最优一张，总减免设上限）。",
    how_to_fix=["优惠规则引擎：同类券互斥（取最优一张）",
                "总减免金额上限（不超过订单金额）",
                "优惠计算服务端统一执行并审计"],
    auto_fixable=True, guard_key="coupon_stacking", guard_value=True,
    verify_steps=["同时提交多张券，只生效一张"]))

register_template(FixTemplate(
    "order-state-machine", "ecom_order_state", "订单状态机合法流转校验",
    explanation=(
        "【现象】待发货订单可直接跳转到已完成/已退款。\n"
        "【根因】状态转移无前置校验（WSTG-BUSL-06）。\n"
        "【影响】流程绕过（未发货确认收货、未支付完成订单），评级 critical。"),
    rationale="状态流转必须按状态机白名单转移，非法转移直接拒绝。",
    how_to_fix=["定义状态机转移表（待发货→已发货→已完成）",
                "每次转移校验当前状态+目标状态合法性",
                "关键转移（完成/退款）加前置条件校验"],
    code_before=(
        "# 修复前：任意状态直达\n"
        "order.status = request.json['status']"),
    code_after=(
        "# 修复后：状态机白名单\n"
        "TRANSITIONS = {'待发货': {'已发货'}, '已发货': {'已完成'}}\n"
        "if request.status not in TRANSITIONS.get(order.status, set()):\n"
        "    raise Forbidden('非法状态流转')"),
    auto_fixable=True, guard_key="order_state_machine", guard_value=True,
    verify_steps=["待发货订单直接提交已完成，被拒绝"]))

register_template(FixTemplate(
    "callback-verify", "ecom_pay_callback", "支付回调验签 + 状态一致性校验",
    explanation=(
        "【现象】伪造支付成功回调即让订单变已支付。\n"
        "【根因】回调无签名验证/未与支付渠道核对（WSTG-BUSL-03/10）。\n"
        "【影响】零元购/资金损失，评级 critical。"),
    rationale="支付回调必须验签（渠道公钥）+ 订单金额/状态与服务端记录一致才入账。",
    how_to_fix=["回调验签（支付渠道签名/密钥）",
                "回调金额必须与订单金额一致",
                "回调幂等（同回调号只处理一次）"],
    auto_fixable=True, guard_key="pay_callback_verify", guard_value=True,
    verify_steps=["伪造签名回调被拒绝"]))

register_template(FixTemplate(
    "refund-idempotency", "ecom_dup_refund", "退款幂等（同订单只能退一次）",
    explanation=(
        "【现象】同一订单可重复发起退款并多次受理。\n"
        "【根因】退款接口无幂等控制（WSTG-BUSL-04/10）。\n"
        "【影响】重复退款资金损失，评级 critical。"),
    rationale="退款必须幂等：已退款订单再次退款直接拒绝（或返回首次结果）。",
    how_to_fix=["退款前校验订单退款状态，已退款则拒绝",
                "退款申请号幂等（同号只处理一次）",
                "退款金额累计不超过订单实付金额"],
    auto_fixable=True, guard_key="refund_idempotency", guard_value=True,
    verify_steps=["同一订单重复退款，第二次被拦截"]))

# ---- 教育 ----
register_template(FixTemplate(
    "score-scope", "edu_score_idor", "成绩属主校验（禁止跨学生查询）",
    explanation=(
        "【现象】改 user_id 可查询任意学生成绩。\n"
        "【根因】成绩查询只校验登录态不校验属主（BUSL-02/API1）。\n"
        "【影响】学生隐私泄露/成绩数据泄露，评级 critical。"),
    rationale="成绩/学习数据访问必须校验属主（学生本人/其家长/教师角色）。",
    how_to_fix=["成绩查询加属主校验（WHERE student_id=当前用户）",
                "教师角色按班级范围授权，越界拒绝",
                "越权访问审计告警"],
    auto_fixable=True, guard_key="score_scope_check", guard_value=True,
    verify_steps=["学生 A 查询学生 B 成绩，返回 403"]))

register_template(FixTemplate(
    "answer-guard", "edu_answer_leak", "答案发布前不可见（服务端保存）",
    explanation=(
        "【现象】考试期间可通过接口获取标准答案。\n"
        "【根因】答案随试卷接口下发或未做发布状态控制（BUSL-03）。\n"
        "【影响】考试作弊/教育公平受损，评级 critical。"),
    rationale="答案只存服务端，判分在服务端进行，答案下发只在考试结束后。",
    how_to_fix=["答案不随试卷接口下发",
                "答案接口加发布状态控制（考试结束后才开放）",
                "判分在服务端进行，客户端不接触答案"],
    auto_fixable=True, guard_key="answer_leak_guard", guard_value=True,
    verify_steps=["考试期间访问答案接口，返回未发布"]))

register_template(FixTemplate(
    "server-grade", "edu_score_tamper", "服务端判分（禁止客户端提交成绩）",
    explanation=(
        "【现象】客户端提交 100 分即记为 100 分。\n"
        "【根因】成绩由客户端提交（BUSL-02）。\n"
        "【影响】成绩造假/权益滥用，评级 critical。"),
    rationale="成绩只能由服务端按答卷判分生成，客户端提交值一律忽略。",
    how_to_fix=["成绩由服务端判分生成（答案比对/评分规则在服务端）",
                "忽略客户端提交的 score 字段",
                "成绩变更留痕（谁改/何时/依据）"],
    auto_fixable=True, guard_key="score_server_grade", guard_value=True,
    verify_steps=["提交 score=100，服务端仍按判分结果记录"]))

register_template(FixTemplate(
    "exam-time-server", "edu_exam_time", "考试时间服务端控制",
    explanation=(
        "【现象】客户端可自行延长考试时间。\n"
        "【根因】考试起止时间由客户端提交（BUSL-06）。\n"
        "【影响】考试作弊，评级 high。"),
    rationale="考试起止时间只由服务端（场次配置）决定，客户端提交一律忽略。",
    how_to_fix=["考试时间取服务端场次配置，忽略客户端参数",
                "答题提交校验服务端时钟窗口",
                "超时自动交卷"],
    auto_fixable=True, guard_key="exam_time_check", guard_value=True,
    verify_steps=["提交延长考试时间，被拒绝"]))

# ---- 金融 ----
register_template(FixTemplate(
    "amount-validation", "fin_negative_transfer", "金额正数校验（服务端强校验）",
    explanation=(
        "【现象】转账负数金额被受理，收款方余额反向增加。\n"
        "【根因】金额只做格式校验未做正数/上限校验（BUSL-02/10）。\n"
        "【影响】资金凭空增加/套利，评级 critical。"),
    rationale="所有资金操作金额必须服务端强校验：正数、精度、上限、与余额关系。",
    how_to_fix=["金额服务端强校验（正数/两位小数/上限）",
                "转账前后余额双向校验（扣减方余额充足）",
                "金额计算用整数分（Decimal）避免浮点误差"],
    auto_fixable=True, guard_key="amount_validation", guard_value=True,
    verify_steps=["提交负数金额转账，被拒绝"]))

register_template(FixTemplate(
    "withdraw-limit", "fin_overdraw", "提现余额校验（超额拒绝）",
    explanation=(
        "【现象】提现金额超过余额仍被受理。\n"
        "【根因】提现未校验余额（BUSL-02）。\n"
        "【影响】资金透支，评级 critical。"),
    rationale="提现必须校验余额充足，且冻结金额防止并发透支。",
    how_to_fix=["提现前校验余额 ≥ 提现金额",
                "并发防护：提现期间冻结金额（乐观锁/悲观锁）",
                "提现限额与频控（防拆分绕过）"],
    auto_fixable=True, guard_key="withdraw_limit_check", guard_value=True,
    verify_steps=["提交超过余额的提现，被拒绝"]))

register_template(FixTemplate(
    "balance-server", "fin_balance_tamper", "余额服务端记账（禁止客户端直改）",
    explanation=(
        "【现象】客户端直接修改钱包余额成功。\n"
        "【根因】余额可由客户端写入（BUSL-02）。\n"
        "【影响】任意修改余额 = 直接造钱，评级 critical。"),
    rationale="余额只能由服务端记账（流水驱动），客户端提交值一律忽略。",
    how_to_fix=["余额由服务端流水记账驱动（充值/转账/消费各自生成流水）",
                "禁止任何接口直接写余额",
                "每日对账（流水汇总 vs 余额）"],
    auto_fixable=True, guard_key="balance_server_side", guard_value=True,
    verify_steps=["提交余额修改请求，被拒绝"]))

# ---- SaaS ----
register_template(FixTemplate(
    "tenant-isolation", "saas_tenant_isolation", "租户数据隔离（tenant_id 属主校验）",
    explanation=(
        "【现象】改 tenant_id 读取到其他租户数据。\n"
        "【根因】租户维度未做强隔离（BUSL-02/API1）。\n"
        "【影响】跨租户数据泄露（多租户系统最严重事故），评级 critical。"),
    rationale="租户 id 必须从认证上下文取得（不可由客户端指定），查询强制 tenant_id=当前租户。",
    how_to_fix=["租户 id 从认证上下文取得，忽略客户端提交",
                "所有查询强制 WHERE tenant_id=当前租户（ORM 自动注入）",
                "跨租户访问审计告警"],
    code_before=(
        "# 修复前：客户端指定租户\n"
        "data = db.query('SELECT * FROM data WHERE tenant_id=?',\n"
        "                (request.json['tenant_id'],))"),
    code_after=(
        "# 修复后：租户从认证上下文注入\n"
        "data = db.query('SELECT * FROM data WHERE tenant_id=?',\n"
        "                (current_user.tenant_id,))  # 客户端参数被忽略"),
    auto_fixable=True, guard_key="tenant_isolation", guard_value=True,
    verify_steps=["A 租户提交 B 租户 id，返回 403"]))

register_template(FixTemplate(
    "plan-enforcement", "saas_plan_downgrade", "套餐降级权益即时回收",
    explanation=(
        "【现象】套餐降级为 basic 后高级功能仍可用。\n"
        "【根因】权益校验只在下单时进行，降级未回收（BUSL-06）。\n"
        "【影响】白嫖高级功能/收入损失，评级 high。"),
    rationale="权益必须每次使用时按当前套餐实时校验，降级即时回收。",
    how_to_fix=["权益按当前套餐实时校验（每次使用查当前 plan）",
                "降级操作立即回收高级功能（同步生效）",
                "计费事件与权益变更审计"],
    auto_fixable=True, guard_key="plan_enforcement", guard_value=True,
    verify_steps=["降级后调用高级功能，被拒绝"]))

# ---- 其余 8 大场景（通用业务逻辑，人工实施指引） ----
_GENERIC_SCENARIOS = [
    ("soc_content_idor", "社交内容属主校验",
     "私密内容可被改 id 越权访问（BUSL-02）。修复：内容访问按可见性+属主/关系校验。"),
    ("soc_moderation_bypass", "内容审核闭环",
     "审核绕过（敏感词变形）。修复：审核引擎升级+先审后发+举报复核。"),
    ("med_record_idor", "病历属主校验",
     "病历/健康数据越权（BUSL-02）。修复：病历访问强属主校验+访问审计。"),
    ("med_appointment_race", "号源并发锁",
     "号源并发超卖（BUSL-04）。修复：号源加锁（乐观锁/唯一约束）+实名限购。"),
    ("game_currency_tamper", "虚拟货币服务端记账",
     "游戏币客户端直改（BUSL-02）。修复：货币流水记账+禁止直写余额。"),
    ("game_item_dup", "道具发放幂等",
     "道具重复领取（BUSL-04）。修复：领取幂等（领取记录唯一约束）。"),
    ("dlv_fee_tamper", "配送费服务端计算",
     "配送费客户端篡改（BUSL-02）。修复：费用按服务端计价规则计算。"),
    ("dlv_confirm_bypass", "送达确认校验",
     "未送达可确认（BUSL-06）。修复：确认需骑手定位/签名等校验。"),
    ("hr_resume_idor", "简历查看鉴权",
     "简历越权查看（BUSL-02）。修复：简历访问按投递关系/企业权限校验。"),
    ("hr_offer_bypass", "面试流程状态机",
     "面试流程跳步（BUSL-06）。修复：流程状态机+审批链校验。"),
    ("med_gift_amount", "打赏金额服务端校验",
     "打赏金额篡改（BUSL-10）。修复：礼物价格服务端定价+金额校验。"),
    ("med_paywall_bypass", "付费内容鉴权",
     "付费内容越权观看（BUSL-02）。修复：播放地址按订阅状态签发（带签名/时效）。"),
    ("mem_subscription_bypass", "订阅周期服务端控制",
     "试用重置/降级保留（BUSL-05/06）。修复：订阅状态服务端控制+权益实时校验。"),
    ("mem_points_farm", "积分获取幂等",
     "积分刷量（BUSL-05）。修复：任务奖励幂等（领取记录）+频控。"),
    ("gov_workflow_jump", "办事流程状态机",
     "流程跳步直接办结（BUSL-06）。修复：流程状态机+材料前置校验+审批链。"),
    ("gov_data_idor", "公民数据属主校验",
     "个人信息越权查询（BUSL-02）。修复：查询强属主校验+访问审计+最小化。"),
]
for _category, _title, _how in _GENERIC_SCENARIOS:
    register_template(FixTemplate(
        template_id=f"scenario-{_category}", category=_category, title=_title,
        explanation=f"【现象】{_how}\n【根因】业务逻辑校验缺失（WSTG-BUSL）。\n"
                    "【影响】越权/刷量/资金异常，评级 high~critical。",
        rationale=_how,
        how_to_fix=[_how.split("修复：")[-1], "加入工复核与审计"],
        auto_fixable=False, manual_steps=(_how.split("修复：")[-1], "回归验证")))
