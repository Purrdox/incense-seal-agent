"""platform/base.py：通道驱动抽象（InputDriver / OutputDriver，收发解耦）。

- 收/发拆分：对称通道（feishu/onebot/webui）成对实现共享 Core；单向出站通道（bark）只实现 Output。
- 能力声明：capabilities 决定出站降级行为（如 bark 无 voice → 自动改发文本），见 outbound.py。
- probe：连通性探测（绑定自检用）。单向通道由用户口头确认闭环（identity 层）。
- 约定：InboundMessage.platform 填 channel 名；target 填 remote_id（通道内稳定标识：
  feishu=open_id、onebot=QQ号、bark=device token）；身份解析（→uid）由 identity 层完成。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from qqlife.agent_core.types import InboundMessage
from qqlife import mech_prompts

MessageHandler = Callable[[InboundMessage], Awaitable[None]]


class CapabilityError(RuntimeError):
    """通道不支持该出站能力（如 bark 不支持语音）。由出站层捕获并降级，不该让上层重试。"""


class InputDriver(ABC):
    """入站驱动：从通道拉消息 → handler。可选（纯提醒场景可无入站通道）。"""

    channel: str = ""
    capabilities: set[str] = set()   # 入站能力："text" / "image" / "voice"

    def set_message_handler(self, fn: MessageHandler) -> None:
        self._handler = fn  # type: ignore[attr-defined]

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...


class OutputDriver(ABC):
    """出站驱动：向端点发消息。必须实现 send；不支持 send_record 的通道抛 CapabilityError。"""

    channel: str = ""
    capabilities: set[str] = set()   # 出站能力："text" / "voice" / "image" / "markdown"

    @abstractmethod
    async def send(self, remote_id: str, content: str) -> None: ...

    async def send_record(self, remote_id: str, file_path: str) -> None:
        raise CapabilityError(f"channel {self.channel} does not support voice")

    async def probe(self, remote_id: str, text: str = "") -> bool:
        """连通性探测：向端点发一条测试消息（绑定自检用）。

        text 由上层按 uid 的机械提示表给出（per-uid 可定制）；缺省回退内置 probe_test。
        """
        try:
            await self.send(remote_id, text or mech_prompts.get("probe_test"))
            return True
        except Exception:
            return False

    async def stop(self) -> None:
        return None
