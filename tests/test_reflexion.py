"""Reflexion 失败反思服务测试（论文 2303.11366）。

验证独立 ReflexionService：Evaluator 判定 / 失败反思写教训 / 上限裁剪 /
无 memory 不崩 / pre-step 记忆注入闭环。
"""
from types import SimpleNamespace

import pytest

import dsh.agent.reflexion as refl_mod
from dsh.agent.reflexion import ReflexionService
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
    svc = ReflexionService(ctx, {})
    svc.apply(ctx)
    return ctx, svc


def _failure(message="step limit reached (25)", code="STEP_LIMIT"):
    return {"message": message, "code": code}


async def test_evaluator_should_reflect():
    """Evaluator：error 触发反思，正常/中止不触发。"""
    assert ReflexionService.should_reflect({"kind": "error"}) is True
    assert ReflexionService.should_reflect({"kind": "completed"}) is False
    assert ReflexionService.should_reflect({"kind": "aborted"}) is False


async def test_reflection_disabled_by_default(monkeypatch):
    """默认 REFLECTION_ENABLED=False：reflect 不写记忆。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", False)
    ctx, svc = await build_ctx()
    lesson = await svc.reflect(_failure())
    assert lesson is None
    assert ctx.memory.list() == []


async def test_reflection_writes_lesson(monkeypatch):
    """REFLECTION_ENABLED=1：失败 → 生成教训写入 memory（reflexion tag）。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", True)
    ctx, svc = await build_ctx()
    lesson = await svc.reflect(_failure())
    assert lesson
    entries = ctx.memory.list()
    assert len(entries) >= 1
    assert "reflexion" in entries[0]["tags"]
    assert entries[0]["content"]


async def test_reflection_prunes_to_max(monkeypatch):
    """上限裁剪（论文 Ω）：超过保留最近 REFLECTION_MAX_MEM 条。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", True)
    monkeypatch.setattr(refl_mod, "REFLECTION_MAX_MEM", 2)
    ctx, svc = await build_ctx()
    for i in range(5):
        await svc.reflect(_failure(f"fail {i}"))
    refl = [e for e in ctx.memory.list() if "reflexion" in e.get("tags", [])]
    assert len(refl) == 2


async def test_no_memory_no_crash(monkeypatch):
    """无 memory 服务时 reflect 安全返回，不崩。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", True)
    ctx, svc = await build_ctx(with_memory=False)
    lesson = await svc.reflect(_failure())
    assert lesson is None


async def test_inject_lesson_on_pre_step(monkeypatch):
    """pre-step 记忆注入闭环：相关教训进入下一步消息上下文。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", True)
    ctx, svc = await build_ctx()
    # 先沉淀一条与"刘汉宬"相关的失败教训
    ctx.memory.add("搜索刘汉宬多次无结果时应停止并如实回答",
                   tags=["reflexion"])
    payload = {"messages": [{"content": "帮我查刘汉宬"}]}

    async def nxt():
        return {"kind": "enter"}

    await svc._on_pre_step(payload, nxt)
    # 应注入一条反思经验（消息数 1 → 2）
    assert len(payload["messages"]) == 2
    assert "反思经验" in payload["messages"][-1]["content"]
    assert payload["messages"][-1]["source"] == {"kind": "system"}


async def test_inject_skips_when_no_hit(monkeypatch):
    """无相关教训时不注入（消息数不变）。"""
    monkeypatch.setattr(refl_mod, "REFLECTION_ENABLED", True)
    ctx, svc = await build_ctx()
    ctx.memory.add("完全不相关的教训xyz", tags=["reflexion"])
    payload = {"messages": [{"content": "你好"}]}

    async def nxt():
        return {"kind": "enter"}

    await svc._on_pre_step(payload, nxt)
    assert len(payload["messages"]) == 1
