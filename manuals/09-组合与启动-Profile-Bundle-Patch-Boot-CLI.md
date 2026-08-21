# 组合与启动组开发手册（Profile / Bundle / Patch / Boot / CLI）

> 对应 TS 版概念：`app-boot` 的 Profiles / composeEntries / boot 契约；`dsh-base` 的 base bundle。
> 源码文件：`dsh/config/profile.py`、`dsh/config/bundles/base.yml`、`dsh/boot.py`、`dsh/cli/main.py`（顶层 `run.py` 是其入口）。
> 生成方式：本文按代码逐函数撰写；函数签名以 `python -c "import inspect; ..."` 逐一核验。

## 1. 模块定位与架构位置

本组是「组合 + 挂载 + 启动」层：把 YAML 配置树（Profile/Bundle/Patch）组合成 `PluginTree`，按 `provides/inject` 拓扑排序后挂载为运行时 `Context` 服务，再交给 CLI（web/headless）或 server 使用。

- `dsh/config/profile.py`：无服务注册，纯函数库。产出 `PluginTree`（不挂载）。
- `dsh/config/bundles/base.yml`：随安装自带的 base bundle（脊柱 + 全部能力域）。
- `dsh/boot.py`：`build_patches`（构造 `--patch` 覆盖层）+ `boot`（组合 → 应用 patch 层 → 挂载）。
- `dsh/cli/main.py`：`argparse` 入口，分发到 `--dump-config` / `plugin` / `web` / `headless`。

依赖关系（关键引用）：

| 函数/模块 | 依赖 |
|---|---|
| `compose` | `PluginTree`（`dsh.kernel`）、`Context`、`apply_patch`（`dsh.kernel.loader`） |
| `boot` | `compose`、`PluginTree`、`Context` |
| `run_headless` | `boot`、`ctx.agents`、`ctx.sessions`、`AgentHandle` |
| `run_server` | `boot`、`dsh.server.app.build_app`、`uvicorn` |
| `build_patches` | `os.path.abspath`；insert 行引用 `dsh.agent.plugins:DefaultOptionsPlugin` |

组合顺序（与 loader 注释一致）：各 bundle 行 → profile patch（`<profile>/cordis.patch.yml`）→ home 级 patch（`~/.dsh/cordis.patch.yml`）→ `--patch` 覆盖层（workspace/mock/provider/model）→ `extra_patches`。后者覆盖前者。

## 2. 文件清单表

| 文件 | 职责 |
|---|---|
| `dsh/config/profile.py` | Profile 目录管理、bundle 行解析、patch 行读取、配置树组合、配置树 dump |
| `dsh/config/bundles/base.yml` | base bundle：脊柱 + 全部内置能力域的配置行（id/plugin/config） |
| `dsh/boot.py` | `build_patches` 构造覆盖层；`boot` 组合并挂载 profile |
| `dsh/cli/main.py` | CLI 参数解析与子命令分发（web/headless/plugin/dump-config） |

## 3. 类型与数据结构

本组无自定义 dataclass；关键结构来自 `dsh.kernel`：

- `PluginTree`（`dsh.kernel.tree`）：`_entries: Dict[str, Entry]`（有序 id→Entry）、`_mounted: List[MountedPlugin]`。方法 `add_bundle_rows(rows, layer_name)`、`apply_patch_rows(rows, layer_name)`、`enabled_entries()`、`entries()`、`mount()`、`dispose()`。
- `Entry`（`dsh.kernel.loader`）：`id / target / config / disabled`。
- `Context`（`dsh.kernel.context`）：服务仓库 + 事件总线。

## 4. 函数详解

### 4.1 `dsh/config/profile.py`

模块级常量：
- `SHIPPED_BUNDLES_DIR = Path(__file__).parent / "bundles"`（安装内自带 bundle 目录）。
- `PROFILE_TEMPLATES = ("web", "headless")`（模板 profile，自动初始化）。
- `DEFAULT_BUNDLES = ["base"]`。

