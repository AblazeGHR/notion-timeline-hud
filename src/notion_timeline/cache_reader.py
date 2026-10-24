"""只读本地 Notion 缓存（%APPDATA%\\Notion\\notion.db）→ Task 列表。

缓存用 journal_mode=delete，桌面端运行时直接开会撞写锁，所以先把库（含 -wal/-shm）
复制到临时目录再以只读方式打开；读完删临时目录。绝不写源库。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from .models import DateValue, PropertyValue, StatusValue, Task

DEFAULT_DB_PATH = Path(os.environ.get("APPDATA", "")) / "Notion" / "notion.db"

# 「状态」是任务状态；「筛选占位的」这类同类型属性只是筛选占位，不当状态用。
STATUS_PROPERTY_NAME = "状态"

_COPY_ATTEMPTS = 3
_COPY_RETRY_DELAY = 0.15


class CacheReadError(RuntimeError):
    """缓存不可读（找不到文件、被独占、表结构不符等）。"""


class _PropSchema(NamedTuple):
    name: str
    type: str
    colors: dict[str, str]  # select/status 选项值 → 颜色


# ---------------------------------------------------------------- 快照


@contextmanager
def _snapshot(db_path: Path) -> Iterator[Path]:
    """复制缓存到临时目录，yield 副本路径；退出时清理。"""
    if not db_path.exists():
        raise CacheReadError(f"找不到 Notion 本地缓存：{db_path}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="notion_timeline_"))
    target = tmp_dir / db_path.name
    try:
        last_error: Exception | None = None
        for attempt in range(_COPY_ATTEMPTS):
            try:
                shutil.copy2(db_path, target)
                break
            except (PermissionError, OSError) as exc:  # 桌面端同步瞬间可能独占
                last_error = exc
                if attempt + 1 == _COPY_ATTEMPTS:
                    raise CacheReadError(
                        f"复制本地缓存失败（Notion 可能正在写入）：{db_path}：{exc}"
                    ) from exc
                time.sleep(_COPY_RETRY_DELAY)
        else:  # pragma: no cover - 循环必然 break 或 raise
            raise CacheReadError(str(last_error))

        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                try:
                    shutil.copy2(sidecar, target.with_name(target.name + suffix))
                except OSError:
                    pass
        yield target
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@contextmanager
def _open_ro(path: Path) -> Iterator[sqlite3.Cursor]:
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        yield con.cursor()
    except sqlite3.DatabaseError as exc:
        raise CacheReadError(f"打开缓存数据库失败：{exc}") from exc
    finally:
        if con is not None:
            con.close()


# ---------------------------------------------------------------- 结构解析


def _json(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _load_schema(cur: sqlite3.Cursor, collection_id: str) -> dict[str, _PropSchema]:
    row = cur.execute("select schema from collection where id = ?", (collection_id,)).fetchone()
    if row is None:
        raise CacheReadError(f"缓存里没有 collection {collection_id}")
    schema = _json(row["schema"], {})
    out: dict[str, _PropSchema] = {}
    for short_id, entry in schema.items():
        if not isinstance(entry, dict):
            continue
        colors = {
            str(opt.get("value")): str(opt.get("color", "default"))
            for opt in entry.get("options") or []
            if isinstance(opt, dict) and opt.get("value") is not None
        }
        out[short_id] = _PropSchema(
            name=str(entry.get("name") or short_id),
            type=str(entry.get("type") or "unknown"),
            colors=colors,
        )
    return out


def _resolve_collection(cur: sqlite3.Cursor, collection_id: str | None) -> str:
    if collection_id:
        return collection_id.strip()
    env_id = os.environ.get("NOTION_COLLECTION_ID", "").strip()
    if env_id:
        return env_id
    rows = cur.execute("select id from collection where alive = 1").fetchall()
    best: tuple[int, str] | None = None
    for row in rows:
        cid = row["id"]
        count = cur.execute(
            "select count(*) from block where parent_table = 'collection'"
            " and parent_id = ? and type = 'page' and alive = 1",
            (cid,),
        ).fetchone()[0]
        if best is None or count > best[0]:
            best = (count, cid)
    if best is None:
        raise CacheReadError("缓存里没有任何 collection")
    return best[1]


def _ms_to_iso(value: Any) -> str | None:
    """block.created_time / last_edited_time 是 epoch 毫秒。"""
    if value is None:
        return None
    try:
        ts = float(value) / 1000.0
    except (TypeError, ValueError):
        return str(value)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- 值解析


def _segments_text(value: Any) -> str:
    """富文本/select：[["文字",[["h","b"]]], ["第二段"]] → "文字第二段"。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for seg in value:
        if isinstance(seg, str):
            parts.append(seg)
        elif isinstance(seg, list) and seg:
            head = seg[0]
            if isinstance(head, str):
                parts.append(head)
            elif isinstance(head, dict) and isinstance(head.get("text"), str):
                parts.append(head["text"])
    return "".join(parts)


def _find_date_payload(node: Any) -> dict[str, Any] | None:
    """日期值嵌套很深（[["‣", [["d", {...}]]]]），递归找带 start_date 的 dict。"""
    if isinstance(node, dict):
        if "start_date" in node:
            return node
        for child in node.values():
            found = _find_date_payload(child)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_date_payload(child)
            if found is not None:
                return found
    return None


