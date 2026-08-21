"""dsh.session 测试：事件溯源、surface、派生、重建。"""
import pytest

from dsh.session import Session, SessionHeader, SessionStore
from dsh.session.events import SessionEvent, is_json_value
from dsh.errors import SessionError, SessionFormatError
from dsh.kernel import Context
from dsh.llm.messages import ContentBlock


def make_session():
    return Session("test-session")


def test_append_and_seq_contiguity():
    s = make_session()
    e1 = s.append("user/message", {"content": "hi", "source": {"kind": "user"}},
                  surface_op="append")
    e2 = s.append("user/message", {"content": "again", "source": {"kind": "user"}},
                  surface_op="append")
    assert e1.seq == 0 and e2.seq == 1
    assert s.seq == 2
    assert s.events == [e1, e2]


def test_append_rejects_non_json():
    s = make_session()
    with pytest.raises(SessionError):
        s.append("user/message", {"content": {"bad": object()}},
                 surface_op="append")
    assert s.seq == 0  # 坏事件未入日志


def test_surface_event_requires_surface_op():
    s = make_session()
    with pytest.raises(SessionError):
        s.append("user/message", {"content": "hi"})
    # 非 surface 事件禁止带 surface_op
    with pytest.raises(SessionError):
        s.append("turn/start", {"turn": 1}, surface_op="append")


def test_derive_messages_projection():
    s = make_session()
    s.append("user/message", {"content": "hello", "source": {"kind": "user"}},
             surface_op="append")
    s.append("assistant/message",
             {"blocks": [ContentBlock(kind="text", text="world")],
              "provider": "mock", "model": "mock"},
             surface_op="append", source_event_seqs=[])
    s.append("tool/result",
             {"call_id": "call-1", "name": "read", "content": "file content"},
             surface_op="append")
    messages = s.derive_messages()
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[1].content[0].text == "world"
    assert messages[2].content[0].kind == "tool-result"


def test_empty_assistant_message_skipped():
    s = make_session()
    s.append("assistant/message", {"blocks": [], "provider": "mock", "model": "mock"},
             surface_op="append")
    assert s.derive_messages() == []


def test_replace_surface_shadows_range():
    s = make_session()
    for i in range(5):
        s.append("user/message", {"content": f"m{i}", "source": {"kind": "user"}},
                 surface_op="append")
    assert len(s.surface.nodes) == 5
    gen = s.surface.replace_generation
    s.append("compaction/summary",
             {"summary": "前面 5 条的摘要"},
             surface_op={"op": "replace", "start": 0, "end": 4},
             source_event_seqs=[0, 1, 2, 3, 4])
    assert s.surface.replace_generation == gen + 1
    messages = s.derive_messages()
    assert len(messages) == 1
    assert "摘要" in messages[0].content[0].text


def test_replace_requires_full_shadow_coverage():
    s = make_session()
    for i in range(3):
        s.append("user/message", {"content": f"m{i}", "source": {"kind": "user"}},
                 surface_op="append")
    with pytest.raises(SessionError):
        s.append("compaction/summary", {"summary": "x"},
                 surface_op={"op": "replace", "start": 0, "end": 2},
                 source_event_seqs=[0, 1])  # 缺 seq 2


def test_is_json_value():
    assert is_json_value({"a": [1, 2.5, "x", None, True]})
    assert not is_json_value({"a": object()})
    assert not is_json_value(float("nan"))
    assert not is_json_value({1: "non-str-key"})


def test_from_seed_validation():
    header = SessionHeader(id="s1")
    good = [
        {"type": "user/message", "seq": 0, "time": 1,
         "data": {"content": "hi"}, "surface_op": "append"},
    ]
    s = Session.from_seed("s1", header, good)
    assert s.has_seed
    assert len(s.derive_messages()) == 1
    # 未知必填事件 → 拒绝
    bad = good + [{"type": "alien/event", "seq": 1, "time": 2, "data": {}}]
    with pytest.raises(SessionFormatError):
        Session.from_seed("s1", header, bad)
    # 可忽略事件 → 接受
    ok = good + [{"type": "alien/event", "seq": 1, "time": 2, "data": {},
                  "ignorable": True}]
    Session.from_seed("s1", header, ok)
    # 版本不符 → 拒绝
    future = SessionHeader(id="s1", version=99)
    with pytest.raises(SessionFormatError):
        Session.from_seed("s1", future, good)


async def test_store_create_broadcast_and_fork():
    ctx = Context()
    store = SessionStore(ctx, {})
    store.apply(ctx)
    seen = []
    ctx.on("session/event", lambda s, e: seen.append(e.type))
    ctx.on("session/created", lambda s: seen.append("session/created"))

    parent = store.create(meta={"cwd": "C:/ws"})
    parent.append("user/message", {"content": "hi", "source": {"kind": "user"}},
                  surface_op="append")
    await ctx.parallel("session/flush", parent)  # 触发广播汇聚
    import asyncio
    await asyncio.sleep(0.05)  # 等 fire-and-forget task 落地
    assert "session/created" in seen
    assert "user/message" in seen

    child = store.fork(parent, boundary=0, child_session_id="child-1")
    assert child.header.parent_session == parent.id
    assert child.header.seed_length == 1
    assert [m.plain_text() for m in child.derive_messages()] == \
           [m.plain_text() for m in parent.derive_messages()]

    store.remove(parent)
    assert store.get(parent.id) is None
