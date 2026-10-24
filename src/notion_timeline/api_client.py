"""Notion REST API 客户端（写入唯一通道 + 读取兜底）。

Notion-Version 用 2025-09-03：查询走 data_source，database 只是 data_source 的容器。
token 从环境变量 NOTION_TOKEN 或项目根 .env 读取；缺失直接抛异常，不静默降级。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import DateValue, PropertyValue, StatusValue, Task

NOTION_API_BASE = "https://api.notion.com"
NOTION_VERSION = "2025-09-03"

STATUS_PROPERTY_NAME = "状态"
DEFAULT_TITLE_PROPERTY = "名称"

_ENV_TOKEN = "NOTION_TOKEN"
_ENV_DATABASE = "NOTION_DATABASE_ID"
_ENV_DATA_SOURCE = "NOTION_DATA_SOURCE_ID"

_TIMEOUT = 30.0
_PAGE_SIZE = 100


class NotionAPIError(RuntimeError):
    """API 调用失败（非 2xx 或网络错误）。"""


class MissingTokenError(NotionAPIError):
    """没有配置 NOTION_TOKEN。"""


# ---------------------------------------------------------------- .env


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE、# 注释、可选引号、可选 export 前缀。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, raw = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        else:
            raw = raw.split(" #", 1)[0].strip()
        if key:
            values[key] = raw
    return values


def load_dotenv() -> dict[str, str]:
    """项目根 .env 优先，其次当前目录 .env。"""
    merged: dict[str, str] = {}
    for candidate in (Path.cwd() / ".env", _project_root() / ".env"):
        if candidate.is_file():
            merged.update(_parse_env_file(candidate))
    return merged


def _windows_user_env(name: str) -> str:
    """读用户级环境变量（注册表）。

    给“设置环境变量之前就已经启动的进程”兜底：os.environ 是进程启动时快照，
    不会自动刷新，但注册表里已经是新值。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip()
    except OSError:
        return ""


def resolve_token() -> str:
    token = os.environ.get(_ENV_TOKEN, "").strip()
    if token:
        return token
    token = load_dotenv().get(_ENV_TOKEN, "").strip()
    if token:
        return token
    token = _windows_user_env(_ENV_TOKEN)
    if token:
        return token
    raise MissingTokenError(
        f"缺少 {_ENV_TOKEN}：请设置环境变量，或在 {_project_root() / '.env'} 中配置（参考 .env.example）。"
    )


# ---------------------------------------------------------------- 富文本


def _api_rich_text(items: Iterable[Mapping[str, Any]] | None) -> str:
    parts: list[str] = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        parts.append(str(text))
    return "".join(parts)


