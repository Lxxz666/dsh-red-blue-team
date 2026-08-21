"""
dsh.persistence —— 会话持久化缝（JSONL + SQLite 后端）。
"""
from .jsonl import JsonlPersistence
from .service import SessionPersistence
from .sqlite import SqlitePersistence

__all__ = ["SessionPersistence", "JsonlPersistence", "SqlitePersistence"]

