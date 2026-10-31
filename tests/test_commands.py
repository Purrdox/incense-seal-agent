"""指令层：新旧名称、帮助文案与 purge 保留项。"""
from __future__ import annotations

import qqlife.commands as commands


def test_time_and_status():
    assert commands.handle_command(".time").startswith("当前时间")
    assert "当前状态" in commands.handle_command(".status")


def test_help_lists_new_commands_only():
    text = commands.handle_command(".help")
    for name in (".time", ".status", ".compact", ".usage", ".create", ".link",
                 ".profile", ".unlink", ".persona", ".mech", ".purge", ".quit"):
        assert name in text
    for old in (".now", ".check", ".sum", ".tokens", ".bind", ".whoami", ".unbind",
                ".idnew", ".clean", ".exit"):
        assert old not in text


def test_old_commands_fall_back_to_help():
    for old in (".now", ".check", ".sum", ".tokens", ".bind", ".whoami", ".unbind",
                ".idnew", ".clean", ".exit", ".stop", ".mute"):
        assert "可用指令" in commands.handle_command(old)


def test_command_word():
    assert commands.command_word(".create alice") == "create"
    assert commands.command_word("。persona end") == "persona"
    assert commands.command_word("你好") == ""


def test_detectors():
    assert commands.is_compact(".compact")
    assert commands.is_purge("。purge")
    assert commands.is_usage(".usage")
    assert not commands.is_compact(".sum")


def test_purge_keeps_persona_mech_uid(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "DATA_DIR", tmp_path)
    home = tmp_path / "alice"
    home.mkdir()
    for name in ("persona.md", "mech_prompts.json", "uid.json", "items.json",
                 "session.json", "expenses.json", "usage.json"):
        (home / name).write_text("{}", encoding="utf-8")
    att = home / "attachments"
    att.mkdir()
    (att / "tts_1.wav").write_bytes(b"x")

    msg = commands.purge_uid("alice")
    assert "人设 / 机械提示 / 身份端点" in msg
    assert (home / "persona.md").exists()
    assert (home / "mech_prompts.json").exists()
    assert (home / "uid.json").exists()
    assert not (home / "items.json").exists()
    assert not (home / "session.json").exists()
    assert not (att / "tts_1.wav").exists()


def test_purge_is_scoped_to_one_uid(tmp_path, monkeypatch):
    """`.purge` 只清当前身份：另一身份的文件必须原样保留。"""
    monkeypatch.setattr(commands, "DATA_DIR", tmp_path)
    for uid in ("alice", "bob"):
        home = tmp_path / uid
        home.mkdir()
        (home / "uid.json").write_text("{}", encoding="utf-8")
        (home / "items.json").write_text("{}", encoding="utf-8")

    commands.purge_uid("alice")

    assert not (tmp_path / "alice" / "items.json").exists()
    assert (tmp_path / "bob" / "items.json").exists()
    assert (tmp_path / "bob" / "uid.json").exists()


def test_purge_rejects_bad_uid(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "DATA_DIR", tmp_path)
    assert "无效" in commands.purge_uid("../etc")
    assert "没有可清理" in commands.purge_uid("carol")
