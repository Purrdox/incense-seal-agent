"""EventBus：emit 返回值语义（批次 A 的投递成败判定基础）。"""
from __future__ import annotations

from qqlife.agent_core.event_bus import EventBus
from qqlife.agent_core.types import EventType


def _handler(result=None, *, raises: bool = False, calls: list | None = None, name: str = ""):
    async def h(event, data):
        if calls is not None:
            calls.append(name)
        if raises:
            raise RuntimeError("boom")
        return result
    return h


async def test_no_handler_is_not_delivered():
    assert await EventBus().emit(EventType.SHUTDOWN) is False


async def test_none_handler_does_not_count_as_success():
    bus = EventBus()
    bus.subscribe(EventType.SHUTDOWN, _handler(None))
    assert await bus.emit(EventType.SHUTDOWN) is False


async def test_none_bypass_plus_true_is_success():
    bus = EventBus()
    bus.subscribe(EventType.SHUTDOWN, _handler(None))
    bus.subscribe(EventType.SHUTDOWN, _handler(True))
    assert await bus.emit(EventType.SHUTDOWN) is True


async def test_false_stops_and_fails():
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe(EventType.SHUTDOWN, _handler(False, calls=calls, name="first"))
    bus.subscribe(EventType.SHUTDOWN, _handler(True, calls=calls, name="second"))
    assert await bus.emit(EventType.SHUTDOWN) is False
    assert calls == ["first"]


async def test_exception_fails_and_stops():
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe(EventType.SHUTDOWN, _handler(raises=True, calls=calls, name="first"))
    bus.subscribe(EventType.SHUTDOWN, _handler(True, calls=calls, name="second"))
    assert await bus.emit(EventType.SHUTDOWN) is False
    assert calls == ["first"]


async def test_handlers_run_in_weight_order():
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe(EventType.SHUTDOWN, _handler(True, calls=calls, name="w0-a"), weight=0)
    bus.subscribe(EventType.SHUTDOWN, _handler(True, calls=calls, name="w5"), weight=5)
    bus.subscribe(EventType.SHUTDOWN, _handler(True, calls=calls, name="w0-b"), weight=0)
    assert await bus.emit(EventType.SHUTDOWN) is True
    assert calls == ["w0-a", "w0-b", "w5"]
