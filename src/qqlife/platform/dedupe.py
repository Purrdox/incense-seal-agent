"""platform/dedupe.py：消息去重（借鉴 MaiBot dedupe 的极简版）。"""
from __future__ import annotations

import time
import hashlib


class Deduper:
    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._seen: dict[str, float] = {}

    def is_dup(self, channel: str, remote_id: str, text: str, msg_time: float = 0.0) -> bool:
        """按「通道 + 端点 + 文本」去重：不同用户发相同文本互不影响。"""
        key = hashlib.md5(f"{channel}:{remote_id}:{text}".encode("utf-8")).hexdigest()
        now = msg_time or time.time()
        # 顺手清掉窗口外的 key，避免长期运行后 _seen 只增不减
        cutoff = now - self.window
        self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
        last = self._seen.get(key)
        if last is not None and now - last < self.window:
            return True
        self._seen[key] = now
        return False


deduper = Deduper()