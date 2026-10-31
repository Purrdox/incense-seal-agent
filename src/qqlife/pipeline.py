"""qqlife/pipeline.py：入站消息的纯处理件（长文拆条 + 连续消息聚合）。

从 app.py 拆出，保持无状态、无外部 I/O，可直接单测。
"""
from __future__ import annotations

import asyncio
import re

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LEADING = ("提醒我", "提醒TA", "提醒她", "提醒他", "提醒", "记得", "别忘了")


def extract_title(raw_output: str) -> str:
    """从模型原始输出里取 `<title>...</title>`；没有则返回空串。"""
    m = _TITLE_RE.search(raw_output or "")
    if not m:
        return ""
    title = m.group(1).strip().strip("【】[]（）() ").strip()
    return title[:20]


def derive_title(content: str) -> str:
    """兜底标题：从事项名清洗出用户视角短标题，并补「提醒」。

    仅用于模型漏写 `<title>` 的情况；正常路径由模型生成标题。
    """
    s = (content or "").strip()
    for p in _LEADING:
        if s.startswith(p):
            s = s[len(p):].strip(" ：:，,。.!！")
            break
    s = s.strip("【】[]（）() ：:，,。.!！")
    if not s:
        return "提醒"
    if len(s) > 12:
        s = s[:12] + "…"
    return s if s.endswith("提醒") else s + "提醒"


def format_reminder(title: str, body: str, label: str = "【{title}】") -> str:
    """按 `【标题】正文` 组装最终提醒；剥离误写进正文的 `<title>`，且不重复加前缀。"""
    title = (title or "").strip().strip("【】 ").strip()
    body = _TITLE_RE.sub("", body or "").strip()
    if not title:
        return body
    try:
        tag = label.format(title=title) if label else f"【{title}】"
    except (KeyError, IndexError, ValueError):
        tag = f"【{title}】"
    if body.startswith(tag):
        return body
    return tag + body


def chunks(text: str, size: int) -> list[str]:
    """按字符数把一段切成多段（兜底，防止单条超长发不出）。

    优先在换行/句号/叹号等自然边界切，避免在句子中间硬切；仍无边界才按 size 硬切。
    python 切片按码点，不会拆散 emoji。返回的每段长度 ≤ size（至少在 size 内）。
    """
    size = max(1, size)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        j = min(i + size, n)
        if j < n:
            # 向左找最近的换行/句末，尽量在语义边界断开
            newline = text.rfind("\n", i, j)
            if newline > i:
                j = newline
            else:
                for sep in ("。", "！", "？", "！", "\n"):
                    k = text.rfind(sep, i, j)
                    if k > i:
                        j = k + 1
                        break
        out.append(text[i:j])
        i = j
    return out


class MsgAggregator:
    """连续无句号消息的合并缓冲（滑动等待窗口）。

    规则：
    - 收到不以句号结尾的消息 → 入缓冲，重置计时，等 seconds 秒后若无新消息则把整批 flush；
    - 新消息到来会重置计时（可连续累积多条）；
    - 收到以句号结尾的消息（或 seconds<=0）→ 立即把缓冲中已累积的连同本条一起 flush。
    一批消息由 on_batch 经 _flush 处理（key=端点），text 用换行拼接。
    """

    def __init__(self, seconds: float, on_batch) -> None:
        self.seconds = float(seconds)
        self.on_batch = on_batch
        self._buckets: dict[str, list[str]] = {}
        self._timers: dict[str, asyncio.Task] = {}

    @staticmethod
    def _ends(text: str) -> bool:
        """句尾结束符：中英文句号/问号/叹号。以其中之一收尾的消息视为"完整"，立即收批。"""
        return text.rstrip().endswith(("。", ".", "？", "?", "！", "!"))

    async def put(self, owner: str, text: str) -> None:
        if not text.strip():
            return
        self._buckets.setdefault(owner, []).append(text)
        if self.seconds <= 0 or self._ends(text):
            await self.flush(owner)
            return
        t = self._timers.get(owner)
        if t and not t.done():
            t.cancel()
        self._timers[owner] = asyncio.create_task(self._delayed_flush(owner))

    async def _delayed_flush(self, owner: str) -> None:
        try:
            await asyncio.sleep(self.seconds)
            await self.flush(owner)
        except asyncio.CancelledError:
            pass

    async def flush(self, owner: str) -> None:
        # 取走挂起 timer：flush 可能由该 timer 自己触发，此时 pop 到的正是当前正在运行的任务，
        # 不能 cancel 自身（否则后面的 await on_batch 会抛 CancelledError 导致不回复）。
        # 但被 pop 的若是一条**已作废的旧 timer**，必须 cancel——否则它到点会调用 flush，
        # 把后续新累积的批次提前收批（连续打字被拆成两条回复）。故只 cancel 非当前任务。
        t = self._timers.pop(owner, None)
        if t is not None and t is not asyncio.current_task():
            t.cancel()
        parts = self._buckets.pop(owner, None)
        if not parts:
            return
        await self.on_batch(owner, "\n".join(parts))
