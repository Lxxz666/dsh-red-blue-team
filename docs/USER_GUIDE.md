# 用户手册（USER_GUIDE）

## 1. 安装

```bash
cd dsh_red_blue_team
pip install -r requirements.txt        # httpx / PyYAML / fastapi / uvicorn（dsh 框架已内置）
pip install -e .                       # 可选：安装 dsh-redteam / dsh-python 命令行入口
```

无需 API Key 即可运行：判定器是确定性的，内置靶场不依赖真实 LLM。
配置 `DEEPSEEK_API_KEY` 后自动启用 DeepSeek 适配器（LLM 弱信号裁判/攻击态势综述）。

推荐写法：在项目根目录 `.env` 配置（面板/CLI 启动时自动加载，已存在的环境变量优先）：

```bash
DEEPSEEK_API_KEY=ark-xxxx              # 火山方舟 API Key
DEEPSEEK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3   # Agent Plan 端点
DEEPSEEK_MODEL=deepseek-v4-flash       # 模型名
DEEPSEEK_DISABLE_THINKING=1            # 关闭思考模式，让工具循环直接输出 content
```

Web 面板顶部徽标显示「LLM 已就绪（模型名）」时，三个 LLM 开关（自主攻击/主动侦察/修复建议）
自动解锁；显示「LLM 未配置」则只会跑确定性引擎。

## 2. 三种目标输入

| 模式 | 命令 | 适用 |
|:--|:--|:--|
| **网址动态扫描** | `dsh-redteam scan --config scan.yml` | 已部署的业务系统（HTTP API / 对话 agent） |
| **文件夹静态扫描** | `dsh-redteam static <文件夹>` 或 `scan` + `type: folder` | 本地项目源码（代码级审计，免授权声明） |
| **MCP 服务工具面攻击** | `scan` + `type: mcp`（`examples/scan_mcp.yaml`） | 暴露 stdio MCP 工具的 agent 系统 |

## 3. 命令一览

