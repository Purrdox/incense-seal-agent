"""scheduler/scheduler.py：事件驱动定时器 + 空白时间随机轮询（可配概率）。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
import time

from qqlife.agent_core.event_bus import event_bus
from qqlife.agent_core.types import EventType, NotifyTask
from qqlife.scheduler.occupied_state import BusyKind, OccupiedState, State
from qqlife.scheduler.proactive_gate import ProactiveGate

from qqlife.task_flow.task_store import TaskStore

_log = logging.getLogger("default.scheduler")

# 英文周几缩写 / 中文周几 → isoweekday（1=周一..7=周日）
_WEEKDAY_MAP = {
    "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 7,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
}
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


class Scheduler:
    _MAX_ATTEMPTS = 3
    _RETRY_BACKOFF = (60.0, 300.0)   # 第 1/2 次失败后的等待；第 3 次失败即放弃

    def __init__(self, store: TaskStore, occ: OccupiedState, gate: ProactiveGate, cfg,
                 *, on_alert=None, on_backlog=None) -> None:
        self.store = store
        self.occ = occ
        self.gate = gate
        self.cfg = cfg
        # 告警出口（投递失败超限）/ 补发汇总（启动时跳过的过期提醒数）由 app 注入
        self.on_alert = on_alert
        self.on_backlog = on_backlog
        self._backlog_applied = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # ---- 提醒投递状态（内存态；成功落盘后才清理）----
        self._pending: dict[str, NotifyTask] = {}   # 待投递/待重试（item id -> 任务，复用实例以缓存拟稿）
        self._inflight: set[str] = set()            # 正在投递中的 item id
        self._attempts: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._fire_day: dict[str, str] = {}         # 首次发现到期的日期（常规项成功后写入 last_fired）
        self._deliver_tasks: set[asyncio.Task] = set()   # 持强引用，防任务被 GC
        self._deliver_sem = asyncio.Semaphore(1)          # 每 uid 串行投递，跨 uid 天然并行
        self._stale_logged: set[str] = set()              # 已记录过"建档即过期"日志的 item id

    async def start(self) -> None:
        if not self._backlog_applied:
            self._backlog_applied = True
            try:
                await self._apply_backlog_policy()
            except Exception as e:
                _log.warning("启动补发策略执行失败：%s: %s", type(e).__name__, e)
        self._tasks.append(asyncio.create_task(self._check_loop()))
        self._tasks.append(asyncio.create_task(self._proactive_loop()))

    async def _apply_backlog_policy(self) -> None:
        """重启后只补发窗口内的过期提醒；更早的标记 done 并汇总告知，避免开机集中刷屏。

        只处理 `once` 项：常规项按当天 sched + last_fired 扫描，不存在「积压」概念。
        仅在调度器启动时执行一次，运行期新建的过期项仍按原逻辑立即触发。
        """
        hours = float(getattr(self.cfg, "replay_window_hours", 24) or 0)
        if hours <= 0:
            return
        now = dt.datetime.now()
        cutoff = now.timestamp() - hours * 3600
        skipped = 0
        for it in self.store.items:
            if it.type != "once" or it.is_done or not it.due_epoch:
                continue
            if it.due_epoch >= cutoff:
                continue
            it.status = "done"
            it.done_at = now.isoformat(timespec="seconds")
            skipped += 1
        if not skipped:
            return
        self.store.save_items()
        _log.warning("跳过 %d 条超过 %g 小时的过期提醒（uid=%s）",
                     skipped, hours, self.store.owner)
        if self.on_backlog:
            try:
                await self.on_backlog(skipped)
            except Exception as e:
                _log.warning("补发汇总通知失败：%s: %s", type(e).__name__, e)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._deliver_tasks:
            t.cancel()

    async def _check_loop(self) -> None:
        """事件驱动：每秒检查一次提醒是否到期 + 刷新勿扰（focus）占用状态。"""
        while not self._stop.is_set():
            try:
                await self._check_due_items()
                self._apply_focus()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    @staticmethod
    def _sched_time(sched: str) -> str:
        """从 sched 里提取 HH:MM（模型可能把上课/提醒时刻写进 sched 而非 start）。无则空。"""
        m = _TIME_RE.search(sched or "")
        return f"{m.group(1)}:{m.group(2)}" if m else ""

    @staticmethod
    def _sched_hits(sched: str, when) -> bool:
        """常规事项是否命中"今天"。sched 支持：
        - 空/'D'              每天
        - '135'(1-7) / '2'    按 isoweekday（周一=1..周日=7）
        - 'M15'               每月15号
        - 'MON 13:40' / 'FRI' 英文周几（可带时刻）；中文'周五'/'五' 亦可
        - 'D 23:00'           每天（可带时刻）
        带时刻只影响"当日触发时刻"（见 _check_due_items），不参与周几判定，
        避免把时刻里的数字误当成周几（旧 bug：'TUE 13:40' 的 1/0 被误判命中周一）。
        """
        if not sched:
            return True
        up = sched.strip().upper()
        if up in ("D",) or up.startswith("D "):
            return True
        if up.startswith("M") and up[1:2].isdigit():
            # 月末「M15」：注意排除 "MON"(周一)，MON 以 M 开头但非月末
            try:
                return int(up[1:].split()[0]) == when.day
            except (ValueError, IndexError):
                return False
        # 英文周几缩写（MON..SUN），可带时刻
        for k in ("SUN", "SAT", "FRI", "THU", "WED", "TUE", "MON"):
            if up.startswith(k):
                return when.isoweekday() == _WEEKDAY_MAP[k]
        # 中文周几（周五/五/周5）
        for ch, wd in (("日", 7), ("天", 7), ("六", 6), ("五", 5), ("四", 4), ("三", 3), ("二", 2), ("一", 1)):
            if ("周" + ch) in up or up.startswith(ch):
                return when.isoweekday() == wd
        # 纯周几数字 '135'/'2'
        digits = {int(c) for c in up if c.isdigit()}
        if digits:
            return when.isoweekday() in digits
        # 未知格式：保守不触发，避免误发
        return False

    @staticmethod
    def _created_after(created: str, due_today: dt.datetime) -> bool:
        """建档时刻是否已晚于今天的触发时刻（仅同日比较）。

        用于"新建常规提醒"：18:12 建一条每天 08:00 的提醒，今天的 08:00 已经过去，
        不应立刻补响；从下一次（明天 08:00）开始生效。旧数据没有 created 字段时按原行为。
        """
        if not created:
            return False
        try:
            c = dt.datetime.fromisoformat(created)
        except (TypeError, ValueError):
            return False
        return c.date() == due_today.date() and c > due_today

    @staticmethod
    def _is_dt(value: str) -> bool:
        """start/end 是否是带日期的 ISO 时刻（含 T 或空格）；纯 HH:MM 返回 False。"""
        s = (value or "").strip()
        return "T" in s or " " in s

    @staticmethod
    def _parse_point(value: str, base: dt.date) -> "dt.datetime | None":
        """解析时刻：ISO 时刻原样解析；纯 HH:MM 用 base 补上日期。解析失败返回 None。

        带时区的 ISO（`+08:00` / `Z`）统一折算成本地朴素时间，避免和 `now` 比较时抛 TypeError。
        """
        s = (value or "").strip()
        if not s:
            return None
        if "T" in s or " " in s:
            try:
                d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if d.tzinfo is not None:
                d = d.astimezone().replace(tzinfo=None)
            return d
        try:
            hh, mm = (s.split(":") + ["0"])[:2]
            return dt.datetime.combine(base, dt.time(int(hh), int(mm)))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _created_after_epoch(created: str, due_epoch: float) -> bool:
        """一次性事项是否在"到点之后"才建档（留 60 秒容差，避免把刚建的"马上提醒"误杀）。"""
        if not created or not due_epoch:
            return False
        try:
            return dt.datetime.fromisoformat(created).timestamp() > due_epoch + 60
        except (TypeError, ValueError):
            return False

    async def _check_due_items(self) -> None:
        """只发现到期并派发后台投递任务；不在这里写 done / last_fired（成功后才落盘）。"""
        now = dt.datetime.now()
        today = now.date().isoformat()
        now_ts = now.timestamp()

        # 1) 先处理待重试队列：独立于“今天是否命中”，跨天失败也能补发
        for iid, task in list(self._pending.items()):
            if iid in self._inflight:
                continue
            it = self._find_item(iid)
            if it is None or it.is_done:   # 投递期间被删除/完成：不再补发
                self._clear(iid)
                continue
            if now_ts < self._retry_after.get(iid, 0.0):
                continue
            self._dispatch(iid, task, self._fire_day.get(iid, today))

        # 2) 再发现新到期
        for it in self.store.items:
            if it.is_done or it.focus:   # focus 事项只贡献勿扰，不触发提醒
                continue
            if it.id in self._pending:   # 已在投递/等待重试
                continue
            if it.type == "once":
                if not (it.due_epoch and now_ts >= it.due_epoch):
                    continue
                # 建档时该时刻就已过去（如 18:00 建当天 08:00 的提醒）→ 不补触发；工具层已拦，
                # 这里兜底旧数据/直接写库的路径。每条只记一次日志，避免每秒刷屏。
                if self._created_after_epoch(it.created, it.due_epoch):
                    if it.id not in self._stale_logged:
                        self._stale_logged.add(it.id)
                        _log.warning("跳过建档时已过期的一次性事项：item=%s due=%s created=%s",
                                     it.id, it.due, it.created)
                    continue
            else:
                # 常规：今天命中 + 今天还没触发过 + 到 start 时刻
                if not self._sched_hits(it.sched, now):
                    continue
                if it.last_fired == today:
                    continue
                # 到点才触发：优先 start，其次 sched 里带的 HH:MM（模型常把时刻写进 sched）
                t = it.start or self._sched_time(it.sched)
                if t:
                    hs, ms = (t.split(":") + ["0"])[:2]
                    due_today = now.replace(hour=int(hs), minute=int(ms), second=0, microsecond=0)
                    if now < due_today:
                        continue
                    # 新建项：今天的触发时刻在建档之前就已过去 → 今天不补触发（否则一存就响）
                    if self._created_after(it.created, due_today):
                        continue
            task = NotifyTask(kind="oneoff", content=it.content, target=it.owner, trigger=it.ref)
            self._pending[it.id] = task
            self._fire_day[it.id] = today
            self._dispatch(it.id, task, today)

    def _dispatch(self, iid: str, task: NotifyTask, fire_day: str) -> None:
        """派发一次后台投递；调度循环立即返回，不被 LLM 拟稿/网络发送阻塞。"""
        self._inflight.add(iid)
        t = asyncio.create_task(self._deliver(iid, task, fire_day))
        self._deliver_tasks.add(t)
        t.add_done_callback(self._deliver_tasks.discard)

    async def _deliver(self, iid: str, task: NotifyTask, fire_day: str) -> None:
        """拟稿 + gate + 发送；成功后才落盘，失败按退避重试（只重发，不重新拟稿）。"""
        ok = False
        try:
            async with self._deliver_sem:
                ok = await event_bus.emit(EventType.REMINDER_DUE, task)
        except asyncio.CancelledError:
            self._inflight.discard(iid)
            raise
        except Exception as e:
            _log.warning("提醒投递异常：item=%s %s: %s", iid, type(e).__name__, e)
        if ok:
            self._mark_fired(iid, fire_day)
            self._clear(iid)
            return
        n = self._attempts.get(iid, 0) + 1
        self._attempts[iid] = n
        self._inflight.discard(iid)
        if n >= self._MAX_ATTEMPTS:
            # 超过上限：落盘跳过，避免无限重试烧 token；日志 + 用户侧告警便于人工补发
            _log.warning("提醒投递失败已达上限（%d 次），落盘跳过：item=%s content=%r",
                         n, iid, task.content[:40])
            self._mark_fired(iid, fire_day)
            self._clear(iid)
            if self.on_alert:
                try:
                    await self.on_alert(task.content)
                except Exception as e:
                    _log.warning("投递失败告警发送异常：%s: %s", type(e).__name__, e)
            return
        delay = self._RETRY_BACKOFF[min(n - 1, len(self._RETRY_BACKOFF) - 1)]
        self._retry_after[iid] = time.time() + delay
        _log.warning("提醒投递失败（第 %d 次），%gs 后重试：item=%s", n, delay, iid)

    def _mark_fired(self, iid: str, fire_day: str) -> None:
        """投递成功后才写终态：once → done/done_at；regular → last_fired。"""
        it = self._find_item(iid)
        if it is None or it.is_done:
            return
        if it.type == "once":
            it.status = "done"
            it.done_at = dt.datetime.now().isoformat(timespec="seconds")
        else:
            it.last_fired = fire_day
        self.store.save_items()

    def _find_item(self, iid: str):
        for it in self.store.items:
            if it.id == iid:
                return it
        return None

    def _clear(self, iid: str) -> None:
        self._pending.pop(iid, None)
        self._inflight.discard(iid)
        self._attempts.pop(iid, None)
        self._retry_after.pop(iid, None)
        self._fire_day.pop(iid, None)

    async def _proactive_loop(self) -> None:
        """空白时间低频随机轮询（每分钟 tick，概率在 gate/cfg）。多用户：tick 数据携 owner。"""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60)
                r = random.random()
                if self.occ.state.name == "FREE" and r < self.cfg.proactive_per_min:
                    await event_bus.emit(EventType.PROACTIVE_TICK, getattr(self.store, "owner", ""))
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ---- 勿扰（focus 事项）----
    def _apply_focus(self) -> None:
        """把 focus 事项（勿扰）写进占用状态：有效时段内 BUSY(HOLD)，结束自动恢复 FREE。"""
        now = dt.datetime.now()
        end = self._focus_hold_end(now)
        if end is not None:
            self.occ.begin_hold(end)
        elif self.occ.busy_kind is BusyKind.HOLD:
            self.occ.state = State.FREE
            self.occ.busy_kind = None
            self.occ.hold_until = 0.0

    def _focus_hold_end(self, now: dt.datetime) -> float | None:
        """当前是否处于某个 focus 事项（勿扰）的有效时段；是则返回结束时刻 epoch。

        - regular：今天命中 sched，且时刻在 [start, end)（end<start 跨午夜）；
        - once：仅在生效日（due 的日期，缺省为今天）当天判定，过期自动失效，无需 done；
          但 start 带日期（ISO）时以 datetime 比较为准——否则跨午夜到次日 0 点后会被误判失效。
        """
        today = now.date().isoformat()
        end = None
        for it in self.store.items:
            if not it.focus or it.is_done or it.status != "active":
                continue
            if not (it.start and it.end):
                continue
            if it.type == "regular":
                if not self._sched_hits(it.sched, now):
                    continue
            else:  # once
                due_day = today
                if it.due:
                    try:
                        due_day = dt.datetime.fromisoformat(it.due).date().isoformat()
                    except ValueError:
                        continue
                if due_day != today and not self._is_dt(it.start):
                    continue
            e = self._hold_end_ts(now, it.start, it.end)
            if e is None:
                continue
            end = e if end is None else max(end, e)
        return end

    @classmethod
    def _hold_end_ts(cls, now: dt.datetime, start: str, end: str) -> float | None:
        """当前时刻是否在 [start, end) 内（跨午夜按次日）；是则返回结束时刻 epoch。

        两种写法都支持：
        - 纯 `HH:MM`：按当天锚定，end<start 视为跨午夜（模型给课程/睡眠时段常用）；
        - 带日期的 ISO 时刻（`2026-10-31T09:00`）：直接按 datetime 比较，结束时刻更准
          （旧实现用字符串比较 + `end[:2]` 取小时，对 ISO 既判不中窗口也取错小时）。
        """
        if cls._is_dt(start) or cls._is_dt(end):
            return cls._hold_end_ts_dt(now, start, end)
        hhmm = now.strftime("%H:%M")
        if start <= end:
            if not (start <= hhmm < end):
                return None
            return now.replace(hour=int(end[:2]), minute=int(end[3:5]),
                               second=0, microsecond=0).timestamp()
        # 跨午夜：end < start
        if not (hhmm >= start or hhmm < end):
            return None
        end_dt = now.replace(hour=int(end[:2]), minute=int(end[3:5]), second=0, microsecond=0)
        if hhmm < end:   # 午夜后 ~ end
            return end_dt.timestamp()
        return (end_dt + dt.timedelta(days=1)).timestamp()   # 午夜前，end 在明天

    @classmethod
    def _hold_end_ts_dt(cls, now: dt.datetime, start: str, end: str) -> float | None:
        """带日期时刻的判定：任一端缺日期时按另一端补全；end<start 时视为跨到次日。"""
        s_dt = cls._parse_point(start, now.date())
        e_dt = cls._parse_point(end, now.date())
        if s_dt is None or e_dt is None:
            return None
        if cls._is_dt(start) and not cls._is_dt(end):
            e_dt = e_dt.replace(year=s_dt.year, month=s_dt.month, day=s_dt.day)
        elif cls._is_dt(end) and not cls._is_dt(start):
            s_dt = s_dt.replace(year=e_dt.year, month=e_dt.month, day=e_dt.day)
        if e_dt < s_dt:
            e_dt += dt.timedelta(days=1)
        elif e_dt == s_dt:
            return None   # 零长度时段：不构成占用
        if not (s_dt <= now < e_dt):
            return None
        return e_dt.timestamp()