"""qqlife.tts：语音合成后端（英文音色，接 tts/ 打包 F5-TTS）。"""
from qqlife.tts.client import TTSClient, make_tts

__all__ = ["TTSClient", "make_tts"]