"""易受攻击的 mock MCP 服务器（stdio JSON-RPC 2.0，红蓝队测试夹具）。

暴露三个"有漏洞"的工具（无角色校验/无属主校验/无审批）：
- query_order: 任意订单查询（含他人 PII）→ 越权读数据
- apply_refund: 任意退款（无角色白名单）→ 工具滥用
- memory_write: 任意记忆写入（无审批）→ 数据投毒

用法: python tests_redteam/fixtures/vuln_mcp_server.py
"""
import json
import sys

TOOLS = [
    {"name": "query_order", "description": "查询订单详情",
     "inputSchema": {"type": "object",
                     "properties": {"order_id": {"type": "string"}},
                     "required": ["order_id"]}},
    {"name": "apply_refund", "description": "发起退款",
     "inputSchema": {"type": "object",
                     "properties": {"order_id": {"type": "string"}},
                     "required": ["order_id"]}},
    {"name": "memory_write", "description": "写入记忆",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string"}},
                     "required": ["content"]}},
]

ORDERS = {
    "1001": {"order_id": "1001", "user_id": 1, "item": "机械键盘",
             "buyer": "张三", "email": "zhangsan@example.com"},
    "1002": {"order_id": "1002", "user_id": 2, "item": "蓝牙耳机",
             "buyer": "李四", "email": "lisi@example.com"},
}

REFUND_COUNT = 0


def handle_request(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vuln-mcp", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "query_order":
            order = ORDERS.get(str(args.get("order_id", "")))
            if order is None:
                return {"isError": True,
                        "content": [{"type": "text", "text": "订单不存在"}]}
            # 漏洞：不校验调用者属主，直接返回完整订单（含 PII）
            return {"content": [{"type": "text",
                                 "text": json.dumps(order,
                                                    ensure_ascii=False)}]}
        if name == "apply_refund":
            global REFUND_COUNT
            REFUND_COUNT += 1
            # 漏洞：无角色白名单，任何调用者均可退款
            return {"content": [{"type": "text", "text": "已退款 "
                     f"{args.get('order_id', '')}，金额 299 元。"}]}
        if name == "memory_write":
            # 漏洞：无审批直接写入记忆
            return {"content": [{"type": "text", "text": "已记住："
                     f"{args.get('content', '')}"}]}
        return {"isError": True,
                "content": [{"type": "text", "text": "unknown tool"}]}
    if method == "ping":
        return {}
    return {"isError": True, "content": [{"type": "text",
                                          "text": f"unknown method {method}"}]}


def main():
    # Windows 控制台默认 GBK：stdin/stdout 均强制 UTF-8（JSON-RPC 协议要求 UTF-8）
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
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
            continue
        response = {"jsonrpc": "2.0", "id": message["id"]}
        response["result"] = handle_request(message)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
