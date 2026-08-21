"""
dsh.ids —— 身份标识生成。

与 TypeScript 版的 branded ID（`SessionId`/`CallId`/`JobId`）对应。
Python 不做编译期品牌，用前缀保证肉眼可辨 + 唯一。
"""
from __future__ import annotations

import uuid

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _short(prefix: str) -> str:
    """生成 `<prefix>-<10位随机>` 的短 id。"""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def new_session_id() -> str:
    """生成会话 id（`session-xxxx`）。"""
    return _short("session")


def new_call_id() -> str:
    """生成工具调用 id（`call-xxxx`），用于配对 tool/call 与 tool/result。"""
    return _short("call")


def new_message_id() -> str:
    """生成消息 id（`msg-xxxx`）。"""
    return _short("msg")


def new_job_id() -> str:
    """生成后台任务 id（`job-xxxx`）。"""
    return _short("job")


def new_goal_id() -> str:
    """生成目标 id（`goal-xxxx`）。"""
    return _short("goal")