```python
def resolve_home() -> Path
```
- Harness home：`$DSH_HOME` 优先（经 `expanduser` 展开），否则 `Path.home() / ".dsh"`；`Path.home()` 失败时回退 `Path("~/.dsh").expanduser()`。

```python
def profiles_dir() -> Path
```
返回 `resolve_home() / "profiles"`。

```python
def profile_dir(name: str) -> Path
```
返回 `profiles_dir() / name`。

```python
def init_profile(name: str, bundles: Optional[List[str]] = None) -> Path
```
幂等初始化 profile 目录：`mkdir(parents=True, exist_ok=True)`；若 `profile.yml` 不存在写 `yaml.safe_dump({"bundles": bundles or DEFAULT_BUNDLES}, allow_unicode=True)`；若 `cordis.patch.yml` 不存在写注释 + `[]`（空 patch 层）。返回目录。已存在则不动。

```python
def load_profile_manifest(name: str) -> Dict[str, Any]
```
若 `name in PROFILE_TEMPLATES` 先 `init_profile(name)`；读 `profile_dir(name)/"profile.yml"`；不存在抛 `FileNotFoundError(f"profile {name!r} not found; run 'dsh-python plugin init {name}'")`；返回 `yaml.safe_load(...) or {}`。

```python
def load_bundle_rows(name: str, profile: Optional[str] = None) -> List[dict]
```
安装内自带优先：`SHIPPED_BUNDLES_DIR/f"{name}.yml"` 存在则返回其 YAML 列表（或 `[]`）；否则若 `profile` 非空，试 `profile_dir(profile)/"bundles"/f"{name}.yml"`。两处都没有抛 `FileNotFoundError(f"bundle {name!r} not found")`。

```python
def read_patch_rows(path: Path) -> List[dict]
```
文件不存在返回 `[]`；`yaml.safe_load` 结果为 `None`（空/纯注释）返回 `[]`；非 list 抛 `ValueError(f"patch file {path} must be a YAML list")`；否则返回列表。

```python
def compose(profile: str = "web",
            extra_patches: Optional[Sequence[Tuple[List[dict], str]]] = None
            ) -> Tuple[PluginTree, List[Tuple[List[dict], str]]]
```
- `manifest = load_profile_manifest(profile)`；`bundle_names = manifest.get("bundles") or DEFAULT_BUNDLES`。
- `ctx = Context(f"boot:{profile}")`；`tree = PluginTree(ctx)`。
- 对每个 bundle 名、每个 `load_bundle_rows(name, profile)` 行：`tree.add_bundle_rows([row], layer_name=f"bundle:{name}")`。
- 读 `profile_dir(profile)/"cordis.patch.yml"` 与 `resolve_home()/"cordis.patch.yml"`；按 `(rows, f"profile:{profile}")`、`(rows, "home")` 顺序，非空则 `tree.apply_patch_rows(rows, label)` 并记入 `layers`。
- 再对 `extra_patches or []` 逐个 `tree.apply_patch_rows(rows, label)` 并记入 `layers`。
- 返回 `(tree, layers)`（层顺序即优先级，后者覆盖前者）。

```python
def dump_config(tree: PluginTree) -> str
```
渲染组合后的配置树（一行一个插件）：对 `tree.entries()` 每个 entry 输出 `- id: {id}  [enabled|disabled]`、`    plugin: {target!r}`，若 `entry.config` 非空再输出 `    config: {config!r}`。返回拼接文本。

### 4.2 `dsh/config/bundles/base.yml`

每行含义与依赖关系（`id` / `plugin` / 可选 `config` / 可用 `disabled`）：

