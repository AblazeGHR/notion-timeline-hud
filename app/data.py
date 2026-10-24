"""把 notion_timeline 的 Task 转成界面要的形状，并注入到 HTML 里。

界面约定（与 app/ui/index.html 对齐）：
  tasks:   [{id, title, status, tag, sched, due, allDaySched, allDayDue}]
           sched / due 是相对「今天 0 点」的小时数
  undated: [{title, status, tag}]
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from notion_timeline.api_client import NotionClient, load_dotenv  # noqa: E402
from notion_timeline.cache_reader import read_tasks  # noqa: E402
from notion_timeline.models import Task  # noqa: E402

DATA_SOURCE_ID = (
    os.environ.get("NOTION_DATA_SOURCE_ID", "").strip()
    or load_dotenv().get("NOTION_DATA_SOURCE_ID", "").strip()
    or "31c55e1d-4f6d-80a2-8f74-000b970d5404"
)

_STATUS = {
    "未开始": "todo",
    "进行中": "doing",
    "已完成": "done",
    "完成": "done",
}

TRACK_SCHED = "安排日期"
TRACK_DUE = "截止日期"


def _hours(date: str, clock: str | None, today0: datetime) -> float:
    text = f"{date}T{clock or '00:00'}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    return round((moment - today0).total_seconds() / 3600, 3)


def _tag(task: Task) -> str:
    for name in ("大方向", "学科"):
        value = task.properties.get(name)
        if value and isinstance(value.value, str) and value.value:
            return value.value
    return ""


def to_ui(tasks: list[Task]) -> dict[str, Any]:
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []

    for task in tasks:
        status = _STATUS.get(task.status.name, "") if task.status else ""
        tag = _tag(task)
        item: dict[str, Any] = {
            "id": task.id,
            "title": task.title,
            "status": status,
            "tag": tag,
        }
        sched = task.dates.get(TRACK_SCHED)
        due = task.dates.get(TRACK_DUE)
        if sched is not None:
            item["sched"] = _hours(sched.start, sched.start_time, today0)
            item["allDaySched"] = sched.start_time is None
        if due is not None:
            item["due"] = _hours(due.start, due.start_time, today0)
            item["allDayDue"] = due.start_time is None

        if sched is None and due is None:
            undated.append({"title": task.title, "status": status, "tag": tag})
        else:
            rows.append(item)

    return {"tasks": rows, "undated": undated}


def load() -> tuple[dict[str, Any], str, str | None]:
    """返回 (数据, 来源, 错误信息)。API 优先，失败回落到本地缓存。"""
    try:
        with NotionClient() as client:
            tasks = client.query_tasks(data_source_id=DATA_SOURCE_ID)
        return to_ui(tasks), "api", None
    except Exception as api_error:  # noqa: BLE001 - 任何失败都该能回落
        try:
            tasks = read_tasks(collection_id=DATA_SOURCE_ID)
            return to_ui(tasks), "cache", str(api_error)
        except Exception as cache_error:  # noqa: BLE001
            return {"tasks": [], "undated": []}, "none", f"{api_error} / {cache_error}"


if __name__ == "__main__":
    import json

    data, source, error = load()
    print("source:", source, "tasks:", len(data["tasks"]), "undated:", len(data["undated"]))
    if error:
        print("fallback reason:", error[:200])
    print(json.dumps(data["tasks"][:3], ensure_ascii=False, indent=2))