def _ref_ids(value: Any, kind: str) -> list[str]:
    """relation 取 ["p", page_id, space_id]，people 取 ["u", user_id]。"""
    ids: list[str] = []
    if not isinstance(value, list):
        return ids
    for seg in value:
        if not (isinstance(seg, list) and len(seg) > 1 and isinstance(seg[1], list)):
            continue
        for item in seg[1]:
            if (
                isinstance(item, list)
                and len(item) > 1
                and item[0] == kind
                and isinstance(item[1], str)
            ):
                ids.append(item[1])
    return ids


def _as_number(text: str) -> int | float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _file_items(value: Any) -> list[str]:
    """files 属性的形态在缓存里不常见，尽量取出 url/名称，取不到就交给 raw。"""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node:
                found.append(node)
        elif isinstance(node, dict):
            for key in ("url", "src", "name"):
                if isinstance(node.get(key), str) and node[key]:
                    found.append(node[key])
                    return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _parse_value(ptype: str, value: Any) -> PropertyValue:
    if ptype in ("title", "text", "rich_text"):
        return PropertyValue(ptype if ptype != "text" else "text", _segments_text(value))
    if ptype in ("select", "status"):
        text = _segments_text(value)
        return PropertyValue(ptype, text or None)
    if ptype == "multi_select":
        raw_text = _segments_text(value)
        items = [part.strip() for part in raw_text.split(",") if part.strip()]
        return PropertyValue(ptype, items)
    if ptype == "number":
        return PropertyValue(ptype, _as_number(_segments_text(value)))
    if ptype in ("url", "email", "phone_number"):
        return PropertyValue(ptype, _segments_text(value) or None)
    if ptype == "checkbox":
        text = _segments_text(value).strip().lower()
        return PropertyValue(ptype, text in ("yes", "true", "1", "checked"))
    if ptype == "relation":
        return PropertyValue(ptype, _ref_ids(value, "p"))
    if ptype in ("person", "people"):
        return PropertyValue(ptype, _ref_ids(value, "u"))
    if ptype == "files":
        items = _file_items(value)
        return PropertyValue(ptype, items) if items else PropertyValue(ptype, None, raw=value)
    if ptype in ("created_time", "last_edited_time"):
        payload = _find_date_payload(value)
        if payload is not None and payload.get("start_date"):
            return PropertyValue(ptype, str(payload["start_date"]))
        return PropertyValue(ptype, _segments_text(value) or None)
    return PropertyValue(ptype, None, raw=value)


# ---------------------------------------------------------------- 入口


def read_tasks(
    collection_id: str | None = None,
    db_path: str | Path | None = None,
) -> list[Task]:
    """读缓存，返回 Task 列表。

    collection_id 为空时依次取环境变量 NOTION_COLLECTION_ID、行数最多的 collection。
    """
    source = Path(db_path) if db_path else DEFAULT_DB_PATH
    with _snapshot(source) as snapshot:
        with _open_ro(snapshot) as cur:
            cid = _resolve_collection(cur, collection_id)
            schema = _load_schema(cur, cid)
            rows = cur.execute(
                "select id, properties, created_time, last_edited_time from block"
                " where parent_table = 'collection' and parent_id = ?"
                " and type = 'page' and alive = 1"
                " order by created_time, id",
                (cid,),
            ).fetchall()

    tasks: list[Task] = []
    for row in rows:
        props = _json(row["properties"], {})
        dates: dict[str, DateValue] = {}
        properties: dict[str, PropertyValue] = {}
        status: StatusValue | None = None
        title = ""
        status_candidates: list[tuple[str, StatusValue]] = []

        for short_id, raw_value in props.items():
            meta = schema.get(short_id)
            name = meta.name if meta else short_id
            ptype = meta.type if meta else "unknown"

            if ptype == "title":
                title = _segments_text(raw_value)
                continue
            if ptype == "date":
                payload = _find_date_payload(raw_value)
                parsed = DateValue.from_cache(payload) if payload else None
                if parsed is not None:
                    dates[name] = parsed
                continue
            if ptype == "status":
                text = _segments_text(raw_value)
                if text:
                    color = meta.colors.get(text, "default") if meta else "default"
                    status_candidates.append((name, StatusValue(text, color)))
                continue

            properties[name] = _parse_value(ptype, raw_value)

        # 状态优先取「状态」属性，其次取第一个有值的 status 属性。
        for name, candidate in status_candidates:
            if name == STATUS_PROPERTY_NAME:
                status = candidate
                break
        if status is None and status_candidates:
            status = status_candidates[0][1]
        for name, candidate in status_candidates:
            if candidate is not status:
                properties[name] = PropertyValue("status", candidate.name)

        tasks.append(
            Task(
                id=row["id"],
                title=title,
                created_time=_ms_to_iso(row["created_time"]),
                last_edited_time=_ms_to_iso(row["last_edited_time"]),
                dates=dates,
                status=status,
                properties=properties,
            )
        )
    return tasks