| id | plugin | 说明 |
|---|---|---|
| `sessions` | `dsh.session.store:SessionStore` | 会话仓库 `ctx.sessions`（provides `sessions`） |
| `system-prompt` | `dsh.prompt.system_prompt:SystemPromptService` | 系统提示组装 `ctx.systemPrompt` |
| `tools` | `dsh.tools.registry:ToolRuntime` | 工具注册表 `ctx.tools` |
| `llm` | `dsh.llm.adapters:LlmRuntime` | LLM 适配器注册表 `ctx.llm` |
| `llm-deepseek` | `dsh.llm.plugins:DeepSeekAdapterPlugin` | 注册 deepseek 适配器（`--mock` 时被 disable） |
| `llm-mock` | `dsh.llm.plugins:MockAdapterPlugin` | 注册 mock 适配器 |
| `agents` | `dsh.agent.agent:AgentRegistry` | agent 注册表 `ctx.agents` |
| `agent-loop` | `dsh.agent.loop:AgentLoopService` | 驱动循环 `ctx.agentLoop`（挂载时 `agents.set_factory(self)`） |
| `approval` | `dsh.agent.approval:ApprovalService` | 人机审批 `ctx.approval` |
| `persistence` | `dsh.persistence.jsonl:JsonlPersistence`，`config: {dir: ~/.dsh/sessions}` | 会话持久化（JSONL 目录） |
| `persona` | `dsh.prompt.sections:PersonaPlugin` | harness 身份 + persona 提示分节 |
| `fs` | `dsh.fs.local:LocalFsService` | 文件系统服务 `ctx.fs`（工作区根，`--workspace` 覆盖其 root） |
| `tool-fs` | `dsh.fs.tool_fs:ToolFsPlugin` | 注册 fs_* 工具 |
| `subprocess` | `dsh.subprocess.local:SubprocessService` | 子进程服务 |
| `tool-bash` | `dsh.subprocess.tool_bash:ToolBashPlugin` | 注册 `bash` 工具 |
| `jobs` | `dsh.jobs.jobs:JobsService` | 后台任务注册表 `ctx.jobs` |
| `tool-jobs` | `dsh.jobs.jobs:ToolJobsPlugin` | 注册 job_* 工具 |
| `todo-tool` | `dsh.todo.todo:ToolTodoPlugin` | 注册 `todo_write` 工具 |
| `subagents` | `dsh.subagent.subagent:SubagentRegistry` | 子代理注册表 `ctx.subagents` |
| `subagent-in-process` | `dsh.subagent.subagent:InProcessProviderPlugin` | 注册 `in-process` provider |
| `tool-subagent` | `dsh.subagent.subagent:ToolSubagentPlugin` | 注册 `subagent` 工具 |
| `goals` | `dsh.goal.goal:GoalService` | 目标状态 `ctx.goals` |
| `goal-round-driver` | `dsh.goal.goal:GoalRoundDriverPlugin` | 目标续轮驱动 |
| `tool-goals` | `dsh.goal.goal:ToolGoalPlugin` | 注册 goal_* 工具 |
| `compaction` | `dsh.compaction.compaction:CompactionService` | 压缩服务 `ctx.compaction` |
| `compaction-policy` | `dsh.compaction.compaction:CompactionPolicyPlugin` | 压力驱动自动压缩 |
| `commands` | `dsh.commands.commands:CommandRegistry` | 命令注册表 `ctx.commands` |
| `commands-builtin` | `dsh.commands.commands:BuiltinCommandsPlugin` | 内置命令 |
| `plan-mode` | `dsh.plan.plan_mode:PlanModeService` | 计划模式状态 `ctx.planMode` |
| `plan-plugin` | `dsh.plan.plan_mode:PlanModePlugin` | 计划模式分节 + 只读守卫 + 退出工具 |
| `session-title` | `dsh.session.title:SessionTitleService` | 会话标题 provider `ctx.sessionTitle`（首条用户消息截断 60 字符，可注册自定义 provider） |
| `telemetry` | `dsh.telemetry.service:SessionTelemetryService` | 会话遥测分发 `ctx.sessionTelemetry`（`session-telemetry/record` waterfall） |
| `telemetry-console` | `dsh.telemetry.service:ConsoleTelemetryPlugin`，`config: {enabled: false}` | JSONL 遥测后端（replayable trace，默认关闭） |
| `settings` | `dsh.settings.service:SettingsService` | 设置服务 `ctx.settings`（settings.json 后端 + settings/updated） |
| `storage` | `dsh.storage.service:StorageService` | 分区存储 `ctx.storage`（storage.json + domain/changed） |
| `web-tools` | `dsh.web.tool:WebPlugin` | 注册 `web_fetch`/`web_search` 工具 |
| `user-questions` | `dsh.interaction.user_questions:UserQuestionsService` | 文本问答通道 `ctx.userQuestions`（`ask_user` 用） |
| `ask-user-tool` | `dsh.interaction.user_questions:ToolAskUserPlugin` | 注册 `ask_user` 工具 |
| `time-context` | `dsh.context.time_context:TimeContextPlugin` | 动态上下文注入当前时间 |
| `persistence-sqlite` | `dsh.persistence.sqlite:SqlitePersistence`，`disabled: true` | SQLite 持久化后端（与 JSONL 互斥：同一时刻只能启用一个，同时启用会双写；切换 = disable 其一 + enable 另一） |
| `skills` | `dsh.skill.skill:SkillService` | 技能注册表 `ctx.skills` |
| `skills-plugin` | `dsh.skill.skill:SkillsPlugin` | 注册 `skill_list`/`skill_load` 工具 |
| `hooks` | `dsh.hooks.hooks:HooksPlugin` | hooks.yml 桥（shell 命令 + 事件化） |
| `agent-presets` | `dsh.preset.presets:AgentPresets` | agent 预设注册表 `ctx.agentPresets`（发布前挂到 agent 作用域） |
| `schedule` | `dsh.schedule.schedule:ScheduleService` | 定时任务 `ctx.schedule`（interval + cron 表达式，见手册 15） |
| `tool-schedule` | `dsh.schedule.schedule:ToolSchedulePlugin` | 注册 `schedule_register`/`schedule_list`/`schedule_remove` 工具 |
| `sandbox` | `dsh.sandbox.sandbox:SandboxService` | 进程限制缝 stub `ctx.sandbox`（local=identity，留扩展点） |
| `mcp-example` | `dsh.mcp.mcp:McpServerPlugin`，`disabled: true` | MCP 示例行（第三批）：patch 启用后启动 stdio MCP 服务器并把远端工具注册进 `ctx.tools`；`command` 为 argv 列表、`prefix` 可选前缀 |

