"""platform/outbound.py：出站路由器（回程 + 广播 + 能力降级 + 失败重试）。

- reply / reply_record：对话回执走**来源通道**（哪条通道进来的，就从那条出去）。
- notify：提醒 / 主动关怀 / 系统通知 → 广播到 uid 全部**已验证**出站端点（多端必达）。
- 能力降级：voice 请求遇到不支持语音的通道 → CapabilityError → 打日志跳过（文本已另行发送），
  绝不静默重试 3 次。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from qqlife.platform.base import CapabilityError, OutputDriver
from qqlife.platform.identity import Identity
from qqlife.platform.registry import ChannelBundle


class OutboundSender:
    def __init__(self, bundles: dict[str, ChannelBundle], identity: Identity,
                 retries: int = 3) -> None:
        self._outputs: dict[str, OutputDriver] = {c: b.output for c, b in bundles.items()
                                                  if b.output is not None}
        self._identity = identity
        self.retries = retries

    # ---- 对话回执（来源通道回程）----

    async def reply(self, channel: str, remote_id: str, content: str) -> bool:
        drv = self._outputs.get(channel)
        if drv is None:
            print(f"[outbound] 通道 {channel} 无出站驱动，跳过回复：{content[:40]!r}", flush=True)
            return False
        return await self._retry(lambda: drv.send(remote_id, content))

    async def reply_record(self, channel: str, remote_id: str, file_path: str) -> bool:
        """发送语音；不支持语音的通道按能力降级跳过（文本已另行发送，无需重试）。"""
        drv = self._outputs.get(channel)
        if drv is None:
            return False
        if "voice" not in drv.capabilities:
            print(f"[语音] {channel} 不支持语音，已降级跳过（文本已发）", flush=True)
            return False
        try:
            return await self._retry(lambda: drv.send_record(remote_id, file_path))
        except CapabilityError:
            print(f"[语音] {channel} 不支持语音（能力缺失），已降级跳过", flush=True)
            return False

    # ---- 通知广播（提醒 / 关怀 / 系统）----

    async def notify(self, uid: str, content: str) -> list[str]:
        """广播到 uid 全部已验证端点，返回送达的通道列表（无出站通道时静默：调用方已有 print）。"""
        if not self._outputs:
            return []
        sent: list[str] = []
        for ep in self._identity.endpoints(uid, verified_only=True):
            drv = self._outputs.get(ep["channel"])
            if drv is None:
                continue
            if await self._retry(lambda d=drv, r=ep["remote_id"]: d.send(r, content)):
                sent.append(ep["channel"])
        if not sent:
            print(f"[outbound] 广播无端点可达：uid={uid}（检查绑定表 verified 状态）", flush=True)
        return sent

    async def probe(self, channel: str, remote_id: str, text: str = "") -> bool:
        """对指定端点做连通性探测（绑定自检；如 bark 测试推送）。

        text 为探测消息文案（上层按 uid 的机械提示表给出），缺省由驱动回退内置 probe_test。
        """
        drv: Optional[OutputDriver] = self._outputs.get(channel)
        if drv is None:
            return False
        try:
            return await drv.probe(remote_id, text)
        except Exception:
            return False

    def output_channels(self) -> list[str]:
        return list(self._outputs)

    # ---- 重试（对齐旧实现：可重试的是网络类异常；能力缺失不重试）----

    async def _retry(self, call) -> bool:
        for attempt in range(self.retries):
            try:
                await call()
                return True
            except CapabilityError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == self.retries - 1:
                    return False
                await asyncio.sleep(1)
        return False
