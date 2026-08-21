"""读写投影类测试：sessionProjections / workspaceRegistry / sessionReferenceResolver。"""
import asyncio

import pytest

from dsh.boot import boot
from dsh.errors import SessionError
from dsh.kernel import Context
from dsh.projection import (ProjectionDefinition, ProjectionUnitsPlugin,
                            SessionProjectionRegistry)
from dsh.session import SessionStore
from dsh.workspace import SessionWorkspacePlugin, WorkspaceRegistry


# ---- 装配 ----

def _projection_ctx():
    ctx = Context("proj")
    store = SessionStore(ctx, {})
    store.apply(ctx)
    registry = SessionProjectionRegistry(ctx, {})
    registry.apply(ctx)
    return ctx, store, registry


def _counting_unit():
    def apply(state, event):
        if event.type == "user/message":
            return state + 1
        return state
    return ProjectionDefinition(key="user_count", init=0, apply=apply)


# ---- sessionProjections ----

async def test_projection_register_validation_and_refcount():
    ctx, store, registry = _projection_ctx()
    with pytest.raises(ValueError):
        registry.register(ProjectionDefinition(key="  ", init=0,
                                               apply=lambda s, e: s))
    with pytest.raises(ValueError):
        registry.register(ProjectionDefinition(key="k", init=0,
                                               apply=lambda s, e: s,
                                               state_version=0))
    unit = _counting_unit()
    dispose_a = registry.register(unit)
    # 同 key 不同定义冲突
    with pytest.raises(ValueError):
        registry.register(ProjectionDefinition(key="user_count", init=0,
                                               apply=lambda s, e: s))
    dispose_b = registry.register(unit)  # 同 key 引用计数
    dispose_a()
    assert "user_count" in registry.unit_keys()  # 最后一个卸载才消失
    dispose_b()
    assert registry.unit_keys() == []


async def test_projection_eager_drive_and_snapshot():
    ctx, store, registry = _projection_ctx()
    dispose = registry.register(_counting_unit())
    session = store.create()
    session.append("user/message", {"content": "a", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "b", "source": {"kind": "user"}},
                   surface_op="append")
    snap = registry.snapshot(session)
    assert snap["values"]["user_count"] == 2
    assert snap["as_of_seq"] == 2
    empty = store.create()
    assert registry.snapshot(empty) == {"as_of_seq": -1,
                                        "values": {"user_count": 0}}
    dispose()


