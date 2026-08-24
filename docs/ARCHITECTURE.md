# 架构设计（ARCHITECTURE）

## 1. 定位

dsh-red-blue-team 是 **dsh-python 框架之上的二次开发层**：不修改框架，只复用框架基座，
用插件/服务形态把红蓝队能力挂到 dsh 的 Context 上。本仓库内含 dsh 框架完整副本
（`dsh/`），红蓝队代码（`redteam/`）与内置靶场（`target_lab/`）是增量层。

## 2. 分层图

```
┌─────────────────────────────────────────────────────────────┐
│ 接入层   dsh-redteam CLI + Web 面板（FastAPI，任务步骤/日志追踪）  │
│  scan(网址) / static(文件夹) / fix / lab / report / bench   │
│  demo / samples / scenarios / web / batch / schedule        │
├─────────────────────────────────────────────────────────────┤
│ 主Agent 编排层 redteam/agents/                               │
│  AttackOrchestrator（主Agent：计划→派发→汇总→攻击报告）       │
│  ├── ReconAgent（侦察子Agent：能力/端点/业务场景指纹）        │
│  ├── StaticAgent（静态子Agent：文件夹代码级审计）             │
│  ├── AttackWorkerAgent × N（攻击子Agent：按角色分组并行攻击） │
│  └── LlmAttackAgent（LLM 自主攻击：确定性多轮循环，          │
│       可选 http_probe/http_attack 主动侦察工具）             │
├─────────────────────────────────────────────────────────────┤
│ 红蓝队能力层 redteam/                                        │
│  scenarios(12大业务场景指纹) static(静态规则引擎)             │
│  vectors(样本库) detector(判定) engine(执行核心)              │
│  reporter(攻击报告+修复报告) blueteam(修复模板/规划/回归)     │
│  adaptive(wanter地形) storage/audit/config/models            │
├─────────────────────────────────────────────────────────────┤
│ dsh 框架基座（完整复用，未修改）                               │
│  kernel 插件内核: Context/Service/EventBus/scoped/可逆效应   │
│  wanter 势能地形: Terrain/TraceField/HashCoordinator/flow    │
│  llm 接缝: LlmRuntime/Mock/DeepSeek 适配器                   │
├─────────────────────────────────────────────────────────────┤
│ 数据层   SQLite(扫描/结果/漏洞/修复/地形) + JSONL 审计日志     │
│         target_lab 内置靶场（HTTP + SDK 双接入，53 埋入漏洞 · 12 大场景） │
└─────────────────────────────────────────────────────────────┘
```

## 3. 主Agent + 子Agent 并行编排（核心架构）

对齐 dsh 的 per-agent scope 模型，实现「主 Agent 计划 → 子 Agent 并行攻击 →
结构化报告回流 → 主 Agent 汇总出攻击报告」：

```
AttackOrchestrator（主Agent, 根 ctx）
    │ ① 派发 ReconAgent ──→ WorkerReport{probe, scenarios, endpoints}
    │ ② 派发 StaticAgent（folder 模式）──→ WorkerReport{static_findings}
    │ ③ 攻击计划：样本 = 检测面 × 角色 × 业务场景
    │ ④ 并行派发攻击子Agent（每个子Agent = 一个角色，独立 scoped ctx）
    │      attacker-student ─┐
    │      attacker-customer ─┼─→ WorkerReport{verdicts...}（asyncio.gather 并行）
    │      attacker-admin ───┘
    │ ⑤ 汇总：判定→漏洞→落库→攻击报告（报告器 + 可选 LLM 态势综述）
```

- **子Agent 隔离**：每个子 Agent 用 `ctx.scoped("attacker/<id>")`（dsh per-agent scope）
  持有自己的作用域，失败互不影响（单子代理异常被捕获进 WorkerReport.error）；
