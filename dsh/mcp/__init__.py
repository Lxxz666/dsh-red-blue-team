"""dsh.mcp —— MCP 客户端域。"""
from .mcp import (McpClient, McpServerPlugin, build_mcp_tool_definition,
                  safe_schema)

__all__ = ["McpClient", "McpServerPlugin", "build_mcp_tool_definition",
           "safe_schema"]
