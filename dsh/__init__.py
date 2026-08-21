"""
dsh-python: DeepSeek Harness 的 Python 重实现。

一切皆插件：Context（服务仓库）+ 类型化事件（emit/waterfall/parallel/serial）
+ 可逆效应（effect）+ YAML 配置树（Profile/Bundle/Patch）。

包结构（与 docs 架构一一对应）:
    dsh.kernel        —— Cordis 式插件内核
    dsh.session       —— 事件溯源会话日志
    dsh.tools         —— 工具注册表与守卫执行管线
    dsh.llm           —— LLM 适配器接缝
    dsh.prompt        —— System Prompt 组装
    dsh.agent         —— Agent 句柄与驱动循环
    dsh.persistence   —— 会话持久化
    dsh.fs / dsh.subprocess / dsh.subagent / dsh.goal / dsh.compaction
    dsh.commands / dsh.jobs / dsh.plan / dsh.todo
                      —— 核心能力域
    dsh.skill / dsh.hooks / dsh.preset / dsh.schedule / dsh.sandbox
    dsh.web / dsh.interaction / dsh.context
                      —— 补齐批次能力域（对应 TS 版同名包）
    dsh.settings / dsh.storage / dsh.telemetry / dsh.persistence
                      —— 缝与后端
    dsh.config        —— Profile / Bundle / Patch 组合
    dsh.cli           —— 命令行入口
    dsh.server        —— FastAPI Web UI
"""
import logging

__version__ = "0.1.0"

_LOGGER_INITIALIZED = False


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志器（幂等）。所有模块统一使用 `logging.getLogger("dsh.*")`。"""
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("dsh")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _LOGGER_INITIALIZED = True
