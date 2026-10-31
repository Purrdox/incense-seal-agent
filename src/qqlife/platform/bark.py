"""platform/bark.py：Bark 出站通道（纯单向，无入站）。

- 发送：POST {BARK_URL}/{device_token}，JSON {"title","body","group"}（bark-server 支持 POST JSON，
  比拼 URL 稳：不受长度/换行/转义限制）。
- capabilities={"text"}：无语音；上层对 voice 调用自动降级（CapabilityError → 跳过/改文本）。
- probe：推送一条测试消息（绑定自检；用户回复 `.link ok` 闭环由 identity 层完成）。
- remote_id = Bark device token（App 首页可复制）。
"""
from __future__ import annotations

import httpx

from qqlife import mech_prompts
from qqlife.platform.base import OutputDriver


class BarkOutput(OutputDriver):
    channel = "bark"
    capabilities = {"text"}

    def __init__(self, base_url: str = "https://api.day.app") -> None:
        self.base_url = (base_url or "https://api.day.app").rstrip("/")

    async def send(self, remote_id: str, content: str) -> None:
        token = (remote_id or "").strip()
        if not token:
            raise RuntimeError("bark 发送失败：device token 为空")
        body = {"title": "Incense-Seal", "body": content, "group": "agent"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self.base_url}/{token}", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"bark 推送失败 HTTP {r.status_code}：{r.text[:120]}")

    async def probe(self, remote_id: str, text: str = "") -> bool:
        try:
            await self.send(remote_id, text or mech_prompts.get("probe_test"))
            return True
        except Exception as e:
            print(f"[bark] probe 失败：{type(e).__name__}: {e}", flush=True)
            return False
