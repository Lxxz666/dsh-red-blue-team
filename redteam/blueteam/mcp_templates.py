"""redteam.blueteam.mcp_templates —— MCP 目标类别的修复模板（人工实施指引）。

MCP 服务器是外部进程，红蓝队框架不自动修改其代码；模板给出完整的
服务端加固指引 + 验证步骤，供人工实施（修复报告中的代码级示例）。
在 templates.py 尾部自动注册进 FIX_TEMPLATES。
"""
from __future__ import annotations

from .templates import FixTemplate, register_template

register_template(FixTemplate(
    "mcp-tool-authz", "mcp_tool_abuse", "MCP 工具层鉴权（角色白名单 + 审批闸门）",
    explanation=(
        "【现象】低权限调用者通过 MCP 工具 apply_refund 成功发起退款。\n"
        "【根因】MCP 服务器对 tools/call 不做调用者角色校验，权限完全依赖"
        "宿主 LLM 的提示词约束（LLM08:2025）。\n"
        "【影响】任何能触达 MCP 端点的客户端（含注入后的 LLM）都可调用高权限"
        "工具，评级 critical。"),
    rationale="MCP 工具是权限边界，必须在服务器端强制校验，不能信任模型自述身份。",
    how_to_fix=[
        "MCP 服务器在 tools/call 前解析调用者身份（会话头/token），并校验工具角色白名单",
        "高风险工具（退款/删除/转账）强制审批：返回审批工单号而非直接执行",
        "工具描述中声明权限要求，供宿主 LLM 正确拒绝（辅助，非安全边界）",
        "越权调用记录审计日志并告警",
    ],
    code_before=(
        "# 修复前：MCP 服务器无鉴权\n"
        "def handle_tools_call(name, arguments):\n"
        "    return execute(name, arguments)   # 任何调用者直达工具"),
    code_after=(
        "# 修复后：身份解析 + 角色白名单 + 审批\n"
        "ALLOWLIST = {\"apply_refund\": {\"admin\"}, \"query_order\": {\"admin\", \"customer\"}}\n"
        "def handle_tools_call(name, arguments, caller):\n"
        "    if caller.role not in ALLOWLIST.get(name, set()):\n"
        "        raise Forbidden(f\"{caller.role} 无权调用 {name}\")\n"
        "    if name in HIGH_RISK_TOOLS:\n"
        "        return {\"content\": [{\"type\": \"text\",\n"
        "                              \"text\": approval.submit(name, arguments)}]}\n"
        "    return execute(name, arguments)"),
    auto_fixable=False,
    manual_steps=("MCP 服务器端工具角色白名单", "高风险工具审批闸门",
                  "越权调用审计告警"),
    verify_steps=["低权限调用者调用白名单外工具被拒绝", "审计日志出现越权记录"]))

register_template(FixTemplate(
    "mcp-arg-scope", "mcp_data_disclosure", "MCP 工具参数属主校验（防越权读数据）",
    explanation=(
        "【现象】通过 query_order(order_id=1002) 读到他人订单（含 PII）。\n"
        "【根因】MCP 工具按 id 直查，无属主校验（API1 BOLA 的工具面形态）。\n"
        "【影响】任意数据横向读取，评级 critical。"),
    rationale="工具参数中的资源 id 必须与调用者身份做属主绑定校验。",
    how_to_fix=[
        "资源查询强制属主过滤：WHERE id=? AND user_id=当前调用者",
        "资源 id 使用不可枚举标识（UUID）",
        "越权访问返回统一错误，不泄露资源存在性差异",
    ],
    code_before=(
        "# 修复前：按 id 直查\n"
        "def query_order(order_id, caller):\n"
        "    return db.orders[order_id]           # 不校验属主"),
    code_after=(
        "# 修复后：属主过滤\n"
        "def query_order(order_id, caller):\n"
        "    order = db.query(\"SELECT * FROM orders WHERE id=? AND user_id=?\",\n"
        "                     (order_id, caller.user_id))\n"
        "    if not order:\n"
        "        raise Forbidden(\"无权访问该订单\")\n"
        "    return order"),
    auto_fixable=False,
    manual_steps=("工具查询加属主过滤", "资源 id 不可枚举化"),
    verify_steps=["A 调用者查询 B 的资源被拒绝"]))

register_template(FixTemplate(
    "mcp-memory-approval", "mcp_memory_poisoning", "MCP 记忆写入审批 + 来源标注",
    explanation=(
        "【现象】memory_write 工具接受任意内容直接写入记忆，投毒内容持久生效。\n"
        "【根因】记忆写入无审批、无来源可信度标注（LLM04:2025 数据投毒）。\n"
        "【影响】投毒内容跨会话污染 agent 决策，评级 high。"),
    rationale="记忆写入必须审批制 + 内容标注来源可信度，检索时降低不可信来源权重。",
    how_to_fix=[
        "memory_write 改为审批制：写入候选区，人工确认后生效",
        "写入内容标注来源（用户/工具/系统）与可信度",
        "检索时不可信来源内容降权或隔离（<untrusted> 标记）",
    ],
    auto_fixable=False,
    manual_steps=("记忆写入审批制", "内容来源与可信度标注"),
    verify_steps=["未审批内容不被后续会话检索命中"]))
