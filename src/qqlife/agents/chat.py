"""agents/chat.py：「主会话」技能（全融合，§对话）。

单一主会话：既聊天/RP，也能在一句话里内联办事（通过 OpenAI function-calling 调工具）。
- 用户说"明天记得提醒我 X" → 模型返回 tool_call(add_reminder) → 程序执行 → 用同一人格总结。
- 扮演不失断、安排不丢失。定时提醒(系统触发)不经过这里，走轻量措辞。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

from qqlife.agent_core.types import SkillResult
from qqlife.agents.chat_log import full_enabled, messages_summary, sanitize_messages, write_chat_log
from qqlife.agents.mantools import SCHEMAS, ToolExecutor
from qqlife.runtime_state import set_state
from qqlife.agents.promptkit import render
from qqlife.agents.skill_base import Skill
from qqlife.llm.budget import BudgetExceeded
from qqlife import mech_prompts, usage_rec

_MAX_TOOL_ROUNDS = 10
_SEARCH_FAIL_MARK = "__SEARCH_FAIL__"
# 仅当输出严格是空 <output></output> 时才是"主动静默"（晚安/结束）。模型空输出（什么都没产出）视为异常，不应静默。
_ACTIVE_SILENT = re.compile(r"^\s*<output>\s*</output>\s*$")
_MAX_EMPTY_RETRY = 2


def _status(text: str) -> None:
    """处理状态实时打印到命令行，便于定位"卡在哪一步"（等模型/等工具/等网络）。"""
    print(f"[{dt.datetime.now():%H:%M:%S}] [状态] {text}", flush=True)


def _chat_log(owner: str, text: str, messages: list[dict], traces: list[dict],
              final_reply: str, silent: bool, voices: list[dict] | None = None) -> None:
    """把一次主会话处理落盘到 logs/chat.log（追加、按 10MB×5 轮转）。

    默认只记请求摘要（角色条数 / 字符数 / 图片数）；`CHAT_LOG_FULL=1` 时记录完整 messages，
    但图片 base64 会替换为体积占位符。工具轨迹、语音段、最终输出始终记录。
    """
    lines = [
        f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] === 用户({owner}) ===",
        f"[in] {text}",
    ]
    if full_enabled():
        lines.append("[llm_req] " + json.dumps(sanitize_messages(messages), ensure_ascii=False))
    else:
        lines.append(messages_summary(messages))
    for t in traces:
        lines.append(f"[tool] {t.get('name')} args={t.get('args')} -> {t.get('result')}")
    if voices:
        for v in voices:
            lines.append(f"[voice] voice={v.get('voice') or '(默认音色)'} text={v.get('text')!r}")
    if silent:
        lines.append("[out] <output></output> （静默，不发送）")
    else:
        lines.append(f"[out] <output>{final_reply}</output>")
    write_chat_log("\n".join(lines) + "\n")


def _extract_outputs(content: str) -> tuple[list[str], bool]:
    """从模型输出中提取所有 <output>...</output> 块（每块是一条待发消息）。

    - 找到块：收集每个非空块；块外文字全部丢弃（含"（无回复）"占位杂讯）。
     ‎- 若模型把 <voice>…</voice> 误写进 <output> 块内，先剥掉标签，**绝不把标签原文发给用户**。
    - 没有任何非空块（含空块 <output></output>）：返回 ([], True) 表示本轮静默。
    - 没找到任何块（模型格式偶发崩）：宽容兜底，把去头尾后的全文当一条文本（同样剥内嵌 voice 标签）。
    """
    if not content:
        return [], True
    _strip_v = lambda s: _EMBEDDED_VOICE.sub("", s)
    blocks = re.findall(r"<output>(.*?)</output>", content, re.S)
    segs = [b.strip() for b in (_strip_v(b) for b in blocks) if b.strip()]
    if blocks:
        return segs, not segs          # 有块：按块拆分，全空即静默
    text = _strip_v(content).strip()   # 宽容兜底：无块则整段当文本
    return ([text] if text else [], not text)


# 内嵌在 <output> 块里的 <voice> 段（模型误放时剥掉，防止标签原文外发）
_EMBEDDED_VOICE = re.compile(r"<voice\b[^>]*>.*?</voice>", re.S)


def _extract_voices(content: str) -> list[dict]:
    """从模型输出中提取语音段 <voice voice="angry|sad|calm">一句自然英文</voice>。

    - 标签解析宽松：voice 属性可双引号/单引号/无引号/缺失（缺失回退默认音色），文本可为多行。
    - 模型在合适时机（如重要提醒、道别、情绪价值）主动输出；无此标签 → 空列表 = 纯文本回复。
    """
    if not content:
        return []
    out: list[dict] = []
    for m in re.finditer(r"<voice\b([^>]*)>(.*?)</voice>", content, re.S):
        attrs, text = m.group(1), (m.group(2) or "").strip()
        if not text:
            continue
        vm = re.search(r"""voice\s*=\s*["']?([^"'\s>]+)""", attrs)
        name = (vm.group(1) if vm else "").strip()
        out.append({"voice": name, "text": text})
    return out


class MainSkill(Skill):
    name = "chat"
    backend = "deepseek"
    prompt_file = "prompts/skill_chat.md"

    def __init__(self, card: dict | None = None) -> None:
        self.card = card or {}
        self.persona: str = ""
        self._stores: dict[str, TaskStore] = {}

    def set_persona(self, text: str) -> None:
        self.persona = text or ""

    def load_persona_file(self, path: str) -> None:
        """读取人类可编辑的 Markdown 人设文件作为 persona（推荐）。"""
        from pathlib import Path
        self.persona = Path(path).read_text(encoding="utf-8").strip()

    def set_card(self, card: dict | None) -> None:
        self.card = card or {}

    def load_card(self, path: str) -> None:
        """从 ST 卡导入 persona（作一次性种子；优先使用 load_persona_file）。"""
        from qqlife.agents.character_card import load_character_card, to_persona
        self.card = load_character_card(path)
        self.persona = to_persona(self.card)

    def _system(self, ctx: dict) -> str:
        # 多用户：route 时按 uid 注入 ctx["persona"]（user_home/persona.md 文本）；
        # 缺省回退 self.persona（全局 / ST 卡种子）
        persona = ctx.get("persona") or self.persona
        return render("chat", PERSONA=persona)

    def _executor(self, ctx: dict) -> ToolExecutor:
        from qqlife.task_flow.task_store import TaskStore
        owner = ctx.get("target") or "me"
        # store 按 target 动态隔离：owner 与 ctx 主 store 一致则复用（保持磁带/调度共享同一实例）；
        # 否则为其它用户单独建 store（真多用户隔离）。
        store = ctx.get("store")
        if store is None or getattr(store, "owner", None) != owner:
            store = self._stores.get(owner)
            if store is None:
                store = TaskStore.for_owner(owner)
                self._stores[owner] = store
        return ToolExecutor(store=store, hub=ctx.get("hub"), owner=owner,
                            session=ctx.get("session"), cfg=ctx.get("cfg"),
                            mech=ctx.get("mech"))

    async def run(self, ctx: dict) -> SkillResult:
        hub = ctx.get("hub")
        llm = hub.for_kind("chat") if hub else None
        # per-uid 机械提示表（user_home/mech_prompts.json，缺省回退内置 BUILTIN）——全部固定文案的唯一来源
        mech = ctx.get("mech") or mech_prompts.BUILTIN
        if not (llm and llm.is_available()):
            return SkillResult(reply=mech.get("backend_unready", ""))
        executor = self._executor(ctx)
        owner = ctx.get("target", "me")
        ephemeral = bool(ctx.get("ephemeral"))
        session = ctx.get("session")
        messages = [{"role": "system", "content": self._system(ctx)}]
        # 注入顺序 = 缓存命中率设计（DeepSeek 前缀缓存，64 token 块匹配，10.16 §A2）：
        # 稳定内容在前且只追加（人设 → 长期记忆 facts → 待固化 new → 历史 turns），
        # 每轮变化的内容（当前时间）放历史之后——否则时间一变，其后记忆与全部历史都无法命中缓存。
        # facts 仅 fold 时变；new 仅工具写入时尾部追加（该轮 turns 缓存失效一次，频次低）。
        rows = session.prompt_context(owner) if session else []
        messages += list(rows)
        # 固定注入当前时间（避免模型去猜/依赖 get_current_time 工具）；放最后不打断稳定前缀
        messages.append({"role": "system", "content": f"【当前时间】{dt.datetime.now():%Y-%m-%d %H:%M}（周{dt.datetime.now().isoweekday()}）"})
        # 用户消息：纯文本，或带图时拼成 OpenAI 规范数组块（deepseek-flash 支持看图）
        uc: dict | list
        if ctx.get("images"):
            uc = [{"type": "text", "text": ctx.get("text", "")}]
            for im in ctx["images"]:
                mime = im.get("mime") or "image/jpeg"
                b64 = im.get("b64") or ""
                if b64:
                    uc.append({"type": "image_url",
                               "image_url": {"url": f"data:{mime};base64,{b64}"}})
        else:
            uc = ctx.get("text", "")
        messages.append({"role": "user", "content": uc})
        n = 0
        outputs: list[str] = []
        voices: list[dict] = []
        silent = False
        aborted = False
        empty_n = 0
        raw = ""
        tokens = ctx["cfg"].chat_max_tokens if ctx.get("cfg") else 4096
        traces: list[dict] = []
        _status("进入主会话处理")
        set_state("处理中", "进入主会话")
        while n < _MAX_TOOL_ROUNDS:
            n += 1
            _status(f"等模型回复中（第 {n} 轮）")
            set_state("等待模型回复", f"第{n}轮")
            usage: dict = {}   # llm 层透传的本次调用用量（命中/未命中/输出，10.16 §A3）
            try:
                msg = await llm.complete(messages, tools=SCHEMAS, max_tokens=tokens, usage=usage)
            except BudgetExceeded:
                # 本地额度拦截（非网络故障）：窗口未过，重试必然再被拦 → 直接回限流提示，
                # 不再误报成"网络不稳"（此前每次密集连发都会被错报为模型调用失败）
                _status("本地额度拦截（分钟/会话 token 预算打满），不重试")
                set_state("空闲", "额度拦截")
                fallback = mech.get("rate_limited", "")
                _chat_log(owner, ctx.get("text", ""), messages, [], fallback, False)
                return SkillResult(reply=fallback, messages=[fallback], raw="ERR BudgetExceeded")
            except Exception as e:
                # 网络超时/临时故障：重试一次；仍失败回友好提示，绝不让用户收不到回复
                _status(f"模型调用失败（{type(e).__name__}: {e}），重试一次")
                try:
                    await asyncio.sleep(1.0)
                    msg = await llm.complete(messages, tools=SCHEMAS, max_tokens=tokens, usage=usage)
                except Exception as e2:
                    _status(f"模型调用仍失败：{type(e2).__name__}")
                    set_state("空闲", "模型调用失败")
                    fallback = mech.get("network_fail", "")
                    _chat_log(owner, ctx.get("text", ""), messages, [], fallback, False)
                    return SkillResult(reply=fallback, messages=[fallback],
                                       raw=f"ERR {type(e2).__name__}: {e2}")
            if usage:
                usage_rec.record(owner, usage)   # 落盘 data/<uid>/usage.json（.usage 可查）
            if not msg.get("tool_calls"):
                content = msg.get("content") or ""
                thinking = msg.get("reasoning_content") or ""
                raw = ("<thinking>\n" + thinking + "\n</thinking>\n" if thinking else "") + content
                if _ACTIVE_SILENT.match(content):
                    # 显式空块 = 主动静默（晚安/结束），不回复
                    outputs, silent = [], True
                    break
                if not content.strip():
                    # 模型空输出（意外）：重试；反复空则兜底回一句，绝不无声消失
                    empty_n += 1
                    _status(f"模型输出为空，重试中（{empty_n}/{_MAX_EMPTY_RETRY}）")
                    if empty_n > _MAX_EMPTY_RETRY:
                        outputs, silent = [mech.get("empty_output", "")], False
                        _status("空输出重试到上限，回兜底提示")
                        break
                    await asyncio.sleep(0.3)
                    continue
                outputs, silent = _extract_outputs(content)
                voices = _extract_voices(content)
                break
            messages.append(msg)
            net_notified = False
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                if name in ("web_search", "weather"):
                    _status("联网搜索中…（等待网络响应）")
                    set_state("联网搜索", name == "weather" and "查天气" or "联网搜索")
                    # 先向用户发一条"正在搜索"的机械提示（仅提供 net_notify 回调时）
                    if not net_notified and ctx.get("net_notify"):
                        try:
                            await ctx["net_notify"](mech.get("searching", ""))
                        except Exception:
                            pass
                        net_notified = True
                else:
                    _status(f"执行工具：{name}")
                    set_state("调用工具", name)
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    result = await executor.call(name, args)
                except Exception as e:
                    args = {}
                    result = f"ERR: {type(e).__name__}: {e}"
                # 失败标记（__SEARCH_FAIL__/__WEATHER_FAIL__/执行异常 ERR:）一律不报"完成"，避免误导排查
                if isinstance(result, str) and result.startswith(("__SEARCH_FAIL__", "__WEATHER_FAIL__", "ERR:")):
                    _status(f"工具失败：{name}（{result[:80]}）")
                else:
                    _status(f"工具完成：{name}")
                traces.append({"name": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(result)})
                # 搜索失败：不再让模型拿着失败结果再组织一次（省时间），直接回固定提示
                if name in ("web_search", "weather"):
                    mark = _SEARCH_FAIL_MARK if name == "web_search" else "__WEATHER_FAIL__"
                    if str(result).startswith(mark):
                        outputs = [mech.get("search_fail", "")]
                        silent = False
                        aborted = True
            if aborted:
                break
        if not silent and not outputs:
            outputs = [mech.get("tool_loop_limit", "")]
        _status("静默（本轮不回复）" if silent else ("回复完成" if outputs else "无输出"))
        set_state("空闲", "静默" if silent else "完成")
        reply = outputs[0] if outputs else ""
        _chat_log(owner, ctx.get("text", ""), messages, traces, " | ".join(outputs), silent, voices)
        # 写回会话（内部瞬时调用如提醒/关怀 ephemeral 不落盘）；达硬上限安全网则自动 fold
        if session and not ephemeral:
            session.add(owner, "user", ctx.get("text", ""))
            # 多气泡回复要整体入档：只存第一段会让后续轮次看不到自己说过的后半句
            full_reply = "\n".join(outputs) if outputs else ""
            if full_reply:
                session.add(owner, "assistant", full_reply)
            session.save()
            from qqlife.agents.memory_service import maybe_compact
            cfg = ctx.get("cfg")
            compressed = await maybe_compact(
                session, owner, hub,
                turn_hard=cfg.dialogue_turn_hard if cfg else 300)
        return SkillResult(reply=reply, messages=outputs, raw=raw, voices=voices)