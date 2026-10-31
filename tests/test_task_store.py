"""TaskStore：坏文件备份兜底、非法数据过滤、next_id 续计、持久化。"""
from __future__ import annotations

import json

from qqlife.task_flow.task_store import TaskStore


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_corrupt_items_backed_up_and_empty(tmp_path):
    bad = tmp_path / "items.json"
    bad.write_text("{broken", encoding="utf-8")

    st = TaskStore(root=tmp_path, owner="me")

    assert st.items == []
    assert not bad.exists()   # 坏文件被改名，不能被后续写入覆盖
    assert st.corrupt_files and st.corrupt_files[0][0] == "items.json"
    backups = list(tmp_path.glob("items.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"

    iid = st.add_item("me", "新提醒")
    assert iid == "i1"
    assert _read(tmp_path / "items.json")["items"][0]["content"] == "新提醒"


def test_top_level_not_dict_backed_up(tmp_path):
    (tmp_path / "items.json").write_text("[]", encoding="utf-8")
    st = TaskStore(root=tmp_path, owner="me")
    assert st.items == []
    assert st.corrupt_files and st.corrupt_files[0][0] == "items.json"
    assert len(list(tmp_path.glob("items.json.corrupt-*"))) == 1


def test_invalid_entries_filtered_and_next_id_derived(tmp_path):
    (tmp_path / "items.json").write_text(json.dumps({
        "next_id": "oops",
        "items": [{"id": "i7", "content": "留下"}, "坏条目", 3],
    }, ensure_ascii=False), encoding="utf-8")

    st = TaskStore(root=tmp_path, owner="me")

    assert [i.id for i in st.items] == ["i7"]
    assert st.add_item("me", "下一个") == "i8"


def test_non_positive_next_id_falls_back(tmp_path):
    (tmp_path / "items.json").write_text(json.dumps({
        "next_id": 0,
        "items": [{"id": "i2", "content": "旧条目"}],
    }, ensure_ascii=False), encoding="utf-8")

    st = TaskStore(root=tmp_path, owner="me")
    assert st.add_item("me", "续计") == "i3"


def test_invalid_ledger_filtered(tmp_path):
    (tmp_path / "expenses.json").write_text(json.dumps({
        "next_id": "bad",
        "ledger": [{"id": "e1", "amount": 1}, "坏数据"],
    }, ensure_ascii=False), encoding="utf-8")

    st = TaskStore(root=tmp_path, owner="me")
    assert [e["id"] for e in st.expenses] == ["e1"]
    st.add_expense(2.5, "food", "午饭")   # next_id 非法时只要求不崩溃
    assert len(st.expenses) == 2


def test_persistence_roundtrip(tmp_path):
    st = TaskStore(root=tmp_path, owner="me")
    iid = st.add_item("me", "交作业", type_="once", due="2026-10-18T20:00:00")
    st.add_expense(12.5, "food", "晚饭")

    again = TaskStore(root=tmp_path, owner="me")

    assert [(i.id, i.content, i.due) for i in again.items] == [(iid, "交作业", "2026-10-18T20:00:00")]
    assert again.expenses[0]["amount"] == 12.5
