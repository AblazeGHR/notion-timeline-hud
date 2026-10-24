"""命令行入口：python -m notion_timeline.cli dump [--json] [--limit N] [--source api|cache]"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from typing import Sequence

from .models import Task


def _display_width(text: str) -> int:
    """终端里中文占两列，按显示宽度补空格才对得齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _clip(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _display_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _format_table(tasks: Sequence[Task]) -> str:
    widths = (24, 6, 12, 12, 8)
    header = "  ".join(
        _pad(title, width)
        for title, width in zip(("标题", "状态", "安排日期", "截止日期", "id"), widths)
    )
    lines = [header, "-" * _display_width(header)]
    for task in tasks:
        arranged = task.dates.get("安排日期")
        due = task.dates.get("截止日期")
        cells = (
            _clip(task.title, widths[0]),
            task.status.name if task.status else "",
            arranged.start if arranged else "",
            due.start if due else "",
            task.short_id[:8],
        )
        lines.append("  ".join(_pad(cell, w) for cell, w in zip(cells, widths)))
    return "\n".join(lines)


def _cmd_dump(args: argparse.Namespace) -> int:
    if args.source == "api":
        from .api_client import NotionAPIError, NotionClient

        try:
            with NotionClient() as client:
                tasks = client.query_tasks(
                    data_source_id=args.data_source_id,
                    database_id=args.database_id,
                    limit=args.limit,
                )
        except NotionAPIError as exc:
            print(f"API 读取失败：{exc}", file=sys.stderr)
            return 2
    else:
        from .cache_reader import CacheReadError, read_tasks

        try:
            tasks = read_tasks(collection_id=args.collection, db_path=args.db)
        except CacheReadError as exc:
            print(f"缓存读取失败：{exc}", file=sys.stderr)
            return 2
        if args.limit is not None:
            tasks = tasks[: args.limit]

    if args.json:
        print(json.dumps([task.to_dict() for task in tasks], ensure_ascii=False, indent=2))
    else:
        print(_format_table(tasks))
        print(f"\n共 {len(tasks)} 条（source={args.source}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notion_timeline", description="Notion 任务数据层 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump", help="列出任务（默认走本地缓存）")
    dump.add_argument("--json", action="store_true", help="输出 Task 的 JSON")
    dump.add_argument("--limit", type=int, default=None, help="最多输出 N 条")
    dump.add_argument(
        "--source",
        choices=("cache", "api"),
        default="cache",
        help="读取来源：cache=本地 SQLite 缓存（默认），api=Notion REST API",
    )
    dump.add_argument("--collection", default=None, help="collection_id（默认自动选行数最多的库）")
    dump.add_argument("--db", default=None, help="notion.db 路径（默认 %%APPDATA%%\\Notion\\notion.db）")
    dump.add_argument("--database-id", default=None, help="API 模式：Notion database id")
    dump.add_argument("--data-source-id", default=None, help="API 模式：data source id（跳过解析）")
    dump.set_defaults(func=_cmd_dump)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
