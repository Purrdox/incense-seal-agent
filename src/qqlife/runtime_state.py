"""qqlife/runtime_state.py：全局"当前运行状态"快照，供 .status 读取。

各处理阶段（等模型/调工具/联网搜索/完成）写这里，.status 在指令间隙读它。
无需额外并发（异步单事件循环内共享即可）。
"""
from __future__ import annotations

import time

_STATE: dict = {"status": "空闲", "detail": "", "at": "", "_ts": 0.0}


def set_state(status: str, detail: str = "") -> None:
    """写入当前状态；进入非"空闲"时记录起始时刻，回到空闲则清除。"""
    _STATE["status"] = status
    _STATE["detail"] = detail
    if status != "空闲":
        _STATE["at"] = time.strftime("%H:%M:%S")
        _STATE["_ts"] = time.time()
    else:
        _STATE["at"] = ""
        _STATE["_ts"] = 0.0


def snapshot() -> dict:
    """返回只读快照，含已持续秒数。"""
    d = dict(_STATE)
    d["elapsed"] = int(time.time() - d["_ts"]) if d["_ts"] else 0
    return d