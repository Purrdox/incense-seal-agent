"""qqlife.usage_rec.py：token 用量记账（per-uid 小文档 data/<uid>/usage.json，10.16 §A3）。

- llm 层（DeepSeek）把每次调用的 usage（prompt_cache_hit_tokens / prompt_cache_miss_tokens /
  completion_tokens）透传给调用方，调用方带 uid 在此落盘；
- 结构：{"last": {hit, miss, completion}, "total": {hit, miss, completion, calls}}，原子写；
- `.usage` 指令（app 层拦截）经 report() 输出上次请求与累计命中占比。
"""
from __future__ import annotations

import json

from qqlife._paths import user_home


def _read(uid: str) -> dict:
    try:
        raw = json.loads((user_home(uid) / "usage.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def record(uid: str, u: dict) -> None:
    """记一次调用：u = {hit, miss, completion}（llm 层透传）。last 覆写，total 累加。"""
    if not u:
        return
    data = _read(uid)
    last = {"hit": int(u.get("hit") or 0), "miss": int(u.get("miss") or 0),
            "completion": int(u.get("completion") or 0)}
    total = data.get("total")
    if not isinstance(total, dict):
        total = {"hit": 0, "miss": 0, "completion": 0, "calls": 0}
    for k in ("hit", "miss", "completion"):
        total[k] = int(total.get(k) or 0) + last[k]
    total["calls"] = int(total.get("calls") or 0) + 1
    data["last"], data["total"] = last, total
    p = user_home(uid) / "usage.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # 原子替换


def _pct(hit: int, miss: int) -> str:
    total_in = hit + miss
    return f"{hit * 100.0 / total_in:.1f}%" if total_in else "—"


def report(uid: str) -> str:
    """`.usage` 回执：上次请求用量 + 本档案累计用量与输入命中占比。"""
    data = _read(uid)
    last = data.get("last") if isinstance(data.get("last"), dict) else {}
    total = data.get("total") if isinstance(data.get("total"), dict) else {}
    if not total:
        return "【token 用量】暂无记录（还没有过模型调用）。"

    def _g(d: dict, k: str) -> int:
        return int(d.get(k) or 0)

    return (f"【token 用量】上次请求：输入命中 {_g(last, 'hit'):,} / 未命中 {_g(last, 'miss'):,} / "
            f"输出 {_g(last, 'completion'):,}（输入命中率 {_pct(_g(last, 'hit'), _g(last, 'miss'))}）\n"
            f"累计（本档案）：{_g(total, 'calls'):,} 次调用，输入命中 {_g(total, 'hit'):,} / "
            f"未命中 {_g(total, 'miss'):,} / 输出 {_g(total, 'completion'):,}，"
            f"输入总命中率 {_pct(_g(total, 'hit'), _g(total, 'miss'))}")
