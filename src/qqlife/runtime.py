"""qqlife/runtime.py：per-uid 运行时注册表（store/occ/gate/scheduler/session/persona）。

- 每个 uid 独立一套运行态；同一 uid 的 scheduler 与 chat 共享同一个 TaskStore，避免并发写。
- 调度器惰性启动（.create 建档 / CLI 首条消息 / 启动恢复时），并持有任务强引用。
- 告警出口与补发汇总由 app 通过 `on_notice` 注入，registry 不直接依赖 outbound。
"""
from __future__ import annotations

import asyncio

from qqlife._paths import user_home
from qqlife import mech_prompts
from qqlife.scheduler.occupied_state import OccupiedState
from qqlife.scheduler.proactive_gate import ProactiveGate
from qqlife.scheduler.scheduler import Scheduler
from qqlife.session import SessionMemory
from qqlife.task_flow.task_store import TaskStore


def resolve_persona(logger, uid: str) -> tuple[str, str]:
    """按 uid 解析人设：user_home(uid)/persona.md（自包含迁移）。

    缺失返回空串（prompts/skill_chat.md 已定义 ROLE），不新建默认人设文件。
    """
    f = user_home(uid) / "persona.md"
    if f.exists():
        logger.info(f"[人设] {uid} -> {f}（uid 家目录）")
        return f.read_text(encoding="utf-8").strip(), str(f)
    return "", ""


class RuntimeRegistry:
    def __init__(self, *, base: dict, logger, on_notice=None) -> None:
        self.base = base
        self.cfg = base["cfg"]
        self.logger = logger
        self.on_notice = on_notice   # async (uid, text) -> None；由 app 接到 _emit
        self.runtime: dict[str, dict] = {}
        self.bg_tasks: list[asyncio.Task] = []

    # ---- 查询 ----

    def get(self, uid: str) -> dict | None:
        return self.runtime.get(uid)

    def all(self) -> list[dict]:
        return list(self.runtime.values())

    # ---- 创建 / 获取 ----

    def _scheduler(self, uid: str, store: TaskStore, occ: OccupiedState, gate: ProactiveGate) -> Scheduler:
        return Scheduler(store, occ, gate, self.cfg,
                         on_alert=lambda content: self._alert(uid, content),
                         on_backlog=lambda n: self._backlog_notice(uid, n))

    def _make(self, uid: str) -> dict:
        home = user_home(uid)
        store = TaskStore.for_owner(uid)
        occ = OccupiedState()
        gate = ProactiveGate(occ, self.cfg)
        persona, persona_path = resolve_persona(self.logger, uid)
        return {"uid": uid, "store": store, "occ": occ, "gate": gate,
                "sched": self._scheduler(uid, store, occ, gate),
                "session": SessionMemory(home / "session.json"),
                "mech": mech_prompts.load_table(home),
                "pa": {"date": None, "count": 0, "last": 0.0},
                "persona": persona, "persona_path": persona_path}

    def ensure(self, uid: str) -> dict:
        r = self.runtime.get(uid)
        if r is not None:
            return r
        r = self.runtime[uid] = self._make(uid)
        if r["store"].corrupt_files:
            # 坏文件已在 TaskStore 备份并按空表继续；这里向该 uid 主动告警，避免用户以为数据还在
            names = [name for name, _ in r["store"].corrupt_files]
            self.bg_tasks.append(asyncio.create_task(self._corrupt_notice(uid, names)))
        # per-uid 调度器惰性启动：建档 / 首条消息后即开始计时、扫描与主动关怀
        self.bg_tasks.append(asyncio.create_task(r["sched"].start()))
        return r

    def ctx(self, uid: str) -> dict:
        """共享上下文 + 该 uid 的私有运行态（供技能 / 调度使用）。"""
        r = self.ensure(uid)
        return dict(self.base) | {"store": r["store"], "occ": r["occ"], "gate": r["gate"],
                                  "session": r["session"], "mech": r["mech"],
                                  "persona": r["persona"], "persona_path": r["persona_path"]}

    async def stop(self) -> None:
        for r in list(self.runtime.values()):
            try:
                await r["sched"].stop()
            except Exception:
                pass
        for t in list(self.bg_tasks):
            t.cancel()
        self.bg_tasks.clear()

    # ---- 通知出口（调度告警 / 补发汇总）----

    def _mech(self, uid: str) -> dict:
        r = self.runtime.get(uid)
        return r["mech"] if r else mech_prompts.BUILTIN

    async def _notify(self, uid: str, text: str) -> None:
        if not text or self.on_notice is None:
            return
        try:
            await self.on_notice(uid, text)
        except Exception as e:
            self.logger.warning(f"[runtime] {uid} 通知发送失败：{type(e).__name__}: {e}")

    async def _alert(self, uid: str, content: str) -> None:
        text = mech_prompts.fill(self._mech(uid).get("delivery_failed", ""),
                                 content=(content or "")[:40])
        await self._notify(uid, text)

    async def _backlog_notice(self, uid: str, n: int) -> None:
        text = mech_prompts.fill(self._mech(uid).get("backlog_skipped", ""),
                                 n=n, hours=self.cfg.replay_window_hours)
        await self._notify(uid, text)

    async def _corrupt_notice(self, uid: str, names: list[str]) -> None:
        text = mech_prompts.fill(self._mech(uid).get("data_corrupt", ""),
                                 files="、".join(names))
        await self._notify(uid, text)
