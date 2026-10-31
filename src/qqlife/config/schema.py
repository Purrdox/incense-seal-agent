"""config/schema.py：默认行为参数的轻量校验（纯 stdlib，便于无依赖启动）。

程序与 AI 共用的一切 key/枚举一律英文（命名契约）；仅用户可见文本用中文。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_JSON = Path(__file__).resolve().parent / "default.json"


class BehaviorConfig:
    def __init__(self, data: dict) -> None:
        self.data = data

    @classmethod
    def load(cls, path: Path | None = None) -> "BehaviorConfig":
        p = path or DEFAULT_JSON
        return cls(json.loads(p.read_text(encoding="utf-8")))

    # ---- 便捷读取（带默认）----
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    @property
    def behavior(self) -> dict:
        return self.get("behavior", {})

    @property
    def proactive_per_min(self) -> float:
        """主动关怀每分钟概率。env `PROACTIVE_DAILY_TARGET`=每天期望次数优先：p=t/1440；未设回退。"""
        t = os.getenv("PROACTIVE_DAILY_TARGET")
        if t:
            try:
                return max(0.0, float(t)) / 1440.0
            except ValueError:
                pass
        return float(self.behavior.get("proactive_per_min", 0.0014))

    @property
    def proactive_daily_cap(self) -> int:
        v = os.getenv("PROACTIVE_DAILY_CAP")
        if v:
            try:
                return max(0, int(v))
            except ValueError:
                pass
        return max(0, int(self.behavior.get("proactive_daily_cap", 5)))

    @property
    def proactive_min_interval(self) -> float:
        v = os.getenv("PROACTIVE_MIN_INTERVAL")
        if v:
            try:
                return max(0.0, float(v))
            except ValueError:
                pass
        return max(0.0, float(self.behavior.get("proactive_min_interval", 60)))

    @property
    def message_merge_seconds(self) -> float:
        return float(self.behavior.get("message_merge_seconds", 0))

    @property
    def message_split_chars(self) -> int:
        return max(1, int(self.behavior.get("message_split_chars", 800)))

    @property
    def message_send_gap(self) -> float:
        return max(0.0, float(self.behavior.get("message_send_gap", 0.8)))

    @property
    def search_retries(self) -> int:
        return max(0, int(self.behavior.get("search_retries", 2)))

    @property
    def search_timeout(self) -> float:
        return max(1.0, float(self.behavior.get("search_timeout", 8)))

    @property
    def search_backend(self) -> str:
        return str(self.behavior.get("search_backend", "auto")).strip() or "auto"

    @property
    def search_proxy(self) -> str:
        return str(self.behavior.get("search_proxy", "")).strip().strip('"').strip("'")

    @property
    def dialogue_turn_hard(self) -> int:
        """硬上限安全网：turns 达此值自动 fold（软阈值自动压缩已删，10.16 §A3——压缩入口=用户 .compact）。"""
        return int(self.behavior.get("dialogue_turn_hard", 300))

    @property
    def replay_window_hours(self) -> float:
        """重启后补发窗口（小时）：更早的过期 once 项标记 done 并汇总告知，避免开机刷屏。"""
        v = os.getenv("REPLAY_WINDOW_HOURS")
        if v:
            try:
                return max(0.0, float(v))
            except ValueError:
                pass
        return max(0.0, float(self.behavior.get("replay_window_hours", 24)))

    @property
    def compact_hint_turns(self) -> int:
        """软压缩提醒阈值：turns 达此值发一次 compact_hint 机械提示（0=关闭）。fold 后周期重置。"""
        v = os.getenv("COMPACT_HINT_TURNS")
        if v:
            try:
                return max(0, int(v))
            except ValueError:
                pass
        return max(0, int(self.behavior.get("compact_hint_turns", 100)))

    @property
    def chat_max_tokens(self) -> int:
        return max(128, int(self.behavior.get("chat_max_tokens", 8192)))

    # ---- 本地额度（Budget）上限：防失控，非计费 ----
    @property
    def budget_minute_cap(self) -> int:
        """分钟窗口内可预扣的 token 总量。注意：按每次调用的 max_tokens 预扣（chat 档 8192），
        故实际约等于"每分钟最多多少次模型调用"——600000 ≈ chat 档 73 次/分。"""
        return max(1000, int(self.behavior.get("budget_minute_cap", 600000)))

    @property
    def budget_session_cap(self) -> int:
        """进程生命周期内可预扣的 token 总量（重启归零）。"""
        return max(1000, int(self.behavior.get("budget_session_cap", 20_000_000)))

    # ---- 语音输出（TTS）----
    @property
    def tts_enabled(self) -> bool:
        # 默认开启：语音仅在模型输出 <voice> 标签时触发，不影响纯文本对话
        return str(os.getenv("TTS_ENABLED", self.behavior.get("tts_enabled", True))).lower() in ("1", "true", "yes", "on")

    @property
    def tts_backend(self) -> str:
        """subprocess=调打包 CLI；service=常驻 OpenAI 兼容 /v1/audio/speech。"""
        v = (os.getenv("TTS_BACKEND") or "").strip() or str(self.behavior.get("tts_backend", "subprocess"))
        return v if v in ("subprocess", "service") else "subprocess"

    @property
    def tts_service_url(self) -> str:
        return (os.getenv("TTS_SERVICE_URL") or str(self.behavior.get("tts_service_url", ""))).strip()

    @property
    def tts_voice(self) -> str:
        return (os.getenv("TTS_VOICE") or str(self.behavior.get("tts_voice", "calm"))).strip() or "calm"

    @property
    def tts_device(self) -> str:
        return (os.getenv("TTS_DEVICE") or str(self.behavior.get("tts_device", "cuda"))).strip() or "cuda"

    @property
    def tts_timeout(self) -> float:
        return max(1.0, float(os.getenv("TTS_TIMEOUT") or self.behavior.get("tts_timeout", 180)))

    # ---- 身份中心 ----
    @property
    def master_uid(self) -> str:
        """默认身份标识码：CLI 直通 + 提醒兜底目标（通道端点须经 .create/.link 绑定，见 platform/identity.py）。"""
        uid = (os.getenv("MASTER_UID") or "").strip().strip('"').strip("'")
        return uid or "me"

    # ---- 模型思考强度映射（kind → effort，10.16 §A1）----
    _DEFAULT_EFFORTS = {"chat": "xhigh", "summarize": "low", "extract": "low",
                        "evolve": "low", "search": "minimal"}

    @property
    def model_efforts(self) -> dict:
        """各语义 kind 的思考强度：chat=xhigh（人格质量/人情味）/ summarize/extract/evolve=low / search=minimal。"""
        out = dict(self._DEFAULT_EFFORTS)
        m = self.get("model", {}).get("efforts", {})
        if isinstance(m, dict):
            out.update({str(k): str(v) for k, v in m.items()})
        return out