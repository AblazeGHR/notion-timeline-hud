"""快速新增的自然语言解析：从「明天 15:00 交作业」里拆出日期、时间和标题。

只覆盖高频写法，解析不出来的部分原样留在标题里，不猜。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_RE_TIME = re.compile(r"(上午|下午|晚上|中午|早上)?\s*(\d{1,2})\s*[:：]\s*(\d{2})")
_RE_TIME_CN = re.compile(r"(上午|下午|晚上|中午|早上)?\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[点时](半)?")
_RE_DATE_CN = re.compile(r"(今天|今日|明天|明日|后天|大后天)")
_RE_DATE_WEEK = re.compile(r"(?:周|星期|礼拜)\s*([一二三四五六日天])")
_RE_DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_RE_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def _cn_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if text.startswith("十") and len(text) == 2:
        return 10 + _CN_NUM.get(text[1], 0)
    if text.endswith("十") and len(text) == 2:
        return _CN_NUM.get(text[0], 0) * 10
    if "十" in text and len(text) == 3:
        return _CN_NUM.get(text[0], 0) * 10 + _CN_NUM.get(text[2], 0)
    return None


def _strip(text: str, match: re.Match[str]) -> str:
    return (text[: match.start()] + " " + text[match.end():]).strip()


def parse(text: str, now: datetime | None = None) -> tuple[str, str, str | None]:
    """返回 (标题, YYYY-MM-DD, HH:MM 或 None)。"""
    now = now or datetime.now()
    today = now.date()
    rest = text.strip()
    target: date | None = None
    clock: str | None = None

    match = _RE_DATE_ISO.search(rest)
    if match:
        try:
            target = date(int(match[1]), int(match[2]), int(match[3]))
            rest = _strip(rest, match)
        except ValueError:
            pass

    if target is None:
        match = _RE_DATE_MD.search(rest)
        if match:
            month, day = int(match[1]), int(match[2])
            try:
                guess = date(today.year, month, day)
                if guess < today - timedelta(days=30):
                    guess = date(today.year + 1, month, day)
                target = guess
                rest = _strip(rest, match)
            except ValueError:
                pass

    if target is None:
        match = _RE_DATE_CN.search(rest)
        if match:
            offset = {"今天": 0, "今日": 0, "明天": 1, "明日": 1,
                      "后天": 2, "大后天": 3}[match[1]]
            target = today + timedelta(days=offset)
            rest = _strip(rest, match)

    if target is None:
        match = _RE_DATE_WEEK.search(rest)
        if match:
            weekday = _WEEKDAYS[match[1]]
            delta = (weekday - today.weekday()) % 7
            target = today + timedelta(days=delta)
            rest = _strip(rest, match)

    match = _RE_TIME.search(rest)
    if match:
        meridiem, hour, minute = match[1], int(match[2]), int(match[3])
        if meridiem in ("下午", "晚上") and hour < 12:
            hour += 12
        if meridiem == "中午" and hour < 11:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            clock = f"{hour:02d}:{minute:02d}"
            rest = _strip(rest, match)
    else:
        match = _RE_TIME_CN.search(rest)
        if match:
            meridiem = match[1]
            hour = _cn_to_int(match[2])
            if hour is not None:
                if meridiem in ("下午", "晚上") and hour < 12:
                    hour += 12
                if meridiem == "中午" and hour < 11:
                    hour += 12
                minute = 30 if match[3] else 0
                if 0 <= hour <= 23:
                    clock = f"{hour:02d}:{minute:02d}"
                    rest = _strip(rest, match)

    title = re.sub(r"\s+", " ", rest).strip(" ,，。;；")
    if target is None:
        target = today
    return title, target.isoformat(), clock


if __name__ == "__main__":
    for sample in [
        "明天 15:00 交作业",
        "后天下午3点 开组会",
        "周五 复习离散数学",
        "10月30日 提交报告",
        "晚上8点半 跑步",
        "买菜",
    ]:
        print(f"{sample!r} -> {parse(sample)}")
