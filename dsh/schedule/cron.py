"""
dsh.schedule.cron —— cron 表达式解析（5/6 字段）。

字段（5 字段）: minute(0-59) hour(0-23) day(1-31) month(1-12) weekday(0-6, 0=周日)
字段（6 字段）: second(0-59) minute hour day month weekday

语法: ``*``、数字、``a-b`` 范围、``*/n`` 步长、``a,b,c`` 列表。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set

_FIELD_NAMES = ("second", "minute", "hour", "day", "month", "weekday")
_RANGES = {  # 5 字段时的 (min, max)；6 字段时在前面加 second
    "minute": (0, 59), "hour": (0, 23), "day": (1, 31),
    "month": (1, 12), "weekday": (0, 6),
}


class CronError(ValueError):
    """cron 表达式非法。"""


@dataclass(frozen=True)
class _Field:
    """一个字段：允许值集合（已含步长展开）+ 可选 L（月末）标记。"""

    allowed: frozenset
    last_day: bool = False


@dataclass(frozen=True)
class CronSpec:
    """解析后的 cron 规格。"""

    expr: str
    fields: tuple          # 按 minute..weekday（或 second..weekday）顺序的 _Field
    field_names: tuple     # 与 fields 对齐的字段名（day 字段用于 L 语义）

    # ---- 匹配 ----

    def matches(self, dt: datetime) -> bool:
        """当前时间是否命中（weekday: 0=周日..6=周六；day 字段支持 L=月末）。"""
        values = (dt.second, dt.minute, dt.hour, dt.day, dt.month,
                  (dt.weekday() + 1) % 7)
        offset = 1 if len(self.fields) == 5 else 0
        for index, field in enumerate(self.fields):
            value = values[index + offset]
            if value in field.allowed:
                continue
            # L 语义：仅 day 字段支持「当月最后一天」
            if (field.last_day and self.field_names[index] == "day"
                    and value == calendar.monthrange(dt.year, dt.month)[1]):
                continue
            return False
        return True

    def next_after(self, dt: datetime) -> datetime:
        """
        计算 dt 之后（不含 dt 本身）的下一次触发时间。

        实现：从 dt+1 秒起逐秒匹配，最多扫描 400 天（任何非空表达式 400 天内
        必有命中；否则抛 CronError）。
        """
        candidate = dt.replace(microsecond=0) + timedelta(seconds=1)
        limit = candidate + timedelta(days=400)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(seconds=1)
        raise CronError(f"cron expression {self.expr!r} never fires")


def _parse_field(text: str, minimum: int, maximum: int,
                 allow_last: bool = False) -> _Field:
    """
    解析单个字段（* / 数字 / 范围 / 步长 / 列表 / L）。

    ``L`` 仅允许在 day 字段（allow_last=True），表示当月最后一天。
    """
    if not text:
        raise CronError("empty cron field")
    allowed: Set[int] = set()
    last_day = False
    for part in text.split(","):
        part = part.strip()
        if part.upper() == "L":
            if not allow_last:
                raise CronError(
                    f"'L' is only allowed in the day field: {part!r}")
            last_day = True
            continue
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"invalid step: {part!r}") from exc
            if step < 1:
                raise CronError(f"step must be >= 1: {part!r}")
        else:
            base = part
        if base == "*":
            low, high = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            try:
                low, high = int(left), int(right)
            except ValueError as exc:
                raise CronError(f"invalid range: {part!r}") from exc
        else:
            try:
                low = high = int(base)
            except ValueError as exc:
                raise CronError(f"invalid value: {part!r}") from exc
        if low < minimum or high > maximum or low > high:
            raise CronError(f"value out of range [{minimum},{maximum}]: {part!r}")
        allowed.update(range(low, high + 1, step))
    return _Field(frozenset(allowed), last_day)


def parse_cron(expr: str) -> CronSpec:
    """
    解析 5 或 6 字段 cron 表达式。

    :raises CronError: 字段数/取值非法。
    """
    parts = expr.strip().split()
    if len(parts) == 5:
        names = ("minute", "hour", "day", "month", "weekday")
    elif len(parts) == 6:
        names = ("second", "minute", "hour", "day", "month", "weekday")
    else:
        raise CronError(f"cron needs 5 or 6 fields, got {len(parts)}: {expr!r}")
    fields = []
    for name, text in zip(names, parts):
        if name == "second":
            minimum, maximum = 0, 59
        else:
            minimum, maximum = _RANGES[name]
        fields.append(_parse_field(text, minimum, maximum,
                                   allow_last=(name == "day")))
    return CronSpec(expr=expr, fields=tuple(fields), field_names=names)


def next_fire_time(expr: str, now: Optional[datetime] = None) -> datetime:
    """便捷入口：解析 + 求下一次触发时间。"""
    spec = parse_cron(expr)
    return spec.next_after(now or datetime.now())
