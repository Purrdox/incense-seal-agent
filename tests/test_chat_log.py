"""chat.log 治理：摘要、图片脱敏、大小轮转。"""
from __future__ import annotations

from qqlife.agents import chat_log


def test_messages_summary_counts_roles_chars_images():
    msgs = [
        {"role": "system", "content": "x" * 10},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        {"role": "user", "content": [{"type": "image_url",
                                       "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ]
    summary = chat_log.messages_summary(msgs)
    assert "system=1" in summary
    assert "user=2" in summary
    assert "assistant=1" in summary
    assert "chars=14" in summary
    assert "images=1" in summary


def test_sanitize_messages_omits_images_without_mutating_source():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 400}},
    ]}]

    out = chat_log.sanitize_messages(msgs)

    assert out[0]["content"][0] == {"type": "text", "text": "看图"}
    assert out[0]["content"][1]["text"] == "[image omitted, 300 bytes]"
    assert msgs[0]["content"][1]["type"] == "image_url"   # 原对象不变


def test_full_enabled_env(monkeypatch):
    monkeypatch.delenv("CHAT_LOG_FULL", raising=False)
    assert chat_log.full_enabled() is False
    monkeypatch.setenv("CHAT_LOG_FULL", "1")
    assert chat_log.full_enabled() is True


def test_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_log, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(chat_log, "_MAX_BYTES", 200)
    monkeypatch.setattr(chat_log, "_BACKUPS", 2)
    monkeypatch.setattr(chat_log, "_logger", None)
    try:
        for _ in range(10):
            chat_log.write_chat_log("x" * 100)
        assert (tmp_path / "chat.log").exists()
        assert (tmp_path / "chat.log.1").exists()
    finally:
        if chat_log._logger is not None:
            for h in chat_log._logger.handlers:
                h.close()
            chat_log._logger.handlers.clear()
            chat_log._logger = None
