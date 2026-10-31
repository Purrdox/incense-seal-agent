"""task_flow/models.py：结构化数据模型。

统一为单一"事项 Item"（取代原 Course/Reminder/Task）：
- type=regular 常规（周期进行，sched 表达日/周/月规律）或 once 临时（一次性，due 绝对到期）
- 都带 note(单条备注) / notes(开放笔记列表)
- focus 标记占用（上课、重要会议等"不该被打扰"）
- 均为 dict 友好的 dataclass，到 store 存 dict，避免强依赖 pydantic。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    id: str
    content: str = ""            # 事项内容/名称
    owner: str = ""              # 归属用户(QQ号)
    type: str = "once"           # regular 常规 / once 临时
    # 常规(regular)的周期规律：""=一次性；"D"=每天；"1,3,5"=每周一三五(1-7)；"1,15"=每月1、15号
    sched: str = ""
    # 一次性(once)的绝对到期 ISO
    due: str = ""
    # 通用
    start: str = ""              # 时段开始 "08:00"（占用/日程展示）
    end: str = ""
    focus: bool = False          # 占用/优先：上课、重要会议等不该被打扰
    note: str = ""               # 单条重要备注
    notes: list = field(default_factory=list)      # 开放笔记 [{id, ts, content}]
    status: str = "active"       # active 进行中 / done 完成·终态（英文 token，程序与 AI 共用）
    done_at: str = ""            # 置为 done 的时刻 ISO，供确认"几点完成/已提醒"
    ref: str = ""                # 触发原因/关联描述
    last_fired: str = ""         # 常规事项最后一次触发的日期 YYYY-MM-DD（当日去重）
    created: str = ""            # 建档时刻 ISO（调度器据此判断"今天该时刻是否已过"，避免新建即触发）

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            owner=d.get("owner", ""),
            type=d.get("type", "once"),
            sched=d.get("sched", ""),
            due=d.get("due", ""),
            start=d.get("start", ""),
            end=d.get("end", ""),
            focus=bool(d.get("focus", False)),
            note=d.get("note", ""),
            notes=d.get("notes", []),
            status=d.get("status", "active"),
            done_at=d.get("done_at", ""),
            ref=d.get("ref", ""),
            last_fired=d.get("last_fired", ""),
            created=d.get("created", ""),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "content": self.content, "owner": self.owner, "type": self.type,
                "sched": self.sched, "due": self.due, "start": self.start, "end": self.end,
                "focus": self.focus, "note": self.note, "notes": self.notes,
                "status": self.status, "done_at": self.done_at, "ref": self.ref,
                "last_fired": self.last_fired, "created": self.created}

    @property
    def is_done(self) -> bool:
        """终态判定：英文 status 统一入口。"""
        return self.status != "active"

    @property
    def due_epoch(self) -> float:
        """一次性事项的绝对到期 epoch（用于调度）；无 due 返回 0。

        Python 3.10 的 fromisoformat 不认结尾的 `Z`，先换成 `+00:00` 再解析。
        """
        import datetime as dt
        try:
            return dt.datetime.fromisoformat((self.due or "").replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0