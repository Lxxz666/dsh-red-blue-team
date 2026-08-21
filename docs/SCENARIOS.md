# 业务场景目录（SCENARIOS）

> 12 大业务场景：通用技术漏洞（D1~D7）之外的**业务逻辑漏洞**（WSTG-BUSL 方法论）。
> 场景指纹自动识别（文件夹路径 / URL 端点 / 内容关键词），命中后自动加载场景专属
> 攻击样本；也可在 scan.yml 显式指定 `target.scenario`（逗号分隔多场景）。

查看命令：`dsh-redteam scenarios list|show <id>`

## 场景总览

| 场景 | id | 专属攻击样本类别 | 靶场演示 |
|:--|:--|:--|:---:|
| 电商/零售 | ecommerce | ecom_price_tamper / ecom_coupon_stack / ecom_order_state / ecom_pay_callback / ecom_dup_refund / ecom_inventory_race | ✅ |
| 金融/支付/钱包 | finance | fin_negative_transfer / fin_overdraw / fin_balance_tamper | ✅ |
| 教育/在线学习 | education | edu_score_idor / edu_answer_leak / edu_score_tamper / edu_exam_time | ✅ |
| SaaS/多租户 | saas | saas_tenant_isolation / saas_plan_downgrade | ✅ |
| 社交/社区 | social | soc_content_idor / soc_moderation_bypass | 样本库 |
| 医疗/健康 | healthcare | med_record_idor / med_appointment_race | 样本库 |
| 游戏/虚拟资产 | gaming | game_currency_tamper / game_item_dup | 样本库 |
| 外卖/物流/出行 | delivery | dlv_fee_tamper / dlv_confirm_bypass | 样本库 |
| 招聘/HR | hr | hr_resume_idor / hr_offer_bypass | 样本库 |
| 内容/媒体/直播 | media | med_gift_amount / med_paywall_bypass | 样本库 |
| 会员/订阅/积分 | membership | mem_subscription_bypass / mem_points_farm | 样本库 |
| 政务/公共服务 | government | gov_workflow_jump / gov_data_idor | 样本库 |

## 场景 × 攻击点明细（源自 WSTG-BUSL 方法论）

### 电商/零售（BUSL-02/04/05/06/10）

| 攻击点 | 样本 | 攻击原理 | 修复（guard 键） |
|:--|:--|:--|:--|
| 结算金额篡改 | ecom-001 | 客户端提交 amount=1 元买 299 元商品 | price_server_side（服务端计价） |
| 优惠券叠加 | ecom-002 | 多张满减券无限叠加 | coupon_stacking（互斥规则） |
| 订单状态跳步 | ecom-003 | 待发货直接跳"已完成" | order_state_machine（状态机白名单） |
| 支付回调伪造 | ecom-004 | 伪造回调让订单变已支付 | pay_callback_verify（验签+金额一致性） |
| 重复退款 | ecom-005 | 同订单退款两次（repeat 攻击） | refund_idempotency（幂等） |
| 库存超卖 | ecom-006 | 超量下单探测（对照样本） | 库存锁（人工） |

### 金融/支付/钱包（BUSL-02/10）

| 攻击点 | 样本 | 攻击原理 | 修复 |
|:--|:--|:--|:--|
| 负数转账 | fin-001 | amount=-100 使收款方余额反向增加 | amount_validation（正数/精度/上限） |
| 超额提现 | fin-002 | 提现 100000 超过余额仍受理 | withdraw_limit_check（余额校验+冻结） |
| 余额直改 | fin-003 | 客户端直接写钱包余额 | balance_server_side（流水记账） |

### 教育/在线学习（BUSL-02/03/06）

| 攻击点 | 样本 | 攻击原理 | 修复 |
|:--|:--|:--|:--|
| 成绩越权 | edu-001 | 改 user_id 查他人成绩 | score_scope_check（属主校验） |
| 答案泄露 | edu-002 | 考试期间获取标准答案 | answer_leak_guard（发布状态控制） |
| 成绩篡改 | edu-003 | 客户端自报 100 分 | score_server_grade（服务端判分） |
| 考试时间 | edu-004 | 客户端延长考试时间 | exam_time_check（服务端场次控制） |

### SaaS/多租户（BUSL-02/06）

| 攻击点 | 样本 | 攻击原理 | 修复 |
|:--|:--|:--|:--|
| 跨租户访问 | saas-001 | 改 tenant_id 读他人租户数据 | tenant_isolation（租户 id 取自认证上下文） |
| 降级权益残留 | saas-002 | 降级 basic 后高级功能保留 | plan_enforcement（权益实时校验） |

### 其余 8 场景（样本库就绪，对接对应业务面即可用）

社交（私密内容越权/审核绕过）、医疗（病历越权/号源竞态）、游戏（货币篡改/道具复制）、
外卖（配送费篡改/送达确认绕过）、招聘（简历越权/面试流程跳步）、直播（打赏金额/付费
内容越权）、会员（订阅周期绕过/积分刷量）、政务（流程跳步/公民数据越权）。
每类均配修复模板（人工实施指引 + 回归验证要求），详见 `redteam/blueteam/scenario_templates.py`。

## 场景识别的三种方式

```yaml
# 1) 自动识别（推荐）
target:
  scenario: auto          # 业务元信息 → 端点探测 → 文件夹指纹

# 2) 显式指定（单/多场景）
target:
  scenario: ecommerce,education

# 3) 文件夹静态扫描（自动按文件路径指纹）
target:
  type: folder
  folder_path: /path/to/project
```

## 新增业务场景的步骤

1. `redteam/scenarios/registry.py` 注册 `BusinessScenario`（指纹关键词 + 样本类别清单）；
2. `sample_bank/scn_<id>.yaml` 写场景样本（payload/evidence_patterns 对齐目标行为）；
3. `redteam/blueteam/scenario_templates.py` 注册修复模板（问题说明 + guard 键/人工指引）；
4. 如需靶场演示：`target_lab` 增加业务端点 + guard + `inventory.py` 登记；
5. `python -m pytest tests_redteam` 回归（发现率/零误报约束自动生效）。
