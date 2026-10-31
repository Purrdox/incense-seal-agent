"""调度规则纯函数测试：_sched_hits / _sched_time / _hold_end_ts（不触网、不读写 data/）。"""
from __future__ import annotations

import datetime as dt

import pytest

from qqlife.scheduler.scheduler import Scheduler

MON = dt.datetime(2026, 10, 19)      # 周一
TUE = dt.datetime(2026, 10, 20)      # 周二
FRI = dt.datetime(2026, 10, 23)      # 周五
DAY_15 = dt.datetime(2026, 10, 15)   # 15 号（周四）
DAY_1 = dt.datetime(2026, 10, 1)     # 1 号（周四）


@pytest.mark.parametrize("sched, when, expect", [
    ("", MON, True),
    ("D", TUE, True),
    ("D 23:00", TUE, True),
    ("MON", MON, True),
    ("MON", TUE, False),
    ("TUE 13:40", TUE, True),
    ("TUE 13:40", MON, False),    # 旧 bug：把 13:40 里的 1/3/0 当周几
    ("M15", DAY_15, True),
    ("M15", TUE, False),
    ("M1", DAY_1, True),
    ("MON 08:00", DAY_15, False),  # MON 不能被当成「每月 MON 日」
    ("135", MON, True),
    ("135", TUE, False),
    ("2", TUE, True),
    ("2", MON, False),
    ("周五", FRI, True),
    ("周五", MON, False),
    ("五", FRI, True),
    ("周5", FRI, True),
    ("weekly", TUE, False),        # 未知格式保守不触发
])
def test_sched_hits(sched, when, expect):
    assert Scheduler._sched_hits(sched, when) is expect


@pytest.mark.parametrize("sched, expect", [
    ("TUE 13:40", "13:40"),
    ("D 23:00", "23:00"),
    ("MON", ""),
    ("", ""),
])
def test_sched_time(sched, expect):
    assert Scheduler._sched_time(sched) == expect


def test_hold_end_ts_normal_range():
    now = dt.datetime(2026, 10, 19, 13, 50)
    end = Scheduler._hold_end_ts(now, "13:40", "14:00")
    assert end == now.replace(hour=14, minute=0, second=0, microsecond=0).timestamp()
    assert Scheduler._hold_end_ts(now, "14:00", "15:00") is None


def test_hold_end_ts_cross_midnight():
    before_midnight = dt.datetime(2026, 10, 19, 23, 45)
    after_midnight = dt.datetime(2026, 10, 20, 0, 30)
    expect = dt.datetime(2026, 10, 20, 1, 0).timestamp()
    assert Scheduler._hold_end_ts(before_midnight, "23:30", "01:00") == expect
    assert Scheduler._hold_end_ts(after_midnight, "23:30", "01:00") == expect
    assert Scheduler._hold_end_ts(dt.datetime(2026, 10, 20, 2, 0), "23:30", "01:00") is None


def test_hold_end_ts_iso_range():
    """once 事项的 start/end 常写成 ISO 时刻（2026-10-31T09:00），旧实现按字符串比较必然判空。"""
    inside = dt.datetime(2026, 10, 31, 10, 0)
    expect = dt.datetime(2026, 10, 31, 12, 0).timestamp()
    assert Scheduler._hold_end_ts(inside, "2026-10-31T09:00", "2026-10-31T12:00") == expect
    assert Scheduler._hold_end_ts(dt.datetime(2026, 10, 31, 13, 0),
                                  "2026-10-31T09:00", "2026-10-31T12:00") is None
    assert Scheduler._hold_end_ts(dt.datetime(2026, 10, 30, 10, 0),
                                  "2026-10-31T09:00", "2026-10-31T12:00") is None


def test_hold_end_ts_iso_cross_midnight():
    after_midnight = dt.datetime(2026, 11, 1, 0, 30)
    expect = dt.datetime(2026, 11, 1, 2, 0).timestamp()
    assert Scheduler._hold_end_ts(after_midnight,
                                  "2026-10-31T22:00", "2026-11-01T02:00") == expect
    # 只给 HH:MM 的结束时刻：按开始那端的日期补全
    assert Scheduler._hold_end_ts(dt.datetime(2026, 10, 31, 10, 0),
                                  "2026-10-31T09:00", "12:00") == \
        dt.datetime(2026, 10, 31, 12, 0).timestamp()


def test_parse_point_normalizes_timezone():
    """带时区/带 Z 的 ISO 要折算成本地朴素时间，否则和 now 比较会抛 TypeError。"""
    d1 = Scheduler._parse_point("2026-10-31T09:00+08:00", dt.date(2026, 10, 31))
    assert d1 is not None and d1.tzinfo is None
    d2 = Scheduler._parse_point("2026-10-31T09:00Z", dt.date(2026, 10, 31))
    assert d2 is not None and d2.tzinfo is None
    # 判定值随本机时区，只要求不抛异常
    r = Scheduler._hold_end_ts(dt.datetime(2026, 10, 31, 10, 0),
                               "2026-10-31T09:00Z", "2026-10-31T12:00Z")
    assert r is None or isinstance(r, float)
