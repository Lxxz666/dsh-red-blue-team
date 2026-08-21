"""
dsh.fs.tool_fs —— 文件工具族（fs_read/fs_write/fs_edit/fs_glob/fs_grep）。

每个工具都是一等 define_tool 定义；工具体经 ctx.fs 执行（provider 可换，
换 provider 即换存储后端——能力缝的消费者角色）。
"""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..tools import define_tool
from ..tools.presentation import read_result


def _fs_of(run_ctx):
    """解析 fs 服务：agent 作用域优先，回退根 ctx。"""
    agent = run_ctx.execution.agent
    if agent is not None and hasattr(agent, "ctx"):
        return agent.ctx.fs
    return run_ctx.root_ctx.fs


def build_tools() -> List[Any]:
    """构造文件工具族（注册由 ToolFsPlugin.apply 完成）。"""

    @define_tool(
        name="fs_read", description="读取工作区内的文本文件（UTF-8，带行号）。",
        parameters={"path": {"type": "string", "required": True,
                              "description": "相对工作区的文件路径"},
                    "offset": {"type": "integer", "description": "起始行（1 基）"},
                    "limit": {"type": "integer", "description": "最多行数"}},
        output={"type": "string"},
        present_result=lambda args, result: read_result(
            title=f"读取 {args['path']}", path=args["path"],
            offset=int(args.get("offset") or 1), lines=[],
            total_lines=0, content=str(result.value)),
    )
    async def fs_read(args, run_ctx):
        fs = _fs_of(run_ctx)
        content = await fs.read_text(args["path"])
        lines = content.splitlines()
        offset = int(args.get("offset") or 1)
        limit = args.get("limit")
        start = max(0, offset - 1)
        selected = lines[start:] if limit is None else lines[start:start + limit]
        body = "\n".join(f"{start + i + 1}\t{line}"
                         for i, line in enumerate(selected))
        if offset > 1 or limit is not None:
            return (f"(共 {len(lines)} 行，显示 {offset}..{start + len(selected)})\n"
                    + body)
        return "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))

    @define_tool(
        name="fs_write", description="创建或覆盖工作区内的文件。",
        parameters={"path": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True}},
        output={"type": "string"})
    async def fs_write(args, run_ctx):
        fs = _fs_of(run_ctx)
        diff = await fs.write_text(args["path"], args["content"])
        return f"wrote {diff.path} ({len(args['content'])} chars)"

    @define_tool(
        name="fs_edit", description="字符串替换式编辑（old_text 必须唯一命中）。",
        parameters={"path": {"type": "string", "required": True},
                    "old_text": {"type": "string", "required": True},
                    "new_text": {"type": "string", "required": True}},
        output={"type": "string"})
    async def fs_edit(args, run_ctx):
        fs = _fs_of(run_ctx)
        diff = await fs.edit_text(args["path"], args["old_text"], args["new_text"])
        return f"edited {diff.path}"

    @define_tool(
        name="fs_glob", description="按 glob 模式列出工作区内文件。",
        parameters={"pattern": {"type": "string", "required": True,
                                 "description": "如 **/*.py（无 / 时匹配任意深度 basename）"}},
        output={"type": "array", "items": {"type": "string"}},
        render=lambda args, value: "\n".join(value) if value else "(无匹配)")
    async def fs_glob(args, run_ctx):
        fs = _fs_of(run_ctx)
        root = fs.workspace_root()
        pattern = args["pattern"]
        import asyncio

        def _walk() -> List[str]:
            out: List[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in
                               (".git", "__pycache__", ".venv", "node_modules")]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), root) \
                        .replace(os.sep, "/")
                    if fnmatch.fnmatch(rel, pattern) or \
                            ("/" not in pattern and fnmatch.fnmatch(name, pattern)):
                        out.append(rel)
            return out
        return await asyncio.to_thread(_walk)

    @define_tool(
        name="fs_grep", description="按正则搜索工作区文件内容。",
        parameters={"pattern": {"type": "string", "required": True},
                    "include": {"type": "string",
                                "description": "可选文件名过滤 glob"}},
        output={"type": "array", "items": {"type": "object"}},
        render=lambda args, value: _render_grep(value))
    async def fs_grep(args, run_ctx):
        fs = _fs_of(run_ctx)
        root = fs.workspace_root()
        include = args.get("include")
        try:
            regex = re.compile(args["pattern"])
        except re.error as exc:
            from ..errors import ToolArgsError
            raise ToolArgsError(f"invalid regex: {exc}")
        import asyncio

        def _search() -> List[Dict[str, Any]]:
            results: List[Dict[str, Any]] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in
                               (".git", "__pycache__", ".venv", "node_modules")]
                for name in filenames:
                    if include and not fnmatch.fnmatch(name, include):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        with open(full, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            for lineno, line in enumerate(fh, 1):
                                if regex.search(line):
                                    results.append({
                                        "file": os.path.relpath(full, root)
                                        .replace(os.sep, "/"),
                                        "line": lineno,
                                        "text": line.rstrip("\n")})
                                    if len(results) >= 200:
                                        return results
                    except OSError:
                        continue
            return results
        return await asyncio.to_thread(_search)

    return [fs_read, fs_write, fs_edit, fs_glob, fs_grep]


def _render_grep(value: List[Dict[str, Any]]) -> str:
    if not value:
        return "(无匹配)"
    return "\n".join(f"{m['file']}:{m['line']}: {m['text']}" for m in value)


class ToolFsPlugin(Service):
    """注册文件工具族的插件（base bundle 的一行）。"""

    inject = ("tools",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx):
        for tool in build_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
