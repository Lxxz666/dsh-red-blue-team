"""Reflexion 失败反思机制测试（论文 2303.11366）。

验证：失败 turn 生成第一人称教训写入长时记忆；门控默认关；上限裁剪；无 memory 不崩。
"""
from types import SimpleNamespace

import pytest

import dsh.agent.loop as loop_mod
from dsh.agent import AgentLoopService
from dsh.kernel import Context
from dsh.llm.adapters import LlmRuntime
from dsh.llm.mock import MockAdapter
from dsh.memory.memory import MemoryService
from dsh.session import SessionStore


async def build_ctx(with_memory: bool = True):
    ctx = Context("reflexion")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    llm = LlmRuntime(ctx, {})
    llm.apply(ctx)
    llm.register_adapter(MockAdapter())
    if with_memory:
        mem = MemoryService(ctx, {})
        mem.apply(ctx)
    loop = AgentLoopService(ctx, {})
    loop.apply(ctx)
    return ctx, loop


def _agent():
    return SimpleNamespace(ctx_name="agent:x", options={})


async def test_reflection_disabled_by_default(monkeypatch):
    """默认 REFLECTION_ENABLED=False：失败 turn 不写反思记忆。"""
    monkeypatch.setattr(loop_mod, "REFLECTION_ENABLED", False)
    ctx, loop = await build_ctx()
    await loop._reflect_failure(_agent(), 1,
                                {"kind": "error",
                                 "error": {"message": "step limit reached (25)",
                                           "code": "STEP_LIMIT"}})
    assert ctx.memory.list() == []


async def test_reflection_writes_lesson(monkeypatch):
    """REFLECTION_ENABLED=1：失败 → 生成教训写入 memory（reflexion tag）。"""
    monkeypatch.setattr(loop_mod, "REFLECTION_ENABLED", True)
    ctx, loop = await build_ctx()
    await loop._reflect_failure(_agent(), 1,
                                {"kind": "error",
                                 "error": {"message": "step limit reached (25)",
                                           "code": "STEP_LIMIT"}})
    entries = ctx.memory.list()
    assert len(entries) >= 1
    assert "reflexion" in entries[0]["tags"]
    assert entries[0]["content"]


async def test_reflection_prunes_to_max(monkeypatch):
    """反思记忆上限裁剪（论文 Ω）：超过保留最近 REFLECTION_MAX_MEM 条。"""
    monkeypatch.setattr(loop_mod, "REFLECTION_ENABLED", True)
    monkeypatch.setattr(loop_mod, "REFLECTION_MAX_MEM", 2)
    ctx, loop = await build_ctx()
    for t in range(1, 6):
        await loop._reflect_failure(_agent(), t,
                                    {"kind": "error", "error": {"message": "x"}})
    refl = [e for e in ctx.memory.list() if "reflexion" in e.get("tags", [])]
    assert len(refl) == 2


async def test_no_memory_no_crash(monkeypatch):
    """无 memory 服务时反思安全返回，不阻塞 turn 收尾。"""
    monkeypatch.setattr(loop_mod, "REFLECTION_ENABLED", True)
    ctx, loop = await build_ctx(with_memory=False)
    await loop._reflect_failure(_agent(), 1, {"kind": "error", "error": {}})
    assert True