依赖关系由 `inject` 声明、`_topo_sort` 拓扑排序保证：`inject` 里列出的服务 key 必须先由某 `provides` 插件挂载（`base.yml` 中所有依赖都能在本 bundle 内满足）。`provides=None` 的插件（如 `persona`、`DefaultOptionsPlugin`）不参与图，视作叶子最后挂载。

### 4.3 `dsh/boot.py`

```python
def build_patches(workspace: Optional[str] = None,
                  mock_llm: bool = False,
                  provider: Optional[str] = None,
                  model: Optional[str] = None,
                  profile: str = "web") -> List[Tuple[List[dict], str]]
```
构造 `--patch` 覆盖层列表（每项 `(rows, label)`）：
- `workspace` 非空：追加 `([{"id": "fs", "config": {"root": os.path.abspath(workspace)}}], "--workspace")`（整体替换 `fs` 行 config 的 `root`）。
- `mock_llm` 为真：追加 `([{"disable": ["llm-deepseek"]}], "--mock")`（禁用 deepseek 适配器）。
- `provider or model` 非空：组装 `config`（含 `provider`/`model`），追加 `([{"insert": [{"id": "agent-default-options", "plugin": "dsh.agent.plugins:DefaultOptionsPlugin", "config": config}]}], "--provider")`（插入默认选项插件，供 `AgentLoopService._default_config` 回退）。
- 恒追加 `--hmr` 层：`([{"id": "config-watcher", "config": {"paths": [profile 的 cordis.patch.yml, home 的 cordis.patch.yml]}}], "--hmr")`（把本 profile 的 patch 路径注入热重载监视器）。
返回 layers 列表。

