"""platform/feishu.py：飞书通道（FeishuCore + FeishuInput + FeishuOutput）。

- Core：凭据 + lark REST client（tenant_access_token 由 SDK 自动管理）+ 文本/语音发送原语。
- Input：WebSocket 长连接收消息（无需公网 IP；SDK 内置断线重连）。事件回调在 SDK 分发线程执行，
  只做解析 + run_coroutine_threadsafe 投递，立即返回（3 秒 ACK 硬约束）；耗时处理在事件循环后台串行。
- Output：REST 出站；语音 = WAV → PyAV 转 16k 单声道 opus → 上传 → audio 消息（临时文件发完即删）。
- 依赖（可选安装，缺失时报错不崩）：lark-oapi、av。
- 权限/后台配置与踩坑记录见 开发文档/开发提示与约定.md §3-§5。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import wave
from typing import Optional

from qqlife.agent_core.types import InboundMessage
from qqlife.platform.base import InputDriver, OutputDriver

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
except ImportError:  # 可选依赖，未安装时延迟到运行期报错
    lark = None  # type: ignore[assignment]

try:
    import av
except ImportError:
    av = None  # type: ignore[assignment]

# 图片内联上限：超过则不内联进主会话（base64 体积 = 原始 ×4/3，太大既费 token 也易超请求体限制）
_IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _guess_mime(data: bytes) -> str:
    """按文件头魔数粗判图片 mime（png/gif/webp/jpg），兜底 image/jpeg。"""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _wav_duration_ms(wav_path: str) -> int:
    """标准库读 WAV 帧数换算毫秒（飞书 audio 消息要带 duration，否则客户端不显示时长）。"""
    with wave.open(wav_path, "rb") as w:
        return int(w.getnframes() * 1000 / w.getframerate())


def _post_content(content: str) -> dict:
    """取富文本正文对象：接收侧为 {"title","content":[[...]]}，发送侧带 zh_cn 语言外层，两种都兼容。"""
    try:
        data = json.loads(content or "")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("content"), list):
        return data
    for v in data.values():   # zh_cn / en_us / ja_jp
        if isinstance(v, dict) and isinstance(v.get("content"), list):
            return v
    return {}


def post_parts(content: str) -> tuple[str, list[str]]:
    """飞书富文本（msg_type=post）→ (纯文本, 图片 image_key 列表)。

    用户在飞书里以「1. 」开头的行会被客户端自动转成有序列表，整条消息随之变成 post；
    旧实现只解析 text 类型，于是这类消息被静默丢弃（表现为"发了没反应"）。
    接收侧结构：{"title": "标题", "content": [[{"tag": "text", "text": "第一行"}, ...], ...]}
    """
    body = _post_content(content)
    title = str(body.get("title") or "").strip()
    lines: list[str] = []
    keys: list[str] = []
    for para in body.get("content") or []:
        if not isinstance(para, list):
            continue
        parts: list[str] = []
        for el in para:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag in ("text", "a", "code_block", "md"):
                parts.append(str(el.get("text") or ""))
            elif tag == "at":
                who = str(el.get("user_name") or el.get("user_id") or "")
                if who:
                    parts.append(who if who.startswith("@") else "@" + who)
            elif tag in ("img", "media"):
                key = el.get("image_key")
                if key:
                    keys.append(str(key))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    if title:
        lines.insert(0, title)
    return "\n".join(lines), keys


def _wav_to_opus(wav_path: str, opus_path: str) -> None:
    """WAV → 16kHz 单声道 opus（飞书官方推荐参数，等价 ffmpeg -acodec libopus -ac 1 -ar 16000）。"""
    if av is None:
        raise RuntimeError("缺少依赖 av：pip install av（PyAV wheel 内置 libopus，无需 ffmpeg）")
    with av.open(wav_path) as src, av.open(opus_path, "w") as dst:
        ostream = dst.add_stream("libopus", rate=16000)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in src.decode(audio=0):
            for nf in (resampler.resample(frame) or []):
                for pkt in ostream.encode(nf):
                    dst.mux(pkt)
        for pkt in ostream.encode(None):  # 冲刷编码器
            dst.mux(pkt)


class FeishuCore:
    """共享内核：凭据 + lark REST client + 出站原语（Input/Output 共用，连接/客户端只建一份）。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        if lark is None:
            raise RuntimeError("缺少依赖 lark-oapi：pip install lark-oapi")
        if not app_id or not app_secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    # ---- 出站原语（同步；OutputDriver 内用 asyncio.to_thread 包裹）----

    def send_text(self, open_id: str, content: str) -> None:
        req = (CreateMessageRequest.builder()
               .receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(open_id)
                             .msg_type("text")
                             .content(json.dumps({"text": content}))
                             .build())
               .build())
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"feishu 发送失败 code={resp.code} msg={resp.msg}")

    def upload_opus(self, opus_path: str, duration_ms: int) -> str:
        with open(opus_path, "rb") as f:
            req = (CreateFileRequest.builder()
                   .request_body(CreateFileRequestBody.builder()
                                 .file_type("opus")
                                 .duration(duration_ms)
                                 .file(f)
                                 .build())
                   .build())
            resp = self.client.im.v1.file.create(req)
        if not resp.success():
            raise RuntimeError(f"feishu 语音上传失败 code={resp.code} msg={resp.msg}")
        return resp.data.file_key

    def send_audio(self, open_id: str, file_key: str, duration_ms: int) -> None:
        req = (CreateMessageRequest.builder()
               .receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(open_id)
                             .msg_type("audio")
                             .content(json.dumps({"file_key": file_key, "duration": duration_ms}))
                             .build())
               .build())
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"feishu 语音发送失败 code={resp.code} msg={resp.msg}")

    def download_image(self, message_id: str, file_key: str) -> bytes:
        """下载某条消息里的图片资源（返回原始字节）。同步调用，出站/入站层用 to_thread 包裹。"""
        req = (GetMessageResourceRequest.builder()
               .message_id(message_id)
               .file_key(file_key)
               .type("image")
               .build())
        resp = self.client.im.v1.message_resource.get(req)
        if not resp.success():
            raise RuntimeError(f"feishu 图片下载失败 code={resp.code} msg={resp.msg}")
        f = getattr(resp, "file", None)
        if f is None:
            raise RuntimeError("feishu 图片下载失败：响应无文件内容")
        return bytes(f.read() if hasattr(f, "read") else f)


