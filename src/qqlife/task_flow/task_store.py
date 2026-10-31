"""task_flow/task_store.py：按用户存储的单一"事项"存储（+ 记账）。

- items.json：全部事项（常规/临时）。程序与 AI 共用的 key/枚举一律英文（命名契约）。
- expenses.json：账本。

id 用持久化自增计数（每文件带 next_id），删除后不复用旧 id。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path

from qqlife._paths import DATA_DIR
from qqlife.task_flow.models import Item

STORE_ROOT = DATA_DIR   # uid 家目录直接挂 data/<uid>/（无 users/ 中间层）
_log = logging.getLogger("default.task_store")


class TaskStore:
    def __init__(self, root: Path | None = None, owner: str = "me") -> None:
        self.owner = str(owner or "me")
        self.root = root or STORE_ROOT / self.owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: list[Item] = []
        self.expenses: list[dict] = []
        self.corrupt_files: list[tuple[str, str]] = []   # [(文件名, 原因)]，供上层向用户告警
        self._item_seq = 1
        self._expense_seq = 1
        self.load_all()

    @classmethod
    def for_owner(cls, owner: str) -> "TaskStore":
        return cls(owner=owner)

    def _write_atomic(self, name: str, obj: dict) -> None:
        path = self.root / name
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _read(self, name: str, default: dict) -> dict:
        """读 JSON；解析失败先备份原文件再按空表继续（绝不能直接覆盖坏文件）。"""
        path = self.root / name
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._backup_corrupt(path)
            reason = f"{type(e).__name__}: {e}"
            self.corrupt_files.append((path.name, reason))
            _log.warning("%s 解析失败（%s），已备份为 %s.corrupt-*，按空表继续",
                         path.name, reason, path.name)
            return default
        if not isinstance(data, dict):
            self._backup_corrupt(path)
            reason = f"顶层不是对象（{type(data).__name__}）"
            self.corrupt_files.append((path.name, reason))
            _log.warning("%s %s，已备份为 %s.corrupt-*，按空表继续",
                         path.name, reason, path.name)
            return default
        return data

    @staticmethod
    def _backup_corrupt(path: Path) -> None:
        """坏文件改名保留，供人工恢复；改名前不删除、不覆盖。"""
        backup = path.with_name(f"{path.name}.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S-%f}")
        try:
            path.replace(backup)
        except OSError as e:
            _log.warning("%s 备份失败：%s: %s", path.name, type(e).__name__, e)

    @staticmethod
    def _seq(value, fallback: int) -> int:
        """next_id 兜底：非法/非正数回退到由现有数据推算的值。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return fallback
        return n if n > 0 else fallback

    # ---- 加载/保存 ----
    def load_all(self) -> None:
        id_ = self._read("items.json", {})
        raw_items = id_.get("items", [])
        if not isinstance(raw_items, list):
            raw_items = []
        self.items = [Item.from_dict(x) for x in raw_items if isinstance(x, dict)]
        self._item_seq = self._seq(id_.get("next_id"), self._max_num(self.items) + 1)
        ed = self._read("expenses.json", {})
        raw_ledger = ed.get("ledger", [])
        self.expenses = ([x for x in raw_ledger if isinstance(x, dict)]
                         if isinstance(raw_ledger, list) else [])
        self._expense_seq = self._seq(ed.get("next_id"), 1)

    @staticmethod
    def _max_num(items) -> int:
        """取现有 Item 最大数字后缀（id 形如 i1/i2…），用于旧数据续计。"""
        m = 0
        for it in items:
            rid = getattr(it, "id", "") or ""
            if isinstance(rid, str) and rid.startswith("i"):
                try:
                    m = max(m, int(rid[1:]))
                except ValueError:
                    pass
        return m

    def save_items(self) -> None:
        self._write_atomic("items.json", {"next_id": self._item_seq,
                                          "items": [i.to_dict() for i in self.items]})

    def save_expenses(self) -> None:
        self._write_atomic("expenses.json", {"next_id": self._expense_seq, "ledger": self.expenses})

    # ---- 事项操作 ----
    def add_item(self, owner: str, content: str, type_: str = "once", due: str = "",
                 sched: str = "", start: str = "", end: str = "", focus: bool = False,
                 note: str = "", status: str = "active") -> str:
        iid = f"i{self._item_seq}"
        self._item_seq += 1
        self.items.append(Item(id=iid, content=content, owner=owner, type=type_,
                               due=due, sched=sched, start=start, end=end,
                               focus=focus, note=note, status=status,
                               created=dt.datetime.now().isoformat(timespec="seconds")))
        self.save_items()
        return iid

    # ---- 记账（英文 key）----
    def add_expense(self, amount: float, category: str = "", purpose: str = "", date: str = "") -> str:
        today = date or dt.date.today().isoformat()
        eid = f"e{self._expense_seq}"
        self._expense_seq += 1
        self.expenses.append({"id": eid, "date": today, "amount": round(float(amount or 0), 2),
                              "category": (category or "other").strip() or "other",
                              "purpose": (purpose or "").strip()})
        self.save_expenses()
        return eid

    def update_expense(self, eid: str, amount=None, category=None, purpose=None, note=None) -> bool:
        for e in self.expenses:
            if e.get("id") == eid:
                if amount is not None:
                    try:
                        e["amount"] = round(float(amount), 2)
                    except (TypeError, ValueError):
                        return False
                if category is not None:
                    e["category"] = (category or "other").strip() or "other"
                if purpose is not None:
                    e["purpose"] = (purpose or "").strip()
                if note is not None:
                    e["note"] = (note or "").strip()
                self.save_expenses()
                return True
        return False

    def delete_expense(self, eid: str) -> bool:
        for i, e in enumerate(self.expenses):
            if e.get("id") == eid:
                del self.expenses[i]
                self.save_expenses()
                return True
        return False

    def list_expenses(self, date: str = "") -> dict:
        rows = [e for e in self.expenses if (not date or e.get("date") == date)]
        return {"date": date or "all", "count": len(rows),
                "total": round(sum(float(e.get("amount", 0)) for e in rows), 2), "rows": rows}