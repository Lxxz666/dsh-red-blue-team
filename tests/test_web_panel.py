"""wanter Web 地形面板 + 会话节点树测试。"""
from fastapi.testclient import TestClient

from dsh.boot import boot
from dsh.server.app import build_app


async def test_wanter_panel_endpoints(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        app = build_app(ctx)
        client = TestClient(app)
        res = client.get("/api/wanter/terrain")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in res.text
        assert "wanter" in res.text
        state = client.get("/api/wanter/state").json()
        assert state["dim"] == 2
        assert "trace_events" in state and "erosion_bumps" in state
        assert state["goals"][0][0] == [3.0, 0.0]  # base.yml 目标
        page = client.get("/static/wanter.html")
        assert page.status_code == 200 and "wanter" in page.text
    finally:
        await tree.dispose()


async def test_session_tree_endpoint(tmp_path):
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        app = build_app(ctx)
        client = TestClient(app)
        store = ctx.sessions
        root = store.create()
        child = store.fork(root)
        grandchild = store.fork(child)
        payload = client.get("/api/sessions/tree").json()
        # 全局树含历史持久化会话 → 只断言本测试会话的谱系关系
        assert root.id in payload["roots"]
        nodes = payload["nodes"]
        assert nodes[root.id]["children"] == [child.id]
        assert nodes[child.id]["children"] == [grandchild.id]
        assert nodes[root.id]["live"] is True
        assert nodes[child.id]["parent"] == root.id
        # 冷会话（持久化后移除活跃副本）仍在树中
        cold = store.create()
        cold.append("user/message", {"content": "冷会话内容",
                                     "source": {"kind": "user"}},
                    surface_op="append")
        await store.flush(cold)
        store.remove(cold)
        payload = client.get("/api/sessions/tree").json()
        assert cold.id in payload["nodes"]
        assert payload["nodes"][cold.id]["live"] is False
        page = client.get("/static/tree.html")
        assert page.status_code == 200 and "会话节点树" in page.text
    finally:
        await tree.dispose()
