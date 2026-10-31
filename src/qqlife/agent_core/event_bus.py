"""agent_core/event_bus.py：订阅式事件总线（借鉴 MaiBot core/event_bus 的极简实现）。

- 纯 async handler，不引入继承；按注册权重排序（拦截型 weight>0 可拦后续同事件）。
"""
from __future__ import annotations

import asyncio
from bisect import insort_right
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from qqlife.agent_core.types import EventType

Handler = Callable[[EventType, Any], Awaitable[Any]]


@dataclass(order=True)
class _Entry:
    weight: int
    order: int = field(compare=False)
    fn: Handler = field(compare=False)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[_Entry]] = {}
        self._seq = 0

    def subscribe(self, event: EventType, fn: Handler, *, weight: int = 0) -> None:
        """weight>0：拦截型（若返回 False 则不继续广播给后续 handler）。"""
        self._seq += 1
        insort_right(self._handlers.setdefault(event, []), _Entry(weight, self._seq, fn))

    async def emit(self, event: EventType, data: Any = None) -> bool:
        """广播事件并返回处理结果。

        返回 True 表示至少一个 handler 明确返回 True 且无失败；
        handler 返回 False 或抛异常均视为失败（调用方据此决定是否重试）；
        返回 None 的旁路 handler（日志/调试）不改变成功判定。
        """
        ok = False
        for e in list(self._handlers.get(event, ())):
            try:
                r = await e.fn(event, data)
            except asyncio.CancelledError:
                raise
            except Exception:  # 一个 handler 出错不影响其余，但整体视为失败
                return False
            if r is False:
                return False
            if r is True:
                ok = True
        return ok


event_bus = EventBus()