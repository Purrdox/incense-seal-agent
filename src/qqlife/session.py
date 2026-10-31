"""qqlife.session.py：多轮会话记忆（per-uid 家目录文件，session.json 统一记忆，10.16 §A2）。纯数据层，不含 LLM。

结构：{"facts": {}, "new": [], "turns": [], "last_compact_at": 0, "compact_hint_at": 0}
- facts：长期记忆（field:text），**取代旧 summary 的位置**，仅在 fold 时更新（上限见 memory_service.FACTS_MAX）；
- new：模型用 remember_user 工具即时写入的待固化句子（每轮随上下文注入，fold 时并入 facts）；
- turns：全部未压缩轮次（append-only，缓存契约见下）。

缓存契约（DeepSeek 前缀缓存）：注入序 = 人设 → facts → new → turns → 时间 → 当轮消息。
turns 只追加不打头；facts 仅 fold 时变；new 仅工具写入时尾部追加——追加后**下一轮** turns
缓存会失效一次（记忆换来的小代价，频次低），fold 周期性重置属设计内。

fold（"快照→合并→写回"在 agents/memory_service.py 编排，本类只提供数据操作）：
- snapshot()：fold 前把整个 session.json 复制为 .bak（防更新丢失，保留最近一份）；
- apply_fold()：写入合并后的新 facts 并清空 new/turns；**提取失败不调用本方法**（原文保留降级重试）。

各方法的 owner 形参为兼容历史调用点而保留，实际存储以实例文件为准（per-uid 实例无需再按 owner 分桶）。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path


class SessionMemory:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict = {"facts": {}, "new": [], "turns": [],
                            "last_compact_at": 0, "compact_hint_at": 0}
        self._load()

    # ---- 加载与迁移 ----

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("turns"), list):
            self._data = self._normalize(raw)
            return
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            # 兼容旧全局布局（sessions.json 整体搬迁）里的单桶数据
            for bucket in sessions.values():
                if isinstance(bucket, dict) and isinstance(bucket.get("turns"), list):
                    self._data = self._normalize(bucket)
                    return

    def _normalize(self, raw: dict) -> dict:
        """结构规整 + 旧结构一次性迁移：旧 summary 并入 facts 一条；旧 memory.json 字段并入 facts。"""
        facts = raw.get("facts")
        facts = dict(facts) if isinstance(facts, dict) else {}
        if "facts" not in raw:
            # 旧结构（10.16 §A2 之前）：叙事摘要保留为一条 fact，待下次 fold 吸收
            old_sum = str(raw.get("summary", "") or "").strip()
            if old_sum:
                facts["既往对话摘要"] = old_sum
            legacy = self.path.parent / "memory.json"   # 旧 UserMemory（user_memory.py 已删）并入
            if legacy.exists():
                try:
                    mraw = json.loads(legacy.read_text(encoding="utf-8"))
                    if isinstance(mraw, dict):
                        buckets = ([mraw] if not isinstance(mraw.get("users"), dict)
                                   else list(mraw["users"].values()))
                        for bucket in buckets:
                            if isinstance(bucket, dict):
                                facts.update({str(k): str(v) for k, v in bucket.items() if v})
                                break
                    legacy.rename(legacy.with_name(legacy.name + ".bak"))  # 迁移完改名，防重复导入
                except Exception:
                    pass
        new = raw.get("new")
        new = [str(x) for x in new if str(x).strip()] if isinstance(new, list) else []
        turns = [t for t in raw.get("turns", []) if isinstance(t, dict)]
        return {"facts": facts, "new": new, "turns": turns,
                "last_compact_at": int(raw.get("last_compact_at", 0) or 0),
                "compact_hint_at": int(raw.get("compact_hint_at", 0) or 0)}

    # ---- 读 ----

    def facts(self, owner: str) -> dict:
        return dict(self._data["facts"])

    def new_entries(self, owner: str) -> list[str]:
        return list(self._data["new"])

    def all_turns(self, owner: str) -> list[dict]:
        return list(self._data["turns"])

    def turns_count(self) -> int:
        return len(self._data["turns"])

    # ---- 写 ----

    def add(self, owner: str, role: str, content: str) -> None:
        self._data["turns"].append({"role": role, "content": content,
                                    "ts": dt.datetime.now().isoformat(timespec="minutes")})

    def add_new(self, owner: str, sentence: str) -> None:
        """remember_user 工具写入：待固化区尾部追加一句（每轮随上下文注入，fold 时并入 facts）。"""
        s = (sentence or "").strip()
        if not s:
            return
        self._data["new"].append(s)
        self.save()

    def snapshot(self) -> None:
        """fold 前快照：整个 session.json → session.json.bak（防更新丢失，保留最近一份）。"""
        if not self.path.exists():
            return
        try:
            shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
        except OSError:
            pass

    def apply_fold(self, owner: str, new_facts: dict) -> None:
        """fold 落盘：写入合并后的 facts，清空 new/turns；压缩提醒记位一并重置（进入新周期）。"""
        self._data["facts"] = {str(k): str(v) for k, v in (new_facts or {}).items()
                               if str(v).strip()}
        self._data["new"] = []
        self._data["turns"] = []
        self._data["last_compact_at"] = 0
        self._data["compact_hint_at"] = 0
        self.save()

    def take_compact_hint(self, owner: str, threshold: int) -> int | None:
        """软压缩提醒（10.16 §A3）：turns 达阈值且本周期未提醒过 → 记位并返回当前轮数；否则 None。

        记位在 apply_fold 时重置——一个压缩周期最多提醒一次，不重复刷屏。
        """
        if threshold <= 0:
            return None
        n = len(self._data["turns"])
        if n >= threshold and int(self._data.get("compact_hint_at") or 0) < threshold:
            self._data["compact_hint_at"] = n
            self.save()
            return n
        return None

    # ---- 上下文组装 ----

    def prompt_context(self, owner: str, recent: int = 0) -> list[dict]:
        """喂给 LLM 的上下文（注入序见模块 docstring 缓存契约）：facts → new → turns。

        recent>0 时仅取最近 N 轮（兼容旧窗口语义，默认不用——窗口滑动会逐轮击穿前缀缓存）。
        """
        rows: list[dict] = []
        facts = self._data["facts"]
        if facts:
            block = "\n".join(f"{k}：{v}" for k, v in facts.items() if v)
            if block:
                rows.append({"role": "system", "content": "【长期记忆】\n" + block})
        if self._data["new"]:
            rows.append({"role": "system",
                         "content": "【待固化记忆】\n" + "\n".join(f"- {s}" for s in self._data["new"])})
        turns = self._data["turns"]
        if recent and recent > 0:
            turns = turns[-recent:]
        for t in turns:
            row = {"role": t["role"], "content": t["content"]}
            # 仅给 user 轮附时间戳，让模型能判断"这条是多久前发的"；assistant 轮不附省 token
            ts = t.get("ts")
            if t["role"] == "user" and ts:
                try:
                    m, d, hh, mm = (ts[5:7], ts[8:10], ts[11:13], ts[14:16])
                    row["content"] = f"[{m}-{d} {hh}:{mm}] {t['content']}"
                except Exception:
                    pass
            rows.append(row)
        return rows

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # 原子替换，避免写一半损坏
