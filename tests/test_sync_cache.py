"""sync_cache 的纯逻辑测试：合并 patch / 去抖 / 回滚。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sync_cache import WriteCache, merge_patch, refresh_skip_reason  # noqa: E402


def _task(sched: float = 14.0, title: str = "T") -> dict:
    return {"id": "t1", "title": title, "status": "todo", "sched": sched,
            "endSched": sched + 1, "allDaySched": False}


def test_merge_patch() -> None:
    base = {"dates": {"安排日期": {"start": "2026-11-01", "start_time": "14:00"}}, "title": "a"}
    extra = {"dates": {"安排日期": {"start_time": "15:00"}, "截止日期": {"start": "2026-11-02"}},
             "status": "doing"}
    out = merge_patch(base, extra)
    assert out["dates"]["安排日期"] == {"start_time": "15:00"}   # 属性级后者胜
    assert out["dates"]["截止日期"]["start"] == "2026-11-02"
    assert out["title"] == "a"
    assert out["status"] == "doing"
    assert base["dates"]["安排日期"]["start_time"] == "14:00"    # 不改入参


def test_coalesce_and_debounce() -> None:
    cache = WriteCache(delay=0.5)
    cache.ingest([_task(14.0)])
    p1 = {"dates": {"安排日期": {"start": "2026-11-01", "start_time": "14:00"}}}
    p2 = {"dates": {"安排日期": {"start": "2026-11-01", "start_time": "15:00"}}}
    cache.mutate("t1", _task(14.5), p1, now=100.0)
    cache.mutate("t1", _task(15.0), p2, now=100.2)
    assert cache.pending_ids() == ["t1"]
    assert cache.due(now=100.5) == []          # 距最后一次不足 500ms
    assert cache.due(now=100.8) == ["t1"]
    job = cache.begin("t1")
    assert job and job["gen"] == 2
    assert job["patch"]["dates"]["安排日期"]["start_time"] == "15:00"
    assert cache.get("t1")["sched"] == 15.0
    assert cache.succeed("t1", 2, _task(15.0)) is True
    assert cache.pending_ids() == []
    assert cache.get("t1")["sched"] == 15.0


def test_fail_rolls_back_last_good() -> None:
    cache = WriteCache(delay=0.5)
    cache.ingest([_task(14.0)])
    cache.mutate("t1", _task(15.0), {"title": "b"}, now=200.0)
    job = cache.begin("t1")
    assert job
    good = cache.fail("t1", job["gen"])
    assert good is not None and good["sched"] == 14.0
    assert cache.get("t1")["sched"] == 14.0
    assert cache.pending_ids() == []


def test_newer_change_during_flight_keeps_pending() -> None:
    cache = WriteCache(delay=0.5)
    cache.ingest([_task(14.0)])
    cache.mutate("t1", _task(15.0), {"title": "b"}, now=300.0)
    job = cache.begin("t1")
    assert job and job["gen"] == 1
    cache.mutate("t1", _task(16.0), {"title": "c"}, now=300.1)   # 请求飞行中又改
    assert cache.pending_ids() == ["t1"]
    assert cache.succeed("t1", 1) is False
    assert cache.pending_ids() == ["t1"]                          # 新修改还在
    assert cache.get("t1")["sched"] == 16.0
    job2 = cache.begin("t1")
    assert job2 and job2["gen"] == 2
    assert cache.fail("t1", 1) is None                            # 旧失败不影响新值
    assert cache.get("t1")["sched"] == 16.0


def test_ingest_keeps_pending_local_value() -> None:
    cache = WriteCache(delay=0.5)
    cache.ingest([_task(14.0)])
    cache.mutate("t1", _task(15.0, "local"), {"title": "local"}, now=400.0)
    merged = cache.ingest([_task(12.0, "server")])
    assert merged[0]["sched"] == 15.0
    assert merged[0]["title"] == "local"
    assert cache.get("t1")["sched"] == 15.0


def test_succeed_keeps_local_until_next_ingest() -> None:
    """写成功后服务端回包不同：不立刻改本地，留到下一轮刷新收口。"""
    cache = WriteCache(delay=0.8)
    cache.ingest([_task(14.0)])
    cache.mutate("t1", _task(15.0), {"title": "local"}, now=500.0)
    job = cache.begin("t1")
    assert job is not None
    assert cache.succeed("t1", job["gen"], _task(12.0, "server")) is True
    assert cache.pending_ids() == []
    assert cache.get("t1")["sched"] == 15.0          # 本地值继续生效
    merged = cache.ingest([_task(12.0, "server")])
    assert merged[0]["sched"] == 12.0                # 下一轮刷新才接受服务端
    assert cache.get("t1")["sched"] == 12.0


def test_debounce_800ms() -> None:
    cache = WriteCache(delay=0.8)
    cache.ingest([_task(14.0)])
    cache.mutate("t1", _task(14.5), {"title": "a"}, now=600.0)
    cache.mutate("t1", _task(15.0), {"title": "b"}, now=600.4)
    assert cache.due(now=601.1) == []                # 距最后一次不足 800ms
    assert cache.due(now=601.2) == ["t1"]            # 800ms 后合并成一次
    job = cache.begin("t1")
    assert job and job["patch"] == {"title": "b"}


def test_refresh_skip_reason() -> None:
    assert refresh_skip_reason(True, False, 99.0) == "busy"
    assert refresh_skip_reason(False, True, 99.0) == "pending"
    assert refresh_skip_reason(False, False, 1.99) == "idle"
    assert refresh_skip_reason(False, False, 2.0) is None


if __name__ == "__main__":
    test_merge_patch()
    test_coalesce_and_debounce()
    test_fail_rolls_back_last_good()
    test_newer_change_during_flight_keeps_pending()
    test_ingest_keeps_pending_local_value()
    test_succeed_keeps_local_until_next_ingest()
    test_debounce_800ms()
    test_refresh_skip_reason()
    print("sync_cache tests passed")
