"""MCP Streamable HTTP 传输测试：SSE/JSON 双形态、会话头、404、插件注册。"""
import threading
import time

import pytest

from dsh.boot import boot
from dsh.errors import ToolError
from dsh.mcp.http import McpHttpClient


@pytest.fixture(scope="module")
def http_server_url():
    import uvicorn
    from tests.fixtures.mock_mcp_http_server import app
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


async def test_mcp_http_roundtrip_sse_and_json(http_server_url):
    client = McpHttpClient(http_server_url)
    await client.start()  # initialize 走 SSE 流
    assert client.session_id == "sess-1"
    tools = await client.list_tools()  # tools/list 走 JSON
    assert {t["name"] for t in tools} == {"echo", "lenient"}
    assert await client.call_tool("echo", {"text": "hi"}) == "echo:hi"
    await client.stop()
    with pytest.raises(ToolError):
        await client.request("tools/list")  # 关闭后拒绝


async def test_mcp_http_not_found(http_server_url):
    client = McpHttpClient(http_server_url + "/nope")
    with pytest.raises(ToolError) as exc:
        await client.request("tools/list")
    assert exc.value.code == "MCP_NOT_FOUND"


async def test_mcp_http_plugin_registers_tools(http_server_url, tmp_path):
    ctx, tree = await boot(
        profile="headless", workspace=str(tmp_path), mock_llm=True,
        extra_patches=[([{"insert": [{
            "id": "mcp-http",
            "plugin": "dsh.mcp.http:McpHttpServerPlugin",
            "config": {"url": http_server_url}}]}], "test")])
    try:
        assert ctx.tools.get("echo") is not None
        assert ctx.tools.get("lenient") is not None  # anyOf 超子集 → 安全降级
        result = await ctx.tools.execute("c1", "echo", {"text": "hi"})
        assert not result.is_error and result.content == "echo:hi"
    finally:
        await tree.dispose()
