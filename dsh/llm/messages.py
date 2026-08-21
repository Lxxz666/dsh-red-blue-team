"""
dsh.llm.messages —— 对话词汇：ContentBlock 与 Message（对应 ContentBlockMap/MessageSourceMap）。

与 TS 版 llm-streaming.md 对齐：

- 一条消息 = 一组类型化 content block；
- block 类型通过注册表扩展（text/reasoning/image/tool-call/tool-result）；
- ``Message.source`` 回答「谁生产的」，``form`` 回答「什么类型的信息」（语义轴，绝不视觉化）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..ids import new_message_id

#: block 类型目录（可扩展）
BLOCK_CATALOG: Dict[str, str] = {
    "text": "可见文本。",
    "reasoning": "思维链（与可见文本区分）。",
    "image": "图片附件。",
    "tool-call": "模型发起的工具调用（id/name/原始 JSON 参数）。",
    "tool-result": "工具结果回填（tool_call_id/content/is_error）。",
}


@dataclass(frozen=True)
class ContentBlock:
    """
    一个类型化内容块。

    ``kind`` 决定语义字段（text 块带 ``text``，tool-call 块带 ``call_id`` 等），
    未知字段被忽略以保证前向兼容。
    """

    kind: str
    text: Optional[str] = None
    call_id: Optional[str] = None
    name: Optional[str] = None
    arguments: Optional[str] = None
    tool_call_id: Optional[str] = None
    content: Any = None
    is_error: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        """序列化为无损失 JSON（会话日志持久化协议）。"""
        out: Dict[str, Any] = {"kind": self.kind}
        for key in ("text", "call_id", "name", "arguments", "tool_call_id"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.content is not None:
            out["content"] = self.content
        if self.is_error:
            out["is_error"] = True
        return out

    def to_openai(self) -> Dict[str, Any]:
        """投影为 OpenAI 兼容协议的 content 片段。"""
        if self.kind == "text":
            return {"type": "text", "text": self.text or ""}
        if self.kind == "reasoning":
            return {"type": "text", "text": self.text or ""}
        if self.kind == "tool-call":
            return {"type": "tool_call", "id": self.call_id or "",
                    "function": {"name": self.name or "",
                                 "arguments": self.arguments or "{}"}}
        return {"type": "text", "text": str(self.content or "")}

    @staticmethod
    def text_block(text: str) -> "ContentBlock":
        """便捷构造：文本块。"""
        return ContentBlock(kind="text", text=text)

    @staticmethod
    def tool_call_block(call_id: str, name: str, arguments: str) -> "ContentBlock":
        """便捷构造：工具调用块。"""
        return ContentBlock(kind="tool-call", call_id=call_id, name=name,
                            arguments=arguments)

    @staticmethod
    def tool_result_block(tool_call_id: str, content: str,
                          is_error: bool = False) -> "ContentBlock":
        """便捷构造：工具结果块。"""
        return ContentBlock(kind="tool-result", tool_call_id=tool_call_id,
                            content=content, is_error=is_error)


#: 消息来源目录
SOURCE_CATALOG: Dict[str, str] = {
    "user": "真人直接输入。",
    "plugin": "插件注入的上下文。",
    "model": "模型产出（带 provider/model 出处）。",
    "tool": "工具结果。",
    "goal": "目标续轮。",
    "compaction": "压缩摘要。",
}


@dataclass(frozen=True)
class Message:
    """
    一条不可变消息：身份 + 角色 + 内容块 + 来源。

    ``role``: user / assistant / system。
    ``source``: dict，必含 ``kind``（见 SOURCE_CATALOG），可选 ``form``/``plugin`` 等。
    """

    id: str
    role: str
    content: List[ContentBlock]
    source: Dict[str, Any]

    @staticmethod
    def user(text: str, source: Optional[Dict[str, Any]] = None) -> "Message":
        """便捷构造：用户消息。"""
        return Message(id=new_message_id(), role="user",
                       content=[ContentBlock.text_block(text)],
                       source=source or {"kind": "user"})

    @staticmethod
    def assistant(blocks: List[ContentBlock], provider: Optional[str] = None,
                  model: Optional[str] = None) -> "Message":
        """便捷构造：助手消息（带出处）。"""
        return Message(id=new_message_id(), role="assistant", content=blocks,
                       source={"kind": "model", "provider": provider, "model": model})

    def plain_text(self) -> str:
        """拼接可见文本（UI/标题用）。"""
        parts = []
        for block in self.content:
            if block.kind in ("text", "reasoning") and block.text:
                parts.append(block.text)
        return "\n".join(parts)


def messages_to_openai(messages: List[Message]) -> List[Dict[str, Any]]:
    """
    把派生历史投影为 OpenAI 兼容的 messages 列表。

    tool-result 块被折叠成 role=tool 消息；其余按角色分组。
    """
    out: List[Dict[str, Any]] = []
    for message in messages:
        tool_results = [b for b in message.content if b.kind == "tool-result"]
        normal = [b for b in message.content if b.kind != "tool-result"]
        if normal:
            out.append({"role": message.role,
                        "content": [b.to_openai() for b in normal]})
        for block in tool_results:
            out.append({"role": "tool", "tool_call_id": block.tool_call_id or "",
                        "content": str(block.content)})
    return out
