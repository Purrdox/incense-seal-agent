"""agents/character_card.py：ST 角色卡加载器。

支持：
- PNG 内嵌文本块：`chara`(V2)、`ccv3`(V3)，兼容 `tEXt` / `zTXt` / `iTXt`；
- 独立 .json 文件，或直接传入 bytes / dict；
- 兼容 V2/V3 的 `{spec, data}` 包装。
返回标准字段：name/description/personality/scenario/mes_example/...
"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

_FIELDS = ("name", "description", "personality", "scenario", "mes_example")


def _png_text_chunks(data: bytes) -> dict[str, bytes]:
    chunks: dict[str, bytes] = {}
    i = 8
    while i + 12 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        raw = data[i + 8:i + 8 + length]
        try:
            if ctype == b"tEXt":
                k, _, v = raw.partition(b"\x00")
                chunks[k.decode(errors="replace")] = v
            elif ctype == b"zTXt":
                k, _, rest = raw.partition(b"\x00")
                if rest:
                    chunks[k.decode(errors="replace")] = zlib.decompress(rest[1:])
            elif ctype == b"iTXt":
                k, _, rest = raw.partition(b"\x00")
                if len(rest) >= 2:
                    compressed = rest[0:1] == b"\x01"
                    rest = rest[2:]
                    _, _, rest = rest.partition(b"\x00")   # language tag
                    _, _, text = rest.partition(b"\x00")   # translated keyword
                    chunks[k.decode(errors="replace")] = zlib.decompress(text) if compressed else text
        except Exception:
            pass
        i += 12 + length
        if ctype == b"IEND":
            break
    return chunks


def _normalize(card: dict) -> dict:
    """V2/V3 常见包装：{spec, data:{...}} → data。"""
    if isinstance(card, dict) and isinstance(card.get("data"), dict):
        data = card["data"]
        if any(k in data for k in _FIELDS):
            return data
    return card


def _decode_card(chunks: dict[str, bytes]) -> dict:
    for key in ("ccv3", "chara"):
        if key not in chunks:
            continue
        raw = chunks[key].strip()
        for parser in (lambda b: json.loads(base64.b64decode(b)),
                       lambda b: json.loads(b)):
            try:
                card = parser(raw)
                if isinstance(card, dict):
                    return _normalize(card)
            except Exception:
                continue
    raise ValueError("不是可识别的角色卡")


def load_character_card_bytes(data: bytes) -> dict:
    """从 PNG bytes 解析角色卡。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("不是 PNG 文件")
    return _decode_card(_png_text_chunks(data))


def load_character_card(path: str | Path) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".json":
        return _normalize(json.loads(p.read_text(encoding="utf-8")))
    return load_character_card_bytes(p.read_bytes())


def card_name(card: dict) -> str:
    return str((_normalize(card or {})).get("name") or "").strip()


def to_persona(card: dict, max_chars: int = 8000) -> str:
    """把卡组织成 chat 人设文本；超长时截断，避免 persona 无限膨胀。"""
    card = _normalize(card)
    labels = (("name", "姓名"), ("description", "设定"),
              ("personality", "性格"), ("scenario", "场景"))
    lines: list[str] = []
    for key, label in labels:
        v = card.get(key)
        if v:
            lines.append(f"{label}：{str(v).strip()}")
    example = str(card.get("mes_example") or "").strip()
    if example:
        if len(example) > 1200:
            example = example[:1200] + "…"
        lines.append(f"示例对话：\n{example}")
    text = "\n\n".join(lines).strip()
    if len(text) > max_chars:
        suffix = "\n（角色卡过长，已截断）"
        text = text[:max(0, max_chars - len(suffix))].rstrip() + suffix
    return text
