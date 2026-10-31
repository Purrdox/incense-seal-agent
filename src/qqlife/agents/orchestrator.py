"""agents/orchestrator.py：技能注册表 + 路由（统一入口）。

现状：一切输入都落主会话 chat（聊天/办事/临时扮演合一）；
保留 discover 自动注册，为将来插件技能留扩展点。
"""
from __future__ import annotations

from qqlife.agent_core.types import InboundMessage, SkillResult
from qqlife.agents.skill_base import Skill
from qqlife import mech_prompts


class Orchestrator:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self.skills[skill.name] = skill

    # 启动时 importlib 自动发现 Skill 子类
    def discover(self, package: str = "qqlife.agents") -> None:
        import importlib
        import pkgutil
        from qqlife import agents as pkg
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            if modname in ("__init__", "skill_base"):
                continue
            try:
                m = importlib.import_module(f"{package}.{modname}")
            except Exception:
                continue
            for obj in vars(m).values():
                if isinstance(obj, type) and issubclass(obj, Skill) and obj is not Skill and getattr(obj, "name", ""):
                    self.register(obj())

    async def route(self, msg: InboundMessage, ctx: dict) -> SkillResult:
        t = msg.text.strip()
        base = dict(ctx) | {"text": t, "target": msg.target, "course": None}
        skill = self.skills.get("chat")
        if not skill:
            mech = ctx.get("mech") or mech_prompts.BUILTIN
            return SkillResult(reply=mech.get("no_skill", ""))
        return await skill.run(base)
