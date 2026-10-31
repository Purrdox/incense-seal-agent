"""SessionMemory：上下文注入序、时间戳、软压缩提醒、旧结构迁移。"""
from __future__ import annotations

import json
import re

from qqlife.session import SessionMemory


def test_prompt_context_order_and_timestamp(tmp_path):
    s = SessionMemory(tmp_path / "session.json")
    s.apply_fold("me", {"称呼": "阿盐"})
    s.add_new("me", "喜欢安静")
    s.add("me", "user", "在吗")
    s.add("me", "assistant", "在")

    rows = s.prompt_context("me")

    assert [r["role"] for r in rows] == ["system", "system", "user", "assistant"]
    assert "【长期记忆】" in rows[0]["content"]
    assert "称呼：阿盐" in rows[0]["content"]
    assert "【待固化记忆】" in rows[1]["content"]
    assert re.match(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] 在吗$", rows[2]["content"])
    assert rows[3]["content"] == "在"   # assistant 轮不附时间戳


def test_prompt_context_recent_window(tmp_path):
    s = SessionMemory(tmp_path / "session.json")
    for i in range(3):
        s.add("me", "user", f"m{i}")
    rows = s.prompt_context("me", recent=2)
    assert [r["content"].split("] ")[-1] for r in rows] == ["m1", "m2"]


def test_compact_hint_once_per_cycle(tmp_path):
    s = SessionMemory(tmp_path / "session.json")
    for i in range(3):
        s.add("me", "user", f"m{i}")
    assert s.take_compact_hint("me", 3) == 3
    assert s.take_compact_hint("me", 3) is None
    s.apply_fold("me", {})
    for i in range(3):
        s.add("me", "user", f"n{i}")
    assert s.take_compact_hint("me", 3) == 3


def test_legacy_summary_migrates_to_facts(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"summary": "旧摘要", "turns": []}, ensure_ascii=False),
                    encoding="utf-8")
    s = SessionMemory(path)
    assert s.facts("me") == {"既往对话摘要": "旧摘要"}


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{broken", encoding="utf-8")
    s = SessionMemory(path)
    assert s.facts("me") == {}
    assert s.all_turns("me") == []
    s.add("me", "user", "还能写")
    s.save()
    assert json.loads(path.read_text(encoding="utf-8"))["turns"][0]["content"] == "还能写"
