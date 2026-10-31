"""agents/promptkit.py：提示词加载与注入（改提示词不动代码）。

- 读取 prompts/{name}.md 作为该提示词正文（无 skill_ 前缀，任意系统提示词都放这）。
- 兼容旧 `skill_*` 命名：`skill_{name}.md` 存在时优先（技能主提示词仍走这）。
- 支持 {{VAR}} 占位替换；未替换的占位保持原样。
- 稳定系统文本缓存，减少重复 IO。
"""
from __future__ import annotations

import re
from pathlib import Path

from qqlife._paths import PROMPTS_DIR as _PROMPTS_DIR
_CACHE: dict[str, str] = {}
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load(name: str) -> str:
    if name not in _CACHE:
        bare = _PROMPTS_DIR / f"{name}.md"
        p = bare if bare.exists() else _PROMPTS_DIR / f"skill_{name}.md"
        _CACHE[name] = p.read_text(encoding="utf-8")
    return _CACHE[name]


def render(name: str, **kw) -> str:
    t = load(name)
    return _VAR.sub(lambda m: str(kw.get(m.group(1), m.group(0))), t)