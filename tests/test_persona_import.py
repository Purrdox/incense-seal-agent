"""角色卡导入：长文本状态机、JSON/PNG、长度限制与运行时刷新。"""
from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

import qqlife.persona_import as persona_import_mod
from qqlife.persona_import import PersonaImport


def _card_bytes(card: dict) -> bytes:
    value = base64.b64encode(json.dumps(card, ensure_ascii=False).encode())
    data = b"chara\x00" + value
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(
        ">I", zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF)
    end = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk + end


class FakeRegistry:
    def __init__(self) -> None:
        self.runtimes: dict[str, dict] = {}

    def ensure(self, uid: str) -> dict:
        return self.runtimes.setdefault(uid, {"persona": "", "persona_path": ""})


@pytest.fixture
def env(tmp_path, monkeypatch):
    def _home(uid: str):
        p = tmp_path / "data" / uid
        p.mkdir(parents=True, exist_ok=True)
        return p

    monkeypatch.setattr(persona_import_mod, "user_home", _home)
    registry = FakeRegistry()
    return PersonaImport(registry=registry, max_chars=200), registry, tmp_path


async def test_inline_text_import(env):
    pi, registry, tmp_path = env
    text = "姓名：测试角色\n设定：一个用于测试的角色，喜欢安静。"
    reply = await pi.handle("feishu", "u1", "alice", ".persona " + text)
    assert "角色卡已导入" in reply
    assert (tmp_path / "data" / "alice" / "persona.md").read_text(encoding="utf-8") == text
    assert registry.runtimes["alice"]["persona"] == text


async def test_multi_message_import(env):
    pi, registry, tmp_path = env
    assert "请发送角色卡文本" in await pi.handle("feishu", "u1", "alice", ".persona")
    assert pi.active("feishu", "u1")
    assert pi.feed("feishu", "u1", "alice", "第一段：姓名与设定。") == ""
    assert pi.feed("feishu", "u1", "alice", "第二段：性格与场景。") == ""
    reply = pi.feed("feishu", "u1", "alice", ".persona end")
    assert "角色卡已导入" in reply
    saved = (tmp_path / "data" / "alice" / "persona.md").read_text(encoding="utf-8")
    assert "第一段" in saved and "第二段" in saved
    assert not pi.active("feishu", "u1")


async def test_cancel_import(env):
    pi, _registry, tmp_path = env
    await pi.handle("feishu", "u1", "alice", ".persona")
    pi.feed("feishu", "u1", "alice", "一些文本内容足够长。")
    assert "已取消" in pi.feed("feishu", "u1", "alice", ".persona cancel")
    assert not (tmp_path / "data" / "alice" / "persona.md").exists()


async def test_json_card_import(env):
    pi, _registry, tmp_path = env
    card = {"name": "测试角色", "description": "一个用于测试的角色"}
    reply = await pi.handle("feishu", "u1", "alice", ".persona " + json.dumps(card, ensure_ascii=False))
    assert "测试角色" in reply
    saved = (tmp_path / "data" / "alice" / "persona.md").read_text(encoding="utf-8")
    assert "姓名：测试角色" in saved


async def test_png_card_cache_and_import(env):
    pi, _registry, tmp_path = env
    card = {"name": "PNG角色", "description": "从 PNG 导入的角色"}
    image = {"b64": base64.b64encode(_card_bytes(card)).decode()}
    assert "检测到角色卡" in pi.cache_from_image("feishu", "u1", image)
    reply = await pi.handle("feishu", "u1", "alice", ".persona card")
    assert "PNG角色" in reply
    saved = (tmp_path / "data" / "alice" / "persona.md").read_text(encoding="utf-8")
    assert "姓名：PNG角色" in saved


async def test_too_long_rejected(env):
    pi, _registry, _tmp_path = env
    reply = await pi.handle("feishu", "u1", "alice", ".persona " + "很长" * 200)
    assert "超过" in reply


async def test_too_short_rejected(env):
    pi, _registry, _tmp_path = env
    reply = await pi.handle("feishu", "u1", "alice", ".persona 短")
    assert "太短" in reply


async def test_unbound_requires_create(env):
    pi, _registry, _tmp_path = env
    reply = await pi.handle("feishu", "u1", None, ".persona")
    assert ".create" in reply


async def test_similar_word_is_not_persona_command(env):
    pi, _registry, _tmp_path = env
    assert await pi.handle("feishu", "u1", "alice", ".personal 不是指令") is None
