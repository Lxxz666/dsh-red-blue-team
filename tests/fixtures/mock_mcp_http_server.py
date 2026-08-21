"""
MCP Streamable HTTP 夹具服务器（FastAPI）：单端点 POST /mcp。

- initialize 以 SSE 流形态响应并回传 Mcp-Session-Id（覆盖客户端 SSE 解析路径）；
- tools/list（echo + anyOf 降级 lenient）、tools/call（echo: 前缀）为 JSON 形态；
- 用于 dsh.mcp.http 的测试。
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock-mcp-http")

TOOLS = [
    {"name": "echo", "description": "回显文本",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "lenient", "description": "anyOf 超子集（应安全降级）",
     "inputSchema": {"type": "object", "anyOf": [{"type": "object"}]}},
]


@app.post("/mcp")
async def mcp(request: Request):
    payload = await request.json()
    method = payload.get("method")
    request_id = payload.get("id")

    if method == "initialize":
        envelope = {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-http", "version": "1.0"}}}

        def stream():
            yield "event: message\n"
            yield f"data: {json.dumps(envelope)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Mcp-Session-Id": "sess-1"})
    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0"})
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                             "result": {"tools": TOOLS}})
    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name", "?")
        arguments = params.get("arguments") or {}
        text = str(arguments.get("text", ""))
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": f"echo:{text}"}]}})
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {
        "code": -32601, "message": f"unknown method: {method}"}})
