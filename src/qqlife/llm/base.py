"""llm/base.py：OpenAI 兼容 client 基类（本地 llama / Deepseek 都继承）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMBase(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], *, temperature: float = 0.7,
                   json_mode: bool = False, max_tokens: int = 512,
                   thinking: bool | None = None) -> str:
        """messages: [{role, content}, ...]，返回模型文本。json_mode=True 时要求结构化输出。

        thinking=None 沿用实例默认；True/False 覆盖本次调用——程序化提炼（记忆合并、资料摘要）
        应显式关闭思考：思考模式下 reasoning 与正文共享 max_tokens，容易把额度耗光导致正文为空。
        """

    @abstractmethod
    def is_available(self) -> bool: ...