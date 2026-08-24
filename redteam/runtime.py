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
import os
from typing import Optional

from dsh.kernel import Context
from dsh.llm.adapters import LlmRuntime
from dsh.llm.mock import MockAdapter

from .adaptive.terrain import AttackTerrain
from .audit import AuditSink
from .config import ScanConfig
from .detector.verdict import VerdictEngine
from .storage import StorageService
from .vectors.registry import VectorRegistry

log = logging.getLogger("redteam.runtime")


def _load_dotenv() -> None:
    """加载项目根目录 .env（无第三方依赖；已存在的环境变量优先）。

    支持 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL，
    用于把 LLM 接缝指向火山方舟 Agent Plan 等 OpenAI 兼容端点。
    """
    dotenv_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    if not os.path.exists(dotenv_path):
        return
    try:
        with open(dotenv_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        log.debug("已加载项目 .env（%s）", dotenv_path)
    except OSError:
        pass


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
