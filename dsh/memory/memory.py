"""
dsh.memory.memory —— MemoryService（ctx.memory）+ memory_* 工具。

设计（对应 TS 版 mcp-memory 的「内部记忆」形态，零外部进程）：

- 条目 ``{id, content, tags?, created_at}``，持久化到 ctx.storage domain
  "memory"（未挂载 storage 则仅内存，同 feedback 的降级约定）；
- 检索 = 词集 Jaccard 相似度：文本 casefold 后取单词 token，并对连续 CJK
  片段补相邻二字组（中文无需分词即可命中）；score = |A∩B| / |A∪B|；
- 每次变更广播 ``memory/changed`` {op}；
- 工具：memory_add（content + tags?）、memory_search（query + limit）、
  memory_list（limit）、memory_remove（id）。
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..kernel import Service
from ..tools import define_tool

STORAGE_DOMAIN = "memory"

_WORD_RE = re.compile(r"[0-9a-z_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize(text: str) -> set:
    """词集：casefold 单词 token + 连续 CJK 片段的相邻二字组。"""
    folded = (text or "").casefold()
    tokens = set(_WORD_RE.findall(folded))
    for run in _CJK_RE.findall(folded):
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
        if len(run) == 1:
            tokens.add(run)
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class MemoryService(Service):
    """原生记忆服务（ctx.memory）。"""

    provides = "memory"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._entries: Dict[str, Dict[str, Any]] = {}

    def apply(self, ctx) -> None:
        ctx.set("memory", self)
        if ctx.has("storage"):
            self._entries = ctx.storage.get(STORAGE_DOMAIN, "_entries") or {}

    # ---- 内部 ----

    def _persist(self) -> None:
        if self.ctx.has("storage"):
            self.ctx.storage.put(STORAGE_DOMAIN, "_entries",
                                 dict(self._entries))

    def _emit(self, op: str) -> None:
        try:
            self.ctx.events.emit("memory/changed", {"op": op})
        except Exception:
            pass

    # ---- 对外 API ----

    def add(self, content: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """写入一条记忆（空内容拒绝）。"""
        if not (content or "").strip():
            raise ValueError("memory content must not be empty")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "content": content,
            "tags": [str(t) for t in (tags or [])],
            "created_at": int(time.time() * 1000),
        }
        self._entries[entry["id"]] = entry
        self._persist()
        self._emit("add")
        return dict(entry)

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """按 id 读取（无则 None）。"""
        entry = self._entries.get(entry_id)
        return dict(entry) if entry else None

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        """最新在前（created_at 降序）。"""
        entries = sorted(self._entries.values(),
                         key=lambda e: e.get("created_at", 0), reverse=True)
        return [dict(e) for e in entries[: max(0, int(limit))]]

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Jaccard 相似度检索（score > 0 才返回；同分按时间倒序）。"""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for entry in self._entries.values():
            haystack = " ".join([entry.get("content", ""),
                                 *entry.get("tags", [])])
            score = _jaccard(query_tokens, _tokenize(haystack))
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at", 0)),
                    reverse=True)
        return [dict(entry, score=round(score, 4))
                for score, entry in scored[: max(0, int(limit))]]

    def remove(self, entry_id: str) -> bool:
        """删除一条记忆。:return: 是否存在并删除。"""
        if entry_id in self._entries:
            del self._entries[entry_id]
            self._persist()
            self._emit("remove")
            return True
        return False

    def clear(self) -> None:
        self._entries.clear()
        self._persist()
        self._emit("clear")

    def close(self) -> None:
        self._entries.clear()


# ---- 工具 ----

def _memory_add_tool() -> Any:
    @define_tool(
        name="memory_add",
        description="把一条事实/偏好写入长期记忆（跨会话保留，供 memory_search 检索）。",
        parameters={
            "content": {"type": "string", "required": True,
                        "description": "要记住的内容"},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "可选标签，参与检索"},
        },
        output={"type": "object"},
    )
    async def memory_add(args, run_ctx):
        return run_ctx.root_ctx.memory.add(args.get("content", ""),
                                           args.get("tags"))

    return memory_add


def _memory_search_tool() -> Any:
    @define_tool(
        name="memory_search",
        description="按相似度检索长期记忆（中英混合：单词 + 汉字二字组 Jaccard）。",
        parameters={
            "query": {"type": "string", "required": True,
                      "description": "检索词/短语"},
            "limit": {"type": "integer", "default": 5},
        },
        output={"type": "array"},
    )
    async def memory_search(args, run_ctx):
        return run_ctx.root_ctx.memory.search(args.get("query", ""),
                                              args.get("limit", 5))

    return memory_search


def _memory_list_tool() -> Any:
    @define_tool(
        name="memory_list",
        description="列出最近的记忆条目（最新在前）。",
        parameters={
            "limit": {"type": "integer", "default": 100},
        },
        output={"type": "array"},
    )
    async def memory_list(args, run_ctx):
        return run_ctx.root_ctx.memory.list(args.get("limit", 100))

    return memory_list


def _memory_remove_tool() -> Any:
    @define_tool(
        name="memory_remove",
        description="按 id 删除一条记忆。",
        parameters={
            "id": {"type": "string", "required": True},
        },
        output={"type": "boolean"},
    )
    async def memory_remove(args, run_ctx):
        return run_ctx.root_ctx.memory.remove(args.get("id", ""))

    return memory_remove


class ToolMemoryPlugin(Service):
    """注册 memory_* 工具的插件。"""

    inject = ("tools", "memory")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        for factory in (_memory_add_tool, _memory_search_tool,
                        _memory_list_tool, _memory_remove_tool):
            self._disposers.append(ctx.tools.register(factory()))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
