"""ProactiveGate：提醒不做限频；仅 mute / 前台重型 / BUSY 拦关怀。"""
from __future__ import annotations

import time

from qqlife.agent_core.types import NotifyResult, NotifyTask
from qqlife.scheduler.occupied_state import BusyKind, OccupiedState, State
from qqlife.scheduler.proactive_gate import ProactiveGate


def _task(kind: str) -> NotifyTask:
    return NotifyTask(kind=kind, content="x", target="me")


def _gate() -> ProactiveGate:
    return ProactiveGate(OccupiedState(), object())


def test_reminders_are_not_rate_limited():
    gate = _gate()
    # 批次 A 回归：同一分钟内连续多条提醒必须全部放行
    assert all(gate.try_notify(_task("reminder")) is NotifyResult.SENT for _ in range(50))


def test_mute_rejects_everything():
    gate = _gate()
    gate.muted = True
    assert gate.try_notify(_task("reminder")) is NotifyResult.REJECT
    assert gate.try_notify(_task("care")) is NotifyResult.REJECT


def test_mute_until_rejects_before_expiry():
    gate = _gate()
    gate.mute_until = time.time() + 60
    assert gate.try_notify(_task("reminder")) is NotifyResult.REJECT


def test_foreground_heavy_defers():
    gate = _gate()
    gate.foreground_heavy = lambda: True
    assert gate.try_notify(_task("reminder")) is NotifyResult.DEFER


def test_busy_only_drops_care():
    gate = _gate()
    gate.occ.state = State.BUSY
    gate.occ.busy_kind = BusyKind.IN_CLASS
    gate.occ.busy_until = time.time() + 600   # 未到期，tick 不会把状态清回 FREE
    assert gate.try_notify(_task("care")) is NotifyResult.DROPPED
    assert gate.try_notify(_task("reminder")) is NotifyResult.SENT
    assert gate.try_notify(_task("oneoff")) is NotifyResult.SENT