```python
async def boot(profile: str = "web", workspace: Optional[str] = None,
               mock_llm: bool = False, provider: Optional[str] = None,
               model: Optional[str] = None,
               extra_patches: Optional[Sequence[Tuple[List[dict], str]]] = None
               ) -> Tuple[Context, PluginTree]
```
- `tree, _layers = compose(profile)`（compose 已应用 profile/home patch 层）。
- `layers = build_patches(workspace, mock_llm, provider, model, profile)`；`layers.extend(extra_patches or [])`。
- 对每个 `(rows, label)` 执行 `tree.apply_patch_rows(rows, label)`。
- `await tree.mount()`（拓扑排序挂载；失败 `PluginTree.mount` 内部 `_rollback` 后抛 `LoaderError`）。
- `tree.ctx.set("pluginTree", tree)`（HMR 入口：watcher 经 ctx.pluginTree 做运行期增量操作）。
- 返回 `(tree.ctx, tree)`。

### 4.4 `dsh/cli/main.py`

```python
def build_parser() -> argparse.ArgumentParser
```
构建 `argparse` 解析器：prog `dsh-python`。全局参数：`--profile`、`--workspace`、`--mock`、`--provider`、`--model`、`--dump-config`。
子命令（`dest="command"`）：
- `web`（`add_common` + `--port`(int, 3080) + `--host`(default `127.0.0.1`)）。
- `headless`（`add_common` + 位置参数 `prompt` + `--max-tokens`(int, None)）。
- `plugin`（`action` choices `init|list|path` + `name` nargs="?"）。
内部函数 `add_common(sub_parser)` 为 web/headless 添加 `--workspace/--mock/--provider/--model`。

```python
def main(argv: Optional[List[str]] = None) -> int
```
`setup_logging()` → 对 `sys.stdout/sys.stderr` 尝试 `reconfigure(encoding="utf-8", errors="replace")`（Windows 控制台默认 GBK，重配 UTF-8 避免中文输出乱码；`hasattr` 守卫 + 异常吞掉）→ `parse_args` → `profile = args.profile or ("web" if args.command != "headless" else "headless")`。
- `args.dump_config`：`compose(profile)` + `dump_config`，打印后返回 0。
- `args.command == "plugin"`：`return _plugin_cmd(args)`。
- `args.command == "web"`：`asyncio.run(run_server(profile=..., workspace=..., mock=..., provider=..., model=..., host=..., port=...))`，返回 0。
- `args.command == "headless"`：`asyncio.run(run_headless(...))`，返回 0。
- 否则 `parser.print_help()` 返回 1。

```python
def _plugin_cmd(args: argparse.Namespace) -> int
```
- `init`：`name = args.name or "web"`，`init_profile(name)`，打印 `profile {name!r} initialized at {profile_dir(name)}`，返回 0。
- `path`：打印 `profile_dir(args.name or "web")`，返回 0。
- `list`：`profiles_dir()` 不存在打印 `(no profiles)`；否则遍历子目录打印目录名，返回 0。
- 其它返回 1。

```python
async def run_headless(prompt: str, profile: str, workspace: Optional[str],
                       mock: bool, provider: Optional[str],
                       model: Optional[str], max_tokens: Optional[int]) -> None
```
- `ctx, tree = await boot(profile="headless", workspace=workspace, mock_llm=mock, provider=provider, model=model)`（注意：固定用 `headless` profile，忽略传入 `profile`）。
- `agent = await ctx.agents.create()`；`agent.followup(prompt)`；`await agent.when_idle()`；`await asyncio.sleep(0.05)`；`await ctx.sessions.flush(agent.session)`。
- `messages = agent.session.derive_messages()`；取最后 assistant 消息打印 `plain_text()`，无则打印 `(no assistant reply)`。
- `finally`：对 `ctx.agents.list()` 逐个 `AgentHandle(agent).dispose()`，再 `await tree.dispose()`。

## 5. 关键流程