- **并行与安全**：全局 `asyncio.Semaphore(concurrency)` 限并发；
  状态型样本（`stateful`/副作用期望）走共享串行通道 + 靶场 reset 隔离，
  防数据污染误判；
- **审计**：`agent/dispatched`（主Agent派发）与 `agent/report`（子Agent回报）
  进入事件溯源审计日志，与攻击判定事件构成完整证据链；
- **主Agent 报告**：汇总 WorkerReport → 攻击报告（确定性模板 + DeepSeek 可用时的
  LLM 态势综述；LLM 不可用时自动降级为确定性综述，离线可跑）。

## 4. 业务场景适配层（D19）

`redteam/scenarios/`：12 大业务场景（电商/金融/教育/SaaS/社交/医疗/游戏/外卖/
招聘/直播/会员/政务），每个场景 = 指纹（文件夹路径/URL 端点/内容关键词）+
专属攻击样本类别 + 专属修复模板。

**识别优先级**（target.scenario=auto 时）：
1. 目标业务元信息（`/api/meta/business`，靶场提供）；
2. URL 模式：侦察子 Agent 的端点发现（有界 GET 探测）→ 端点关键词计分；
3. 文件夹模式：文件路径关键词计分；
4. 显式指定：`target.scenario: ecommerce,education`（逗号分隔，可多场景）。

场景命中后，场景专属样本自动并入攻击计划（样本库纯 YAML，注册表零改动接入新场景）。

## 5. 三种目标输入

| 模式 | 输入 | 执行路径 |
|:--|:--|:--|
| 动态扫描 | `target.type: lab/http`（网址） | 侦察子Agent → 攻击子Agent（对话/API 样本） |
| 静态扫描 | `target.type: folder`（文件夹） | 静态子Agent：规则引擎逐文件审计（file:line 证据） |
| MCP 工具面 | `target.type: mcp`（stdio MCP 服务） | McpAdapter（复用 dsh.mcp.McpClient）：tools/list 发现 → tools/call 注入恶意参数 |

静态规则库（`redteam/static/rules.py`）：硬编码密钥/密码、弱哈希、SQL 拼接、
shell=True、pickle/yaml.load/eval、DEBUG、CORS 通配、XSS sink、
CVE-lite 依赖版本表、Docker root/privileged、敏感文件（.env/私钥/备份）。
每条规则绑定代码级修复模板（before/after）。

**MCP 攻击约定**：样本 `path=工具名、body=工具参数`，工具返回文本作为判定证据；
对话型样本对 MCP 目标抛 `UnsupportedSurface` → 攻击子代理记为 `skipped`
（绝不误报 error）；MCP 为外部进程，蓝队输出人工实施修复报告（工具层鉴权/
属主校验/审批的代码级示例）。

## 6. dsh 框架复用点（对照清单）

