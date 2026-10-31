"""agents/memory_service.py：记忆编排服务（LLM + 存储的胶水层，10.16 §A2/§A3）。

fold 是**唯一**更新长期记忆（session.facts）的时机（`.compact` 手动 / 硬上限安全网自动）：
  1. snapshot：整个 session.json 快照为 .bak（防更新丢失）；
  2. LLM（extract 档）合并 旧 facts + 待固化 new + 即将清空的 turns → 新 facts（上限 FACTS_MAX 行）；
  3. 合并成功 → apply_fold 落盘（清空 new/turns）；**失败不清空**，原文保留降级重试。

压缩入口（用户裁决，10.16 落地）：手动 `.compact`（force_compact，任意长度可用）是唯一用户压缩
入口；自动仅剩硬上限安全网（maybe_compact，300 轮兜底）——软阈值自动压缩已删除，软提醒
（compact_hint 机械提示）只起引导作用。存储层（session.py）只提供纯数据操作，不持有 LLM 调用。
"""
from __future__ import annotations

import logging

from qqlife import usage_rec
from qqlife.agents.promptkit import render as render_prompt

_log = logging.getLogger("default.memory")

FACTS_MAX = 40   # 长期记忆条数上限（超限由模型在合并时去重压缩）
# fold 的输出预算。注意：思考模式下 reasoning 与正文**共享** max_tokens，整个额度会被思考吃光
# （实测 completion=4095、content 为空 → 解析失败），所以这里显式关闭思考（thinking=False）。
FOLD_MAX_TOKENS = 4096


def _parse_facts(text: str) -> dict:
    """解析 LLM 输出的若干行 `field: text` 为 dict（容忍全角冒号与行首 '-'）。"""
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        field, _, value = line.partition("：")
        if not value:
            field, _, value = line.partition(":")
        field, value = field.strip("【】 ").strip(), value.strip()
        if field and value:
            out[field] = value
    # 提示词要求最多 FACTS_MAX 行；这里做硬上限，防止模型超量输出
    return dict(list(out.items())[:FACTS_MAX])


async def _merge_facts(hub, facts: dict, new: list[str], turns: list[dict],
                       owner: str = "") -> dict | None:
    """LLM 合并（extract 档）：旧 facts + 待固化 new + 即将清空的 turns → 新 facts。

    模型不可用 / 调用异常 / 输出为空 / 输出被截断 / 解析为空 → 返回 None（上层保留原文不清空）。
    """
    llm = hub.for_kind("extract") if hub else None
    if not (llm and llm.is_available()):
        _log.warning("记忆压缩失败（extract 档模型不可用）：uid=%s", owner)
        return None
    old_block = "\n".join(f"{k}：{v}" for k, v in facts.items()) or "（空）"
    new_block = "\n".join(f"- {s}" for s in new) or "（无）"
    digest = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
    user = (f"【已存在的长期记忆】\n{old_block}\n\n【待固化记忆】\n{new_block}\n\n"
            f"【即将清空的对话】\n{digest or '（无）'}")
    usage: dict = {}
    try:
        out = (await llm.chat([{"role": "system", "content": render_prompt("extract_user_memory")},
                               {"role": "user", "content": user}], max_tokens=FOLD_MAX_TOKENS,
                              usage=usage, thinking=False)).strip()
    except Exception as e:
        _log.warning("记忆压缩失败（模型调用异常）：uid=%s %s: %s", owner, type(e).__name__, e)
        return None
    finally:
        if usage:
            usage_rec.record(owner, usage)
    # 撞到 max_tokens：输出不完整，不能拿半截 facts 覆盖长期记忆，本轮放弃、原文保留
    if usage.get("finish") == "length":
        _log.warning("记忆压缩输出被截断（finish_reason=length，completion=%s，max_tokens=%d）：uid=%s preview=%r",
                     usage.get("completion"), FOLD_MAX_TOKENS, owner, out[:200])
        return None
    if not out:
        _log.warning("记忆压缩失败（模型输出为空，completion=%s）：uid=%s",
                     usage.get("completion"), owner)
        return None
    facts = _parse_facts(out)
    if not facts:
        _log.warning("记忆压缩失败（输出无法解析为 `field: text`）：uid=%s preview=%r",
                     owner, out[:200])
        return None
    return facts


async def _fold(session, owner: str, hub) -> bool:
    """fold：唯一更新长期记忆的时机。快照 → 合并 → 清空；**失败不清空**（原文保留降级重试）。"""
    turns = session.all_turns(owner)
    new = session.new_entries(owner)
    if not (turns or new):
        return False
    session.snapshot()
    merged = await _merge_facts(hub, session.facts(owner), new, turns, owner=owner)
    if merged is None:
        return False
    session.apply_fold(owner, merged)
    return True


async def maybe_compact(session, owner: str, hub, turn_hard: int = 300) -> bool:
    """硬上限安全网：turns 超过硬上限才自动 fold（软阈值自动压缩已删，见模块 docstring）。"""
    if session.turns_count() < max(1, turn_hard):
        return False
    return await _fold(session, owner, hub)


async def force_compact(session, owner: str, hub) -> dict:
    """`.compact`：无条件 fold（任意长度可用）。返回前后统计。"""
    before = session.turns_count()
    ok = await _fold(session, owner, hub)
    return {"compressed": ok, "before": before, "after": session.turns_count(),
            "facts": len(session.facts(owner))}
