"""日期平移：拖拽改期用。

规则：
- 有时间（start_time）时按分钟平移，日期跟着进位/退位；
- 只有日期时按天平移（分钟不足一天则原地不动）；
- time_zone 原样保留，end / end_time 一起平移，相对间隔不变。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .models import DateValue


def _parse_clock(text: str) -> tuple[int, int]:
    parts = str(text).split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时间：{text}")
    return hour, minute


def _format_clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def shift(dv: DateValue, minutes: int) -> DateValue:
    """把 DateValue 平移 minutes 分钟，返回新对象（原对象不动）。"""
    delta = timedelta(minutes=int(minutes))
    start_day = date.fromisoformat(dv.start)

    if dv.start_time:
        hour, minute = _parse_clock(dv.start_time)
        moved = datetime.combine(start_day, time(hour, minute)) + delta
        new_start = moved.date().isoformat()
        new_start_time: str | None = _format_clock(moved.hour, moved.minute)
        day_shift = (moved.date() - start_day).days
    else:
        moved_day = start_day + delta  # date + timedelta 会自动丢弃不足一天的部分
        new_start = moved_day.isoformat()
        new_start_time = None
        day_shift = (moved_day - start_day).days

    new_end: str | None = dv.end
    new_end_time: str | None = dv.end_time
    if dv.end or dv.end_time:
        # 只有 end_time 没有 end_date 时，视为与 start 同一天。
        end_day = date.fromisoformat(dv.end) if dv.end else start_day
        if dv.end_time:
            end_hour, end_minute = _parse_clock(dv.end_time)
            moved_end = datetime.combine(end_day, time(end_hour, end_minute)) + delta
            new_end_time = _format_clock(moved_end.hour, moved_end.minute)
            if dv.end or moved_end.date().isoformat() != new_start:
                new_end = moved_end.date().isoformat()
        elif dv.end:
            new_end = (end_day + timedelta(days=day_shift)).isoformat()
            new_end_time = None

    return DateValue(
        start=new_start,
        end=new_end,
        start_time=new_start_time,
        end_time=new_end_time,
        time_zone=dv.time_zone,
    )
