"""机械提示词加载：全局基准、UID 覆盖与 BUILTIN 回退。"""
from __future__ import annotations

import json

import qqlife.mech_prompts as mech_prompts


def test_baseline_keys_match_builtin():
    baseline = mech_prompts.baseline_table()
    assert set(baseline) == set(mech_prompts.BUILTIN)


def test_baseline_contains_required_keys():
    baseline = mech_prompts.baseline_table()
    for key in ("searching", "unbound_guide", "probe_test", "compact_hint",
                "backlog_skipped", "delivery_failed", "data_corrupt", "reminder_label"):
        assert key in baseline
    assert ".create" in baseline["unbound_guide"]
    assert ".link ok" in baseline["probe_test"]
    assert "{title}" in baseline["reminder_label"]


def test_get_reads_baseline():
    assert "查询" in mech_prompts.get("searching")


def test_uid_override(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(json.dumps({"searching": "基准查询中"}), encoding="utf-8")
    monkeypatch.setattr(mech_prompts, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(mech_prompts, "_BASELINE_CACHE", None)

    assert mech_prompts.load_table(tmp_path)["searching"] == "基准查询中"

    home = tmp_path / "uid"
    home.mkdir()
    (home / "mech_prompts.json").write_text(
        json.dumps({"searching": "角色查询中"}), encoding="utf-8")
    assert mech_prompts.load_table(home)["searching"] == "角色查询中"


def test_missing_baseline_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(mech_prompts, "BASELINE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(mech_prompts, "_BASELINE_CACHE", None)
    assert mech_prompts.load_table(tmp_path)["searching"] == mech_prompts.BUILTIN["searching"]
