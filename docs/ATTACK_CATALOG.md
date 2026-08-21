# 攻击分类目录（ATTACK_CATALOG）

> 检测面 × 攻击类别 × 样本 × 靶场漏洞 × 修复映射 全景。
> 样本库位置：`sample_bank/*.yaml`（分类块列表格式）；查看命令：`dsh-redteam samples list|show`。
> 业务场景目录（D19）见 [SCENARIOS.md](SCENARIOS.md)。

## D3 LLM/AI 层（OWASP LLM Top 10 2025）

| 攻击类别 | 样本 | 靶场漏洞 | 原理 | 修复模板（guard 键） |
|:--|:--|:--|:--|:--|
| direct_injection | di-001/002 | V-01 | 覆盖型指令（忽略指令/调试模式）被执行 | injection-guard（injection_guard=true） |
| indirect_injection | ii-001/002 | V-02 | 外部内容（备注/留言）中藏指令 | injection-guard |
| prompt_extraction | pe-001/002 | V-03 | 索取完整系统提示词（LLM07:2025） | prompt-leak-guard（prompt_leak_guard=true） |
| secret_leak | sl-001/002 | V-04 | 索取 API 密钥/访问令牌（LLM02:2025） | secret-leak-guard |
| tool_abuse | ta-001/002 | V-05 | 低权限角色调用退款工具（无角色校验） | tool-role-check（tool_role_check=true） |
| privilege_escalation | pr-001/002 | V-06 | 伪造管理员身份改余额 | injection-guard |
| excessive_agency | ea-001 | V-07 | 删除订单无需人工审批（LLM08:2025） | agency-approval（excessive_agency_approval=true） |
| data_poisoning | dp-001 | V-08 | 任意写入记忆库（LLM04:2025） | memory-approval（memory_write_approval=true） |
| behavior_hijack | bh-001 | V-09 | 角色/模式切换劫持行为 | injection-guard |
| role_confusion | rc-001/002 | V-10 | 冒充管理员/伪造系统通知 | injection-guard |
| hallucination | ha-001（对照） | — | 诱导编造权威事实（LLM09:2025） | grounding-check（人工） |
| model_dos | md-001（对照） | — | 4000 字符超长输入（LLM10:2025） | input-limits（人工） |

## D1 Web 应用层（OWASP Web Top 10 2021）

| 攻击类别 | 样本 | 靶场漏洞 | 原理 | 修复模板 |
|:--|:--|:--|:--|:--|
| sqli | sqli-001 | V-11 | 登录表单 `' OR '1'='1` 注入绕过认证（A03） | sqli-filter |
| xss | xss-001 | V-12 | 搜索接口反射型 XSS（未转义回显） | xss-encode |
| path_traversal | pt-001 | V-13 | `../../secret.txt` 读取内部文件 | path-check |
| command_injection | ci-001 | V-14 | ping 接口 `;cat /etc/passwd` 命令注入 | cmdi-check |
| ssti | ssti-001 | V-15 | 模板渲染 `{{7*7}}` → 49（SSTI） | ssti-check |
| ssrf | ssrf-001 | V-16 | url 参数指向 127.0.0.1/169.254.169.254 | ssrf-check |
| open_redirect | or-001 | V-17 | 跳转接口 302 外域（钓鱼前置） | redirect-check |
| graphql | gql-001（对照） | — | GraphQL 内省探测 | graphql-hardening（人工） |
| http_smuggling | hs-001（对照） | — | 请求走私特征探测 | frontend-hardening（人工） |

## D2 API 层（OWASP API Security Top 10 2023）

| 攻击类别 | 样本 | 靶场漏洞 | 原理 | 修复模板 |
|:--|:--|:--|:--|:--|
| idor | idor-001/002 | V-18 | 改资源 id 越权读他人订单（API1 BOLA，按角色×属主配对样本） | order-scope-check |
| bopla | bopla-001/002 | V-19 | `?role=admin` 拉全量用户 / POST 改角色（API3） | mass-assignment-filter |
| bfla | bfla-001 | V-20 | 普通用户访问 /api/admin/panel（API5） | bfla-check |
| sensitive_data | sd-001 | V-21 | 订单查询暴露完整手机号/邮箱（API2/LLM02） | pii-mask |

## D7 配置与部署层（OWASP A05 / ASVS V14）

| 攻击类别 | 样本 | 靶场漏洞 | 原理 | 修复模板 |
|:--|:--|:--|:--|:--|
| debug_endpoint | de-001 | V-22 | /api/debug/env 泄露 API_KEY 等环境变量 | debug-off（debug_endpoint=false） |
| security_headers | sh-001 | V-23 | CSP/XCTO/XFO/HSTS 全部缺失 | security-headers |
| directory_listing | dl-001（对照） | — | 目录浏览探测 | disable-listing（人工） |