### 5.1 boot 全流程
`boot(profile)` → `compose(profile)`：读 manifest 得 bundle 列表 → 逐行 `add_bundle_rows`（bundle:name 层）→ 应用 profile patch → home patch →（boot 内）`build_patches` 覆盖层（workspace/mock/provider）→ `extra_patches` → `tree.mount()`：`_topo_sort(enabled_entries())` 按 provides/inject 拓扑排序 → 逐个实例化并 `apply(ctx)` + `start()`；失败 `_rollback` 逆序 close 已挂载 → 抛 `LoaderError`。返回 `(tree.ctx, tree)`。

### 5.2 patch 应用语义（`apply_patch`）
顶层 YAML 列表，每行三种形态：
- `{"disable": [id,...]}`：把对应 entry 置 `disabled=True`（未知 id 仅 warning）。
- `{"insert": [{id, plugin, config}]}`：插入新行（id 重复抛 `LoaderError`）。
- `{"id": id, "config": {...}}`：整体替换该行 config（非深合并），并按新 config 里的 `enabled/disabled` 平台条件重算 `disabled`。
条件禁用：`disabled: {platform: [win32]}`（平台在列表则禁用）、`enabled: {platform: [linux, darwin]}`（在列表才启用）。

### 5.3 CLI 分发
`python run.py ...` → `main()` → 按 `command` 与 `--dump-config` 分发：dump 打印配置树；`web` 走 server；`headless` 走一次性运行；`plugin init/list/path` 管理 profile 目录。

## 6. 事件与扩展点

本组几乎不 emit 事件；扩展点体现在：
- Patch 三层（profile / home / `--patch`）是配置级扩展点。
- `build_patches` 的 insert 行 `agent-default-options`（`dsh.agent.plugins:DefaultOptionsPlugin`）提供 `ctx.agentDefaultModel`，被 `AgentLoopService._default_config` 消费（未在 base bundle 中声明，仅当 `--provider/--model` 时插入）。
- `extra_patches` 参数允许调用方注入额外覆盖层。

## 7. 常见改动指引

- **新增一个 bundle**：在 `dsh/config/bundles/` 下放 `<name>.yml`（或 profile 目录 `bundles/<name>.yml`），并在 profile 的 `profile.yml` 的 `bundles` 列表加入 `name`。
- **新增一个插件行**：在 base bundle 或用户 patch 的 `insert` 里写 `{id, plugin: "module.path:Attr", config: {...}}`，确保 `plugin` 是 `module:Attr` 形式。
- **在 patch 里禁用一个内置工具**：例如 `- disable: [tool-bash]`（禁 `bash`）、`[todo-tool]`（禁 todo）、`[tool-goals]`/`[tool-jobs]`/`[tool-subagent]` 等，id 见 base.yml。
- **替换一个内置服务 config**：`- id: fs` + `config: {root: "C:/path"}`（整体替换，非深合并，需写全 config）。
- **改默认端口/主机**：改 `build_parser` 的 `web.add_argument("--port", ..., default=3080)` 与 `--host` 默认值。
- **新增一个 CLI 子命令**：在 `build_parser` 加 `sub.add_parser(...)`，在 `main` 加分支。
- **改 headless 输出逻辑**：改 `run_headless`（例如打印全部 assistant 而非最后一条）。

## 8. 相关测试

- `tests/test_domains.py::test_boot_base_bundle`：`boot(profile="headless", workspace=..., mock_llm=True)` 后断言全部服务 key 可解析（含补齐批次新增的 `sessionTitle`/`sessionTelemetry`/`settings`/`storage`/`skills`/`userQuestions`/`schedule`/`sandbox`/`agentPresets`）、mock provider 与 `fs_read`/`bash`/`todo_write`/`subagent`/`web_fetch`/`ask_user`/`skill_list`/`schedule_register` 工具已注册——覆盖 `compose`/`boot`/`base.yml` 组合与挂载。
- CLI/server 无独立测试。手动验证：
  - `python run.py --dump-config` 打印组合后的配置树。
  - `python run.py plugin list` / `python run.py plugin init web` 管理 profile。
  - `python run.py headless "你好" --mock` 一次性运行。
  - `python run.py web --port 3081 --mock` 启动 Web UI。
