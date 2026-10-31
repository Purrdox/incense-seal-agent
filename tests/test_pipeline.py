"""pipeline：长文拆条与消息聚合（从 app.py 拆出的纯逻辑）。"""
from __future__ import annotations

import asyncio

from qqlife.pipeline import (MsgAggregator, chunks, derive_title, extract_title,
                             format_reminder)


def test_extract_title():
    assert extract_title("<title>【喝水提醒】</title><output>该喝水了。</output>") == "喝水提醒"


def test_extract_title_missing():
    assert extract_title("<output>该喝水了。</output>") == ""


def test_derive_title_appends_reminder():
    assert derive_title("喝水") == "喝水提醒"
    assert derive_title("提醒我交作业") == "交作业提醒"


def test_derive_title_does_not_double_reminder():
    assert derive_title("喝水提醒") == "喝水提醒"
    assert derive_title("") == "提醒"


def test_format_reminder():
    assert format_reminder("喝水提醒", "该喝水了。") == "【喝水提醒】该喝水了。"


def test_format_reminder_strips_title_from_body():
    body = "<title>喝水提醒</title>该喝水了。"
    assert format_reminder("喝水提醒", body) == "【喝水提醒】该喝水了。"


def test_format_reminder_no_double_prefix():
    body = "【喝水提醒】该喝水了。"
    assert format_reminder("喝水提醒", body) == body


def test_format_reminder_empty_title():
    assert format_reminder("", "该喝水了。") == "该喝水了。"


def test_format_reminder_custom_label():
    assert format_reminder("喝水提醒", "该喝水了。", label="[ {title} ]") == "[ 喝水提醒 ]该喝水了。"


def test_chunks_hard_split():
    assert chunks("abcdef", 4) == ["abcd", "ef"]


def test_chunks_prefers_sentence_boundary():
    assert chunks("12345。678", 8) == ["12345。", "678"]


def test_chunks_keeps_emoji_intact():
    assert chunks("a😀b", 1) == ["a", "😀", "b"]


async def test_aggregator_flushes_immediately_when_disabled():
    batches: list[tuple[str, str]] = []

    async def on_batch(key, text):
        batches.append((key, text))

    agg = MsgAggregator(0, on_batch)
    await agg.put("feishu:u1", "第一句")
    await agg.put("feishu:u1", "第二句")
    assert batches == [("feishu:u1", "第一句"), ("feishu:u1", "第二句")]


async def test_aggregator_sentence_end_flushes_whole_bucket():
    batches: list[tuple[str, str]] = []

    async def on_batch(key, text):
        batches.append((key, text))

    agg = MsgAggregator(3, on_batch)
    await agg.put("feishu:u1", "还没说完")
    await agg.put("feishu:u1", "说完了。")
    assert batches == [("feishu:u1", "还没说完\n说完了。")]
    assert agg._timers == {}


async def test_aggregator_ignores_blank():
    batches: list[tuple[str, str]] = []

    async def on_batch(key, text):
        batches.append((key, text))

    agg = MsgAggregator(0, on_batch)
    await agg.put("feishu:u1", "   ")
    assert batches == []


async def test_aggregator_waiting_window():
    batches: list[tuple[str, str]] = []

    async def on_batch(key, text):
        batches.append((key, text))

    agg = MsgAggregator(0.2, on_batch)
    await agg.put("feishu:u1", "一")
    await asyncio.sleep(0.08)
    await agg.put("feishu:u1", "二")   # 重置计时
    await asyncio.sleep(0.08)
    assert batches == []              # 距第二条仅 0.08s，尚未 flush
    await asyncio.sleep(0.2)
    assert batches == [("feishu:u1", "一\n二")]
