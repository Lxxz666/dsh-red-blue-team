"""Reflexion 失败反思服务（论文 2303.11366）—— 独立架构组件。

将 Reflexion 从 AgentLoop 的私有方法抽离为一等公民服务（对应论文三模型
Actor / Evaluator / Self-Reflection + 双记忆），通过 waterfall 事件接入：

- **Evaluator**：判定 turn 失败（step limit / 工具错误 / 探索超限等失败信号）；
- **Self-Reflection**：失败后调用 LLM 生成第一人称语言教训；
- **长时记忆**：教训写入 ``ctx.memory``（reflexion tag），上限 Ω 裁剪；
- **记忆注入（闭环）**：挂 ``agent/pre-step``，检索相关教训注入下一步上下文，
  让后续同类任务直接引用失败经验，避免重蹈。

门控 ``REFLECTION_ENABLED=1``（默认关——自省是强模型的涌现能力，
弱模型自省无收益，见论文 Appendix A）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from ..errors import LlmFailure
from ..kernel import Service
from ..llm.adapters import LlmCallConfig, LlmRequest
from ..llm.messages import Message

log = logging.getLogger("dsh.agent")

# 门控：默认关，仅强模型有效（弱模型自省无收益，论文 Appendix A）
REFLECTION_ENABLED = os.environ.get("REFLECTION_ENABLED", "").strip() == "1"
REFLECTION_MAX_MEM = 3       # 保留最近 N 条反思教训（论文 Ω 上限）
REFLECTION_INJECT_MAX = 2    # pre-step 最多注入几条教训

# Evaluator 失败信号：这些 reason 触发反思
REFLECTION_FAIL_KINDS = {"error"}


class ReflexionService(Service):
    """Reflexion 失败反思：Evaluator + Self-Reflection + 长时记忆 + 注入闭环。"""

    provides = "reflexion"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        ctx.set("reflexion", self)
        # 记忆注入：每个 step 前检索相关教训注入上下文（闭环）
        self._disposer = ctx.on("agent/pre-step", self._on_pre_step,
                                prepend=True)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
        return cleanup

    # ---- Self-Reflection：失败 → 语言教训 → 长时记忆 ----

    async def reflect(self, failure: Dict[str, Any],
                      session_id: Optional[str] = None) -> Optional[str]:
        """失败 → LLM 生成第一人称语言教训 → 写入长时记忆。

        异常全部隔离：反思失败返回 None，绝不阻塞调用方（turn 收尾）。
        :return: 教训文本（成功）或 None（未启用 / 失败 / 无 memory）。
        """
        try:
            if not REFLECTION_ENABLED or not self.ctx.has("memory"):
                return None
            prompt = (
                "你是一个经验丰富的智能体。刚才处理用户任务时失败，请反思。\n"
                f"失败原因：{failure.get('message', '未知')} "
                f"(code={failure.get('code', '')})\n"
                "请用第一人称写一条简洁的自我反思（1-2 句），包含：1) 为什么会失败；"
                "2) 下次遇到同类情况应该怎么做。直接输出反思内容，不要客套。"
            )
            config = self._resolve_config()
            request = LlmRequest(config=config, messages=[Message.user(prompt)])
            parts: List[str] = []
            async for chunk in self.ctx.llm.stream(request):
                if chunk.type == "text-delta" and chunk.text:
                    parts.append(chunk.text)
            lesson = "".join(parts).strip()
            if not lesson:
                return None
            tags = ["reflexion"]
            if session_id:
                tags.append(f"session:{session_id}")
            self.ctx.memory.add(lesson, tags=tags)
            self._prune()
            return lesson
        except Exception:
            log.exception("reflexion reflect failed")
            return None

    def _prune(self) -> None:
        """保留最近 REFLECTION_MAX_MEM 条 reflexion 记忆（论文 Ω 上限）。"""
        try:
            if not self.ctx.has("memory"):
                return
            refl = [e for e in self.ctx.memory.list()
                    if "reflexion" in e.get("tags", [])]
            for entry in refl[REFLECTION_MAX_MEM:]:
                self.ctx.memory.remove(entry["id"])
        except Exception:
            log.exception("prune reflexions failed")

    # ---- Evaluator：判定失败信号 ----

    @staticmethod
    def should_reflect(reason: Dict[str, Any]) -> bool:
        """判定 turn 是否触发反思（失败信号）。"""
        return reason.get("kind") in REFLECTION_FAIL_KINDS

    # ---- 记忆注入（pre-step 钩子，闭环）----

    async def _on_pre_step(self, payload: Dict[str, Any], next):
        """step 前把相关 reflexion 教训注入消息上下文，供模型引用。"""
        try:
            if not REFLECTION_ENABLED or not self.ctx.has("memory"):
                return await next()
            batch = payload.get("messages") or []
            query = self._last_user_text(batch)
            if not query:
                return await next()
            hits = self.ctx.memory.search(query,
                                          limit=REFLECTION_INJECT_MAX)
            lessons = [h["content"] for h in hits
                       if "reflexion" in h.get("tags", [])]
            if lessons:
                hint = ("【反思经验·来自之前的失败教训】\n"
                        + "\n".join("- " + l for l in lessons))
                # 追加一条 system 消息（进上下文，前端隐藏）
                payload["messages"] = [
                    *batch,
                    {"content": hint, "source": {"kind": "system"}},
                ]
        except Exception:
            log.exception("reflexion inject failed")
        return await next()

    @staticmethod
    def _last_user_text(batch: List[Any]) -> str:
        for m in reversed(batch or []):
            content = m.get("content", "") if isinstance(m, dict) else ""
            if content:
                return content if isinstance(content, str) else str(content)
        return ""

    # ---- 模型配置（复用 agent 默认路由）----

    def _resolve_config(self) -> LlmCallConfig:
        defaults: Dict[str, Any] = {}
        if self.ctx.has("agentDefaultModel"):
            defaults = self.ctx.agentDefaultModel.current_selection()
        provider = defaults.get("provider")
        model = defaults.get("model")
        if not provider:
            providers = self.ctx.llm.providers()
            if not providers:
                raise LlmFailure("no LLM provider registered",
                                 code="NO_PROVIDER")
            # 无密钥时自动回退 mock（与 loop 默认路由一致）
            provider = ("deepseek" if os.environ.get("DEEPSEEK_API_KEY")
                        and "deepseek" in providers
                        else ("mock" if "mock" in providers else providers[0]))
        if not model:
            if provider == "mock":
                model = "mock"
            else:
                adapter = self.ctx.llm.get_adapter(provider)
                model = (getattr(adapter, "default_model", None)
                         or "deepseek-chat")
        return LlmCallConfig(provider=provider, model=model)
