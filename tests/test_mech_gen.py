"""机械提示词生成：JSON 解析、逐键校验、回退与写入。"""
from __future__ import annotations

import json

import pytest

from qqlife import mech_prompts
from qqlife.mech_gen import (generate_and_save, normalize, parse_json)


def test_parse_json_with_fence():
    raw = "```json\n{\"a\": 1}\n```"
    assert parse_json(raw) == {"a": 1}


def test_parse_json_with_wrapper():
    raw = '{"mech_prompts": {"a": "b"}}'
    assert parse_json(raw) == {"a": "b"}


def test_normalize_accepts_baseline():
    baseline = mech_prompts.baseline_table()
    merged, stats = normalize(dict(baseline), baseline)
    assert merged == baseline
    assert stats["used"] == len(baseline)
    assert stats["fallback"] == 0


def test_normalize_falls_back_on_missing_key():
    baseline = mech_prompts.baseline_table()
    raw = dict(baseline)
    raw.pop("searching")
    merged, stats = normalize(raw, baseline)
    assert merged["searching"] == baseline["searching"]
    assert stats["fallback"] == 1


def test_normalize_falls_back_on_bad_placeholder():
    baseline = mech_prompts.baseline_table()
    raw = dict(baseline)
    raw["compact_hint"] = "没有占位符"
    merged, stats = normalize(raw, baseline)
    assert merged["compact_hint"] == baseline["compact_hint"]
    assert stats["fallback"] == 1


def test_normalize_falls_back_on_unknown_placeholder():
    baseline = mech_prompts.baseline_table()
    raw = dict(baseline)
    raw["searching"] = "{oops}"
    merged, stats = normalize(raw, baseline)
    assert merged["searching"] == baseline["searching"]
    assert stats["fallback"] == 1


def test_normalize_falls_back_on_too_long():
    baseline = mech_prompts.baseline_table()
    raw = dict(baseline)
    raw["searching"] = "长" * 201
    merged, stats = normalize(raw, baseline)
    assert merged["searching"] == baseline["searching"]
    assert stats["fallback"] == 1


def test_normalize_drops_extra_keys():
    baseline = mech_prompts.baseline_table()
    raw = dict(baseline)
    raw["extra"] = "x"
    _merged, stats = normalize(raw, baseline)
    assert stats["dropped"] == ["extra"]


async def test_generate_and_save(tmp_path):
    baseline = mech_prompts.baseline_table()

    async def fake_llm(system: str, user: str) -> str:
        assert "<persona>" in user and "<baseline>" in user
        return json.dumps(baseline, ensure_ascii=False)

    merged, stats, backup = await generate_and_save(tmp_path, "测试角色", fake_llm)
    assert merged == baseline
    assert stats["used"] == len(baseline)
    assert backup == ""
    assert (tmp_path / "mech_prompts.json").exists()


async def test_generate_and_save_requires_persona(tmp_path):
    async def fake_llm(system: str, user: str) -> str:
        return "{}"

    with pytest.raises(ValueError):
        await generate_and_save(tmp_path, "", fake_llm)


def test_save_table_backs_up(tmp_path):
    mech_prompts.save_table(tmp_path, {"searching": "第一版"})
    backup = mech_prompts.save_table(tmp_path, {"searching": "第二版"})
    assert backup and ".bak-" in backup
    assert json.loads((tmp_path / "mech_prompts.json").read_text(encoding="utf-8"))["searching"] == "第二版"
