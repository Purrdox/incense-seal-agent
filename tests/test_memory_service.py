"""记忆压缩（fold）：输出预算、截断判定与"失败不清空"语义。"""
from __future__ import annotations

import pytest

import qqlife.agents.memory_service as ms
from qqlife.session import SessionMemory


class FakeLLM:
    def __init__(self, out: str = "", finish: str = "stop", exc: Exception | None = None):
        self.out = out
        self.finish = finish
        self.exc = exc
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    async def chat(self, messages, *, max_tokens: int = 1024, usage: dict | None = None, **kw):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, **kw})
        if usage is not None:
            usage.update({"hit": 0, "miss": 120, "completion": 64, "finish": self.finish})
        if self.exc:
            raise self.exc
        return self.out


class FakeHub:
    def __init__(self, llm):
        self.llm = llm

    def for_kind(self, kind: str):
        return self.llm if kind == "extract" else None


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.usage_rec, "record", lambda *a, **k: None)   # 不写真实 data 目录
    s = SessionMemory(tmp_path / "session.json")
    s.add("me", "user", "我每天 23:30 收工洗澡")
    s.add("me", "assistant", "记下了")
    s.add_new("me", "作息偏晚")
    return s


async def test_fold_success_applies_facts(session):
    hub = FakeHub(FakeLLM(out="称呼：小明\n作息：23:30 收工，8:00 起床"))
    r = await ms.force_compact(session, "me", hub)

    assert r["compressed"] is True and r["before"] == 2 and r["after"] == 0
    assert session.facts("me") == {"称呼": "小明", "作息": "23:30 收工，8:00 起床"}
    assert session.new_entries("me") == []


async def test_fold_uses_large_output_budget_and_disables_thinking(session):
    """思考模式下 reasoning 与正文共享 max_tokens，会把额度吃光 → 合并不落盘。"""
    hub = FakeHub(FakeLLM(out="作息：早睡"))
    await ms.force_compact(session, "me", hub)
    assert hub.llm.calls[0]["max_tokens"] == ms.FOLD_MAX_TOKENS
    assert hub.llm.calls[0]["thinking"] is False


async def test_truncated_output_keeps_turns(session):
    """finish_reason=length：输出不完整，绝不能拿半截 facts 覆盖长期记忆。"""
    hub = FakeHub(FakeLLM(out="称呼：小明\n作息：23:30", finish="length"))
    r = await ms.force_compact(session, "me", hub)

    assert r["compressed"] is False
    assert session.turns_count() == 2 and session.new_entries("me") == ["作息偏晚"]
    assert session.facts("me") == {}


async def test_empty_output_keeps_turns(session):
    hub = FakeHub(FakeLLM(out="", finish="stop"))
    assert (await ms.force_compact(session, "me", hub))["compressed"] is False
    assert session.turns_count() == 2


async def test_unparsable_output_keeps_turns(session):
    hub = FakeHub(FakeLLM(out="好的，我都记住了。"))
    assert (await ms.force_compact(session, "me", hub))["compressed"] is False
    assert session.turns_count() == 2


async def test_model_exception_keeps_turns(session):
    hub = FakeHub(FakeLLM(exc=RuntimeError("budget exceeded")))
    assert (await ms.force_compact(session, "me", hub))["compressed"] is False
    assert session.turns_count() == 2 and session.facts("me") == {}