class FeishuInput(InputDriver):
    """长连接收消息。群聊消息在事件层直接忽略（chat_type != "p2p"）。"""

    channel = "feishu"
    capabilities = {"text", "image"}

    def __init__(self, core: FeishuCore) -> None:
        self.core = core
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._task_lock = asyncio.Lock()   # 消息处理串行锁（顺序不乱）
        self._on_connected = None

    def set_connected_callback(self, fn) -> None:
        """fn: async (ok: bool)。飞书长连接启动即视为已连接（SDK 内置断线重连）。"""
        self._on_connected = fn

    async def _notify_connected(self, ok: bool) -> None:
        if self._on_connected:
            try:
                await self._on_connected(ok)
            except Exception:
                pass

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_event)
                   .build())
        ws = lark.ws.Client(self.core.app_id, self.core.app_secret,
                            event_handler=handler, log_level=lark.LogLevel.INFO)
        # SDK 的 ws client 模块持有自己的 asyncio loop 并在 start() 里 run_until_complete：
        # 换成专用新 loop（从守护线程驱动），避免与主事件循环冲突。
        import lark_oapi.ws.client as _wsc
        _wsc.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=ws.start, name="feishu-ws", daemon=True)
        self._thread.start()
        await self._notify_connected(True)

    def _on_event(self, data: P2ImMessageReceiveV1) -> None:
        """SDK 分发线程回调：只解析 + 投递到事件循环，立即返回（3 秒 ACK 硬约束）。"""
        try:
            msg = data.event.message
            if msg.chat_type != "p2p":  # 只处理单聊；群组消息直接忽略
                return
            text = ""
            images: list[dict] = []
            if msg.message_type == "text":
                try:
                    text = json.loads(msg.content).get("text", "")
                except Exception:
                    text = ""
            elif msg.message_type == "post":
                # 富文本：客户端把「1. 」开头的行转成有序列表后，整条消息就不再是 text 类型
                text, post_keys = post_parts(msg.content)
                for key in post_keys:
                    images.append({"message_id": msg.message_id, "image_key": key})
            elif msg.message_type == "image":
                # 只记原始段（image_key），下载放到后台 _handle 里做——事件回调须 3 秒内返回
                try:
                    image_key = json.loads(msg.content).get("image_key", "")
                except Exception:
                    image_key = ""
                if image_key:
                    images.append({"message_id": msg.message_id, "image_key": image_key})
            if not text and not images:
                print(f"[feishu] 已忽略暂不支持的消息类型：{msg.message_type}"
                      f"（message_id={msg.message_id}）", flush=True)
                return
            create_ms = float(msg.create_time or 0)
            task = InboundMessage(
                platform=self.channel,
                target=data.event.sender.sender_id.open_id,
                text=text,
                raw={"message_id": msg.message_id, "create_time": msg.create_time,
                     "message_type": msg.message_type},
                time=create_ms / 1000.0 if create_ms else 0.0,
                images=images,
            )
            if getattr(self, "_handler", None) is not None and self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._handle(task), self._loop)
        except Exception as e:
            print(f"[feishu] 事件解析异常（已忽略，连接不受影响）：{type(e).__name__}: {e}", flush=True)

    async def _handle(self, task: InboundMessage) -> None:
        """串行处理一条消息；异常只记录，绝不影响 SDK 分发线程。"""
        try:
            if task.images:   # 图片下载耗网络：放后台做，不阻塞（也让 SDK 回调能立即 ACK）
                task.images = await self._resolve_images(task.images)
            async with self._task_lock:
                await self._handler(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[feishu] 消息处理异常（已忽略，连接不受影响）：{type(e).__name__}: {e}", flush=True)

    async def _resolve_images(self, specs: list[dict]) -> list[dict]:
        """把图片原始段下载成 base64（[{"mime","b64"}]，与主会话多模态链路约定一致）。

        单张失败/超限只跳过该张，不阻断整条消息。
        """
        import base64

        resolved: list[dict] = []
        for spec in specs:
            try:
                data = await asyncio.to_thread(self.core.download_image,
                                               spec.get("message_id", ""),
                                               spec.get("image_key", ""))
            except Exception as e:
                print(f"[feishu] 图片下载失败（已跳过该张）：{type(e).__name__}: {e}", flush=True)
                continue
            if not data or len(data) > _IMAGE_MAX_BYTES:
                print(f"[feishu] 图片过大或为空（已跳过该张）：{len(data or b'')} bytes", flush=True)
                continue
            resolved.append({"mime": _guess_mime(data),
                             "b64": base64.b64encode(data).decode("ascii")})
        return resolved

    async def stop(self) -> None:
        # SDK 未提供长连接的优雅停止；线程为 daemon，进程退出即结束。
        pass


class FeishuOutput(OutputDriver):
    channel = "feishu"
    capabilities = {"text", "voice"}

    def __init__(self, core: FeishuCore) -> None:
        self.core = core

    async def send(self, remote_id: str, content: str) -> None:
        await asyncio.to_thread(self.core.send_text, remote_id, content)

    async def send_record(self, remote_id: str, file_path: str) -> None:
        """发送语音：WAV → opus → 上传 → audio 消息。临时 .opus 与 WAV 同目录，发完即删。"""
        def _prep() -> tuple:
            opus_path = file_path.rsplit(".", 1)[0] + ".opus"
            duration_ms = _wav_duration_ms(file_path)
            _wav_to_opus(file_path, opus_path)
            try:
                return self.core.upload_opus(opus_path, duration_ms), duration_ms
            finally:
                try:
                    os.remove(opus_path)  # 清理临时 opus（失败也删，不留垃圾）
                except OSError:
                    pass

        file_key, duration_ms = await asyncio.to_thread(_prep)
        await asyncio.to_thread(self.core.send_audio, remote_id, file_key, duration_ms)