async def test_projection_lazy_build_after_events():
    ctx, store, registry = _projection_ctx()
    session = store.create()
    session.append("user/message", {"content": "a", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("user/message", {"content": "b", "source": {"kind": "user"}},
                   surface_op="append")
    dispose = registry.register(_counting_unit())  # 注册晚于事件流
    snap = registry.snapshot(session)
    assert snap["values"]["user_count"] == 2
    # 后续事件继续驱动
    session.append("user/message", {"content": "c", "source": {"kind": "user"}},
                   surface_op="append")
    assert registry.snapshot(session)["values"]["user_count"] == 3
    dispose()


async def test_projection_changed_feed_identity():
    ctx, store, registry = _projection_ctx()
    seen = []
    dispose_feed = registry.on_changed(
        lambda key, session, value, seq: seen.append((key, value, seq)))
    dispose_unit = registry.register(_counting_unit())
    session = store.create()
    session.append("user/message", {"content": "a", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("turn/start", {"turn": 1})  # 无关事件 → 状态引用不变
    session.append("user/message", {"content": "b", "source": {"kind": "user"}},
                   surface_op="append")
    assert seen == [("user_count", 1, 0), ("user_count", 2, 2)]
    dispose_feed()
    session.append("user/message", {"content": "c", "source": {"kind": "user"}},
                   surface_op="append")
    assert len(seen) == 2  # 订阅已注销
    dispose_unit()


async def test_units_todos_and_session_stats():
    ctx, store, registry = _projection_ctx()
    plugin = ProjectionUnitsPlugin(ctx, {})
    dispose_plugin = plugin.apply(ctx)
    session = store.create()
    session.append("user/message", {"content": "hi", "source": {"kind": "user"}},
                   surface_op="append")
    session.append("todo/write",
                   {"todos": [{"id": "1", "content": "A", "status": "pending"}]})
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    snap = registry.snapshot(session)
    assert snap["values"]["todos"][0]["id"] == "1"
    assert snap["values"]["session_stats"]["user_messages"] == 1
    assert snap["values"]["session_stats"]["started_at"] is not None
    # 新 turn 清空 todo（当前有效计划语义）
    session.append("turn/start", {"turn": 2})
    assert registry.snapshot(session)["values"]["todos"] is None
    dispose_plugin()


# ---- workspaceRegistry ----

def _workspace_ctx(storage_path=None):
    from dsh.storage.service import StorageService
    ctx = Context("ws")
    if storage_path:
        StorageService(ctx, {"path": storage_path}).apply(ctx)
    registry = WorkspaceRegistry(ctx, {})
    registry.apply(ctx)
    return ctx, registry


async def test_workspace_create_dedupe_and_resolve(tmp_path):
    ctx, registry = _workspace_ctx()
    ws_a = registry.create(str(tmp_path), title="标题 A")
    again = registry.create(str(tmp_path), title="不同标题")  # 不改标题
    assert again.id == ws_a.id and again.title == "标题 A"
    other = tmp_path / "other"
    other.mkdir()
    ws_b = registry.create(str(other))
    assert [ws.id for ws in registry.list()] == [ws_b.id, ws_a.id]  # 新在前
    assert registry.get(ws_a.id).path == ws_a.path
    assert registry.resolve_by_path(str(tmp_path)).id == ws_a.id
    with pytest.raises(ValueError):
        registry.resolve_by_path(str(tmp_path / "missing"))
    with pytest.raises(ValueError):
        registry.create(str(tmp_path / "missing"))


async def test_workspace_insert_before_and_delete(tmp_path):
    ctx, registry = _workspace_ctx()
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    (tmp_path / "w3").mkdir()
    a = registry.create(str(tmp_path / "w1"))
    b = registry.create(str(tmp_path / "w2"))
    c = registry.create(str(tmp_path / "w3"))
    assert [ws.id for ws in registry.list()] == [c.id, b.id, a.id]
    # a 移到 c 之前
    order = registry.insert_before(a.id, c.id)
    assert order == [a.id, c.id, b.id]
    # 自身为锚 / 原位 → 不写完成
    assert registry.insert_before(a.id, a.id) == order
    assert registry.insert_before(c.id, b.id) == order
    # 锚点省略 = 追加末尾
    assert registry.insert_before(a.id) == [c.id, b.id, a.id]
    with pytest.raises(ValueError):
        registry.insert_before("ws-999", None)
    assert registry.delete(b.id) is True
    assert registry.delete(b.id) is False
    assert [ws.id for ws in registry.list()] == [c.id, a.id]


async def test_workspace_session_accounting_and_archive(tmp_path):
    ctx, registry = _workspace_ctx()
    ws = registry.create(str(tmp_path))
    registry.account_session(ws.id, "s1")
    registry.account_session(ws.id, "s2")
    registry.account_session(ws.id, "s1")  # 去重 + 最新在前
    assert registry.sessions_of(ws.id) == ["s1", "s2"]
    registry.archive_session("s2")
    assert registry.sessions_of(ws.id) == ["s1"]
    assert registry.archived_session_ids == ["s2"]
    registry.archive_session("s2")  # 已归档 → 不写完成
    registry.unarchive_session("s2")
    assert registry.sessions_of(ws.id) == ["s1", "s2"]
    with pytest.raises(ValueError):
        registry.archive_session("unknown-session")
    grouped = registry.group_sessions()
    assert grouped[0]["workspace_id"] == ws.id


async def test_workspace_persistence_round_trip(tmp_path):
    path = str(tmp_path / "storage.json")
    ctx, registry = _workspace_ctx(storage_path=path)
    ws = registry.create(str(tmp_path), title="持久")
    registry.account_session(ws.id, "s1")
    registry.archive_session("s1")
    # 等价重启：新实例从 storage 恢复
    ctx2, registry2 = _workspace_ctx(storage_path=path)
    restored = registry2.get(ws.id)
    assert restored is not None and restored.title == "持久"
    assert restored.path == ws.path
    assert registry2.sessions_of(ws.id) == []
    assert registry2.archived_session_ids == ["s1"]
    assert [w.id for w in registry2.list()] == [ws.id]


async def test_workspace_plugin_accounts_sessions(tmp_path):
    ctx, registry = _workspace_ctx()
    store = SessionStore(ctx, {})
    store.apply(ctx)
    plugin = SessionWorkspacePlugin(ctx, {})
    dispose = plugin.apply(ctx)
    session = store.create(meta={"cwd": str(tmp_path)})
    grouped = registry.group_sessions()
    assert grouped[0]["session_ids"] == [session.id]
    # 无 cwd 的会话跳过
    store.create()
    assert len(registry.group_sessions()[0]["session_ids"]) == 1
    dispose()


# ---- sessionReferenceResolver ----

async def test_reference_resolve_live_bounded(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("你好")
        await agent.when_idle()
        await asyncio.sleep(0.05)
        resolver = ctx.sessionReferenceResolver
        snapshot = await resolver.resolve({"session_id": agent.id,
                                           "max_blocks": 1})
        assert snapshot["session_id"] == agent.id
        assert snapshot["truncated"] is True  # 多于 1 块
        assert len(snapshot["blocks"]) == 1
        assert snapshot["blocks"][0]["source"]["kind"] == "model"
        full = await resolver.resolve({"session_id": agent.id,
                                       "max_blocks": 50})
        kinds = [b["source"]["kind"] for b in full["blocks"]]
        assert kinds[0] == "user" and "model" in kinds
    finally:
        await tree.dispose()


async def test_reference_compact_checkpoint_skips_prefix(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        store = ctx.sessions
        session = store.create()
        session.append("user/message", {"content": "被压缩的老对话",
                                        "source": {"kind": "user"}},
                       surface_op="append")
        session.append("compaction/summary",
                       {"summary": "此前聊了天气"}, surface_op="append")
        session.append("user/message", {"content": "新问题",
                                        "source": {"kind": "user"}},
                       surface_op="append")
        snapshot = await ctx.sessionReferenceResolver.resolve(
            {"session_id": session.id})
        contents = [b["content"] for b in snapshot["blocks"]]
        assert "新问题" in contents and "被压缩的老对话" not in contents
    finally:
        await tree.dispose()


async def test_reference_persisted_session_and_missing(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        store = ctx.sessions
        session = store.create()
        session.append("user/message", {"content": "持久化的问题",
                                        "source": {"kind": "user"}},
                       surface_op="append")
        await store.flush(session)
        store.remove(session)  # 只剩持久化副本
        snapshot = await ctx.sessionReferenceResolver.resolve(
            {"session_id": session.id})
        assert "持久化的问题" in snapshot["blocks"][0]["content"]
        with pytest.raises(SessionError):
            await ctx.sessionReferenceResolver.resolve(
                {"session_id": "no-such-session"})
    finally:
        await tree.dispose()


async def test_boot_mounts_projection_seams(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        for key in ("sessionProjections", "workspaceRegistry",
                    "sessionReferenceResolver"):
            assert ctx.has(key), f"ctx.{key} 未挂载"
        session = ctx.sessions.create()
        session.append("todo/write", {"todos": [{"id": "1", "content": "A",
                                                 "status": "pending"}]})
        snap = ctx.sessionProjections.snapshot(session)
        assert snap["values"]["todos"][0]["content"] == "A"
        assert snap["values"]["session_stats"]["messages"] == 0
    finally:
        await tree.dispose()
