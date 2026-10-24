"""dates.shift 的断言测试：直接 python tests/test_dates.py 即可运行。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion_timeline.dates import shift  # noqa: E402
from notion_timeline.models import DateValue  # noqa: E402


def test_same_day_keeps_time_and_tz() -> None:
    dv = DateValue("2026-10-20", start_time="14:00", end_time="15:50", time_zone="UTC+8")
    out = shift(dv, 90)
    assert out.start == "2026-10-20", out
    assert out.start_time == "15:30", out
    assert out.end_time == "17:20", out
    assert out.end is None, out
    assert out.time_zone == "UTC+8", out


def test_cross_midnight_forward() -> None:
    dv = DateValue("2026-10-20", start_time="23:30", time_zone="Asia/Shanghai")
    out = shift(dv, 90)
    assert out.start == "2026-10-21", out
    assert out.start_time == "01:00", out
    assert out.time_zone == "Asia/Shanghai", out


def test_cross_midnight_backward() -> None:
    dv = DateValue("2026-10-01", start_time="00:30")
    out = shift(dv, -90)
    assert out.start == "2026-09-30", out
    assert out.start_time == "23:00", out


def test_date_only_under_one_day_stays() -> None:
    dv = DateValue("2026-10-20")
    out = shift(dv, 90)
    assert out.start == "2026-10-20", out
    assert out.start_time is None, out
    assert out.end is None, out


def test_date_only_whole_day() -> None:
    dv = DateValue("2026-10-20")
    assert shift(dv, 1440).start == "2026-10-21"
    assert shift(dv, -1440).start == "2026-10-19"
    assert shift(dv, 2 * 1440).start == "2026-10-22"


def test_zero_is_identity() -> None:
    dv = DateValue("2026-10-20", "2026-10-22", start_time="09:05", end_time="10:00", time_zone="UTC+8")
    out = shift(dv, 0)
    assert (out.start, out.end, out.start_time, out.end_time, out.time_zone) == (
        "2026-10-20",
        "2026-10-22",
        "09:05",
        "10:00",
        "UTC+8",
    ), out


def test_range_keeps_duration_across_days() -> None:
    dv = DateValue("2026-10-31", "2026-10-31", start_time="23:00", end_time="23:30", time_zone="UTC+8")
    out = shift(dv, 120)
    assert out.start == "2026-11-01", out
    assert out.start_time == "01:00", out
    assert out.end == "2026-11-01", out
    assert out.end_time == "01:30", out


def test_end_date_only_follows_day_shift() -> None:
    dv = DateValue("2026-10-20", "2026-10-21", start_time="23:30")
    out = shift(dv, 60)
    assert (out.start, out.start_time) == ("2026-10-21", "00:30"), out
    assert out.end == "2026-10-22", out


def test_input_not_mutated() -> None:
    dv = DateValue("2026-10-20", start_time="14:00")
    shift(dv, 90)
    assert dv.start == "2026-10-20" and dv.start_time == "14:00", dv


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