def _rich_text_payload(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [{"type": "text", "text": {"content": text}}]


# ---------------------------------------------------------------- 读：page → Task


def _task_from_page(page: Mapping[str, Any]) -> Task:
    dates: dict[str, DateValue] = {}
    properties: dict[str, PropertyValue] = {}
    status_candidates: list[tuple[str, StatusValue]] = []
    title = ""

    for name, prop in (page.get("properties") or {}).items():
        ptype = str(prop.get("type") or "unknown")
        if ptype == "title":
            title = _api_rich_text(prop.get("title"))
        elif ptype == "date":
            payload = prop.get("date")
            parsed = DateValue.from_api(payload) if isinstance(payload, Mapping) else None
            if parsed is not None:
                dates[name] = parsed
        elif ptype == "status":
            payload = prop.get("status")
            if isinstance(payload, Mapping) and payload.get("name"):
                status_candidates.append(
                    (name, StatusValue(str(payload["name"]), str(payload.get("color", "default"))))
                )
        else:
            properties[name] = _property_from_api(ptype, prop)

    status = next((c for n, c in status_candidates if n == STATUS_PROPERTY_NAME), None)
    if status is None and status_candidates:
        status = status_candidates[0][1]
    for name, candidate in status_candidates:
        if candidate is not status:
            properties[name] = PropertyValue("status", candidate.name)

    return Task(
        id=str(page.get("id", "")),
        title=title,
        created_time=page.get("created_time"),
        last_edited_time=page.get("last_edited_time"),
        dates=dates,
        status=status,
        properties=properties,
    )


def _property_from_api(ptype: str, prop: Mapping[str, Any]) -> PropertyValue:
    if ptype == "rich_text":
        return PropertyValue(ptype, _api_rich_text(prop.get("rich_text")))
    if ptype == "select":
        payload = prop.get("select")
        return PropertyValue(ptype, str(payload["name"]) if payload else None)
    if ptype == "multi_select":
        return PropertyValue(
            ptype, [str(o["name"]) for o in prop.get("multi_select") or [] if o.get("name")]
        )
    if ptype == "number":
        return PropertyValue(ptype, prop.get("number"))
    if ptype in ("url", "email", "phone_number"):
        return PropertyValue(ptype, prop.get(ptype))
    if ptype == "checkbox":
        return PropertyValue(ptype, bool(prop.get("checkbox")))
    if ptype == "relation":
        return PropertyValue(ptype, [str(r["id"]) for r in prop.get("relation") or [] if r.get("id")])
    if ptype in ("people", "person"):
        return PropertyValue(ptype, [str(p["id"]) for p in prop.get("people") or [] if p.get("id")])
    if ptype == "files":
        names: list[str] = []
        for item in prop.get("files") or []:
            if item.get("name"):
                names.append(str(item["name"]))
        return PropertyValue(ptype, names)
    if ptype in ("created_time", "last_edited_time", "unique_id", "formula", "rollup"):
        return PropertyValue(ptype, prop.get(ptype), raw=prop)
    return PropertyValue(ptype, None, raw=prop)


# ---------------------------------------------------------------- 写：值 → payload


def _coerce(prop: PropertyValue | Mapping[str, Any]) -> tuple[str, Any]:
    if isinstance(prop, PropertyValue):
        return prop.type, prop.value
    return str(prop.get("type") or "unknown"), prop.get("value")


def _iso_datetime(date_part: str, time_part: str | None, tz: str | None) -> str:
    """把（日期, 时分）拼成 Notion 接受的 ISO 时间。

    实测（2026-10-24）：Notion 不会用 time_zone 做换算，反而会把 start 当成
    本地墙钟时间再附上工作区时区偏移，导致 UTC 写法整体偏移。可靠做法是
    **只发本地墙钟时间 + 偏移，不发 time_zone**。
    """
    if not time_part:
        return date_part
    clock = time_part if len(time_part) >= 5 else f"{time_part}:00"
    try:
        moment = datetime.fromisoformat(f"{date_part}T{clock}")
    except ValueError:
        return date_part
    if tz:
        try:
            moment = moment.replace(tzinfo=ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return moment.isoformat(timespec="milliseconds")


def _date_value_payload(value: DateValue | Mapping[str, Any]) -> dict[str, Any]:
    data = value.to_dict() if isinstance(value, DateValue) else dict(value)
    if not data.get("start"):
        raise ValueError("缺少 start")
    payload: dict[str, Any] = {
        "start": _iso_datetime(str(data["start"]), data.get("start_time"), data.get("time_zone"))
    }
    if data.get("end"):
        payload["end"] = _iso_datetime(
            str(data["end"]), data.get("end_time"), data.get("time_zone")
        )
    # 不写 time_zone：Notion 会忽略它并把 start 当墙钟时间，见 _iso_datetime 注释。
    return {"date": payload}


def _property_payload(prop: PropertyValue | Mapping[str, Any]) -> dict[str, Any]:
    ptype, value = _coerce(prop)
    if ptype in ("title", "rich_text"):
        key = ptype
        return {key: _rich_text_payload("" if value is None else str(value))}
    if ptype == "date":
        if value is None:
            return {"date": None}
        return _date_value_payload(value)
    if ptype in ("select", "status"):
        return {ptype: {"name": str(value)} if value else None}
    if ptype == "multi_select":
        items = [value] if isinstance(value, str) else list(value or [])
        return {"multi_select": [{"name": str(v)} for v in items]}
    if ptype == "relation":
        ids = [value] if isinstance(value, str) else list(value or [])
        return {"relation": [{"id": str(i)} for i in ids]}
    if ptype == "people":
        ids = [value] if isinstance(value, str) else list(value or [])
        return {"people": [{"id": str(i)} for i in ids]}
    if ptype == "number":
        return {"number": None if value is None else float(value) if "." in str(value) else int(value)}
    if ptype in ("url", "email", "phone_number"):
        return {ptype: None if value is None else str(value)}
    if ptype == "checkbox":
        return {"checkbox": bool(value)}
    if ptype == "files":
        urls = [value] if isinstance(value, str) else list(value or [])
        return {"files": [{"name": str(u), "external": {"url": str(u)}} for u in urls]}
    raise NotionAPIError(f"不支持写入的属性类型：{ptype}")


def _date_payload(name: str, value: DateValue | Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _date_value_payload(value)
    except ValueError as exc:
        raise ValueError(f"日期属性「{name}」{exc}") from exc


# ---------------------------------------------------------------- 客户端


class NotionClient:
    def __init__(self, token: str | None = None, *, timeout: float = _TIMEOUT) -> None:
        self._token = (token or resolve_token()).strip()
        if not self._token:
            raise MissingTokenError(f"{_ENV_TOKEN} 为空")
        self._client = httpx.Client(
            base_url=NOTION_API_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        self._ds_cache: dict[str, str] = {}

    # -- 生命周期

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 底层请求

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NotionAPIError(f"{method} {path} 网络错误：{exc}") from exc
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("message", detail)
            except ValueError:
                pass
            raise NotionAPIError(f"{method} {path} 失败（HTTP {response.status_code}）：{detail}")
        if not response.content:
            return {}
        return response.json()

    # -- data source

    def _get_optional(self, path: str) -> dict[str, Any] | None:
        """GET，404 时返回 None（用于探测 id 类型），其他错误照常抛。"""
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise NotionAPIError(f"GET {path} 网络错误：{exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("message", detail)
            except ValueError:
                pass
            raise NotionAPIError(f"GET {path} 失败（HTTP {response.status_code}）：{detail}")
        return response.json()

    def resolve_data_source(self, identifier: str) -> str:
        """接受 data source id 或 database id 两种写法。

        本地缓存里的 collection.id 就是 data source id；直接用 /v1/databases/{id}
        查会 404，所以先按 data source 试，失败再按 database 解析。
        """
        ident = identifier.strip()
        cached = self._ds_cache.get(ident)
        if cached:
            return cached
        if self._get_optional(f"/v1/data_sources/{ident}") is not None:
            self._ds_cache[ident] = ident
            return ident
        data = self._request("GET", f"/v1/databases/{ident}")
        sources = data.get("data_sources") or []
        if isinstance(sources, list) and sources and isinstance(sources[0], Mapping):
            resolved = str(sources[0].get("id") or ident)
        else:
            resolved = ident
        self._ds_cache[ident] = resolved
        return resolved

    def _data_source_from_env(self) -> str:
        env = load_dotenv()
        data_source = os.environ.get(_ENV_DATA_SOURCE, "").strip() or env.get(
            _ENV_DATA_SOURCE, ""
        ).strip()
        if data_source:
            return self.resolve_data_source(data_source)
        database_id = os.environ.get(_ENV_DATABASE, "").strip() or env.get(
            _ENV_DATABASE, ""
        ).strip()
        if not database_id:
            raise NotionAPIError(
                f"缺少 {_ENV_DATA_SOURCE} / {_ENV_DATABASE}：请在 .env 或环境变量里配置。"
            )
        return self.resolve_data_source(database_id)

    # -- 读

    def query_tasks(
        self,
        data_source_id: str | None = None,
        *,
        database_id: str | None = None,
        page_size: int = _PAGE_SIZE,
        filter: Mapping[str, Any] | None = None,  # noqa: A002 - 对齐 API 字段名
        sorts: Sequence[Mapping[str, Any]] | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        """POST /v1/data_sources/{dsid}/query，自动翻页，产出与缓存读取同形状的 Task。"""
        if data_source_id:
            dsid = data_source_id
        elif database_id:
            dsid = self.resolve_data_source(database_id)
        else:
            dsid = self._data_source_from_env()

        tasks: list[Task] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": max(1, min(page_size, 100))}
            if cursor:
                body["start_cursor"] = cursor
            if filter:
                body["filter"] = dict(filter)
            if sorts:
                body["sorts"] = [dict(s) for s in sorts]
            data = self._request("POST", f"/v1/data_sources/{dsid}/query", json=body)
            for page in data.get("results") or []:
                tasks.append(_task_from_page(page))
                if limit is not None and len(tasks) >= limit:
                    return tasks
            if not data.get("has_more"):
                return tasks
            cursor = data.get("next_cursor")
            if not cursor:
                return tasks

    def get_block_children(
        self,
        block_id: str,
        page_size: int = 100,
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1/blocks/{id}/children，单次最多 100 个。"""
        params: dict[str, Any] = {"page_size": max(1, min(page_size, 100))}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._request("GET", f"/v1/blocks/{block_id}/children", params=params)

    def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        """GET /v1/data_sources/{id}：拿属性定义（选项名随库不同，写之前要核对）。"""
        return self._request("GET", f"/v1/data_sources/{data_source_id}")

    def get_task(self, page_id: str) -> Task:
        """GET /v1/pages/{id}：改期前先读当前日期值，避免拿 UI 里的旧数据平移。"""
        return _task_from_page(self._request("GET", f"/v1/pages/{page_id}"))

    # -- 写

    def create_task(
        self,
        title: str,
        dates: Mapping[str, DateValue | Mapping[str, Any]] | None = None,
        properties: Mapping[str, PropertyValue | Mapping[str, Any]] | None = None,
        *,
        data_source_id: str | None = None,
        database_id: str | None = None,
        title_property: str = DEFAULT_TITLE_PROPERTY,
    ) -> Task:
        """POST /v1/pages，parent 用 data_source_id。"""
        dsid = (
            data_source_id
            or (self.resolve_data_source(database_id) if database_id else self._data_source_from_env())
        )
        payload: dict[str, Any] = {
            title_property: {"title": _rich_text_payload(title)},
        }
        for name, value in (dates or {}).items():
            payload[name] = _date_payload(name, value)
        for name, value in (properties or {}).items():
            payload[name] = _property_payload(value)
        data = self._request(
            "POST",
            "/v1/pages",
            json={"parent": {"type": "data_source_id", "data_source_id": dsid}, "properties": payload},
        )
        return _task_from_page(data)

    def update_task(
        self,
        page_id: str,
        *,
        title: str | None = None,
        dates: Mapping[str, DateValue | Mapping[str, Any]] | None = None,
        properties: Mapping[str, PropertyValue | Mapping[str, Any]] | None = None,
        title_property: str = DEFAULT_TITLE_PROPERTY,
    ) -> Task:
        """PATCH /v1/pages/{id}，只提交传入的字段。"""
        payload: dict[str, Any] = {}
        if title is not None:
            payload[title_property] = {"title": _rich_text_payload(title)}
        for name, value in (dates or {}).items():
            payload[name] = _date_payload(name, value)
        for name, value in (properties or {}).items():
            payload[name] = _property_payload(value)
        if not payload:
            raise ValueError("update_task 至少要改一个字段")
        data = self._request("PATCH", f"/v1/pages/{page_id}", json={"properties": payload})
        return _task_from_page(data)

    def delete_task(self, page_id: str) -> dict[str, Any]:
        """DELETE /v1/blocks/{id}。

        注意：Notion API 没有永久删除。这个调用只是把页面移入回收站（trash），
        30 天内仍可在 Notion 里恢复；UI 上的「删除」就是这个语义。
        """
        return self._request("DELETE", f"/v1/blocks/{page_id}")
