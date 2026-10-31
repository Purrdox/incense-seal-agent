"""scheduler/occupied_state.py：占用状态机（三态 FREE/BUSY/ACTIVE，§2.2）。

BUSY 包含子类型(IN_CLASS/SLEEP/ON_TASK/HOLD)，但对上层只暴露"静默"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(Enum):
    FREE = "free"
    BUSY = "busy"
    ACTIVE = "active"


class BusyKind(Enum):
    SLEEP = "sleep"
    IN_CLASS = "in_class"
    ON_TASK = "on_task"
    HOLD = "hold"


@dataclass
class OccupiedState:
    state: State = State.FREE
    busy_kind: BusyKind | None = None
    busy_until: float = 0.0        # BUSY 到期 epoch
    hold_until: float = 0.0        # HOLD 暂缓到期 epoch
    active_until: float = 0.0      # ACTIVE 到期 epoch
    on_task_id: str | None = None
    _sub: dict = field(default_factory=dict)  # 供 task_flow 记录的额外信息

    @property
    def silent(self) -> bool:
        """上层只问这一个：当前是否静默（BUSY 一律静默）。"""
        return self.state is State.BUSY

    def begin_task(self, task_id: str, until: float) -> None:
        self.state = State.BUSY
        self.busy_kind = BusyKind.ON_TASK
        self.on_task_id = task_id
        self.busy_until = until

    def begin_hold(self, until: float) -> None:
        self.state = State.BUSY
        self.busy_kind = BusyKind.HOLD
        self.hold_until = until

    def begin_active(self, until: float) -> None:
        self.state = State.ACTIVE
        self.active_until = until

    def tick(self, now: float) -> None:
        """推进时间：BUSY/ACTIVE 到期自动回 FREE。"""
        if self.state is State.BUSY:
            if self.busy_kind in (BusyKind.ON_TASK, BusyKind.SLEEP, BusyKind.IN_CLASS) and now >= self.busy_until:
                self.state = State.FREE
                self.busy_kind = None
            elif self.busy_kind is BusyKind.HOLD and now >= self.hold_until:
                self.state = State.FREE
                self.busy_kind = None
        elif self.state is State.ACTIVE and now >= self.active_until:
            self.state = State.FREE