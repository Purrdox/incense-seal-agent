"""提示词契约：结构、空人设、关键标签与记忆上限。"""
from __future__ import annotations

from qqlife.agents.memory_service import FACTS_MAX, _parse_facts
from qqlife.agents.promptkit import render


def test_chat_prompt_contains_required_contracts():
    text = render("chat", PERSONA="测试角色")
    for token in ("<persona>", "测试角色", "<output>", "<title>", "<voice>",
                  "【当前时间】", "remember_user", "喝水提醒"):
        assert token in text


def test_chat_prompt_works_without_persona():
    text = render("chat", PERSONA="")
    assert "你是。" not in text
    assert "<persona>" in text


def test_care_prompt_is_internal_instruction():
    text = render("care_prompt")
    assert "系统提示" in text
    assert "<output></output>" in text


def test_memory_prompt_contract_and_cap():
    text = render("extract_user_memory")
    assert "40" in text
    assert "field: text" in text
    lines = [f"k{i}: v{i}" for i in range(FACTS_MAX + 5)]
    assert len(_parse_facts("\n".join(lines))) == FACTS_MAX


def test_sub_extract_prompt_contract():
    text = render("sub_extract")
    assert "2-4 句" in text
    assert "资料不足" in text


def test_mech_generation_prompt_contract():
    text = render("gen_mech_prompts")
    assert "JSON" in text
    assert "{title}" in text
    assert "占位符" in text
