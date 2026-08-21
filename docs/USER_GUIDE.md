# 用户手册（USER_GUIDE）

## 1. 安装

```bash
cd dsh_red_blue_team
pip install -r requirements.txt        # httpx / PyYAML / fastapi / uvicorn（dsh 框架已内置）
pip install -e .                       # 可选：安装 dsh-redteam / dsh-python 命令行入口
```

无需 API Key 即可运行：判定器是确定性的，内置靶场不依赖真实 LLM。
配置 `DEEPSEEK_API_KEY` 后自动启用 DeepSeek 适配器（LLM 弱信号裁判/攻击态势综述）。

## 2. 两种输入模式

| 模式 | 命令 | 适用 |
|:--|:--|:--|
| **网址动态扫描** | `dsh-redteam scan --config scan.yml` | 已部署的业务系统（HTTP API / 对话 agent） |
| **文件夹静态扫描** | `dsh-redteam static <文件夹>` 或 `scan` + `type: folder` | 本地项目源码（代码级审计，免授权声明） |

## 3. 命令一览

| 命令 | 作用 |
|:--|:--|
| `dsh-redteam demo [--out DIR]` | **一键演示**：起靶场→扫描（4 业务场景）→修复→回归→复扫验收 |
| `dsh-redteam scan --config scan.yml [--fix]` | 网址/文件夹扫描（主Agent+子Agent 编排） |
| `dsh-redteam static <文件夹> [--scenario auto]` | 免配置快速静态扫描 |
| `dsh-redteam fix --config scan.yml --scan ID [--dry-run]` | 蓝队：方案/沙箱应用/回归/**完整修复报告** |
| `dsh-redteam lab [--port 8765] [--guards FILE]` | 启动内置靶场（37 个埋入漏洞） |
| `dsh-redteam report --config scan.yml [--list｜--scan ID]` | 历史扫描/重建报告 |
| `dsh-redteam bench --config scan.yml` | 自适应优先级基准（随机基线 vs wanter 地形序） |
| `dsh-redteam samples [list｜show CATEGORY]` | 攻击样本库（51 类别） |
| `dsh-redteam scenarios [list｜show ID]` | 业务场景库（12 场景指纹与攻击点） |

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

### 对接真实 HTTP 目标协议约定

- 对话接口：`POST {base_url}/api/chat`，body `{"messages":[{"role":"user","content":"..."}],
  "role":"customer"}`，返回文本；
- API 类样本带 `x-role` 头传递角色上下文；
- 副作用探测（可选）：`GET {base_url}/api/state` 返回状态 JSON（带 `x-scanner-token`）；
- 业务场景识别（可选）：`GET {base_url}/api/meta/business` 返回 `{"scenarios": [...]}`；
  未提供时自动降级为端点探测（有界 GET）。

## 6. 主Agent/子Agent 编排（attack agent 架构）

扫描即一场多 Agent 协同任务：

1. **主 Agent（AttackOrchestrator）** 制定攻击计划并派发子 Agent；
2. **侦察子 Agent** 探测目标能力/安全头/业务场景指纹/端点；
3. **静态子 Agent**（文件夹模式）做代码级审计；
4. **攻击子 Agent × N**（按角色分组，每个独立 dsh scoped ctx）并行攻击，
   结构化 WorkerReport 回报主 Agent；
5. 主 Agent 汇总 → 判定/落库/攻击报告（含态势综述）。

全过程事件（`agent/dispatched`、`agent/report`、`attack/verdict`…）写入
`audit/<scan_id>.jsonl`，可回放审计。

## 7. 报告解读

产物（`out_dir/`）：
- `report_<target>_<时间>.md/.json`：**攻击报告**——漏洞总览/详情（载荷+证据+OWASP
  映射+攻击链路）/态势综述/修复工单；
- `remediation_<target>_<scan_id>.md`：**完整修复报告**（蓝队交付物）——每条漏洞的
  问题说明（现象/根因/影响）+ 分步修复 + 代码级 before/after + 验证步骤 + 回归结果；
- `audit/<scan_id>.jsonl`：事件溯源审计日志。

**存疑（suspicious）**：只命中弱信号（基线偏离/慢响应）的样本，**绝不自动上报漏洞**，
请在审计日志中人工复核。

## 8. 蓝队修复闭环

```
scan → fix --scan <id>
  ① 规划: 51 类修复模板 → 每条漏洞出方案（问题说明+修复理由，可审计）
  ② 应用: lab 目标 → guards 备份 → 收紧防护 → 热重载
          外部/文件夹目标 → 只输出人工实施方案（含代码级示例）
  ③ 回归: 重跑同一样本（uid 确定性重建，含 repeat 多步攻击），必须清零
  ④ 回滚: 未清零自动恢复备份
  ⑤ 交付: remediation_*.md 完整修复报告
```

## 9. 自适应基准解读

`dsh-redteam bench` 对靶场跑两轮：**随机基线序**（新手扫描）vs **wanter 地形序**
（学完第一轮后）。输出命中率@预算对比、覆盖效率（发现全部漏洞类别所需样本数）、
地形统计（沉积/净刻蚀深度）。

## 10. 常见问题

- **Windows 控制台中文乱码**：CLI 已自动重配 UTF-8；仍乱码用 `python -m redteam.cli ...`
  运行或直接看报告文件（UTF-8）。
- **只想测业务场景**：`target.scenario: ecommerce,education` + `vectors.categories` 留 all。
- **扫描太慢**：`profile: quick` + `variants_per_sample: 1` + `engine.min_interval_ms: 0`。
- **新增攻击样本**：`sample_bank/` 对应 YAML 加分类块（`dsh-redteam samples show <cat>` 参考格式）。
- **新增业务场景**：见 docs/SCENARIOS.md「新增业务场景的步骤」。
- **如何证明没误报**：`python -m pytest tests_redteam`（含全加固靶场 0 命中与
  拒绝话术不误报的强制测试）。
