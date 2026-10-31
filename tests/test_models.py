"""Item 数据模型：到期 epoch、终态语义、dict 往返。"""
from __future__ import annotations

import datetime as dt

from qqlife.task_flow.models import Item


def test_due_epoch():
    it = Item(id="i1", due="2026-10-18T09:00:00")
    assert it.due_epoch == dt.datetime(2026, 10, 18, 9, 0, 0).timestamp()


def test_due_epoch_invalid_or_empty():
    assert Item(id="i1", due="not-a-date").due_epoch == 0.0
    assert Item(id="i2").due_epoch == 0.0


def test_due_epoch_accepts_trailing_z():
    """Python 3.10 的 fromisoformat 不认 `Z`；调度到期计算必须先归一化。"""
    it = Item(id="i1", due="2026-10-18T01:00:00Z")
    assert it.due_epoch == dt.datetime(2026, 10, 18, 1, 0, tzinfo=dt.timezone.utc).timestamp()


def test_is_done():
    assert Item(id="i1", status="active").is_done is False
    assert Item(id="i1", status="done").is_done is True
    assert Item(id="i1", status="cancelled").is_done is True


def test_dict_roundtrip():
    it = Item(id="i3", content="晨会", owner="me", type="regular", sched="MON 09:00",
              start="09:00", end="09:30", focus=True, note="带电脑",
              notes=[{"id": "n1", "content": "x"}], last_fired="2026-10-19")
    again = Item.from_dict(it.to_dict())
    assert again == it
