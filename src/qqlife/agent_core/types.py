"""agent_core/types.py：事件类型、消息信封与通用结果类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(Enum):
    """订阅式事件总线的关键事件。"""

    MESSAGE_INBOUND = "message_inbound"      # 收到用户消息（被动）
    REMINDER_DUE = "reminder_due"            # 一次性/重复提醒到期
    TASK_COMPLETED = "task_completed"        # 任务完成
    OCCUPANCY_CHANGED = "occupancy_changed"  # 占用状态变化
    ACTIVE_ENTER = "active_enter"            # 进入激发态
    ACTIVE_EXIT = "active_exit"              # 退出激发态
    PROACTIVE_TICK = "proactive_tick"        # 空白时间随机轮询 tick
    SHUTDOWN = "shutdown"                    # 关闭


@dataclass
class InboundMessage:
    """上行消息信封（平台无关）。"""

    platform: str          # "onebot" 等
    target: str            # 发送者QQ（唯一用户）
    text: str
    raw: Any = None
    time: float = 0.0
    images: list = field(default_factory=list)  # 图片附件：[{"mime","b64"}]（data:/URL 内联用）


@dataclass
class NotifyTask:
    """一次主动触达的候选。"""

    kind: str              # reminder / plan / care / oneoff
    content: str           # 已拟好的内容（skills 产出）
    target: str
    trigger: str = ""      # 触发原因描述（日志用）
    draft: str = ""        # 已现场拟稿的成品；重试只重发、不再重新拟稿


class NotifyResult(Enum):
    SENT = "sent"
    REJECT = "reject"      # 闸门拒绝（勿扰/频率/静默）
    DEFER = "defer"        # 前台重型→推迟
    DROPPED = "dropped"    # 无需发出


@dataclass
class SkillResult:
    """技能执行结果（或产物）。"""

    ok: bool = True
    output: Any = None           # 结构化产物 / 动作包
    reply: str = ""              # 可直接发回用户的话
    messages: list = field(default_factory=list)  # 长文本分多段：逐段发送（QQ 拆条）；空则回退用 reply
    raw: str = ""             # 模型原始输出全文（含 <output>/<thinking> 等标签，供 CLI/调试显示）
    voices: list = field(default_factory=list)  # 语音段：[{"voice":"calm","text":"en..."}]（用户要求语音时）
    write_to_store: dict = field(default_factory=dict)  # 待持久化的键值