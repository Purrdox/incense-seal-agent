"""Scheduler 批次 A 回归：两阶段提交、退避重试、拟稿复用、非阻塞派发。

用假 event_bus 替代真实 LLM / 通道，不触网；每个用例用 tmp_path 隔离数据。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pytest

import qqlife.scheduler.scheduler as sched_mod
from qqlife.agent_core.types import NotifyTask
from qqlife.scheduler.occupied_state import OccupiedState
from qqlife.scheduler.proactive_gate import ProactiveGate
from qqlife.scheduler.scheduler import Scheduler
from qqlife.task_flow.task_store import TaskStore


class FakeBus:
    """可配置返回值的 event_bus 替身；result 也可以是 async 函数。"""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[NotifyTask] = []

    async def emit(self, event, data):
        self.calls.append(data)
        if callable(self.result):
            return await self.result(event, data)
        return self.result


@pytest.fixture
def make_scheduler(tmp_path, monkeypatch):
    def _make(bus: FakeBus) -> Scheduler:
        monkeypatch.setattr(sched_mod, "event_bus", bus)
        store = TaskStore(root=tmp_path, owner="me")
        occ = OccupiedState()
        gate = ProactiveGate(occ, object())
        return Scheduler(store, occ, gate, SimpleNamespace(proactive_per_min=0.0))
    return _make


def _past(seconds: int = 5) -> str:
    return (dt.datetime.now() - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


async def _scan(s: Scheduler) -> None:
    """跑一轮扫描并等待所有后台投递结束。"""
    await s._check_due_items()
    tasks = list(s._deliver_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def test_once_marks_done_only_after_success(make_scheduler):
    bus = FakeBus(True)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "交作业", type_="once", due=_past())

    await _scan(s)

    it = s.store.items[0]
    assert it.status == "done" and it.done_at
    assert iid not in s._pending and iid not in s._inflight
    assert s._attempts == {} and s._retry_after == {}
    assert bus.calls[0].content == "交作业"


async def test_delivery_failure_keeps_item_active(make_scheduler):
    bus = FakeBus(False)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "交作业", type_="once", due=_past())

    await _scan(s)

    it = s.store.items[0]
    assert it.status == "active"
    assert iid in s._pending
    assert s._attempts[iid] == 1
    assert s._retry_after[iid] > time.time()


async def test_retry_reuses_draft(make_scheduler):
    bus = FakeBus(False)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "交作业", type_="once", due=_past())

    await _scan(s)
    s._pending[iid].draft = "已经拟好的文案"   # 模拟 app 层缓存拟稿
    bus.result = True
    s._retry_after[iid] = 0.0
    await _scan(s)

    assert bus.calls[-1].draft == "已经拟好的文案"
    assert s.store.items[0].status == "done"
    assert iid not in s._pending


async def test_gives_up_after_max_attempts(make_scheduler):
    bus = FakeBus(False)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "交作业", type_="once", due=_past())

    for _ in range(Scheduler._MAX_ATTEMPTS):
        s._retry_after[iid] = 0.0
        await _scan(s)

    assert len(bus.calls) == Scheduler._MAX_ATTEMPTS
    assert s.store.items[0].status == "done"   # 超限落盘跳过，避免无限烧 token
    assert iid not in s._pending and s._attempts == {}


async def test_regular_last_fired_only_on_success(make_scheduler):
    bus = FakeBus(False)
    s = make_scheduler(bus)
    s.store.add_item("me", "晨间提醒", type_="regular")

    await _scan(s)
    assert s.store.items[0].last_fired == ""
    assert s.store.items[0].status == "active"

    bus.result = True
    s._retry_after[s.store.items[0].id] = 0.0
    await _scan(s)
    assert s.store.items[0].last_fired == dt.date.today().isoformat()


async def test_focus_and_done_items_are_skipped(make_scheduler):
    bus = FakeBus(True)
    s = make_scheduler(bus)
    s.store.add_item("me", "勿扰事项", type_="once", due=_past(), focus=True)
    s.store.add_item("me", "已完成", type_="once", due=_past(), status="done")

    await _scan(s)

    assert bus.calls == []
    assert s._pending == {}


async def test_scan_does_not_wait_for_slow_delivery(make_scheduler):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(event, data):
        started.set()
        await release.wait()
        return True

    s = make_scheduler(FakeBus(slow))
    iid = s.store.add_item("me", "慢提醒", type_="once", due=_past())

    await s._check_due_items()      # 不应被 LLM/网络阻塞
    await asyncio.sleep(0)          # 让后台投递跑到 emit
    assert started.is_set()
    assert iid in s._inflight
    assert s.store.items[0].status == "active"   # 发送完成前不落盘

    release.set()
    await asyncio.gather(*list(s._deliver_tasks))
    assert s.store.items[0].status == "done"


async def test_pending_retry_ignores_today_schedule(make_scheduler):
    bus = FakeBus(True)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "昨日提醒", type_="regular", sched="NOPE")
    task = NotifyTask(kind="oneoff", content="昨日提醒", target="me")
    s._pending[iid] = task
    s._fire_day[iid] = "2026-10-17"
    s._retry_after[iid] = 0.0

    await _scan(s)

    assert len(bus.calls) == 1 and bus.calls[0] is task
    assert s.store.items[0].last_fired == "2026-10-17"   # 按首次到期日补记


async def test_pending_is_cleared_when_item_done(make_scheduler):
    bus = FakeBus(True)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "提醒", type_="regular", sched="NOPE")
    s._pending[iid] = NotifyTask(kind="oneoff", content="提醒", target="me")
    s._retry_after[iid] = 0.0
    s.store.items[0].status = "done"

    await _scan(s)

    assert iid not in s._pending
    assert bus.calls == []


async def test_backlog_skips_only_outside_window(make_scheduler, tmp_path):
    s = make_scheduler(FakeBus(True))
    now = dt.datetime.now()
    old_due = (now - dt.timedelta(hours=25)).isoformat(timespec="seconds")
    recent_due = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    s.store.add_item("me", "旧提醒", type_="once", due=old_due)
    s.store.add_item("me", "新提醒", type_="once", due=recent_due)

    skipped: list[int] = []

    async def on_backlog(n):
        skipped.append(n)

    s.on_backlog = on_backlog
    await s._apply_backlog_policy()

    assert [i.status for i in s.store.items] == ["done", "active"]
    assert skipped == [1]
    assert TaskStore(root=tmp_path, owner="me").items[0].status == "done"   # 已落盘


async def test_backlog_notice_only_when_skipped(make_scheduler):
    s = make_scheduler(FakeBus(True))
    s.store.add_item("me", "一小时前", type_="once",
                     due=(dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds"))
    skipped: list[int] = []

    async def on_backlog(n):
        skipped.append(n)

    s.on_backlog = on_backlog
    await s._apply_backlog_policy()
    assert skipped == []


async def test_recent_overdue_is_still_replayed(make_scheduler):
    """重启后窗口内的过期项仍补发——前提是它本来就在到点之前建档（created 早于 due）。"""
    bus = FakeBus(True)
    s = make_scheduler(bus)
    due = dt.datetime.now() - dt.timedelta(hours=1)
    s.store.add_item("me", "一小时前", type_="once", due=due.isoformat(timespec="seconds"))
    s.store.items[0].created = (due - dt.timedelta(minutes=1)).isoformat(timespec="seconds")

    await s._apply_backlog_policy()
    await _scan(s)

    assert bus.calls and s.store.items[0].status == "done"


async def test_new_regular_item_with_passed_time_does_not_fire(make_scheduler):
    """新建的常规提醒：今天的触发时刻在建档前已过 → 今天不补响（回归：18:12 建 08:00 立即触发）。"""
    bus = FakeBus(True)
    s = make_scheduler(bus)
    now = dt.datetime.now()
    past = now - dt.timedelta(minutes=5)
    if past.date() != now.date():      # 跨午夜（00:00~00:05）无法构造"今天已过的时刻"
        pytest.skip("刚过午夜，构造不出今天已过的时刻")
    iid = s.store.add_item("me", "起床吃早饭", type_="regular", sched="D",
                           start=past.strftime("%H:%M"))

    await _scan(s)

    assert bus.calls == [] and iid not in s._pending
    assert s.store.items[0].last_fired == ""


async def test_regular_item_created_before_due_still_fires(make_scheduler):
    """建档时刻早于今天的触发时刻（进程晚起等场景）→ 仍按迟到补触发。"""
    bus = FakeBus(True)
    s = make_scheduler(bus)
    now = dt.datetime.now()
    past = now - dt.timedelta(minutes=5)
    if past.date() != now.date():
        pytest.skip("刚过午夜，构造不出今天已过的时刻")
    s.store.add_item("me", "起床吃早饭", type_="regular", sched="D",
                     start=past.strftime("%H:%M"))
    s.store.items[0].created = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")

    await _scan(s)

    assert len(bus.calls) == 1
    assert s.store.items[0].last_fired == dt.date.today().isoformat()


async def test_once_created_after_due_is_skipped(make_scheduler):
    """新建一次性事项的 due 已过（建档晚于到点）→ 不补触发（工具层会拒绝，这里兜底旧数据）。"""
    bus = FakeBus(True)
    s = make_scheduler(bus)
    due = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    iid = s.store.add_item("me", "当天已过的提醒", type_="once", due=due)

    await _scan(s)

    assert bus.calls == [] and iid not in s._pending
    assert s.store.items[0].status == "active"


async def test_legacy_once_without_created_still_fires(make_scheduler):
    """旧数据没有 created：保持原行为（到期即补发），不要误杀。"""
    bus = FakeBus(True)
    s = make_scheduler(bus)
    due = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    s.store.add_item("me", "旧提醒", type_="once", due=due)
    s.store.items[0].created = ""

    await _scan(s)

    assert len(bus.calls) == 1 and s.store.items[0].status == "done"


async def test_alert_after_giving_up(make_scheduler):
    bus = FakeBus(False)
    s = make_scheduler(bus)
    iid = s.store.add_item("me", "交作业", type_="once", due=_past())
    alerts: list[str] = []

    async def on_alert(content):
        alerts.append(content)

    s.on_alert = on_alert
    for _ in range(Scheduler._MAX_ATTEMPTS):
        s._retry_after[iid] = 0.0
        await _scan(s)

    assert alerts == ["交作业"]
