"""Deduper：去重 key 必须按「通道 + 端点 + 文本」隔离。"""
from __future__ import annotations

from qqlife.platform.dedupe import Deduper


def test_same_channel_endpoint_text_is_dup():
    d = Deduper(window=2.0)
    assert d.is_dup("feishu", "u1", "好的", msg_time=100.0) is False
    assert d.is_dup("feishu", "u1", "好的", msg_time=100.5) is True


def test_different_endpoint_is_not_dup():
    d = Deduper(window=2.0)
    assert d.is_dup("feishu", "u1", "好的", msg_time=100.0) is False
    assert d.is_dup("feishu", "u2", "好的", msg_time=100.5) is False


def test_different_channel_is_not_dup():
    d = Deduper(window=2.0)
    assert d.is_dup("feishu", "u1", "好的", msg_time=100.0) is False
    assert d.is_dup("onebot", "u1", "好的", msg_time=100.5) is False


def test_window_expiry_and_cleanup():
    d = Deduper(window=2.0)
    assert d.is_dup("feishu", "u1", "x", msg_time=100.0) is False
    # 窗口外：不再判重，且旧 key 应被清理
    assert d.is_dup("feishu", "u1", "x", msg_time=103.0) is False
    assert len(d._seen) == 1
    # 新时间点写入后，窗口内仍然判重
    assert d.is_dup("feishu", "u1", "x", msg_time=103.5) is True
