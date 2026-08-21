# dsh-python 开发手册总目录

> DeepSeek Harness 的 Python 重实现 —— 每个模块的开发手册，覆盖到函数级技术细节。
> 各手册均由源码逐函数提取（签名经 `inspect` 验证），与代码同步。
> wanter 专题（四法则/机制/量化指标/图表）见仓库根目录 **[WANTER.md](../WANTER.md)**。

| 编号 | 手册 | 覆盖模块 | 源码 |
|---|---|---|---|
| 00 | [总览与架构](00-%E6%80%BB%E8%A7%88%E4%B8%8E%E6%9E%B6%E6%9E%84.md) | 全局心智模型、架构线对应表 | — |
| 01 | [插件内核](01-kernel-%E6%8F%92%E4%BB%B6%E5%86%85%E6%A0%B8.md) | Context / EventBus / Service / Loader / PluginTree | `dsh/kernel/` |
| 02 | [会话日志](02-session-%E4%BC%9A%E8%AF%9D%E6%97%A5%E5%BF%97.md) | SessionEventMap / surface / derive / SessionStore | `dsh/session/` |
| 03 | [工具系统](03-tools-%E5%B7%A5%E5%85%B7%E7%B3%BB%E7%BB%9F.md) | schema / define_tool / 五段管线 / 展示词汇 | `dsh/tools/` |
| 04 | [LLM 接缝](04-llm-%E6%A8%A1%E5%9E%8B%E6%8E%A5%E7%BC%9D.md) | 消息词汇 / 流协议 / 适配器 / DeepSeek / mock | `dsh/llm/` |
| 05 | [Agent 与循环](05-agent-%E6%99%BA%E8%83%BD%E4%BD%93%E4%B8%8E%E5%BE%AA%E7%8E%AF.md) | Agent / Inbox / 注册表 / 审批 / 驱动循环 | `dsh/agent/` |
| 06 | [System Prompt](06-prompt-%E7%B3%BB%E7%BB%9F%E6%8F%90%E7%A4%BA%E8%AF%8D.md) | 分节 / 变量 / 工具 provider / 组装 | `dsh/prompt/` |
| 07 | [持久化与执行](07-%E6%8C%81%E4%B9%85%E5%8C%96%E4%B8%8E%E6%89%A7%E8%A1%8C-%E6%8C%81%E4%B9%85%E5%8C%96-%E6%96%87%E4%BB%B6%E7%B3%BB%E7%BB%9F-%E5%AD%90%E8%BF%9B%E7%A8%8B.md) | JSONL / 崩溃修复 / fs 围栏 / bash 工具 | `dsh/persistence/` `dsh/fs/` `dsh/subprocess/` |
| 08 | [高级能力](08-%E9%AB%98%E7%BA%A7%E8%83%BD%E5%8A%9B-%E5%AD%90%E4%BB%A3%E7%90%86-%E7%9B%AE%E6%A0%87-%E5%8E%8B%E7%BC%A9-%E5%91%BD%E4%BB%A4-%E4%BB%BB%E5%8A%A1-%E8%AE%A1%E5%88%92.md) | subagent / goal / compaction / commands / jobs / plan / todo | 对应目录 |
| 09 | [组合与启动](09-%E7%BB%84%E5%90%88%E4%B8%8E%E5%90%AF%E5%8A%A8-Profile-Bundle-Patch-Boot-CLI.md) | Profile / Bundle / Patch / boot / CLI | `dsh/config/` `dsh/boot.py` `dsh/cli/` |
| 10 | [服务端与 Web UI](10-%E6%9C%8D%E5%8A%A1%E7%AB%AF-Web%E7%95%8C%E9%9D%A2.md) | FastAPI REST / SSE / 前端协议 | `dsh/server/` |
| 11 | [快速开始与 PyCharm 指南](11-%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B%E4%B8%8EPyCharm%E6%8C%87%E5%8D%97.md) | 安装、运行、密钥、自定义、排障 | — |
| 12 | [新增子系统](12-%E6%96%B0%E5%A2%9E%E5%AD%90%E7%B3%BB%E7%BB%9F-%E8%AE%BE%E7%BD%AE-%E9%81%A5%E6%B5%8B-%E5%AD%98%E5%82%A8-%E6%8A%80%E8%83%BD-Hooks-Preset-Schedule-%E6%B2%99%E7%AE%B1-Web.md) | 补齐批次新增缝（函数级） | 对应目录 |
| 13 | [与 TS 版差异对照](13-%E4%B8%8ETS%E7%89%88%E5%B7%AE%E5%BC%82%E5%AF%B9%E7%85%A7%E4%B8%8E%E8%A1%A5%E9%BD%90%E8%AE%B0%E5%BD%95.md) | 逐条差异对照 + 补齐实施记录 | — |
| 14 | [第二批补齐](14-%E7%AC%AC%E4%BA%8C%E6%89%B9-%E5%87%AD%E6%8D%AE-%E8%AE%A1%E9%87%8F-%E6%8C%87%E4%BB%A4-%E4%BF%AE%E5%89%AA-%E6%9F%A5%E8%AF%A2-%E5%8F%8D%E9%A6%88-%E5%B7%A5%E4%BD%9C%E6%B5%81.md) | credentials / token-meter / agent-instructions / pruner / session-query / feedback / workflow | `dsh/credentials/` `dsh/llm/token_meter.py` `dsh/context/instructions.py` `dsh/compaction/pruner.py` `dsh/session/query.py` `dsh/feedback/` `dsh/workflow/` |
| 15 | [MCP 与 Cron](15-MCP%E4%B8%8ECron-%E6%A8%A1%E5%9E%8B%E4%B8%8A%E4%B8%8B%E6%96%87%E5%8D%8F%E8%AE%AE%E5%AE%A2%E6%88%B7%E7%AB%AF%E4%B8%8E%E5%AE%9A%E6%97%B6%E8%A1%A8%E8%BE%BE%E5%BC%8F.md) | MCP stdio 客户端 / cron 表达式 | `dsh/mcp/` `dsh/schedule/cron.py` `dsh/schedule/schedule.py` |
| 16 | [wanter 架构设计](16-wanter%E6%9E%B6%E6%9E%84%E8%AE%BE%E8%AE%A1%E6%89%8B%E5%86%8C.md) | 四法则数学建模 / 落层决策 / 实验证据 | `dsh/wanter/` |
| 17 | [第六批补齐](17-%E7%AC%AC%E5%85%AD%E6%89%B9-%E8%AE%B0%E5%BF%86-%E9%A2%84%E8%AE%BE%E6%8D%A2%E7%BB%91-%E7%BB%93%E6%9E%84%E5%8C%96%E5%B7%A5%E4%BD%9C%E6%B5%81-%E8%AF%B7%E6%B1%82%E8%A7%82%E6%B5%8B.md) | memory / preset recompose / workflow 结构化输出 / agent/request-done | `dsh/memory/` `dsh/preset/presets.py` `dsh/workflow/workflow.py` `dsh/agent/loop.py` |
| 18 | [Code Mode](18-CodeMode-%E4%BB%A3%E7%A0%81%E6%A8%A1%E5%BC%8F.md) | run_code 传输 / Python SDK / 代码执行 seam / 派发桥 / code-only 强制 | `dsh/code/` `dsh/tools/registry.py` |
| 19 | [自指 cordis](19-%E8%87%AA%E6%8C%87cordis-%E5%8A%A8%E6%80%81%E6%8F%92%E4%BB%B6%E8%BF%90%E8%A1%8C%E5%99%A8.md) | 动态 Cordis Plugin 运行器 / host 沙箱 / inspect 目录 / cordis_* 工具 / 事件四件套 | `dsh/cordis/` |
| 20 | [读写投影类](20-%E8%AF%BB%E5%86%99%E6%8A%95%E5%BD%B1-%E4%BC%9A%E8%AF%9D%E6%8A%95%E5%BD%B1-%E5%B7%A5%E4%BD%9C%E5%8C%BA-%E4%BC%9A%E8%AF%9D%E5%BC%95%E7%94%A8.md) | sessionProjections / workspaceRegistry / sessionReferenceResolver | `dsh/projection/` `dsh/workspace/` `dsh/context/session_reference.py` |

## 阅读顺序建议

- 全局理解：00 → 01 → 05 → 09
- 扩展开发：03（加工具）、04（加 provider）、08（加能力）、examples/my_plugin.py
- 界面改造：10
- 排障与运行：11