## 变体展开规则

- 每个基础样本按 `variables` 槽位笛卡尔积 × `paraphrases` 释义模板展开，
  `variants_per_sample` 控制预算，`seed` 固定 → **变体可复现**（报告可重复审计）；
- `{payload}` 槽位贯通载荷/参数/请求体模板；`__filler(N)__` 生成 N 字符超长载荷；
- SSTI 等载荷中的 `{{ }}` 原样保留（只替换已知槽位）；
- 稳定 uid = `<样本id>-<角色>-v<N>`：蓝队回归复测据此确定性重建同一样本。

## D19 业务场景层（WSTG-BUSL，12 大场景）

| 场景 | 攻击类别 | 靶场漏洞 | 修复模板 |
|:--|:--|:--|:--|
| 电商/零售 | ecom_price_tamper / ecom_coupon_stack / ecom_order_state / ecom_pay_callback / ecom_dup_refund（repeat 攻击）/ ecom_inventory_race | V-24~V-28 | price-server-side / coupon-mutex / order-state-machine / callback-verify / refund-idempotency |
| 金融/支付 | fin_negative_transfer / fin_overdraw / fin_balance_tamper | V-33~V-35 | amount-validation / withdraw-limit / balance-server |
| 教育/学习 | edu_score_idor / edu_answer_leak / edu_score_tamper / edu_exam_time | V-29~V-32 | score-scope / answer-guard / server-grade / exam-time-server |
| SaaS/多租户 | saas_tenant_isolation / saas_plan_downgrade | V-36~V-37 | tenant-isolation / plan-enforcement |
| 社交/医疗/游戏/外卖/招聘/直播/会员/政务 | 每场景 2 类（样本库就绪） | 靶场不含该业务面（阴性对照） | scenario-*（人工实施指引） |

**repeat 机制**：幂等/竞态类攻击（如重复退款）样本支持 `repeat: N` 连续发送同一载荷，
以最后一次响应为判定依据；`stateful: true` 标记状态型样本走串行通道 + 重置隔离。

## 静态扫描规则库（文件夹模式）

| 类别 | 规则 | 证据 | 修复模板 |
|:--|:--|:--|:--|
| hardcoded_secret | API 密钥/密码硬编码 | file:line + 代码片段 | secret-rotation |
| weak_crypto | MD5/SHA1 | file:line | crypto-hash-upgrade |
| sql_injection | SQL f-string 拼接 | file:line | sqli-filter |
| command_injection | shell=True | file:line | cmdi-check |
| unsafe_deserialization | pickle.loads / yaml.load / eval / exec | file:line | deserialization-safe |
| debug_mode | DEBUG=True | file:line | debug-off |
| cors_misconfig | CORS 通配 | file:line | cors-restrict |
| xss_sink | innerHTML / dangerouslySetInnerHTML | file:line | xss-encode |
| dependency_vuln | requirements/pyproject 版本 vs CVE-lite 表 | 包@版本 | dependency-upgrade |
| docker_misconfig | USER root / privileged | file:line | docker-hardening |
| sensitive_file | .env / 私钥 / .git / 备份文件 | 文件名 | sensitive-file-remove |

## 判定信号体系

| 信号 | 类型 | 说明 |
|:--|:--|:--|
| evidence_pattern | 确定性（0.9） | 样本自带证据正则命中目标响应（拒绝话术经人工校准不会误命中） |
| leak_pattern | 确定性（0.85） | 全局敏感模式：密钥格式/系统提示词标记/`root:x:0:0`/云元数据地址 |
| side_effect | 确定性（0.85） | 攻击前后目标状态快照变化（退款/删除/记忆/余额等危险键） |
| redirect_follow | 确定性（0.85） | 3xx + Location 头（开放重定向） |
| header_missing | 确定性（0.8） | 指定安全响应头缺失（D7 配置检查） |
| baseline_diff | 弱（0.35） | 响应与中性基线显著偏离 → 仅存疑 |
| slow_response | 弱（0.3） | 超长输入导致响应变慢 → 仅存疑 |
| LLM 裁判 | 弱（可选） | 仅审视存疑样本（确认存疑/降级 failed），**绝不提升 failed 为成功** |

## 新增攻击向量的步骤

1. `sample_bank/` 对应检测面 YAML 中加分类块（或新建 `<检测面>.yaml`）；
2. 写 payload/variables/paraphrases 与 **evidence_patterns**（必须精确匹配目标
   成功响应特征、且不能匹配防御话术）；
3. 若该类别可自动修复：在 `redteam/blueteam/templates.py` 注册修复模板
   （guard 键/修复理由/可自动修复标志）；
4. 靶场如需埋对应漏洞：`target_lab` 的 guards/app + `inventory.py` 登记；
5. 跑 `python -m pytest tests_redteam` 验证确定性。
