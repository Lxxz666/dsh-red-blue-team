"""
dsh.compaction —— 上下文压缩（ctx.compaction + compaction/summary 事件）。

对应 TS 版 compaction 缝。本实现（compaction-basic）:

- ``agent/pre-step`` 上做压力检测：派生历史估算 token 数超过阈值 → 压缩；
- 压缩 = 用一次模型调用把「最旧的 surface 区间」总结为一段摘要，
  以 surface replace 事件 ``compaction/summary`` 落日志（派生历史被摘要替换）；
- ``/compact`` 命令可手动触发。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..session import register_event_type

log = logging.getLogger("dsh.compaction")

register_event_type(
    "compaction/summary",
    "上下文压缩摘要（surface replace 事件，投影为 user 消息）。",
    surface=True)

#: 粗粒度 token 估算：4 字符 ≈ 1 token
TOKENS_PER_CHAR = 4


def estimate_tokens(messages: List[Any]) -> int:
    """粗估派生历史的 token 数。"""
    total = 0
    for message in messages:
        total += sum(len(block.text or "") for block in message.content) \
            // TOKENS_PER_CHAR
    return total


class CompactionService(Service):
    """上下文压缩服务（ctx.compaction）。"""

    provides = "compaction"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.threshold_tokens = int((config or {}).get("threshold_tokens", 8000))
        self.keep_last_messages = int((config or {}).get("keep_last_messages", 4))

    def apply(self, ctx) -> None:
        ctx.set("compaction", self)

    async def compact(self, agent: Any) -> Dict[str, Any]:
        """
        压缩一次：总结最旧 surface 区间并替换。

        :return: {"compacted": bool, "summary": str, "shadowed": [seqs]}。
        """
        session = agent.session
        nodes = list(session.surface.nodes)
        if len(nodes) <= self.keep_last_messages:
            return {"compacted": False, "summary": "", "shadowed": []}
        keep = self.keep_last_messages
        shadowed = nodes[:-keep]
        if not shadowed:
            return {"compacted": False, "summary": "", "shadowed": []}
        history = session.derive_messages()
        to_summarize = history[:len(history) - keep]
        # 可选修剪：超长工具结果先截断（对应 TS 版 summary 前的 tool-result pruning）
        if self.ctx.has("toolResultPruner"):
            to_summarize = self.ctx.toolResultPruner.prune(to_summarize)
        summary = await self._summarize(agent, to_summarize)
        session.append(
            "compaction/summary", {"summary": summary},
            surface_op={"op": "replace", "start": shadowed[0], "end": shadowed[-1]},
            source_event_seqs=shadowed)
        return {"compacted": True, "summary": summary, "shadowed": shadowed}

    async def _summarize(self, agent: Any, messages: List[Any]) -> str:
        """用模型总结（mock 时直接截取）。"""
        from ..llm.adapters import LlmCallConfig, LlmRequest
        from ..llm.stream import AssistantAssembler
        text = "\n".join(m.plain_text() for m in messages)
        request = LlmRequest(
            config=LlmCallConfig(provider=agent.options.get("provider") or "mock",
                                 model=agent.options.get("model") or "mock"),
            messages=[],
            system="把以下对话历史压缩成不超过 500 字的要点摘要，保留任务目标、"
                   "已做工作与未完成事项：\n\n" + text[:6000])
        try:
            assembler = AssistantAssembler()
            async for chunk in agent._factory.ctx.llm.stream(request):
                assembler.feed(chunk)
            result = assembler.finish()
            summary = "".join(b.text or "" for b in result["blocks"])
            if summary:
                return f"[早前对话摘要]\n{summary}"
        except Exception:
            log.exception("compaction summarize failed")
        return "[早前对话摘要]\n" + text[-2000:]


class CompactionPolicyPlugin(Service):
    """压力驱动的自动压缩（挂在 agent/pre-step 上）。"""

    inject = ("compaction",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        self._disposer = ctx.on("agent/pre-step", self._on_pre_step,
                                prepend=True)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
        return cleanup

    async def _on_pre_step(self, payload: Dict[str, Any], next):
        """压力检测：超阈值则压缩后委派。"""
        agent = payload.get("agent")
        if agent is not None:
            messages = agent.session.derive_messages()
            if self._estimate(messages) > self.ctx.compaction.threshold_tokens:
                await self.ctx.compaction.compact(agent)
        return await next()

    def _estimate(self, messages: List[Any]) -> int:
        """token 估算：优先 ctx.tokenMeter，回退本地粗估。"""
        if self.ctx.has("tokenMeter"):
            return self.ctx.tokenMeter.estimate(messages)
        return estimate_tokens(messages)
