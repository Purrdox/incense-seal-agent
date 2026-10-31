"""角色卡解析：tEXt / zTXt / iTXt、V2 包装与 persona 转换。"""
from __future__ import annotations

import base64
import json
import struct
import zlib

from qqlife.agents.character_card import (card_name, load_character_card_bytes,
                                          to_persona)


def _chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def _png_with_text(key: str, value: bytes, kind: bytes = b"tEXt") -> bytes:
    if kind == b"tEXt":
        data = key.encode() + b"\x00" + value
    elif kind == b"zTXt":
        data = key.encode() + b"\x00" + b"\x00" + zlib.compress(value)
    else:
        data = key.encode() + b"\x00" + b"\x00\x00" + b"\x00" + b"\x00" + value
    return (b"\x89PNG\r\n\x1a\n" + _chunk(kind, data) + _chunk(b"IEND", b""))


def _card_bytes(card: dict, kind: bytes = b"tEXt") -> bytes:
    value = base64.b64encode(json.dumps(card, ensure_ascii=False).encode())
    return _png_with_text("chara", value, kind)


CARD = {"name": "测试角色", "description": "一个用于测试的角色",
        "personality": "冷静", "scenario": "在书房里"}


def test_text_chunk():
    card = load_character_card_bytes(_card_bytes(CARD))
    assert card["name"] == "测试角色"


def test_ztxt_chunk():
    card = load_character_card_bytes(_card_bytes(CARD, b"zTXt"))
    assert card["name"] == "测试角色"


def test_itxt_chunk():
    card = load_character_card_bytes(_card_bytes(CARD, b"iTXt"))
    assert card["name"] == "测试角色"


def test_v2_wrapper_and_persona():
    card = {"spec": "chara_card_v2", "data": CARD}
    persona = to_persona(card)
    assert "姓名：测试角色" in persona
    assert "设定：一个用于测试的角色" in persona
    assert card_name(card) == "测试角色"


def test_to_persona_truncates():
    card = {"name": "长卡", "description": "很长" * 200}
    persona = to_persona(card, max_chars=100)
    assert len(persona) <= 110
    assert "已截断" in persona


def test_non_png_rejected():
    try:
        load_character_card_bytes(b"not png")
    except ValueError as e:
        assert "PNG" in str(e)
    else:
        raise AssertionError("应当拒绝非 PNG")
