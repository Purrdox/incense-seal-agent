"""scheduler/proactive_gate.py：主动触发的唯一出口（纯规则，这层绝不调大模型）。

勿扰/占用由 scheduler 每轮扫描 focus 事项写入 `OccupiedState`（BUSY/HOLD）：
gate 只查状态，不碰存储、不调大模型。

多用户：每个 owner 各自建独立 gate（独立 occ + 独立 mute），
A 触发/勿扰绝不消耗 B 的额度。主动关怀的频率由 app 层的日上限 + 最小间隔治理，
提醒只受 mute / 勿扰约束（不做限频）。
"""
from __future__ import annotations

import time

from qqlife.agent_core.types import NotifyResult, NotifyTask
from qqlife.scheduler.occupied_state import OccupiedState, State


class ProactiveGate:
    def __init__(self, occupied: OccupiedState, cfg) -> None:
        self.occ = occupied
        self.cfg = cfg
        self.muted = False
        self.mute_until = 0.0
        self.foreground_heavy = lambda: False   # 由 foreground 注入

    def try_notify(self, task: NotifyTask) -> NotifyResult:
        now = time.time()
        self.occ.tick(now)
        if self.muted or now < self.mute_until:
            return NotifyResult.REJECT
        if self.foreground_heavy():
            return NotifyResult.DEFER
        if self.occ.state in (State.BUSY,) and task.kind == "care":
            return NotifyResult.DROPPED   # 勿扰/占用中：只拦"模型主动找话"的关怀；定时提醒/自动唤醒照常
        return NotifyResult.SENT
