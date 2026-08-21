"""
dsh.context.instructions —— AGENTS.md 指令注入（对应 TS 版 agent-instructions）。

- 根 AGENTS.md：注册为 prompt 分节（每次组装重新读取，改动即时生效）；
- 子目录 AGENTS.md：后台轮询监视器（2s 间隔）发现新增/变更 → 对每个活跃
  agent ``inject`` 通知（source form=instructions，对应 TS 版 on-touch 注入）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..prompt import PromptSection

log = logging.getLogger("dsh.context")

AGENTS_FILENAME = "AGENTS.md"
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".dsh",
              ".pytest_cache", "__pycache__"}


class AgentInstructionsPlugin(Service):
    """AGENTS.md 根分节 + 子目录监视器。"""

    inject = ("systemPrompt",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.interval = float((config or {}).get("interval", 2.0))
        self._disposers: List[Any] = []
        self._watcher_task: Optional[asyncio.Task] = None
        self._seen: Dict[str, tuple] = {}
        self._root_seen: Optional[tuple] = None

    def apply(self, ctx) -> None:
        section = PromptSection(
            name="agent-instructions", order=5,
            text=lambda _ac: self._read_root_instructions())
        self._disposers.append(ctx.systemPrompt.section(section))
        try:
            loop = asyncio.get_running_loop()
            self._watcher_task = loop.create_task(self._watch_loop())
        except RuntimeError:
            self._watcher_task = None

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
            if self._watcher_task is not None:
                self._watcher_task.cancel()
                self._watcher_task = None
        return cleanup

    # ---- 根 AGENTS.md ----

    def _workspace(self) -> str:
        if self.ctx.has("fs"):
            return self.ctx.fs.workspace_root()
        return os.getcwd()

    def _read_root_instructions(self) -> str:
        """读取工作区根 AGENTS.md（不存在返回空串）。"""
        path = os.path.join(self._workspace(), AGENTS_FILENAME)
        if not os.path.exists(path):
            return ""
        try:
            return open(path, "r", encoding="utf-8").read()
        except OSError:
            return ""

    # ---- 子目录监视 ----

    def _scan(self) -> Dict[str, tuple]:
        """扫描工作区全部子目录 AGENTS.md → {path: (mtime, size)}。"""
        found: Dict[str, tuple] = {}
        root = self._workspace()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                if AGENTS_FILENAME in filenames:
                    path = os.path.join(dirpath, AGENTS_FILENAME)
                    if os.path.abspath(path) == os.path.abspath(
                            os.path.join(root, AGENTS_FILENAME)):
                        continue  # 根文件由分节负责
                    stat = os.stat(path)
                    found[path] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
        return found

    async def _watch_loop(self) -> None:
        """轮询循环：发现新增/变更的 AGENTS.md → 注入活跃 agent。"""
        try:
            while True:
                await asyncio.sleep(self.interval)
                current = self._scan()
                changed = {p: s for p, s in current.items()
                           if self._seen.get(p) != s}
                self._seen = current
                if not changed:
                    continue
                if not self.ctx.has("agents"):
                    continue
                for path, _stat in changed.items():
                    try:
                        content = open(path, "r", encoding="utf-8").read()
                    except OSError:
                        continue
                    rel = os.path.relpath(path, self._workspace())
                    for agent in self.ctx.agents.list():
                        if agent._disposed.is_set():
                            continue
                        agent.inject(
                            f"[指令更新] {rel}\n{content}",
                            source={"kind": "plugin",
                                    "plugin": "agent-instructions",
                                    "form": "instructions"})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent-instructions watcher crashed")
