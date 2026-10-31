"""agents/chat_log.py：主会话请求落盘（摘要优先 + 图片脱敏 + 按大小轮转）。

- 默认只记摘要（各角色条数 / 字符数 / 图片数），避免每轮完整 messages 让 chat.log 近似 O(n²) 膨胀；
- `CHAT_LOG_FULL=1` 时记录完整请求，但 base64 图片一律替换为 `[image omitted, N bytes]`；
- RotatingFileHandler：10MB × 5 份，长期运行不再无限增长。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from qqlife._paths import LOGS_DIR

_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5
_logger: logging.Logger | None = None


def full_enabled() -> bool:
    return str(os.getenv("CHAT_LOG_FULL", "")).strip().lower() in ("1", "true", "yes", "on")


def messages_summary(messages: list[dict]) -> str:
    """一行摘要：角色条数 + 文本字符数 + 图片数（不含任何正文）。"""
    counts: dict[str, int] = {}
    chars = 0
    images = 0
    for m in messages or []:
        role = str(m.get("role", "?"))
        counts[role] = counts.get(role, 0) + 1
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    images += 1
                else:
                    chars += len(str(part.get("text", "")))
    roles = " ".join(f"{k}={v}" for k, v in counts.items())
    tail = f" images={images}" if images else ""
    return f"[llm_req] summary: {roles} chars={chars}{tail}"


def _data_url_bytes(url: str) -> int:
    """估算图片体积：data URL 按 base64 长度折算，普通 URL 退化为字符数。"""
    if not url.startswith("data:"):
        return len(url.encode("utf-8"))
    _, _, b64 = url.partition(",")
    return len(b64) * 3 // 4


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """深拷贝一层并把图片内容替换为体积占位符，其余字段原样保留。"""
    out: list[dict] = []
    for m in messages or []:
        row = dict(m)
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url", ""))
                    parts.append({"type": "text",
                                  "text": f"[image omitted, {_data_url_bytes(url)} bytes]"})
                else:
                    parts.append(part)
            row["content"] = parts
        out.append(row)
    return out


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("qqlife.chatlog")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        handler = RotatingFileHandler(LOGS_DIR / "chat.log", maxBytes=_MAX_BYTES,
                                      backupCount=_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
        _logger = lg
    return _logger


def write_chat_log(block: str) -> None:
    try:
        _get_logger().info(block.rstrip("\n"))
    except Exception:
        pass
