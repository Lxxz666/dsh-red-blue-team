"""
dsh.fs.fs —— FsService（ctx.fs）：文件系统能力缝 + 观察策略事件。

事件（对应 TS 版 fs/*）:

- ``fs/write-intent``（waterfall）：写前决策，监听者可拒绝（返回非 None 原因）；
- ``fs/edit-intent``（waterfall）：编辑前决策；
- ``fs/observed``（emit）：读/写完成后的观察通知（观察策略、skill 订阅）。

观察策略（fs-observation-policy 的 Python 对应）:

- ``record_observation`` 维护「已读路径」集合；
- 默认策略可要求「先读后写」（read-before-write，可用配置关闭）。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ToolError
from ..kernel import Service

log = logging.getLogger("dsh.fs")


@dataclass
class FileDiff:
    """一处文件变更（diff 卡片的单元）。"""

    path: str
    old_text: Optional[str]
    new_text: str

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": self.path,
                               "new_text": self.new_text}
        if self.old_text is not None:
            out["old_text"] = self.old_text
        return out


class FsService(Service, ABC):
    """文件系统抽象服务（ctx.fs）。provider 换实现，工具跟着迁移。"""

    provides = "fs"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._observed: set = set()
        self._require_read_before_write = bool(
            (config or {}).get("require_read_before_write", False))

    def apply(self, ctx) -> None:
        ctx.set("fs", self)

    # ---- 观察策略 ----

    def observe(self, path: str) -> None:
        """记录一次对 path 的观察（读）。"""
        self._observed.add(path)
        try:
            self.ctx.events.emit("fs/observed", {"path": path, "op": "read"})
        except Exception:
            pass

    def _check_write(self, path: str) -> None:
        """写前观察策略：require_read_before_write 时未读先写 → 拒绝。"""
        if self._require_read_before_write and path not in self._observed:
            raise ToolError(
                f"write to unobserved path {path!r} denied "
                f"(read-before-write policy)", code="UNOBSERVED_WRITE")

    async def guard_write(self, path: str) -> None:
        """写前 waterfall 决策（fs/write-intent）。"""
        reason = await self.ctx.events.waterfall(
            "fs/write-intent", {"path": path}, default=None)
        if reason:
            raise ToolError(f"write denied: {reason}", code="WRITE_DENIED")
        self._check_write(path)

    async def guard_edit(self, path: str) -> None:
        """编辑前 waterfall 决策（fs/edit-intent）。"""
        reason = await self.ctx.events.waterfall(
            "fs/edit-intent", {"path": path}, default=None)
        if reason:
            raise ToolError(f"edit denied: {reason}", code="EDIT_DENIED")
        self._check_write(path)

    # ---- 抽象操作 ----

    @abstractmethod
    async def read_text(self, path: str) -> str:
        """读文件全文。"""

    @abstractmethod
    async def write_text(self, path: str, content: str) -> FileDiff:
        """写文件（创建/覆盖）。"""

    @abstractmethod
    async def edit_text(self, path: str, old_text: str,
                        new_text: str) -> FileDiff:
        """字符串替换式编辑（old_text 必须唯一命中）。"""

    @abstractmethod
    async def exists(self, path: str) -> bool:
        """文件/目录是否存在。"""

    @abstractmethod
    def workspace_root(self) -> str:
        """工作区根目录。"""
