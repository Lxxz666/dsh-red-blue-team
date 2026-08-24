"""redteam.runtime —— 红蓝队运行时：dsh Context 装配。

复用 dsh 框架基座：插件内核（Context/EventBus/Service）、LLM 接缝
（LlmRuntime + Mock/DeepSeek 适配器）、wanter 势能地形、事件溯源。

服务清单（挂在 ctx 上）：
- ctx.storage    SQLite 存储
- ctx.audit      事件溯源审计
- ctx.registry   攻击向量注册表
- ctx.detector   判定引擎
- ctx.terrain    自适应攻击地形（wanter 封装）
- ctx.llm        LLM 接缝（LLM 裁判/变体生成用）
"""
from __future__ import annotations

import logging
from typing import Optional

from dsh.kernel import Context
from dsh.llm.adapters import LlmRuntime
from dsh.llm.mock import MockAdapter

from ._env import load_dotenv
from .adaptive.terrain import AttackTerrain
from .audit import AuditSink
from .config import ScanConfig
from .detector.verdict import VerdictEngine
from .storage import StorageService
from .vectors.registry import VectorRegistry

log = logging.getLogger("redteam.runtime")


#: 保持历史命名（模块级加载一次；面板/CLI 也可显式调用）
_load_dotenv = load_dotenv

_load_dotenv()


class RedTeamRuntime:
    """一次扫描流程的运行时容器（装配 + 生命周期管理）。"""

    def __init__(self, cfg: ScanConfig) -> None:
        self.cfg = cfg
        self.ctx: Optional[Context] = None
        self.storage: Optional[StorageService] = None
        self.audit: Optional[AuditSink] = None
        self.registry: Optional[VectorRegistry] = None
        self.detector: Optional[VerdictEngine] = None
        self.terrain: Optional[AttackTerrain] = None
        self.llm: Optional[LlmRuntime] = None

    async def start(self) -> None:
        cfg = self.cfg
        cfg.ensure_dirs()
        ctx = Context("redteam")

        # 存储（扫描/结果/漏洞/修复/地形）
        storage = StorageService(ctx, {"db_path": cfg.storage.db_path})
        storage.apply(ctx)

        # 事件溯源审计
        audit = AuditSink(ctx, {"audit_dir": cfg.storage.audit_dir})
        audit.apply(ctx)

        # 攻击向量注册表
        registry = VectorRegistry(
            ctx, {"bank_dir": cfg.vectors.bank_dir or "",
                  "seed": cfg.vectors.seed})
        registry.apply(ctx)

        # 判定引擎
        detector = VerdictEngine(ctx, {"baseline": cfg.detector.baseline,
                                       "llm_judge": cfg.detector.llm_judge,
                                       "min_confidence": cfg.detector.min_confidence})
        detector.apply(ctx)

        # LLM 接缝（弱信号裁判/变体生成；默认 mock，有密钥则挂 DeepSeek）
        llm = LlmRuntime(ctx)
        llm.apply(ctx)
        llm.register_adapter(MockAdapter())
        try:
            from dsh.llm.deepseek import DeepSeekAdapter
            adapter = DeepSeekAdapter()
            if adapter.api_key:
                llm.register_adapter(adapter)
                log.info("DeepSeek 适配器已启用（LLM 弱信号裁判/变体生成可用）")
        except Exception as exc:  # pragma: no cover
            log.debug("DeepSeek 适配器不可用（保持 mock）: %s", exc)

        # 自适应攻击地形
        terrain = AttackTerrain(
            ctx, {"domain": cfg.domain_key(),
                  "temperature": cfg.adaptive.temperature})
        terrain.apply(ctx)

        self.ctx = ctx
        self.storage = storage
        self.audit = audit
        self.registry = registry
        self.detector = detector
        self.terrain = terrain
        self.llm = llm

    async def close(self) -> None:
        if self.ctx is not None:
            await self.ctx.dispose()
            self.ctx = None
