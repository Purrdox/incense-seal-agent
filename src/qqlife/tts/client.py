"""tts/client.py：语音合成后端抽象（双实现，配置切换）。

- SubprocessTTS：调打包好的 `tts/infer_launcher.py`（F5-TTS CLI），复用现成权重/参考语音库。
- ServiceTTS：POST 常驻 OpenAI 兼容 `/v1/audio/speech` 服务，拿音频字节落盘。

语音生成慢（秒级~分钟级），调用方必须用 `asyncio.create_task` 后台运行，不得阻塞消息发送。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

from qqlife._paths import PROJECT_ROOT

# infer_launcher.py 的 --voice 白名单（ref_voice_bank 下的情绪名）
VALID_VOICES = ("angry", "sad", "calm")

# 打包 F5-TTS 推理入口
TTS_LAUNCHER = PROJECT_ROOT / "tts" / "infer_launcher.py"


class TTSClient:
    """语音合成客户端基类。`synthesize` 成功返回落盘音频路径，失败/未启用返回 None。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def _voice(self, voice: str) -> str:
        v = (voice or "").strip().lower()
        return v if v in VALID_VOICES else self.cfg.tts_voice

    async def synthesize(self, text: str, voice: str, out_path: str) -> str | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def enabled(self) -> bool:
        return self.cfg.tts_enabled


class SubprocessTTS(TTSClient):
    """调 tts/infer_launcher.py（子进程，主进程环境即 gsfluid）。"""

    async def synthesize(self, text: str, voice: str, out_path: str) -> str | None:
        if not self.enabled():
            print("[tts] 未启用（TTS_ENABLED 未开启），跳过语音合成", flush=True)
            return None
        text = (text or "").strip()
        if not text:
            return None
        voice = self._voice(voice)
        cmd = [sys.executable, "-W", "ignore", str(TTS_LAUNCHER),
               "--voice", voice,
               "--text", text,
               "--out", out_path,
               "--device", self.cfg.tts_device]
        print(f"[tts] 合成中：voice={voice} device={self.cfg.tts_device} text={text!r}", flush=True)
        try:
            def _run() -> None:
                cp = subprocess.run(cmd, timeout=self.cfg.tts_timeout, capture_output=True)
                if cp.returncode != 0:
                    err = (cp.stderr or b"").decode("utf-8", "replace").strip()
                    raise RuntimeError(f"infer_launcher 退出码 {cp.returncode}：{err[-800:]}")
            await asyncio.to_thread(_run)
        except Exception as e:
            print(f"[tts] 合成失败：{type(e).__name__}: {e}", flush=True)
            return None
        print(f"[tts] 合成完成 -> {out_path}", flush=True)
        return out_path


class ServiceTTS(TTSClient):
    """POST 常驻 OpenAI 兼容 /v1/audio/speech 服务。"""

    async def synthesize(self, text: str, voice: str, out_path: str) -> str | None:
        if not self.enabled():
            print("[tts] 未启用（TTS_ENABLED 未开启），跳过语音合成", flush=True)
            return None
        text = (text or "").strip()
        if not text:
            return None
        url = (self.cfg.tts_service_url or "").rstrip("/")
        if not url:
            print("[tts] service 后端但未配置 TTS_SERVICE_URL，跳过语音", flush=True)
            return None
        voice = self._voice(voice)
        print(f"[tts] 请求服务合成：{url}/audio/speech voice={voice} text={text!r}", flush=True)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.cfg.tts_timeout) as client:
                r = await client.post(
                    f"{url}/audio/speech",
                    json={"model": "tts-1", "input": text, "voice": voice},
                )
                r.raise_for_status()
                data = r.content
            if not data:
                print("[tts] 服务返回空音频", flush=True)
                return None
            import os
            os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[tts] 服务合成失败：{type(e).__name__}: {e}", flush=True)
            return None
        print(f"[tts] 合成完成 -> {out_path}", flush=True)
        return out_path


def make_tts(cfg) -> TTSClient:
    """按 cfg.tts_backend 返回对应后端；未启用时也返回实例（synthesize 会直接返回 None）。"""
    if cfg.tts_backend == "service":
        return ServiceTTS(cfg)
    return SubprocessTTS(cfg)