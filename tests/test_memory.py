"""Memory 子系统测试：tokenize/Jaccard、add/search/list/remove、storage 持久化、工具注册与执行。"""
import pytest

from dsh.boot import boot
from dsh.kernel import Context
from dsh.memory.memory import MemoryService, _jaccard, _tokenize
from dsh.storage.service import StorageService
from dsh.tools import ToolRuntime, define_tool


# ---- 纯函数 ----

def test_tokenize_cjk_and_words():
    tokens = _tokenize("Hello World 记忆系统")
    assert "hello" in tokens and "world" in tokens
    assert "记忆" in tokens and "忆系" in tokens and "系统" in tokens


def test_jaccard_score():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert _jaccard({"a"}, {"b"}) == 0.0
    assert _jaccard({"a", "b"}, {"a", "c"}) == pytest.approx(1 / 3)


# ---- 服务级 ----

async def test_memory_add_search_list_remove(tmp_path):
    ctx = Context("memory")
    StorageService(ctx, {"path": str(tmp_path / "storage.json")}).apply(ctx)
    memory = MemoryService(ctx, {})
    memory.apply(ctx)
    e1 = memory.add("用户喜欢用 Python 写后端", tags=["偏好"])
    e2 = memory.add("数据库选择 PostgreSQL")
    memory.add("今天天气不错")
    # search：中文相邻二字组命中
    hits = memory.search("Python")
    assert hits and hits[0]["id"] == e1["id"]
    assert hits[0]["score"] > 0
    hits = memory.search("数据库")
    assert hits[0]["id"] == e2["id"]
    # 不相关查询返回空
    assert memory.search("完全无关的词") == []
    # list 最新在前
    listing = memory.list()
    assert listing[0]["id"] == memory.list(1)[0]["id"]
    assert len(listing) == 3
    # get / remove
    assert memory.get(e1["id"])["content"] == "用户喜欢用 Python 写后端"
    assert memory.remove(e1["id"]) is True
    assert memory.remove(e1["id"]) is False
    assert memory.get(e1["id"]) is None


async def test_memory_persistence_across_instances(tmp_path):
    """storage 域持久化：新实例（等价于重启）可恢复条目。"""
    path = str(tmp_path / "storage.json")
    ctx = Context("memory-a")
    StorageService(ctx, {"path": path}).apply(ctx)
    MemoryService(ctx, {}).apply(ctx)
    ctx.memory.add("跨会话记住：目标目录是 dsh_python")
    ctx = Context("memory-b")
    StorageService(ctx, {"path": path}).apply(ctx)
    MemoryService(ctx, {}).apply(ctx)
    hits = ctx.memory.search("目标目录")
    assert hits and "dsh_python" in hits[0]["content"]


async def test_memory_rejects_empty_content(tmp_path):
    ctx = Context("memory")
    memory = MemoryService(ctx, {})
    memory.apply(ctx)
    with pytest.raises(ValueError):
        memory.add("   ")


# ---- 工具注册与执行（base.yml 行） ----

async def test_memory_tools_registered(tmp_path):
    ctx, tree = await boot(
        profile="headless", workspace=str(tmp_path), mock_llm=True,
        extra_patches=[([{"id": "storage",
                          "config": {"path": str(tmp_path / "storage.json")}}],
                        "test")])
    try:
        for name in ("memory_add", "memory_search", "memory_list",
                     "memory_remove"):
            assert ctx.tools.get(name) is not None, f"{name} 未注册"
        result = await ctx.tools.execute("m1", "memory_add",
                                         {"content": "记住部署端口 8080",
                                          "tags": ["ops"]})
        assert not result.is_error, result.content
        entry = result.value
        assert entry["content"] == "记住部署端口 8080"
        hits = await ctx.tools.execute("m2", "memory_search",
                                       {"query": "部署端口", "limit": 3})
        assert not hits.is_error
        assert hits.value and hits.value[0]["id"] == entry["id"]
        removed = await ctx.tools.execute("m3", "memory_remove",
                                          {"id": entry["id"]})
        assert removed.value is True
    finally:
        await tree.dispose()
