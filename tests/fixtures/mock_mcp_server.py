"""
mock MCP 服务器（stdio JSON-RPC 2.0，测试夹具）。

提供两个工具: echo（输入 schema 完整）与 lenient（anyOf schema，测试降级）。
用法: python tests/fixtures/mock_mcp_server.py
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "回显文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "required": True}},
            "required": ["text"],
        },
    },
    {
        "name": "lenient",
        "description": "宽松 schema 工具（anyOf）",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"anyOf": [
                {"type": "string"}, {"type": "number"}]}},
        },
    },
]


def handle_request(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return {"content": [{"type": "text",
                                 "text": f"echoed: {args.get('text', '')}"}]}
        if name == "lenient":
            return {"content": [{"type": "text",
                                 "text": f"got: {json.dumps(args.get('value'))}"}]}
        return {"isError": True,
                "content": [{"type": "text", "text": "unknown tool"}]}
    if method == "ping":
        return {}
    return {"isError": True, "content": [{"type": "text",
                                          "text": f"unknown method {method}"}]}


def main():
    # Windows 控制台默认 GBK：强制 UTF-8 输出（JSON-RPC 协议要求 UTF-8）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message or message["id"] is None:
            continue  # 通知（initialized 等）不响应
        response = {"jsonrpc": "2.0", "id": message["id"]}
        result = handle_request(message)
        if isinstance(result, dict) and "isError" in result and \
                "content" in result and "protocolVersion" not in result:
            response["result"] = result
        else:
            response["result"] = result
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
