"""
dsh.workspace.workspace —— WorkspaceRegistry（ctx.workspaceRegistry）。

对应 TS 版 dsh-workspace：工作目录的持久记录——规范路径上的稳定 id、显示
标题、归属于它的会话的有序账本。宿主侧可选能力（无工具、无提示词、无
会话事件），经 storage domain "workspace" 持久化（未挂载 storage 则仅内存）。

- ``create(path, title?)``：realpath 规范化，拒绝不存在/非目录路径；每规范
  路径至多一条记录；新记录前置到持久顺序；重复调用返回现有记录且不改标题。
- ``get(id)`` / ``list()``（同步、持久顺序）/ ``resolve_by_path(path)``
  （同一 realpath 规范化；缺失路径抛 ValueError，不创建）。
- ``insert_before(id, before?)``：DOM 式移动（锚点省略 = 追加到末尾）；
  来源/锚点未知拒绝且不写；自身为锚或原位 = 不写完成；返回完整已提交顺序。
- ``delete(id)``：只移除注册记录、顺序条目与会话归属；未知返回 False。
  目录、用户文件、活跃会话与持久化日志绝不受影响（其会话归入 Ungrouped）。
- 会话归属：``account_session(workspace_id, session_id)``（去重新在前）/
  ``sessions_of(workspace_id, include_archived=False)``；
- 归档：``archive_session(id)`` / ``unarchive_session(id)`` /
  ``archived_session_ids``——归档会话从分组视图消失但保留会话日志与
  sessionIds 席位（取消归档恢复原位置）；已归档再归档 = 不写完成；未知 id
  拒绝（已知 = 已记账 ∪ 活跃会话）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..kernel import Service

log = logging.getLogger("dsh.workspace")

STORAGE_DOMAIN = "workspace"


@dataclass
class Workspace:
    """一个工作区实体（规范路径 + 标题 + 会话归属账本）。"""

    id: str
    path: str
    title: str
    session_ids: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "path": self.path, "title": self.title,
                "session_ids": list(self.session_ids)}


class WorkspaceRegistry(Service):
    """工作区实体注册表（ctx.workspaceRegistry）。"""

    provides = "workspaceRegistry"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._records: Dict[str, Workspace] = {}
        self._order: List[str] = []
        self._archived: set = set()
        self._counter = 0

    def apply(self, ctx) -> None:
        ctx.set("workspaceRegistry", self)
        if ctx.has("storage"):
            order = ctx.storage.get(STORAGE_DOMAIN, "order") or []
            records = ctx.storage.get(STORAGE_DOMAIN, "records") or {}
            archived = ctx.storage.get(STORAGE_DOMAIN, "archived") or []
            for ws_id in order:
                raw = records.get(ws_id)
                if not raw:
                    continue
                ws = Workspace(id=raw["id"], path=raw["path"],
                               title=raw.get("title") or raw["path"],
                               session_ids=list(raw.get("session_ids") or []))
                self._records[ws_id] = ws
                self._order.append(ws_id)
                self._counter = max(self._counter, self._counter_from(ws_id))
            self._archived = set(archived)

    @staticmethod
    def _counter_from(ws_id: str) -> int:
        try:
            return int(ws_id.removeprefix("ws-"))
        except ValueError:
            return 0

    def _persist(self) -> None:
        if not self.ctx.has("storage"):
            return
        self.ctx.storage.put(STORAGE_DOMAIN, "order", list(self._order))
        self.ctx.storage.put(
            STORAGE_DOMAIN, "records",
            {ws_id: self._records[ws_id].to_json() for ws_id in self._order})
        self.ctx.storage.put(STORAGE_DOMAIN, "archived",
                             sorted(self._archived))

    # ---- 创建与查找 ----

    def create(self, path: str, title: Optional[str] = None) -> Workspace:
        """注册一个工作区（规范路径去重；新记录前置到持久顺序）。"""
        canon = os.path.realpath(os.path.abspath(path))
        if not os.path.isdir(canon):
            raise ValueError(f"workspace path is not a directory: {canon}")
        existing = next((ws for ws in self._records.values()
                         if ws.path == canon), None)
        if existing is not None:
            return existing  # 重复调用不改标题
        self._counter += 1
        workspace = Workspace(
            id=f"ws-{self._counter}", path=canon,
            title=title or os.path.basename(canon))
        self._records[workspace.id] = workspace
        self._order.insert(0, workspace.id)
        self._persist()
        return workspace

    def get(self, workspace_id: str) -> Optional[Workspace]:
        return self._records.get(workspace_id)

    def list(self) -> List[Workspace]:
        """持久注册表顺序的全部工作区（同步）。"""
        return [self._records[ws_id] for ws_id in self._order
                if ws_id in self._records]

    def resolve_by_path(self, path: str) -> Workspace:
        """按路径解析（同一 realpath 规范化；缺失路径拒绝而非创建）。"""
        canon = os.path.realpath(os.path.abspath(path))
        for ws in self._records.values():
            if ws.path == canon:
                return ws
        raise ValueError(f"no workspace registered for path: {canon}")

    # ---- 顺序 ----

    def insert_before(self, workspace_id: str,
                      before: Optional[str] = None) -> List[str]:
        """DOM insertBefore 式移动；返回完整的已提交顺序。"""
        if workspace_id not in self._records:
            raise ValueError(f"unknown workspace: {workspace_id!r}")
        if before is not None and before not in self._records:
            raise ValueError(f"unknown anchor workspace: {before!r}")
        current = self._order.index(workspace_id)
        target = (self._order.index(before) if before is not None
                  else len(self._order) - 1)
        # 自身为锚 / 已在锚点前 / 已居末位（锚点省略）→ 不写完成
        if before is None:
            if current == len(self._order) - 1:
                return list(self._order)
        else:
            if current == target or current == target - 1:
                return list(self._order)
        self._order.remove(workspace_id)
        insert_at = (self._order.index(before) if before is not None
                     else len(self._order))
        self._order.insert(insert_at, workspace_id)
        self._persist()
        return list(self._order)

    def delete(self, workspace_id: str) -> bool:
        """只移除注册记录/顺序条目/会话归属；目录与会话日志不受影响。"""
        if workspace_id not in self._records:
            return False
        self._records.pop(workspace_id, None)
        self._order = [ws_id for ws_id in self._order
                       if ws_id != workspace_id]
        self._persist()
        return True

    # ---- 会话归属 ----

    def account_session(self, workspace_id: str, session_id: str) -> None:
        """把会话记入工作区账本（去重 + 最新在前）。"""
        workspace = self._records.get(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace: {workspace_id!r}")
        if session_id in workspace.session_ids:
            workspace.session_ids.remove(session_id)
        workspace.session_ids.insert(0, session_id)
        self._persist()

    def sessions_of(self, workspace_id: str,
                    include_archived: bool = False) -> List[str]:
        workspace = self._records.get(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace: {workspace_id!r}")
        if include_archived:
            return list(workspace.session_ids)
        return [sid for sid in workspace.session_ids
                if sid not in self._archived]

    # ---- 归档 ----

    @property
    def archived_session_ids(self) -> List[str]:
        return sorted(self._archived)

    def archive_session(self, session_id: str) -> None:
        """归档（已归档 = 不写完成；未知 id 拒绝）。"""
        if session_id in self._archived:
            return
        if not self._known_session(session_id):
            raise ValueError(f"unknown session: {session_id!r}")
        self._archived.add(session_id)
        self._persist()

    def unarchive_session(self, session_id: str) -> None:
        if session_id in self._archived:
            self._archived.discard(session_id)
            self._persist()

    def _known_session(self, session_id: str) -> bool:
        for ws in self._records.values():
            if session_id in ws.session_ids:
                return True
        if self.ctx.has("sessions") and self.ctx.sessions.get(session_id):
            return True
        return False

    # ---- 分组视图 ----

    def group_sessions(self) -> List[Dict[str, Any]]:
        """宿主分组视图：每个工作区一行（归档会话消失但保留席位）。"""
        return [{"workspace_id": ws.id, "title": ws.title, "path": ws.path,
                 "session_ids": self.sessions_of(ws.id)}
                for ws in self.list()]

    def close(self) -> None:
        self._records.clear()
        self._order.clear()
        self._archived.clear()


class SessionWorkspacePlugin(Service):
    """把活跃会话按 header.cwd 记账进工作区（session/created 驱动）。"""

    inject = ("workspaceRegistry",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposer = None

    def apply(self, ctx) -> None:
        def on_created(session) -> None:
            cwd = getattr(session.header, "cwd", None)
            if not cwd:
                return
            try:
                workspace = ctx.workspaceRegistry.create(cwd)
                ctx.workspaceRegistry.account_session(workspace.id, session.id)
            except ValueError:
                log.debug("workspace accounting skipped for %s (cwd %r)",
                          session.id, cwd)
        self._disposer = ctx.on("session/created", on_created)

        def cleanup() -> None:
            if self._disposer is not None:
                self._disposer()
                self._disposer = None
        return cleanup