| dsh 能力 | 复用位置 | 用法 |
|:--|:--|:--|
| `dsh.kernel.Context/Service/EventBus/scoped` | `runtime.py` + `agents/` | 装配全部服务；子Agent 各自 scoped ctx；13 类审计事件 |
| `dsh.wanter.Terrain/TraceField/HashCoordinator` | `adaptive/terrain.py` | 攻击空间势能地形：成功→刻蚀（优先），失败→淤积（避开） |
| `dsh.llm.LlmRuntime/MockAdapter/DeepSeekAdapter` | `runtime.py` + `vectors/` | LLM 接缝：mock 默认离线确定性；有密钥自动挂 DeepSeek（弱裁判/态势综述/**载荷变体生成**） |
| `dsh.mcp.McpClient` | `adapters/mcp_adapter.py` | MCP 目标适配：initialize → tools/list → tools/call |
| `dsh.kernel.Context.scoped/effect` | `runtime.py` | 服务生命周期/可逆效应（dispose 逆序回收） |

**LLM 载荷变体（opt-in）**：`vectors.llm_variants: true` 且 DeepSeek 可用时，
主 Agent 攻击计划为对话型基础样本生成语义等价变体（JSON 数组/列表行双格式解析，
纯散文降级为空）；LLM 失败/离线静默降级——验收指标不依赖 LLM，确定性闭环不受影响。

**wanter 地形在红队场景的映射**：坐标 = 攻击向量×角色（哈希投影，可复现）；
成功 +1 沉积刻蚀、失败 −0.6 淤积；优先级 = 未尝试（危害度降序）→ 已尝试（Φ 升序）；
domain 分区 = 目标类型×业务域；持久化 = SQLite JSON 快照。

## 7. 蓝队完整修复闭环与修复报告

```
漏洞报告(JSON) → ① FixPlanner: 修复模板库(68类) → 每条漏洞:
                    问题说明(现象/根因/影响) + 分步修复 + 代码级before/after
                    + 验证步骤 + 修复理由(审计)
                    [+ engine.llm_fix_plan: LLM 逐条生成 AI 修复建议
                       → finding.fix.ai_plan / plan.ai_note，进修复报告]
               → ② FixExecutor: lab 目标沙箱应用(备份/版本化/热重载)
                    外部目标只出方案(manual_only)
               → ③ RegressionRunner: 同攻击样本复测必须清零; 未清零→回滚
               → ④ remediation_<target>.md: 完整修复报告(交付物)
```

## 7½. Web 面板 v2（redteam/web/）

- `panel.py`：FastAPI 应用——`POST /api/tasks`（网址 + LLM 模式开关）、
  `POST /api/tasks/upload`（原始 ZIP 体 → 防 zip-slip 解压 → 静态扫描）、
  `GET /api/tasks`（列表/进度）、`GET /api/tasks/{id}?after=N`（步骤时间线 +
  增量日志）、`GET …/report|remediation`、`POST …/fix`；
- `taskrunner.py`：任务管线执行器（串行队列）——每条任务的 `steps`
  （prepare/recon/attack/fix，状态+起止时间+摘要）与 `logs`（订阅 dsh 事件总线：
  `attack/executed`/`attack/verdict`/`finding/detected`/`fix/…` 实时成行，
  内存环形 2000 条 + `web_runtime/tasks/<id>.jsonl` 落盘可回放）；
- 网址任务类型判定：面板自带靶场（type=lab 且网址相同）保留 lab 语义（自动修复+
  回归可用）；其余网址一律外部目标 http（只出方案，绝不自动修改目标）；
  任意 http(s) 网址直接输入即可（本地测试便利，无授权配置门槛）；
- 前端：`static/index.html` 单文件原生 JS 暗色仪表盘（无外部依赖，离线可用）——
  网址输入/拖拽上传/任务卡片/步骤时间线/日志控制台/漏洞表/Markdown 报告渲染/一键修复。

## 8. 端到端核心流程

```
① 配置: scan.yml 描述目标(网址/文件夹)/角色/场景/授权 → 授权闸门
② 侦察: 侦察子Agent探测能力/安全头/业务场景指纹/端点发现
③ 计划: 样本 = 检测面 × 角色 × 业务场景；自适应优先级排序
④ 攻击: 攻击子Agent按角色并行；状态型样本串行通道+重置；节流
⑤ 判定: 确定性信号优先；弱信号仅存疑；5xx → error 保护
⑥ 报告: 攻击报告(JSON+MD+态势综述) + 漏洞落库 + 审计 JSONL
⑦ 修复: BlueEngine 规划→沙箱应用→回归清零→完整修复报告
⑧ 进化: 地形持久化；二次扫描更早发现全部漏洞(bench 量化)
```

## 9. 可信度自证（测试策略）

- `test_scan_e2e`：靶场发现率 **≥80%**（实测 53/53）；全加固靶场 **0 命中**；两次扫描一致；
- `test_agents`：主Agent派发事件/子Agent报告/失败隔离/文件夹模式端到端；
- `test_scenarios`：12 场景指纹识别（文件夹/端点/文本）+ 样本映射；
- `test_static`：合成项目检出与 file:line 证据、干净项目零误报、CVE-lite 命中；
- `test_lab_scenarios`：电商/教育/金融/SaaS 业务漏洞开启/修复行为；
- `test_remediation`：修复报告四段结构（问题说明/方案/代码级/验证）+ 蓝队输出；
- `test_mcp`：易受攻击 MCP 服务器（工具滥用/越权/投毒全检出）、对话样本跳过不误报；
- `test_llm_variants`：脚本化 LLM 的变体生成/解析/并入计划、mock 与故障静默降级；
- `test_detector`：拒绝话术不误报、5xx 不计漏洞、存疑绝不自动上报；
- `test_llm_agent`：脚本化 LLM 驱动完整 agent loop（攻击/判定/收尾）、无 LLM 优雅降级、
  主 Agent 集成（判定并入/落库/报告）；
- `test_llm_explorer`：http_probe 侦察回执、http_attack 原始攻击确定性判定
  （命中 → `llm_explored` 漏洞；加固目标 → failed）、工具开关门控、主 Agent 合并落库；
- `test_llm_fixplan`：LLM 修复建议写入 plan.ai_note/finding.fix 并呈现在修复报告、
  关闭/无 LLM 时确定性降级；
- `test_web`：面板新 API 全链路（网址任务步骤/增量日志/报告/修复、
  上传 ZIP 静态扫描与 zip-slip 拒绝、非本地目标 403、404/400 语义）；
- `test_regression`：修复→回归清零→复扫 0 命中→guards 逐项验证；
- CI：GitHub Actions 双 Python 版本（3.10/3.11）全量测试 + 靶场验收 job（发现率/零误报/回归）。

## 10. 扩展路线（V2+）

- ~~MCP 目标适配~~（已交付：`dsh.mcp` 客户端直连工具面 + 工具滥用/越权/投毒样本）；
- ~~LLM 载荷变体生成~~（已交付：opt-in，DeepSeek 可用时增强攻击计划）；
- ~~LLM 多轮攻击链 + 定向补打~~（已交付：静态链/LLM 链/未命中向量补打，均 opt-in）；
- ~~LLM 自主攻击 Agent~~（已交付 V11：`engine.llm_agent` 开启后，主 Agent 派发
  LLM 自主攻击 Agent，**确定性多轮驱动循环**——每轮强制工具调用
  （tools + tool_choice=required），持续攻击至 finalize/100 次上限（可配置调大）/超时；
  注入参考攻击手法，实测单轮 100 次自主攻击 · 37 漏洞命中；判定走确定性管线；
  无 LLM 优雅降级为空操作）；
- ~~LLM 主动侦察工具~~（已交付 V13：`engine.llm_explorer_tools` 开启后，LLM 攻击
  Agent 额外获得 `http_probe`/`http_attack` 原始探测工具，跳出样本库自主探索
  攻击面；判定仍由确定性信号管线把关，产出 `llm_explored` 漏洞并入主扫描）；
- ~~LLM 修复建议~~（已交付 V13：`engine.llm_fix_plan` 开启后，蓝队规划逐条漏洞
  生成 AI 修复建议，进修复报告「🤖 AI 修复建议」节；无 LLM 静默降级）；
- ~~Web 面板 v2~~（已交付 V13：网址输入/源码上传 → 任务步骤时间线 + 实时日志
  追踪 → 报告/一键修复，FastAPI + 单文件原生 JS 暗色仪表盘）；
- 子Agent 升级为 dsh 完整 agent loop（LLM 驱动的侦察推理/攻击链编排）；
- D5 业务逻辑/D6 数据/D8 供应链/D9 运行时检测面样本库（YAML 零改动接入）；
- 静态扫描扩展（SAST 语义规则/多语言/IaC 模板）。
