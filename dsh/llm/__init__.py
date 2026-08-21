"""
dsh.llm —— LLM 接缝（能力缝）：消息词汇、流式协议、适配器注册表。

实现包: dsh.llm.adapters（抽象 + 注册表）、dsh.llm.deepseek（DeepSeek 适配器）、
dsh.llm.mock（无密钥确定性 Mock 适配器）。
"""
from .messages import (ContentBlock, Message, messages_to_openai)

__all__ = ["ContentBlock", "Message", "messages_to_openai"]
