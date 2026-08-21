# 11 · 快速开始与 PyCharm 运行指南

> 覆盖：环境准备、依赖安装、三种运行方式、模型密钥配置、自定义 profile、常见问题排查。

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.10（在 3.11.15 上完整验证） |
| 依赖 | fastapi / uvicorn / httpx / PyYAML（测试加 pytest、pytest-asyncio） |
| 网络 | 可选：不配置密钥时用内置 mock 适配器，完全离线可用 |

## 2. PyCharm 打开项目

1. **File → Open** 选择 `dsh_python` 目录（项目根 = 本目录，`dsh` 是顶层包）；
2. 确认解释器为 Python ≥ 3.10（File → Settings → Project → Python Interpreter）；
3. 打开 PyCharm 终端（Alt+F12），安装依赖：

```sh
pip install -r requirements.txt
```

4. 验证：

```sh
python -m pytest tests -q
# 期望输出: 224 passed, 1 skipped（Linux-only Landlock 用例）
```

## 3. 三种运行方式

### 3.1 Web UI（推荐）

直接在 PyCharm 中运行 `run.py`（或终端）：

```sh
python run.py web            # http://127.0.0.1:3080
python run.py web --port 8080 --mock
```

界面功能：会话列表 / 流式聊天 / 工具卡片（call→result）/ 斜杠命令
（`/help` `/goal` `/compact` `/plan`）/ 审批弹窗（ask 决策）/ 状态指示 / 停止按钮。

### 3.2 headless 一次性运行

```sh
python run.py headless "列出当前目录的 Python 文件" --mock
python run.py headless "写一个 hello.py" --workspace C:\my_project
```

有 `DEEPSEEK_API_KEY` 时去掉 `--mock` 即用真实模型。

### 3.3 配置检查与插件管理

```sh
python run.py --dump-config                  # 打印组合后的配置树
python run.py plugin init myprofile          # 初始化自定义 profile
python run.py plugin list                    # 列出全部 profile
python run.py plugin path web                # 打印 profile 目录路径
```

## 4. 模型密钥配置

```powershell
# PowerShell（或系统环境变量）
$env:DEEPSEEK_API_KEY = "sk-..."
# 可选: $env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
```

优先级（见 `dsh/agent/loop.py::_default_config`）：
agent options → `ctx.agentDefaultModel`（--provider/--model）→ 有密钥用 deepseek，否则 mock。

## 5. 自定义 Profile（三层配置）

```yaml
# ~/.dsh/profiles/web/cordis.patch.yml
- id: persona                       # 按 id 整体替换 config
  config:
    persona: "你是一个精通 Python 的资深工程师。"
- disable: [plan-plugin]            # 禁用计划模式
- insert:                           # 挂载自己的插件
  - id: my-plugin
    plugin: examples.my_plugin:MyPlugin
    config: { greeting: "早上好" }
```

生效顺序：bundle 行 → profile patch → home 级 `~/.dsh/cordis.patch.yml` → `--patch`。
web/headless 两个 profile 首次使用自动初始化。

## 6. 数据目录（~/.dsh）

```
~/.dsh/
├── profiles/<name>/profile.yml        # bundles 清单
├── profiles/<name>/cordis.patch.yml   # 用户 patch 层
├── profiles/<name>/bundles/*.yml      # 本地 bundle
├── cordis.patch.yml                   # home 级 patch
└── sessions/<session-id>.jsonl        # 会话持久化（首行 header + 事件行）
```

## 7. 常见问题排查

| 现象 | 原因与处理 |
|---|---|
| `ModuleNotFoundError: fastapi` | 未装依赖：`pip install -r requirements.txt` |
| headless 报 `DEEPSEEK_API_KEY is not set` | 未配密钥且未加 `--mock`；二选一 |
| 工具执行被拒 `approval unavailable` | headless 模式无审批通道，ask 决策自动拒绝（预期行为；Web UI 会弹窗） |
| `path escapes workspace root` | fs 工具只能访问工作区（`--workspace` 指定的根），越界被围栏拒绝 |
| 会话文件在 ~/.dsh/sessions | 正常：JSONL 持久化；崩溃的 turn 会在加载时以 `interrupted` 修复 |
| 端口 3080 被占用 | `python run.py web --port 3081` |

## 8. 学习路径建议

1. 读 `manuals/00-总览与架构.md`（全局心智模型）；
2. 读 `manuals/01`（kernel）与 `manuals/05`（agent 循环）——两条主线；
3. 对着 `examples/my_plugin.py` 动手写第一个插件；
4. 读 `manuals/09` 学会用 patch 调整组合；
5. 需要哪块细节再查对应编号手册（每份都覆盖到函数级）。
