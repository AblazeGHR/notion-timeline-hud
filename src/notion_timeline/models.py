"""统一 Task 数据模型（对应 docs/SPEC.md §3）。

只做数据承载与序列化，不掺 I/O。cache_reader / api_client 各自把来源
转成这里的结构，对外形状保持完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NOTION_PAGE_URL_PREFIX = "https://www.notion.so/"

_TIME_KEYS = ("start_time", "end_time", "time_zone")


def notion_page_url(page_id: str) -> str:
    """页面 URL 规则：id 去掉短横线后拼在 notion.so 后面。"""
    return NOTION_PAGE_URL_PREFIX + page_id.replace("-", "")


def _split_datetime(value: str) -> tuple[str, str | None]:
    """把 "2026-10-20T15:50:00.000+08:00" 拆成 ("2026-10-20", "15:50")。"""
    if "T" not in value:
        return value, None
    date_part, _, rest = value.partition("T")
    time_part = rest[:5]
    return date_part, time_part


def _split_api_datetime(value: str, tz: str | None) -> tuple[str, str | None]:
    """拆 API 返回的时间；有时区时把 UTC 换算回本地时间。"""
    if "T" not in value:
        return value, None
    if tz:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                local = moment.astimezone(ZoneInfo(tz))
                return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return _split_datetime(value)


@dataclass(frozen=True)
class DateValue:
    """一个 date 属性值。start/end 为日期部分（YYYY-MM-DD），时间另存。"""

    start: str
    end: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    time_zone: str | None = None

    @classmethod
    def from_cache(cls, payload: Mapping[str, Any]) -> "DateValue | None":
        """本地缓存里的 {"start_date":..,"end_date":..,"time_zone":..} 结构。"""
        start = payload.get("start_date")
        if not start:
            return None
        return cls(
            start=str(start),
            end=payload.get("end_date") or None,
            start_time=payload.get("start_time") or None,
            end_time=payload.get("end_time") or None,
            time_zone=payload.get("time_zone") or None,
        )

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "DateValue | None":
        """API 里的 {"start":..,"end":..,"time_zone":..} 结构（可能是完整 ISO 时间）。

        Notion 的规则：带 time_zone 时，start/end 必须是 UTC，所以这里要按
        time_zone 转回本地时间，否则 06:30Z 会被当成 06:30 显示。
        """
        raw_start = payload.get("start")
        if not raw_start:
            return None
        tz = payload.get("time_zone") or None
        start, start_time = _split_api_datetime(str(raw_start), tz)
        end = payload.get("end")
        end_date, end_time = _split_api_datetime(str(end), tz) if end else (None, None)
        return cls(
            start=start,
            end=end_date,
            start_time=start_time,
            end_time=end_time,
            time_zone=tz,
        )

    def to_dict(self) -> dict[str, Any]:
        # SPEC §3 只约定 start/end；时间字段有值时才附带，避免改变既有契约。
        out: dict[str, Any] = {"start": self.start, "end": self.end}
        for key in _TIME_KEYS:
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class PropertyValue:
    """非 date 属性的镜像值。type 为 Notion 属性类型（select/relation/...）。"""

    type: str
    value: Any = None
    raw: Any = None  # 无法解析的类型原样留存

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "value": self.value}
        if self.raw is not None:
            out["raw"] = self.raw
        return out


@dataclass(frozen=True)
class StatusValue:
    name: str
    color: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "color": self.color}


@dataclass
class Task:
    id: str
    title: str
    created_time: str | None = None
    last_edited_time: str | None = None
    dates: dict[str, DateValue] = field(default_factory=dict)
    status: StatusValue | None = None
    properties: dict[str, PropertyValue] = field(default_factory=dict)
    source: str = "notion"

    @property
    def url(self) -> str:
        return notion_page_url(self.id)

    @property
    def short_id(self) -> str:
        return self.id.replace("-", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "created_time": self.created_time,
            "last_edited_time": self.last_edited_time,
            "dates": {name: dv.to_dict() for name, dv in self.dates.items()},
            "status": self.status.to_dict() if self.status else None,
            "properties": {name: pv.to_dict() for name, pv in self.properties.items()},
        }