| 命令 | 作用 |
|:--|:--|
| `dsh-redteam demo [--out DIR]` | **一键演示**：起靶场→扫描（4 业务场景）→修复→回归→复扫验收 |
| `dsh-redteam scan --config scan.yml [--fix]` | 网址/文件夹扫描（主Agent+子Agent 编排） |
| `dsh-redteam static <文件夹> [--scenario auto]` | 免配置快速静态扫描 |
| `dsh-redteam fix --config scan.yml --scan ID [--dry-run]` | 蓝队：方案/沙箱应用/回归/**完整修复报告** |
| `dsh-redteam lab [--port 8765] [--guards FILE]` | 启动内置靶场（53 个埋入漏洞 · 12 大业务场景） |
| `dsh-redteam report --config scan.yml [--list｜--scan ID]` | 历史扫描/重建报告 |
| `dsh-redteam bench --config scan.yml` | 自适应优先级基准（随机基线 vs wanter 地形序） |
| `dsh-redteam samples [list｜show CATEGORY]` | 攻击样本库（54 类别） |
| `dsh-redteam scenarios [list｜show ID]` | 业务场景库（12 场景指纹与攻击点） |
| `dsh-redteam web [--config scan.yml] [--port 8766]` | **Web 面板 v2**：网页发起扫描/上传源码解析/任务步骤时间线+实时日志/报告/一键修复（默认自动挂靶场） |
| `dsh-redteam batch --targets targets.yml [--out DIR]` | **多目标批扫**：串行扫描多个目标 + 风险排序汇总报告 |
| `dsh-redteam schedule --config scan.yml --every 24h [--webhook URL] [--once]` | **定时扫描**：周期扫描 + 报告按时间留存 + 可选 webhook 推送 |

## 4. 最小上手（3 条命令）

```bash
dsh-redteam lab --port 8765 --guards ./lab_guards.yml   # 终端 1：起靶场
dsh-redteam scan --config examples/scan_lab.yaml --fix  # 终端 2：扫描+修复+回归+修复报告
dsh-redteam report --config examples/scan_lab.yaml --list  # 查看历史与产物路径
```

## 5. 扫描配置（scan.yml）

完整示例：`examples/scan_lab.yaml`（网址模式）、`examples/scan_folder.yaml`（文件夹模式）。

### 网址模式要点

```yaml
target:
  type: lab / http           # lab=内置靶场 / http=外部HTTP目标
  base_url: "http://127.0.0.1:8765"
  scenario: auto             # 业务场景：auto / ecommerce,education（多场景逗号分隔）
  roles: [student, customer, admin]
  guards_file: "./lab_guards.yml"   # 靶场防护配置（蓝队修复目标）
authorization:               # 合规红线：非本地目标必填
  authorized_by: "目标所有者"
  contact: "sec@example.com"
  scope: "仅限授权测试环境"
engine:
  agent_mode: true           # 主Agent+子Agent 编排（默认开）
  concurrency: 4
```

### 文件夹模式要点

```yaml
target:
  type: folder
  folder_path: "./my-project"
  scenario: auto             # 按文件路径指纹识别业务场景
```

### MCP 模式要点

```yaml
target:
  type: mcp                        # stdio MCP 服务器（复用 dsh.mcp 客户端）
  mcp_command: ["python", "your_mcp_server.py"]   # 启动命令 argv
vectors:
  categories: [mcp_tool_abuse, mcp_data_disclosure, mcp_memory_poisoning]
  llm_variants: false              # LLM 载荷变体（需 DEEPSEEK_API_KEY，opt-in）
```

攻击样本约定：`path=工具名、body=工具参数`，扫描器向目标工具注入恶意参数并以
工具返回文本为判定证据；对话型样本对 MCP 目标自动跳过（skipped，不误报 error）。
MCP 目标为外部进程，蓝队只输出人工实施修复报告（含代码级 before/after 示例）。

### LLM 变体增强（opt-in）

```yaml
vectors:
  llm_variants: true               # 默认 false（离线确定性不受影响）
  llm_variants_per_sample: 2       # 每个基础样本的变体数上限
```

开启后（且配置了 `DEEPSEEK_API_KEY`），主 Agent 的攻击计划会为对话型基础样本
生成语义等价、措辞不同的攻击载荷变体；LLM 失败/离线时静默降级为静态变体，
验收指标（发现率/零误报）不依赖 LLM。

### LLM 自主攻击 Agent（V11，opt-in）

```yaml
engine:
  llm_agent: true                  # LLM 自主攻击（确定性多轮驱动循环）
  llm_agent_max_attacks: 100       # 攻击次数上限（默认 100；并行 Agent 间均分）
  llm_agent_timeout_s: 600         # 单个 Agent 循环总超时
  llm_agent_parallel: 2            # 并行 LLM 攻击 Agent 数（各负责一批类别，默认 2）
  llm_agent_max_tokens: 800        # 每轮 LLM 输出窗口（允许一轮批量 3~5 个工具调用）
```

开启后（需 `DEEPSEEK_API_KEY`），扫描首轮结束后主 Agent 派发 **N 个并行 LLM 自主攻击
Agent**。核心是**确定性多轮驱动循环**——不依赖模型"自觉持续攻击"（多数模型
常攻击 1~4 次就输出文本收尾），而是每轮强制模型给出下一步：

```
循环（直到 finalize / 攻击预算 / 轮次·工具预算 / 超时）：
  ① LLM 调用（tools + tool_choice=required，必须给下一步）；
  ② 解析：attack_vector → 执行真实攻击 + 确定性判定 → 记录，
     结果以文本历史回喂 LLM；finalize_report → 记录报告并结束；
  ③ 历史持续累积（超过 30 条自动压缩摘要，防止每轮延迟线性增长）。
```

- **提速**（并行 × 批量 × 早停）：
  - `llm_agent_parallel` 个 Agent 并行攻击，攻击类别轮转分桶、互不重复；
    状态型攻击跨 Agent 共享串行锁（防数据污染），攻击预算在 Agent 间均分；
  - 每轮一次批量返回 3~5 个工具调用（`llm_agent_max_tokens` 窗口），
    大幅减少 API 往返——实测 LLM 阶段从 240s+ 降到 **14s** 量级；
  - 连续 12 次攻击失败（覆盖饱和）或轮次/工具预算耗尽 → 提前收尾；
  - `http_probe` 侦察每个 Agent 最多 10 次（防只猜路径不攻击拖慢扫描）；
- **过程流式可见（非黑盒）**：每次 LLM 调用/决策/攻击判定实时广播事件——
  `agent/dispatched` 启动派发 → `llm/turn` 决策轮 → `llm/output` 模型文本流 →
  `llm/tool` 工具调用 → `attack/executed`/`attack/verdict` 攻击执行与判定 →
  `llm/stop` 收尾原因——Web 面板日志控制台逐条滚动，CLI 控制台同步输出；
- **参考攻击手法注入**：mission 自动注入样本库代表载荷（每类别一条），
  让模型模仿构造手法生成针对性变体载荷；
- 判定仍走确定性管线（LLM 无法"自我判定成功"）；无 LLM 时优雅降级为空操作。

### LLM 主动侦察工具（V13，opt-in）

```yaml
engine:
  llm_agent: true
  llm_explorer_tools: true         # 开放 http_probe / http_attack 原始探测工具
```

开启后 LLM 自主攻击 Agent 额外拥有两个工具：

- `http_probe`：GET 任意路径，返回状态码/关键响应头/响应截断（侦察用，不落判定；
  每 Agent 最多 10 次，配额用尽强制转攻击，防只猜路径拖慢扫描）；
- `http_attack`：对任意方法/路径/载荷发起原始 HTTP 请求（JSON body 自动识别），
  判定仍走确定性信号管线（敏感泄露模式 / 服务端异常）。

用途：**跳出预定义业务场景与样本库**，由 LLM 自主探索样本库之外的攻击面
（隐藏路径、未文档化端点、任意参数注入）。产出 `llm_explored` 类别漏洞，
并入主扫描结果与修复流程（无内置模板 → 人工修复方案 + 可选 LLM 修复建议）。

### LLM 修复建议（V13，opt-in）

```yaml
engine:
  llm_fix_plan: true               # 每条漏洞生成 AI 修复建议
```

蓝队规划阶段逐条漏洞询问 LLM（根因 → 修复步骤 → 关键代码片段），写入
`finding.fix.ai_plan` 与方案 `ai_note`，呈现在修复报告「🤖 AI 修复建议」节；
无 LLM / 调用失败时静默降级为内置模板，流程不中断。

### 对接真实 HTTP 目标协议约定

- 对话接口：`POST {base_url}/api/chat`，body `{"messages":[{"role":"user","content":"..."}],
  "role":"customer"}`，返回文本；
- API 类样本带 `x-role` 头传递角色上下文；
- 副作用探测（可选）：`GET {base_url}/api/state` 返回状态 JSON（带 `x-scanner-token`）；
- 业务场景识别（可选）：`GET {base_url}/api/meta/business` 返回 `{"scenarios": [...]}`；
  未提供时自动降级为端点探测（有界 GET）。

## 6. Web 面板 v2（任务追踪）

```bash
dsh-redteam web --port 8766        # 打开 http://127.0.0.1:8766
```

面板功能（全部面向验收）：

- **网址输入**：输入 `http://…` 目标 + 四个开关（LLM 自主攻击 / LLM 主动侦察 /
  LLM 修复建议 / 蓝队自动修复）→ 一键发起完整红队检测；任意 http(s) 网址
  直接输入即可（本地测试便利，无授权配置门槛）；「蓝队自动修复」仅对
  面板自带靶场生效，其余网址只输出修复方案，绝不自动修改目标；
- **源码上传**：拖拽/选择 ZIP（≤50MB）→ 面板解压（防 zip-slip）→ 静态代码扫描
  → 修复方案报告；
- **进程追踪**：每条任务有**步骤时间线**（侦察→红队攻击→蓝队修复→回归，
  每步状态/起止时间/耗时/摘要）与**实时日志控制台**（子代理派发、每次攻击
  执行与判定、漏洞确认逐条滚动，`?after=N` 增量拉取 + JSONL 落盘可回放）；
- **产物**：攻击报告 / 完整修复报告（面板内 Markdown 渲染），任务完成后可
  「🛡 蓝队修复」按需触发修复+回归并查看修复报告；
- **API**：`POST /api/tasks`、`POST /api/tasks/upload`、`GET /api/tasks`、
  `GET /api/tasks/{id}?after=N`、`GET …/report`、`POST …/fix`、
  `GET …/remediation`（见 `redteam/web/panel.py`）。

## 7. 主Agent/子Agent 编排（attack agent 架构）

扫描即一场多 Agent 协同任务：

1. **主 Agent（AttackOrchestrator）** 制定攻击计划并派发子 Agent；
2. **侦察子 Agent** 探测目标能力/安全头/业务场景指纹/端点；
3. **静态子 Agent**（文件夹模式）做代码级审计；
4. **攻击子 Agent × N**（按角色分组，每个独立 dsh scoped ctx）并行攻击，
   结构化 WorkerReport 回报主 Agent；
5. 主 Agent 汇总 → 判定/落库/攻击报告（含态势综述）。

全过程事件（`agent/dispatched`、`agent/report`、`attack/verdict`…）写入
`audit/<scan_id>.jsonl`，可回放审计。

## 8. 报告解读

产物（`out_dir/`）：
- `report_<target>_<时间>.md/.json`：**攻击报告**——漏洞总览/详情（载荷+证据+OWASP
  映射+攻击链路）/态势综述/修复工单；
- `remediation_<target>_<scan_id>.md`：**完整修复报告**（蓝队交付物）——每条漏洞的
  问题说明（现象/根因/影响）+ 分步修复 + 代码级 before/after + 验证步骤 + 回归结果；
- `audit/<scan_id>.jsonl`：事件溯源审计日志。

**存疑（suspicious）**：只命中弱信号（基线偏离/慢响应）的样本，**绝不自动上报漏洞**，
请在审计日志中人工复核。

## 9. 蓝队修复闭环

```
scan → fix --scan <id>
  ① 规划: 68 类修复模板 → 每条漏洞出方案（问题说明+修复理由，可审计）
  ② 应用: lab 目标 → guards 备份 → 收紧防护 → 热重载
          外部/文件夹目标 → 只输出人工实施方案（含代码级示例）
  ③ 回归: 重跑同一样本（uid 确定性重建，含 repeat 多步攻击），必须清零
  ④ 回滚: 未清零自动恢复备份
  ⑤ 交付: remediation_*.md 完整修复报告
```

## 10. 自适应基准解读

`dsh-redteam bench` 对靶场跑两轮：**随机基线序**（新手扫描）vs **wanter 地形序**
（学完第一轮后）。输出命中率@预算对比、覆盖效率（发现全部漏洞类别所需样本数）、
地形统计（沉积/净刻蚀深度）。

## 11. 常见问题

- **Windows 控制台中文乱码**：CLI 已自动重配 UTF-8；仍乱码用 `python -m redteam.cli ...`
  运行或直接看报告文件（UTF-8）。
- **只想测业务场景**：`target.scenario: ecommerce,education` + `vectors.categories` 留 all。
- **扫描太慢**：`profile: quick` + `variants_per_sample: 1` + `engine.min_interval_ms: 0`。
- **新增攻击样本**：`sample_bank/` 对应 YAML 加分类块（`dsh-redteam samples show <cat>` 参考格式）。
- **新增业务场景**：见 docs/SCENARIOS.md「新增业务场景的步骤」。
- **如何证明没误报**：`python -m pytest tests_redteam`（含全加固靶场 0 命中与
  拒绝话术不误报的强制测试）。
