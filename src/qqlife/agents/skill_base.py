"""agents/skill_base.py：Skill 声明式基类（§2.5）。"""
from __future__ import annotations

from typing import Any

from qqlife.agent_core.types import SkillResult


class Skill:
    name: str = ""
    desc: str = ""            # 何时触发（意图匹配）
    backend: str = ""         # rule / deepseek
    prompt_file: str = ""
    tool_deps: tuple = ()

    def input_schema(self) -> dict:
        return {}

    def output_schema(self) -> dict:
        return {}

    def context_provider(self, ctx: dict) -> dict:
        """JIT 取料：用到时取相关记忆/课程/任务。"""
        return {}

    async def run(self, ctx: dict) -> SkillResult:
        raise NotImplementedError