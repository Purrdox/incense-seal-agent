"""mantools：list_courses 过滤 + once 的 due 校验。"""
from __future__ import annotations

import datetime as dt
import json

from qqlife.agents.mantools import ToolExecutor
from qqlife.task_flow.task_store import TaskStore


async def test_list_courses_filters_done_and_once(tmp_path):
    st = TaskStore(root=tmp_path, owner="me")
    st.add_item("me", "数学", type_="regular", sched="MON 08:00")
    st.add_item("me", "旧课", type_="regular", sched="TUE 09:00", status="done")
    st.add_item("me", "交作业", type_="once", due="2026-10-18T20:00:00")

    rows = json.loads(await ToolExecutor(store=st).call("list_courses", {}))

    assert [r["name"] for r in rows] == ["数学"]


async def test_item_add_rejects_past_due(tmp_path):
    """once 的到期时刻已过 → 拒绝写入，避免"一存就响"。"""
    st = TaskStore(root=tmp_path, owner="me")
    ex = ToolExecutor(store=st)
    past = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="minutes")

    msg = await ex.call("item", {"action": "add", "content": "当天已过的提醒",
                                 "type": "once", "due": past})
    assert "未写入" in msg and "过去" in msg
    assert st.items == []


async def test_item_add_accepts_future_due(tmp_path):
    st = TaskStore(root=tmp_path, owner="me")
    ex = ToolExecutor(store=st)
    future = (dt.datetime.now() + dt.timedelta(hours=1)).isoformat(timespec="minutes")

    msg = await ex.call("item", {"action": "add", "content": "一小时后",
                                 "type": "once", "due": future})
    assert "已新增事项" in msg and len(st.items) == 1


async def test_item_add_allows_empty_due_but_rejects_bad_due(tmp_path):
    """空 due = 没有到期时刻的待办（永不触发），保持旧行为；写坏了的 due 才拒绝。"""
    st = TaskStore(root=tmp_path, owner="me")
    ex = ToolExecutor(store=st)

    msg = await ex.call("item", {"action": "add", "content": "买牙膏", "type": "once"})
    assert "已新增事项" in msg and len(st.items) == 1

    msg2 = await ex.call("item", {"action": "add", "content": "坏时间",
                                  "type": "once", "due": "明天"})
    assert "未写入" in msg2 and len(st.items) == 1


async def test_item_add_accepts_due_with_z(tmp_path):
    """Python 3.10 的 fromisoformat 不认结尾 Z：工具层要先归一化，否则未来时间也会被误拒。"""
    st = TaskStore(root=tmp_path, owner="me")
    ex = ToolExecutor(store=st)
    due = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    msg = await ex.call("item", {"action": "add", "content": "明天的提醒",
                                 "type": "once", "due": due})
    assert "已新增事项" in msg and len(st.items) == 1


async def test_item_update_rejects_past_due(tmp_path):
    st = TaskStore(root=tmp_path, owner="me")
    ex = ToolExecutor(store=st)
    future = (dt.datetime.now() + dt.timedelta(hours=2)).isoformat(timespec="minutes")
    await ex.call("item", {"action": "add", "content": "提醒",
                           "type": "once", "due": future})
    past = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="minutes")

    msg = await ex.call("item", {"action": "update", "id": st.items[0].id, "due": past})
    assert "未修改" in msg
    assert st.items[0].due == future
