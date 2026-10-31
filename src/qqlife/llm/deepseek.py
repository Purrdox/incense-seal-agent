"""llm/deepseek.py：DeepSeek 调用（OpenAI 兼容 + 思考模式，细则对齐 FX）。

思考模式（对齐 FX harness）：
- 请求体：`thinking: {type:"enabled"}` +（可选）`reasoning_effort`
- 思考模式下**忽略 temperature**（不再是 temperature 字段）
- 工具场景需原样回传上轮 reasoning_content（由调用方在 messages 中保留）
"""
from __future__ import annotations

from qqlife.llm.base import LLMBase
from qqlife.llm.budget import Budget, BudgetExceeded

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class DeepSeek(LLMBase):
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", budget: Budget | None = None,
                 thinking: bool = False, reasoning_effort: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.budget = budget or Budget()
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort if reasoning_effort in _REASONING_EFFORTS else None

    def is_available(self) -> bool:
        return httpx is not None and bool(self.api_key)

    async def complete(self, messages: list[dict], *, temperature: float = 0.7,
                       json_mode: bool = False, max_tokens: int = 1024,
                       tools: list | None = None, usage: dict | None = None,
                       thinking: bool | None = None) -> dict:
        """底层一次补全：返回完整 assistant 消息 dict（含 content / tool_calls / reasoning_content）。

        思考模式 + tools 场景：调用方须把上一轮返回的 assistant 消息（含 reasoning_content）
        原样追加回 messages，再带 tools 请求（对齐 DeepSeek 要求，否则 400）。
        usage（可选出参）：透传本次响应的 DeepSeek usage——prompt_cache_hit_tokens /
        prompt_cache_miss_tokens / completion_tokens（缓存观测，10.16 §A3），原位写进该 dict。
        thinking：本次调用覆盖实例默认（None=沿用）。**注意该服务默认就开思考**——不传 thinking
        字段同样会 reasoning，必须显式 `{"type": "disabled"}` 才真正关闭。程序化提炼/合并关掉思考：
        reasoning 与正文共享 max_tokens，实测大输入下 reasoning 会把额度吃光，正文为空。
        """
        if httpx is None:
            raise RuntimeError("缺少依赖 httpx")
        if not self.budget.try_consume(max_tokens):
            raise BudgetExceeded("预算/防循环触发，本次调用被拦截")
        use_thinking = self.thinking if thinking is None else bool(thinking)
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if use_thinking:
            body["thinking"] = {"type": "enabled"}
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
        else:
            body["thinking"] = {"type": "disabled"}
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self.base_url.rstrip('/')}/chat/completions",
                                  json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            if usage is not None:
                u = data.get("usage") or {}
                choice = (data.get("choices") or [{}])[0] or {}
                usage["hit"] = int(u.get("prompt_cache_hit_tokens") or 0)
                usage["miss"] = int(u.get("prompt_cache_miss_tokens") or 0)
                usage["completion"] = int(u.get("completion_tokens") or 0)
                # 结束原因（length = 撞到 max_tokens 被截断）：调用方据此判定输出是否完整
                usage["finish"] = str(choice.get("finish_reason") or "")
            return data["choices"][0]["message"]

    async def chat(self, messages: list[dict], *, temperature: float = 0.7,
                   json_mode: bool = False, max_tokens: int = 1024,
                   tools: list | None = None, usage: dict | None = None,
                   thinking: bool | None = None) -> str:
        msg = await self.complete(messages, temperature=temperature, json_mode=json_mode,
                                  max_tokens=max_tokens, tools=tools, usage=usage,
                                  thinking=thinking)
        return msg.get("content") or ""