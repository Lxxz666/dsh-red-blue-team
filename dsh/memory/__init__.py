"""
dsh.memory —— 原生记忆子系统：MemoryService（ctx.memory）+ memory_* 工具。

对应 TS 版 mcp-memory 示例的「内部记忆」形态：
- 条目 = {id, content, tags?, created_at}，持久化到 ctx.storage domain "memory"
  （未挂载 storage 则仅内存）；
- 检索 = 词集 Jaccard 相似度（中英混合：单词 token + CJK 相邻二字组）；
- 工具：memory_add / memory_search / memory_list / memory_remove。
"""
from .memory import MemoryService, ToolMemoryPlugin  # noqa: F401
