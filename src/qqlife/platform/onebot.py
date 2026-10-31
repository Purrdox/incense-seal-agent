"""platform/onebot.py：OneBot v11 通道（OneBotCore + OneBotInput + OneBotOutput，接 NapCat）。

- Core：ws 连接状态 + 发送原语（常驻连接优先，无连接时短连兜底）；Input 的接收循环把常驻
  ws 写回 Core 供 Output 复用。
- Input：OneBot WS 客户端接收循环（5 秒断线重连）；上行消息 → handler。
- Output：send_private_msg（文本 / record 语音段，NapCat 负责转 silk）。
- 依赖 NapCat 运行时（不在 agent/ 内，配置 NAPCAT_URL/endpoint）。
"""
from __future__ import annotations

import asyncio
import json
import os

from qqlife.agent_core.types import InboundMessage
from qqlife.platform.base import InputDriver, OutputDriver

try:
    import websockets
except ImportError:  # 可选依赖，未安装时延迟到运行期报错
    websockets = None  # type: ignore[assignment]


class OneBotCore:
    """共享内核：连接状态 + 发送原语（Input 的常驻连接在此共享给 Output）。"""

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self._ws = None
        self._lock = asyncio.Lock()

    def set_ws(self, ws) -> None:
        self._ws = ws

    async def send_payload(self, payload: str) -> None:
        """优先用常驻连接发送（避免 NapCat 对"短连即断"回响应时报 WebSocket is not open）。"""
        ws = self._ws
        if ws is not None:
            try:
                async with self._lock:
                    await ws.send(payload)
                return
            except Exception:
                pass
        await self._send_short(payload)

    async def _send_short(self, payload: str) -> None:
        if websockets is None:
            raise RuntimeError("缺少依赖 websockets：pip install websockets")
        async with websockets.connect(self.url) as ws:
            await ws.send(payload)


class OneBotInput(InputDriver):
    """OneBot WS 接收循环：断线自动重连；上行消息丢后台串行处理。"""

    channel = "onebot"
    capabilities = {"text", "image"}

    def __init__(self, core: OneBotCore) -> None:
        self.core = core
        self._task_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._on_connected = None

    def set_connected_callback(self, fn) -> None:
        """fn: async (ok: bool)。建立/断开连接时调用，供上层切换事件/CLI 模式与显示状态。"""
        self._on_connected = fn

    async def _notify_connected(self, ok: bool) -> None:
        if self._on_connected:
            try:
                await self._on_connected(ok)
            except Exception:
                pass

    async def start(self) -> None:
        if websockets is None:
            raise RuntimeError("缺少依赖 websockets：pip install -r requirements.txt")
        while True:
            try:
                async with websockets.connect(self.core.url) as ws:
                    self.core.set_ws(ws)
                    await self._notify_connected(True)
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 断线重登 keepalive 降频；打印原因便于排查（NapCat 侧断开/心跳超时等）
                print(f"[onebot] 连接异常，5 秒后重连：{type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(5)
            finally:
                # 常驻连接断开后置空，发送走临时短连兜底
                self.core.set_ws(None)
                await self._notify_connected(False)

    async def _dispatch(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("post_type") != "message":
            return
        if msg.get("message_type") != "private":
            return
        if getattr(self, "_handler", None) is None:
            return
        text = self._extract_text(msg.get("message"))
        task = InboundMessage(
            platform=self.channel, target=str(msg.get("user_id")), text=text,
            raw=msg, time=float(msg.get("time", 0) or 0),
            images=self._extract_image_specs(msg.get("message")))
        # 后台处理：接收循环立即返回，不因长处理（等模型十几秒）阻塞而错过
        # NapCat 心跳/消息导致超时断连；串行锁保证同一时刻只处理一条，顺序不乱。
        t = asyncio.create_task(self._handle(task))
        self._tasks.append(t)
        t.add_done_callback(self._tasks.remove)

    async def _handle(self, task: InboundMessage) -> None:
        """串行处理一条消息；任何异常只记录，绝不让接收连接受影响。"""
        try:
            if task.images:  # 图片原始段解析成 base64（只在后台做，不阻塞 ws 接收循环）
                task.images = await self._resolve_images(task.images)
            async with self._task_lock:
                await self._handler(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[onebot] 消息处理异常（已忽略，连接不受影响）：{type(e).__name__}: {e}", flush=True)

    @staticmethod
    def _extract_text(message) -> str:
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            return "".join(seg.get("data", {}).get("text", "") for seg in message if seg.get("type") == "text")
        return ""

    @staticmethod
    def _extract_image_specs(message) -> list[dict]:
        """抓取消息里的 image 段原始信息（不下载）：[{"url","file"}]，供 _resolve_images 后台解析。"""
        if not isinstance(message, list):
            return []
        out = []
        for seg in message:
            if seg.get("type") != "image":
                continue
            data = seg.get("data", {}) or {}
            spec = {"url": data.get("url", "") or "", "file": data.get("file", "") or ""}
            if spec["url"] or spec["file"]:
                out.append(spec)
        return out

    async def _resolve_images(self, specs: list[dict]) -> list[dict]:
        """把图片原始段下载/读成 base64（[{"mime","b64"}]）。上限 48MiB；失败该张跳过，不阻断整条消息。

        优先直链 url 下载（框架无关、稳定）；无 url 或下载失败且是本地文件时兜底读 file 路径；
        file 只是 NapCat 缓存 key（无本地路径）时则跳过（需运行时用 get_file 再补）。
        """
        import base64
        import urllib.request

        resolved: list[dict] = []
        for spec in specs:
            data = None
            url = spec.get("url") or ""
            file_ = spec.get("file") or ""
            try:
                if url:
                    with urllib.request.urlopen(url, timeout=15) as r:
                        data = r.read()
                elif file_ and os.path.isabs(file_):
                    with open(file_, "rb") as f:
                        data = f.read()
            except Exception:
                data = None
            if not data:
                continue
            if len(data) > 48 * 1024 * 1024:   # 48MiB 内联上限
                continue
            resolved.append({"mime": self._guess_mime(data), "b64": base64.b64encode(data).decode("ascii")})
        return resolved

    @staticmethod
    def _guess_mime(data: bytes) -> str:
        """按文件头魔数粗判图片 mime（jpg/png/gif/webp），兜底 image/jpeg。"""
        if data.startswith(b"\x89PNG"):
            return "image/png"
        if data.startswith(b"GIF8"):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"\xff\xd8"):
            return "image/jpeg"
        return "image/jpeg"

    async def stop(self) -> None:
        if self.core._ws is not None:
            await self.core._ws.close()
            self.core.set_ws(None)


class OneBotOutput(OutputDriver):
    channel = "onebot"
    capabilities = {"text", "voice"}

    def __init__(self, core: OneBotCore) -> None:
        self.core = core

    async def send(self, remote_id: str, content: str) -> None:
        payload = json.dumps({
            "action": "send_private_msg",
            "params": {"user_id": int(remote_id), "message": content},
        })
        await self.core.send_payload(payload)

    async def send_record(self, remote_id: str, file_path: str) -> None:
        """发送一条语音：把本地音频路径装成 OneBot record 消息段，NapCat 负责转 silk 发出。"""
        payload = json.dumps({
            "action": "send_private_msg",
            "params": {"user_id": int(remote_id),
                       "message": [{"type": "record", "data": {"file": file_path, "cache": 0}}]},
        })
        await self.core.send_payload(payload)
