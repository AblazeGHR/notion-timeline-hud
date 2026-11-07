"""写入合并缓存：本地先改、按 id 去抖合并、失败回滚。

与 UI 无关的纯数据结构，便于单测；main.py 只负责把写请求落成对 Notion 的
update_task_fields patch。
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass


def merge_patch(base: dict | None, extra: dict | None) -> dict:
    """把新 patch 合并进旧 patch：dates/properties 按属性名覆盖，其余字段后者胜。"""
    out = copy.deepcopy(base or {})
    for key, value in (extra or {}).items():
        if key in ("dates", "properties") and isinstance(value, dict):
            slot = out.setdefault(key, {})
            for name, item in value.items():
                slot[name] = copy.deepcopy(item)
        else:
            out[key] = copy.deepcopy(value)
    return out


def refresh_skip_reason(
    busy: bool, pending: bool, idle_seconds: float, idle_required: float = 2.0
) -> str | None:
    """定时刷新是否让位：交互中 / 有 pending 写 / 刚空闲不足 2s。"""
    if busy:
        return "busy"
    if pending:
        return "pending"
    if idle_seconds < idle_required:
        return "idle"
    return None


@dataclass
class PendingWrite:
    patch: dict
    ts: float
    gen: int
    last_good: dict


class WriteCache:
    """id -> 本地任务；id -> 合并后的 pending patch。"""

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self._tasks: dict[str, dict] = {}
        self._pending: dict[str, PendingWrite] = {}
        self._inflight: dict[str, int] = {}
        self._lock = threading.RLock()

    def ingest(self, tasks: list[dict]) -> list[dict]:
        """服务端整包数据进来：pending 的 id 保留本地值，其余接受服务端值。"""
        with self._lock:
            for task in tasks:
                tid = task.get("id")
                if not tid or tid in self._pending:
                    continue
                self._tasks[tid] = copy.deepcopy(task)
            return [self._merged(task) for task in tasks]

    def _merged(self, task: dict) -> dict:
        tid = task.get("id")
        if tid and tid in self._pending and tid in self._tasks:
            out = copy.deepcopy(task)
            out.update(copy.deepcopy(self._tasks[tid]))
            return out
        return task

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return copy.deepcopy(task) if task else None

    def mutate(self, task_id: str, task: dict, patch: dict, now: float | None = None) -> dict:
        """记录一次本地修改；首次修改时保存 last_good 供回滚。"""
        stamp = time.time() if now is None else now
        with self._lock:
            pending = self._pending.get(task_id)
            if pending is None:
                base = self._tasks.get(task_id)
                self._pending[task_id] = PendingWrite(
                    patch=copy.deepcopy(patch),
                    ts=stamp,
                    gen=1,
                    last_good=copy.deepcopy(base) if base else copy.deepcopy(task),
                )
            else:
                pending.patch = merge_patch(pending.patch, patch)
                pending.ts = stamp
                pending.gen += 1
            self._tasks[task_id] = copy.deepcopy(task)
            return copy.deepcopy(self._tasks[task_id])

    def due(self, now: float | None = None) -> list[str]:
        stamp = time.time() if now is None else now
        with self._lock:
            return [
                tid for tid, p in self._pending.items()
                if stamp - p.ts >= self.delay and tid not in self._inflight
            ]

    def begin(self, task_id: str) -> dict | None:
        with self._lock:
            pending = self._pending.get(task_id)
            if not pending or task_id in self._inflight:
                return None
            self._inflight[task_id] = pending.gen
            return {"id": task_id, "patch": copy.deepcopy(pending.patch), "gen": pending.gen}

    def succeed(self, task_id: str, gen: int, server_task: dict | None = None) -> bool:
        with self._lock:
            if self._inflight.get(task_id) == gen:
                self._inflight.pop(task_id, None)
            pending = self._pending.get(task_id)
            if pending and pending.gen == gen:
                self._pending.pop(task_id, None)
                # 本地镜像继续用乐观值：即使服务端回包不同，也不当场改 UI，
                # 差异留到下一轮空闲刷新（ingest）再收口。
                return True
            return False

    def fail(self, task_id: str, gen: int) -> dict | None:
        """回滚到 last_good；期间又有新修改则不回滚（交给新一轮写）。"""
        with self._lock:
            if self._inflight.get(task_id) == gen:
                self._inflight.pop(task_id, None)
            pending = self._pending.get(task_id)
            if not pending or pending.gen != gen:
                return None
            self._pending.pop(task_id, None)
            if pending.last_good:
                self._tasks[task_id] = copy.deepcopy(pending.last_good)
            return copy.deepcopy(pending.last_good)

    def set_local(self, task_id: str, task: dict) -> None:
        """已知服务端已改（例如右键换状态）：只更新本地镜像，不产生写队列。"""
        with self._lock:
            self._tasks[task_id] = copy.deepcopy(task)

    def forget(self, task_id: str) -> None:
        """任务被删除：本地缓存和待写队列一并丢掉。"""
        with self._lock:
            self._tasks.pop(task_id, None)
            self._pending.pop(task_id, None)
            self._inflight.pop(task_id, None)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)
