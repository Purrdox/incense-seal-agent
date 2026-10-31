"""qqlife/bootstrap.py：共享装配 + 进程生命周期（启动/关闭通知、单实例锁）。

从 app.py 拆出，只依赖底层模块，不反向依赖 qqlife.app，避免循环导入。
"""
from __future__ import annotations

import datetime as dt
import os

from qqlife._paths import PROJECT_ROOT, user_home
from qqlife import mech_prompts
from qqlife.tts import make_tts

# 运行期引用：main 在进程退出时用它补发关闭通知（见 shutdown_notice）
LAST_APP: dict = {}


def ts() -> str:
    """当前时刻 HH:MM:SS，用于给消息/状态打印加时间戳。"""
    return dt.datetime.now().strftime("%H:%M:%S")


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass


def build_context(logger) -> dict:
    """装配**共享**上下文（各 uid 复用的不变部件）。

    store/occ/gate/session/persona 都是 per-uid 的，由 RuntimeRegistry 按 uid 维护；
    这里只建跨 uid 安全的共享件（cfg/hub/orch/tts）。
    """
    from qqlife.agents.orchestrator import Orchestrator
    from qqlife.config.schema import BehaviorConfig
    from qqlife.llm.budget import Budget
    from qqlife.llm.hub import ModelHub

    cfg = BehaviorConfig.load()
    orch = Orchestrator()
    orch.discover()

    # 人设已 per-uid 化：user_home(uid)/persona.md，无全局种子（env PERSONA/CHARACTER_CARD 机制已删除）
    hub = ModelHub(cfg, budget=Budget(minute_cap=cfg.budget_minute_cap,
                                     session_cap=cfg.budget_session_cap))
    logger.info("模型档位：" + ", ".join(hub.summary()))
    return {"cfg": cfg, "orch": orch, "hub": hub, "tts": make_tts(cfg)}


def set_last_app(identity, outbound) -> None:
    """记录运行期引用，供 Ctrl+C / finally 路径补发关闭通知。"""
    LAST_APP.update({"identity": identity, "outbound": outbound, "notified": False})


async def broadcast_notice(identity, outbound, key: str) -> None:
    """向全部已绑定身份（且有已验证端点）广播一条机械提示（启动/关闭等系统通知）。

    文案只取自机械提示表：内置 BUILTIN 兜底 + 该 uid 家目录 mech_prompts.json 覆盖。
    """
    if identity is None or outbound is None:
        return
    for uid in identity.uids():
        if not identity.endpoints(uid, verified_only=True):
            continue
        try:
            text = mech_prompts.load_table(user_home(uid)).get(key, "")
            if not text:
                continue
            print(f"[{ts()}] [发->{uid}] {text}")
            await outbound.notify(uid, text)
        except Exception as e:
            print(f"[{ts()}] [错误] {key} 通知发送失败：{type(e).__name__}: {e}")


async def shutdown_notice() -> None:
    """退出前补发关闭通知：Ctrl+C 时 asyncio.run 会中断主协程（amain 的 finally 不一定跑到），
    故由 main() 在捕获 KeyboardInterrupt 后调用本函数兜底；notified 标记保证只发一次。"""
    app = LAST_APP
    if not app or app.get("notified"):
        return
    app["notified"] = True
    await broadcast_notice(app.get("identity"), app.get("outbound"), "shutdown_notice")


def acquire_single_instance() -> bool:
    """单实例锁：占用本机固定端口（进程退出/崩溃即释放，无残留锁文件）。

    多通道事件推送（飞书长连接等）要求同一 App 全局只有一个实例在线——
    多实例会互相分摊事件，表现酷似丢消息（见 开发文档/开发提示与约定.md §4）。
    出现并发时：新实例直接终止并提示。
    """
    import socket
    port = int(os.getenv("QQLIFE_LOCK_PORT") or 47632)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        globals()["_INSTANCE_LOCK_SOCK"] = s   # 持引用到进程退出，端口即锁
        return True
    except OSError:
        return False
