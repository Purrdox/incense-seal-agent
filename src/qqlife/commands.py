"""commands.py：指令层（统一、少样、跨端一致）。

规则：「指令」一律用英文，且以英文句号 `.` 或中文句号 `。` 开头（电脑/手机一致）。
每种指令只保留一种标准名；`。` 与 `.` 等价。未识别的指令返回 help（不丢给 AI）。

指令表：
  .time               查看当前时间
  .status             查看当前处理状态
  .compact            手动压缩长期记忆（app 层拦截执行）
  .usage              查看 token 用量与缓存命中（app 层拦截执行）
  .purge              清理本身份的运行态数据（app 层拦截执行；保留人设/机械提示/身份端点）
  .create <标识码>     创建身份并绑定当前端点（binding.py）
  .link <标识码>       绑定到已存在身份（binding.py）
  .profile            查看当前身份与全部已绑定端点
  .unlink             解绑当前端点
  .persona            导入角色卡（persona_import.py）
  .mech               按当前角色卡生成机械提示词（mech_gen.py）
  .help               列出可用指令
  .quit               退出调试台（仅 DEBUG_CLI=1）
"""
from __future__ import annotations

import datetime as dt

from qqlife._paths import DATA_DIR
from qqlife import runtime_state

# help 为指令层内置文案，不放入可配置的机械提示表
_HELP = ("可用指令（以 . 或 。 开头）：\n"
         "· .time     查看当前时间\n"
         "· .status   查看当前处理状态\n"
         "· .compact  手动压缩长期记忆（对话沉淀为长期记忆，任意长度可用）\n"
         "· .usage    查看 token 用量与缓存命中\n"
         "· .create <标识码>  创建身份并绑定当前端点\n"
         "· .link <标识码>    绑定到已存在身份（.link ok 确认端点；.link bark <token> 挂载设备）\n"
         "· .profile  查看当前身份与全部已绑定端点\n"
         "· .unlink   解绑当前端点\n"
         "· .persona  导入角色卡（文本 / PNG / JSON）\n"
         "· .mech     按当前角色卡生成机械提示词\n"
         "· .purge    清理本身份的运行态数据（保留人设 / 机械提示 / 身份端点）\n"
         "· .help     本帮助\n"
         "· .quit     退出（仅调试台）\n"
         "其它输入按自然对话交给助理。")


def _is_command(text: str) -> bool:
    return text.strip().startswith((".", "。"))


def _norm(text: str) -> str:
    t = text.strip()
    return t[1:].strip() if _is_command(t) else t


def command_word(text: str) -> str:
    """取指令首词（小写）；不是指令或为空时返回空串。"""
    if not _is_command(text):
        return ""
    t = _norm(text)
    return t.split(maxsplit=1)[0].lower() if t else ""


def is_compact(text: str) -> bool:
    """是否 `.compact`（手动压缩上下文）。因需 async + session，由 app 层拦截执行。"""
    return command_word(text) == "compact"


def is_purge(text: str) -> bool:
    """是否 `.purge`（清理运行态数据）。因需同步重置 app 层内存对象，由 app 层拦截执行。"""
    return command_word(text) == "purge"


def is_usage(text: str) -> bool:
    """是否 `.usage`（token 用量查询）。回执走来源通道，由 app 层拦截执行。"""
    return command_word(text) == "usage"


def _report() -> str:
    return f"当前时间 → {dt.datetime.now().isoformat(timespec='minutes')}"


def purge_uid(uid: str) -> str:
    """清理**指定身份**的运行态（会话/记忆/事项/账本/附件）。

    保留（自包含信息库，非缓存）：persona.md 人设、mech_prompts.json 机械提示、
    uid.json 身份端点。只动 `data/<uid>/`，绝不跨身份清理。
    """
    from qqlife.platform.identity import uid_valid
    uid = (uid or "").strip()
    if not uid_valid(uid):
        return f"身份标识无效：{uid!r}（未做任何清理）"
    home = DATA_DIR / uid
    if not home.is_dir():
        return f"身份 {uid} 没有可清理的运行态数据。"
    keep = {"persona.md", "mech_prompts.json", "uid.json"}
    removed: set[str] = set()
    for f in home.glob("*.json"):
        if f.name in keep:
            continue
        try:
            f.unlink()
            removed.add(f.name)
        except OSError:
            pass   # 文件被占用等：跳过该文件，不影响其余清理
    att = home / "attachments"
    if att.is_dir():
        for f in att.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
    return ("已清理身份 " + uid + " 的运行态数据：" + ("、".join(sorted(removed)) or "无")
            + "（保留：人设 / 机械提示 / 身份端点）")


def handle_command(text: str) -> str | None:
    """返回确认语；若不是指令/未匹配则返回 None。"""
    if not _is_command(text):
        return None
    low = command_word(text)

    if low == "time":
        return _report()

    if low == "status":
        s = runtime_state.snapshot()
        if s["status"] == "空闲":
            return "【当前状态】空闲"
        detail = f"（{s['detail']}）" if s.get("detail") else ""
        return (f"【当前状态】{s['status']}{detail}，"
                f"已进行 {s['elapsed']} 秒（起始 {s.get('at', '')}）")

    if low == "help":
        return _HELP

    # 以 . 或 。 开头但未识别：朴素 help 提示，不丢给 AI 自然语言
    return _HELP


async def handle(text: str) -> str | None:
    """兼容异步入口的统一处理。"""
    return handle_command(text)
