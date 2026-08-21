"""
dsh.schedule.schedule —— Schedule（对应 TS 版 schedule/cron）。

- 条目两种：interval（interval_seconds）或 cron（schedule 表达式 5/6 字段）；
- 后台循环每秒检查到期项；到期 → 对每个活跃 agent ``inject`` 一条通知
  （busy 时排队到下一步，idle 时等下一次唤醒——与 TS 版 cron 语义一致）；
- 条目持久化到 ctx.storage（domain "schedule"，未挂载 storage 则仅内存）；
- 模型可经 ``schedule_register`` / ``schedule_list`` / ``schedule_remove`` 管理。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..ids import new_job_id
from ..kernel import Service
from ..tools import define_tool
from .cron import CronError, parse_cron

STORAGE_DOMAIN = "schedule"


class ScheduleService(Service):
    """定时任务服务（ctx.schedule）。"""

    provides = "schedule"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None

    def apply(self, ctx) -> None:
        ctx.set("schedule", self)
        self._restore()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._loop())

    # ---- 条目 ----

    def _restore(self) -> None:
        if self.ctx.has("storage"):
            saved = self.ctx.storage.domain(STORAGE_DOMAIN)
            for entry_id, entry in saved.items():
                entry.setdefault("last_fired", 0.0)
                self._entries[entry_id] = entry

    def _persist(self) -> None:
        if self.ctx.has("storage"):
            for entry_id, entry in self._entries.items():
                self.ctx.storage.put(STORAGE_DOMAIN, entry_id, dict(entry))

    def register(self, prompt: str, interval_seconds: Optional[float] = None,
                 schedule: Optional[str] = None) -> str:
        """
        注册定时条目（interval 或 cron 二选一），返回 id。

        :param prompt: 到期注入的通知内容。
        :param interval_seconds: 间隔秒数（interval 型条目）。
        :param schedule: cron 表达式 5/6 字段（cron 型条目）。
        :raises CronError: cron 表达式非法。
        :raises ValueError: 两者都缺或都给定。
        """
        if (interval_seconds is None) == (schedule is None):
            raise ValueError(
                "schedule entry needs exactly one of interval_seconds or schedule")
        entry_id = new_job_id()
        entry: Dict[str, Any] = {"id": entry_id, "prompt": prompt,
                                 "last_fired": time.time()}
        if interval_seconds is not None:
            entry["interval_seconds"] = float(interval_seconds)
        else:
            spec = parse_cron(schedule)
            entry["schedule"] = schedule
            entry["next_time"] = spec.next_after(
                datetime.now()).timestamp()
        self._entries[entry_id] = entry
        self._persist()
        return entry_id

    def remove(self, entry_id: str) -> bool:
        """移除条目。"""
        if entry_id not in self._entries:
            return False
        del self._entries[entry_id]
        if self.ctx.has("storage"):
            self.ctx.storage.delete(STORAGE_DOMAIN, entry_id)
        return True

    def list(self) -> List[Dict[str, Any]]:
        return [dict(e) for e in self._entries.values()]

    # ---- 后台循环 ----

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                now = time.time()
                for entry in list(self._entries.values()):
                    if self._due(entry, now):
                        entry["last_fired"] = now
                        self._fire(entry)
        except asyncio.CancelledError:
            raise

    def _due(self, entry: Dict[str, Any], now: float) -> bool:
        """判断条目是否到期（interval：间隔；cron：next_time 命中）。"""
        if "interval_seconds" in entry:
            return now - entry.get("last_fired", 0.0) >= entry["interval_seconds"]
        next_time = entry.get("next_time")
        if next_time is None or now < next_time:
            return False
        # 触发后计算下一次
        spec = parse_cron(entry["schedule"])
        entry["next_time"] = spec.next_after(
            datetime.fromtimestamp(max(now, next_time))).timestamp()
        return True

    def _fire(self, entry: Dict[str, Any]) -> None:
        """到期 → 向每个活跃 agent 注入通知。"""
        if not self.ctx.has("agents"):
            return
        message = {"content": f"[定时任务] {entry['prompt']}",
                   "source": {"kind": "cron", "schedule": entry["id"]}}
        for agent in self.ctx.agents.list():
            if not agent._disposed.is_set():
                agent.inject(message["content"], source=message["source"])

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._entries.clear()


def build_schedule_tools() -> List[Any]:
    """构造 schedule_* 工具族。"""

    def _schedule_of(run_ctx):
        agent = run_ctx.execution.agent
        ctx = agent.ctx if agent is not None else run_ctx.root_ctx
        if not ctx.has("schedule"):
            from ..errors import ToolError
            raise ToolError("schedule service not mounted", code="NO_SCHEDULE")
        return ctx.schedule

    @define_tool(
        name="schedule_register",
        description="注册定时任务（interval_seconds 与 schedule 二选一；"
                    "到期时向本 agent 注入通知）。",
        parameters={"prompt": {"type": "string", "required": True},
                    "interval_seconds": {"type": "number"},
                    "schedule": {"type": "string",
                                 "description": "cron 表达式（5/6 字段）"}},
        output={"type": "string"})
    async def schedule_register(args, run_ctx):
        try:
            entry_id = _schedule_of(run_ctx).register(
                args["prompt"], interval_seconds=args.get("interval_seconds"),
                schedule=args.get("schedule"))
        except (CronError, ValueError) as exc:
            from ..errors import ToolArgsError
            raise ToolArgsError(str(exc))
        return f"scheduled: {entry_id}"

    @define_tool(name="schedule_list", description="列出全部定时任务。",
                 parameters={}, output={"type": "array", "items": {"type": "object"}})
    async def schedule_list(args, run_ctx):
        return _schedule_of(run_ctx).list()

    @define_tool(name="schedule_remove", description="移除定时任务。",
                 parameters={"id": {"type": "string", "required": True}},
                 output={"type": "string"})
    async def schedule_remove(args, run_ctx):
        removed = _schedule_of(run_ctx).remove(args["id"])
        return f"removed {args['id']}" if removed else f"not found: {args['id']}"

    return [schedule_register, schedule_list, schedule_remove]


class ToolSchedulePlugin(Service):
    """注册 schedule_* 工具的插件。"""

    inject = ("tools", "schedule")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        for tool in build_schedule_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
