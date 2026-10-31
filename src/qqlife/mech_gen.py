"""qqlife/mech_gen.py：按角色卡生成机械提示词。

- 纯函数负责解析与校验；LLM 调用以 `llm_call` 注入，测试无需联网。
- 生成结果逐键校验：缺键/空值/占位符不合法/超长 → 回退基准。
- 写入 `data/<uid>/mech_prompts.json`，旧文件自动备份。
"""
from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Awaitable, Callable

from qqlife import mech_prompts
from qqlife.agents.promptkit import render

MAX_LEN = 200

# 每个键必须出现且只能出现的占位符；其它键不允许任何占位符
_REQUIRED: dict[str, set[str]] = {
    "compact_hint": {"n"},
    "compact_done": {"before", "facts"},
    "backlog_skipped": {"n", "hours"},
    "delivery_failed": {"content"},
    "data_corrupt": {"files"},
    "reminder_label": {"title"},
}

LLMCall = Callable[[str, str], Awaitable[str]]


def _fields(text: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError:
        return {"__invalid__"}


def parse_json(text: str) -> dict:
    """解析模型输出；兼容 ```json 围栏与前后解释文字。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        raw = json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("模型输出不是 JSON 对象")
        raw = json.loads(t[i:j + 1])
    if isinstance(raw, dict) and isinstance(raw.get("mech_prompts"), dict):
        raw = raw["mech_prompts"]
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return raw


def normalize(raw: dict, baseline: dict) -> tuple[dict, dict]:
    """逐键校验并回退基准；返回 (最终表, 统计)。"""
    stats = {"used": 0, "fallback": 0, "dropped": sorted(set(raw) - set(baseline))}
    merged: dict[str, str] = {}
    for key, base in baseline.items():
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            merged[key] = base
            stats["fallback"] += 1
            continue
        val = val.strip()
        required = _REQUIRED.get(key, set())
        fields = _fields(val)
        if fields != required or len(val) > MAX_LEN:
            merged[key] = base
            stats["fallback"] += 1
            continue
        merged[key] = val
        stats["used"] += 1
    return merged, stats


async def generate(persona: str, baseline: dict, llm_call: LLMCall) -> tuple[dict, dict]:
    """调用 LLM 生成并校验；失败抛异常，由调用方决定如何回复。"""
    system = render("gen_mech_prompts")
    user = (f"<persona>\n{(persona or '').strip()}\n</persona>\n\n"
            f"<baseline>\n{json.dumps(baseline, ensure_ascii=False, indent=2)}\n</baseline>")
    text = await llm_call(system, user)
    return normalize(parse_json(text), baseline)


async def generate_and_save(home: Path, persona: str, llm_call: LLMCall) -> tuple[dict, dict, str]:
    """生成、校验、写入 per-uid 文件；返回 (最终表, 统计, 备份路径)。"""
    if not (persona or "").strip():
        raise ValueError("当前没有人设，请先 .persona 导入角色卡")
    baseline = mech_prompts.baseline_table()
    merged, stats = await generate(persona, baseline, llm_call)
    backup = mech_prompts.save_table(Path(home), merged)
    return merged, stats, backup
