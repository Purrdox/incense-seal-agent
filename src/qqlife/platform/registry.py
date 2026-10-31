"""platform/registry.py：通道注册表与装配。

- 按 env 装配启用的通道：INPUT_CHANNELS / OUTPUT_CHANNELS（逗号分隔列表）；
- 兼容旧 INPUT_DRIVER / OUTPUT_DRIVER（单通道写法，等价单元素列表）；
- 两者均未配置时按凭据推导：配了 FEISHU_APP_ID → feishu；否则看 NAPCAT_URL → onebot；全空 = 纯 CLI；
- 对称通道共享 Core 实例（连接/客户端只建一份），Input/Output 成对或单取；
- 新通道接入：实现 Core/Input/Output 后在本文件 assemble() 加一个分支即可（见方案文档 §10）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from qqlife.platform.base import InputDriver, OutputDriver


def _channel_list(env_list: str, env_single: str) -> list[str]:
    """解析启用的通道列表：显式列表/单值优先，否则按凭据推导。"""
    raw = (os.getenv(env_list) or "").strip()
    if not raw:
        v = (os.getenv(env_single) or "").strip().lower()
        if v:
            raw = v
    if not raw:
        if (os.getenv("FEISHU_APP_ID") or "").strip():
            return ["feishu"]
        if (os.getenv("NAPCAT_URL") or "").strip():
            return ["onebot"]
        return []
    out: list[str] = []
    for item in raw.replace("，", ",").split(","):
        c = item.strip().lower()
        if c and c not in out:
            out.append(c)
    return out


@dataclass
class ChannelBundle:
    """一个通道的装配产物：共享内核 + 可选的入站/出站驱动。"""
    channel: str
    core: object = None
    input: Optional[InputDriver] = None
    output: Optional[OutputDriver] = None


def assemble(logger) -> dict[str, ChannelBundle]:
    """按 env 装配全部启用通道，返回 {channel: bundle}。单通道装配失败只跳过该通道。"""
    inputs = _channel_list("INPUT_CHANNELS", "INPUT_DRIVER")
    outputs = _channel_list("OUTPUT_CHANNELS", "OUTPUT_DRIVER")
    logger.info(f"通道装配：INPUT_CHANNELS={inputs or '无'}，OUTPUT_CHANNELS={outputs or '无'}")

    bundles: dict[str, ChannelBundle] = {}

    def _get(channel: str) -> ChannelBundle:
        if channel not in bundles:
            bundles[channel] = ChannelBundle(channel=channel)
        return bundles[channel]

    for c in dict.fromkeys(inputs + outputs):   # 去重保序
        try:
            if c == "feishu":
                from qqlife.platform.feishu import FeishuCore, FeishuInput, FeishuOutput
                b = _get("feishu")
                if b.core is None:
                    b.core = FeishuCore(app_id=(os.getenv("FEISHU_APP_ID") or "").strip(),
                                        app_secret=(os.getenv("FEISHU_APP_SECRET") or "").strip())
                if c in inputs and b.input is None:
                    b.input = FeishuInput(b.core)
                if c in outputs and b.output is None:
                    b.output = FeishuOutput(b.core)
            elif c == "onebot":
                from qqlife.platform.onebot import OneBotCore, OneBotInput, OneBotOutput
                b = _get("onebot")
                if b.core is None:
                    b.core = OneBotCore(url=(os.getenv("NAPCAT_URL") or "").strip(),
                                        token=os.getenv("NAPCAT_TOKEN", ""))
                if c in inputs and b.input is None:
                    b.input = OneBotInput(b.core)
                if c in outputs and b.output is None:
                    b.output = OneBotOutput(b.core)
            elif c == "bark":
                from qqlife.platform.bark import BarkOutput
                b = _get("bark")
                if c in outputs and b.output is None:
                    b.output = BarkOutput(base_url=(os.getenv("BARK_URL") or "").strip()
                                           or "https://api.day.app")
            else:
                logger.warning(f"未知通道 {c}，跳过（可用：feishu / onebot / bark）")
        except Exception as e:
            logger.warning(f"通道 {c} 装配失败（跳过）：{e}")
    return bundles
