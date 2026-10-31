"""llm/budget.py：分钟/会话级 token 上限 + 循环检测（防死循环无限消耗，§2.8）。"""
from __future__ import annotations

import time


class BudgetExceeded(RuntimeError):
    """本地额度拦截：本次调用被分钟/会话 token 上限拦下（**非**网络故障）。

    继承 RuntimeError：既有的 `except RuntimeError` 兜底路径（如 app.py 的限流回复）不受影响；
    调用方需区分时用 `except BudgetExceeded` 捕获，避免把额度拦截误报成"网络不稳"并做无效重试。
    """


class Budget:
    def __init__(self, minute_cap: int = 60000, session_cap: int = 2_000_000,
                 window: float = 60.0) -> None:
        self.minute_cap = minute_cap
        self.session_cap = session_cap
        self.window = window
        self._used_minute: list[tuple[float, int]] = []
        self._used_session = 0

    def try_consume(self, tokens: int) -> bool:
        now = time.time()
        self._used_minute = [(t, n) for t, n in self._used_minute if now - t < self.window]
        if sum(n for _, n in self._used_minute) + tokens > self.minute_cap:
            return False
        if self._used_session + tokens > self.session_cap:
            return False
        self._used_minute.append((now, tokens))
        self._used_session += tokens
        return True