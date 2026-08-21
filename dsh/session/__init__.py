"""
dsh.session —— 事件溯源会话域。

导出: Session（日志+surface+派生）、SessionHeader、SessionEvent/事件词汇、
SessionStore（ctx.sessions）、SessionTitleService（ctx.sessionTitle）、
SessionQueryService（ctx.sessionQuery）、register_event_type（插件扩展事件）。
"""
from .events import (EVENT_CATALOG, SESSION_FORMAT_VERSION,
                     SURFACE_EVENT_TYPES, SessionEvent, SurfaceOp,
                     TurnEndReason, is_json_value, register_event_type)
from .query import SessionQueryService
from .session import (Session, SessionHeader, SurfaceManager,
                      derive_event_message)
from .store import SessionStore
from .title import SessionTitleService

__all__ = [
    "Session", "SessionHeader", "SessionEvent", "SurfaceManager",
    "SurfaceOp", "TurnEndReason", "SessionStore", "SessionTitleService",
    "SessionQueryService",
    "SESSION_FORMAT_VERSION", "SURFACE_EVENT_TYPES", "EVENT_CATALOG",
    "is_json_value", "register_event_type", "derive_event_message",
]
