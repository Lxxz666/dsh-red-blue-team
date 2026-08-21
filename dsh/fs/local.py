"""
dsh.fs.local —— LocalFsService：本地文件系统实现（工作区受限）。

安全边界: 所有路径必须落在 ``workspace_root`` 内（相对路径解析到工作区根），
越界访问 → ToolError(OUTSIDE_WORKSPACE)。这就是 Python 版的沙箱围栏。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from ..errors import ToolError
from .fs import FileDiff, FsService


class LocalFsService(FsService):
    """本地文件系统 provider（ctx.fs 的默认实现）。"""

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._root = os.path.abspath(
            (config or {}).get("root", os.getcwd()))

    def workspace_root(self) -> str:
        return self._root

    def resolve(self, path: str) -> str:
        """
        把用户路径解析为绝对路径并校验在工作区内。

        :raises ToolError: 越界（含 ``..`` 逃逸）。
        """
        absolute = path if os.path.isabs(path) \
            else os.path.abspath(os.path.join(self._root, path))
        normalized = os.path.normpath(absolute)
        root_norm = os.path.normpath(self._root)
        if normalized != root_norm and not normalized.startswith(root_norm + os.sep):
            raise ToolError(
                f"path {path!r} escapes workspace root {self._root}",
                code="OUTSIDE_WORKSPACE")
        return normalized

    # ---- 操作 ----

    async def read_text(self, path: str) -> str:
        absolute = self.resolve(path)
        if not os.path.exists(absolute):
            raise ToolError(f"file not found: {path}", code="NOT_FOUND")
        if os.path.isdir(absolute):
            raise ToolError(f"path is a directory: {path}", code="IS_DIRECTORY")
        import asyncio
        content = await asyncio.to_thread(self._read, absolute)
        self.observe(path)
        return content

    @staticmethod
    def _read(absolute: str) -> str:
        with open(absolute, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    async def write_text(self, path: str, content: str) -> FileDiff:
        absolute = self.resolve(path)
        await self.guard_write(path)
        old_text = None
        if os.path.exists(absolute):
            old_text = await self.read_text(path)
        import asyncio
        await asyncio.to_thread(self._write, absolute, content)
        return FileDiff(path=path, old_text=old_text, new_text=content)

    @staticmethod
    def _write(absolute: str, content: str) -> None:
        parent = os.path.dirname(absolute)
        os.makedirs(parent, exist_ok=True)
        with open(absolute, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    async def edit_text(self, path: str, old_text: str,
                        new_text: str) -> FileDiff:
        absolute = self.resolve(path)
        await self.guard_edit(path)
        if not os.path.exists(absolute):
            raise ToolError(f"file not found: {path}", code="NOT_FOUND")
        import asyncio
        current = await asyncio.to_thread(self._read, absolute)
        count = current.count(old_text)
        if count == 0:
            raise ToolError("old_text not found in file", code="NO_MATCH")
        if count > 1:
            raise ToolError(
                f"old_text matches {count} times; make it unique",
                code="AMBIGUOUS_MATCH")
        updated = current.replace(old_text, new_text, 1)
        await asyncio.to_thread(self._write, absolute, updated)
        return FileDiff(path=path, old_text=old_text, new_text=updated)

    async def exists(self, path: str) -> bool:
        try:
            absolute = self.resolve(path)
        except ToolError:
            return False
        return os.path.exists(absolute)
