"""llm/hub.py：模型档位注册表（ModelHub）——kind → 思考强度（effort）映射（10.16 §A1）。

- 单一模型：凭据/模型名取 .env 的 DEEPSEEK_* 基础段，"类别"抽象（DS{cat}_*）已删除。
- 每个语义 kind 显式映射一个思考强度 effort（cfg.model.efforts，可在 default.json 覆盖）：
  chat=high（人格质量）/ summarize=low / extract=low / evolve=low / search=minimal（程序化提炼）。
- 按 effort 去重建 DeepSeek 客户端（同凭据同模型，仅 reasoning_effort 不同），共享同一个 Budget。
- 技能通过 `hub.for_kind(kind)` 拿模型，接口不变、调用方零改动。
"""
from __future__ import annotations

import os
from typing import Mapping

from qqlife.config.schema import BehaviorConfig
from qqlife.llm.base import LLMBase
from qqlife.llm.budget import Budget
from qqlife.llm.deepseek import DeepSeek


class ModelHub:
    def __init__(self, cfg: BehaviorConfig, env: Mapping[str, str] | None = None,
                 budget: Budget | None = None) -> None:
        self.cfg = cfg
        self.env = env if env is not None else os.environ
        self.budget = budget or Budget()
        self.efforts: dict[str, str] = cfg.model_efforts
        # DEEPSEEK_REASONING_EFFORT：未映射 kind 的缺省档（同时作为映射缺省值参与建客户端）
        self._default_effort = (self._get("DEEPSEEK_REASONING_EFFORT") or "high").strip().lower()
        self._clients: dict[str, LLMBase] = {}
        key = self._get("DEEPSEEK_API_KEY")
        if key:
            base = self._get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            model = self._get("DEEPSEEK_MODEL") or "deepseek-chat"
            thinking = (self._get("DEEPSEEK_THINKING") or "0") == "1"
            for eff in dict.fromkeys(list(self.efforts.values()) + [self._default_effort]):
                self._clients[eff] = DeepSeek(api_key=key, base_url=base, model=model,
                                               budget=self.budget, thinking=thinking,
                                               reasoning_effort=eff)

    def _get(self, name: str) -> str:
        return self.env.get(name, "") or ""

    def for_kind(self, kind: str) -> LLMBase | None:
        """按语义 kind 取对应思考强度的客户端；映射缺失回退缺省档。"""
        eff = self.efforts.get(kind) or self._default_effort
        c = self._clients.get(eff)
        if c and c.is_available():
            return c
        # 回退：任意已配置可用的档位（同模型同凭据，正常不会走到）
        for cc in self._clients.values():
            if cc and cc.is_available():
                return cc
        return None

    def summary(self) -> list[str]:
        c = next(iter(self._clients.values()), None)
        model = getattr(c, "model", "?") if c else "(未配置)"
        thinking = getattr(c, "thinking", False) if c else False
        status = "可用" if (c and c.is_available()) else "未配置/不可用"
        kinds = ", ".join(f"{k}:{v}" for k, v in self.efforts.items())
        return [f"{model}[{status}](thinking={thinking})；kind→effort → {kinds}"]
