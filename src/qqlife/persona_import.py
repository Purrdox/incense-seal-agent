"""qqlife/persona_import.py：角色卡导入（长文本 / PNG / JSON）。

- `.persona` 进入多消息导入模式；`.persona end` 保存，`.persona cancel` 取消。
- `.persona <文本>` 单条导入；`.persona card` 导入最近缓存的 PNG 卡。
- 状态按 `(channel, remote_id)` 隔离，10 分钟 TTL；写入后刷新运行时 persona。
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
from pathlib import Path

from qqlife._paths import user_home
from qqlife.agents.character_card import (card_name, load_character_card_bytes,
                                          to_persona)

_MIN_CHARS = 10
_END = {"end"}
_CANCEL = {"cancel"}


def _split_persona(text: str) -> tuple[str, str]:
    """拆 `.persona` 指令：返回 (arg, rest)。arg 为首词，rest 保留原始换行。"""
    t = (text or "").strip()
    if t.startswith((".", "。")):
        t = t[1:].strip()
    if not t:
        return "", ""
    parts = t.split(maxsplit=1)
    if not parts or parts[0].lower() != "persona":
        return "", ""
    rest = parts[1] if len(parts) > 1 else ""
    arg = rest.split(maxsplit=1)[0].lower() if rest else ""
    return arg, rest


def _is_persona_command(text: str) -> bool:
    t = (text or "").strip()
    if not t.startswith((".", "。")):
        return False
    body = t[1:].strip()
    return bool(body) and body.split(maxsplit=1)[0].lower() == "persona"


class PersonaImport:
    def __init__(self, *, registry, max_chars: int | None = None, ttl: int = 600) -> None:
        self.registry = registry
        self.max_chars = int(max_chars or os.getenv("PERSONA_MAX_CHARS") or 8000)
        self.ttl = ttl
        self._mode: dict[tuple[str, str], dict] = {}
        self._cards: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(channel: str, remote_id: str) -> tuple[str, str]:
        return channel, str(remote_id)

    def _expired(self, st: dict) -> bool:
        return (time.monotonic() - float(st.get("at", 0))) > self.ttl

    def active(self, channel: str, remote_id: str) -> bool:
        key = self._key(channel, remote_id)
        st = self._mode.get(key)
        if st is None:
            return False
        if self._expired(st):
            self._mode.pop(key, None)
            return False
        return True

    def clear(self, channel: str, remote_id: str) -> None:
        key = self._key(channel, remote_id)
        self._mode.pop(key, None)
        self._cards.pop(key, None)

    # ---- 文本 / 状态机 ----

    def start(self, channel: str, remote_id: str, uid: str) -> str:
        self._mode[self._key(channel, remote_id)] = {
            "uid": uid, "chunks": [], "at": time.monotonic(),
        }
        return ("请发送角色卡文本，发送 .persona end 结束；也可以直接发送 PNG 角色卡。"
                "取消用 .persona cancel。")

    def cancel(self, channel: str, remote_id: str) -> str:
        self._mode.pop(self._key(channel, remote_id), None)
        return "已取消角色卡导入，人设未改动。"

    def feed(self, channel: str, remote_id: str, uid: str | None,
             text: str, images: list[dict] | None = None) -> str | None:
        """导入模式中的一条消息；返回回执，None 表示不是导入模式。"""
        key = self._key(channel, remote_id)
        st = self._mode.get(key)
        if st is None:
            return None
        if uid is None or st.get("uid") != uid or self._expired(st):
            self._mode.pop(key, None)
            return "导入已超时或身份已变化，已取消。请重新发送 .persona。"

        raw = (text or "").strip()
        arg, _ = _split_persona(raw)
        if arg in _END:
            self._mode.pop(key, None)
            return self._save(uid, "\n".join(st.get("chunks", [])), "角色卡")
        if arg in _CANCEL:
            self._mode.pop(key, None)
            return "已取消角色卡导入，人设未改动。"
        if arg == "card":
            self._mode.pop(key, None)
            return self._import_cached(channel, remote_id, uid)
        if _is_persona_command(raw) and not arg:
            return "已在导入模式：发送 .persona end 保存，.persona cancel 取消。"

        for img in images or []:
            card = self._card_from_image(img)
            if card:
                self._mode.pop(key, None)
                return self._save(uid, card["persona"], card["name"])
        if images:
            return "这张图片不是可识别的角色卡；请继续发送文本，或 .persona cancel 取消。"
        if not raw:
            return ""

        st["chunks"].append(raw)
        st["at"] = time.monotonic()
        total = len("\n".join(st["chunks"]))
        if total > self.max_chars:
            self._mode.pop(key, None)
            return f"角色卡超过 {self.max_chars} 字符，已取消；请精简后重新发送 .persona。"
        return ""   # 已接收，无需逐条回执

    # ---- 指令入口 ----

    async def handle(self, channel: str, remote_id: str, uid: str | None,
                     text: str, images: list[dict] | None = None) -> str | None:
        if not _is_persona_command(text):
            return None
        arg, rest = _split_persona(text)
        if uid is None:
            return "当前端点未绑定。发送 .create <标识码> 创建身份。"

        if arg in _END:
            st = self._mode.pop(self._key(channel, remote_id), None)
            if st is None:
                return "当前没有正在进行的导入。"
            return self._save(uid, "\n".join(st.get("chunks", [])), "角色卡")
        if arg in _CANCEL:
            return self.cancel(channel, remote_id)
        if arg == "card":
            return self._import_cached(channel, remote_id, uid)
        if not rest:
            return self.start(channel, remote_id, uid)
        return self._import_text(uid, rest)

    # ---- 导入与保存 ----

    def _import_text(self, uid: str, text: str) -> str:
        persona, name = self._persona_from_text(text)
        return self._save(uid, persona, name)

    def _import_cached(self, channel: str, remote_id: str, uid: str) -> str:
        key = self._key(channel, remote_id)
        card = self._cards.pop(key, None)
        if card is None or self._expired(card):
            return "没有可用的角色卡。请先发送 PNG 角色卡，再发送 .persona card。"
        return self._save(uid, card["persona"], card["name"])

    def cache_from_image(self, channel: str, remote_id: str, image: dict) -> str | None:
        """普通图片消息：若检测到角色卡则缓存并返回提示，否则 None。"""
        card = self._card_from_image(image)
        if card is None:
            return None
        self._cards[self._key(channel, remote_id)] = {**card, "at": time.monotonic()}
        name = card.get("name") or "未命名角色"
        return f"检测到角色卡《{name}》。发送 .persona card 导入。"

    def _card_from_image(self, image: dict) -> dict | None:
        b64 = str((image or {}).get("b64") or (image or {}).get("data") or "")
        if not b64:
            return None
        try:
            data = base64.b64decode(b64)
            card = load_character_card_bytes(data)
        except Exception:
            return None
        persona = to_persona(card, self.max_chars)
        if not persona:
            return None
        return {"persona": persona, "name": card_name(card)}

    def _persona_from_text(self, text: str) -> tuple[str, str]:
        raw = (text or "").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                persona = to_persona(data, self.max_chars)
                if persona:
                    return persona, card_name(data) or "未命名角色"
        except Exception:
            pass
        return raw, "未命名角色"

    def _save(self, uid: str, persona: str, name: str) -> str:
        text = (persona or "").strip()
        if len(text) < _MIN_CHARS:
            return f"角色卡内容太短（至少 {_MIN_CHARS} 字符），未保存。"
        if len(text) > self.max_chars:
            return f"角色卡超过 {self.max_chars} 字符，未保存；请精简后重新导入。"

        path = user_home(uid) / "persona.md"
        backup = ""
        tmp = path.with_suffix(".md.tmp")
        try:
            if path.exists():
                backup = str(path.with_name(f"persona.md.bak-{dt.datetime.now():%Y%m%d-%H%M%S-%f}"))
                path.replace(backup)
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            if backup and not path.exists():
                try:
                    Path(backup).replace(path)
                except Exception:
                    pass
            return f"人设写入失败：{type(e).__name__}。"

        r = self.registry.ensure(uid)
        r["persona"] = text
        r["persona_path"] = str(path)
        return (f"角色卡已导入：{name or '未命名角色'}\n"
                f"人设长度：{len(text)} 字符\n"
                "下一步：.mech 生成机械提示词。")
