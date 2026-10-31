"""qqlife.app：应用运行时（装配 + 通道强依赖启动 + 事件主流程；CLI 仅调试台，10.16 §7）。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

from qqlife._paths import PROJECT_ROOT, user_home
from qqlife import bootstrap, mech_prompts
from qqlife.agents.promptkit import render
from qqlife.binding import BindingCommands
from qqlife.bootstrap import (acquire_single_instance, broadcast_notice, build_context,
                              load_env, ts as _ts)
from qqlife.commands import (command_word, handle_command, is_compact, is_purge,
                             is_usage)
from qqlife.mech_gen import generate_and_save
from qqlife.persona_import import PersonaImport
from qqlife.pipeline import (MsgAggregator, chunks as _chunks, derive_title,
                             extract_title, format_reminder)
from qqlife.runtime import RuntimeRegistry

# 主动关怀的内部触发指令：视同用户发来这句，交给主会话（ephemeral）决定说什么/是否静默
_CARE_PROMPT = render("care_prompt").strip()


def _is_command(text: str) -> bool:
    return text.strip().startswith((".", "。"))



async def amain(logger) -> int:
    base = build_context(logger)
    cfg = base["cfg"]
    orch = base["orch"]
    master = cfg.master_uid   # CLI 直通身份 + 提醒兜底；通道端点经 .create/.link 绑定（identity）
    # MASTER_UID 格式校验（10.16 §5）：非法即告警退出，不落到磁盘/身份层
    from qqlife.platform.identity import uid_valid
    if not uid_valid(master):
        logger.error(f"MASTER_UID={master!r} 非法（要求：小写字母开头，仅小写字母/数字/下划线，"
                     "≤16 字符），已退出。请修改 .env 的 MASTER_UID。")
        return 2
    logger.info(f"默认身份 MASTER_UID={master}（通道端点需 .create/.link 绑定）")

    from qqlife.agent_core.event_bus import event_bus
    from qqlife.agent_core.types import EventType, NotifyResult, NotifyTask

    # ---- per-uid runtime 注册表：store/occ/gate/scheduler/session/persona 各自独立 ----
    # 同一 uid 的 scheduler 与 chat 共享同一 store 实例，避免并发写 items.json 竞态。
    # session 落在 uid 家目录（user_home(uid)/session.json，facts/new/turns 统一记忆，10.16 §A2）。
    registry = RuntimeRegistry(base=base, logger=logger)
    _ensure_runtime = registry.ensure
    _ctx_for = registry.ctx

    # outbound 在通道装配段创建；_emit 为提醒/关怀统一出口（广播到 uid 全部已验证端点）
    # 返回值表示是否至少有一个端点送达；提醒的落盘与否由调用方据此决定。
    async def _emit(uid: str, text: str) -> bool:
        print(f"[{_ts()}] [发->{uid}] {text}")
        return bool(await outbound.notify(uid, text))

    async def _manual_compact(uid: str) -> str:
        """`.compact`：无条件 fold 当前 uid 的会话（任意长度可用），返回统计。

        回执文案来自该 uid 的机械提示表（compact_done / compact_none / compact_fail）。
        """
        from qqlife.agents.memory_service import force_compact
        r = _ensure_runtime(uid)
        mech = r["mech"]
        stats = await force_compact(r["session"], uid, base["hub"])
        if stats["compressed"]:
            return mech_prompts.fill(mech.get("compact_done", ""),
                                     before=stats["before"], facts=stats["facts"])
        if stats["before"] == 0:
            return mech.get("compact_none", "")
        return mech.get("compact_fail", "")

    async def _clean_all(uid: str) -> str:
        """`.purge`：只清理**本身份**的运行态文件 + 同步重置该 uid 的内存对象（防旧数据写回磁盘）。"""
        from qqlife import commands as _cmds
        msg = _cmds.purge_uid(uid)
        r = _ensure_runtime(uid)
        r["store"].items.clear()
        r["store"].expenses.clear()
        r["session"]._data = {"facts": {}, "new": [], "turns": [],
                              "last_compact_at": 0, "compact_hint_at": 0}
        return msg + "；内存态已同步重置。"

    async def _run_mech(uid: str) -> str:
        """`.mech`：按当前角色卡生成机械提示词并立即生效。"""
        r = _ensure_runtime(uid)
        persona = (r.get("persona") or "").strip()
        if not persona:
            return "当前没有人设，请先发送 .persona 导入角色卡。"

        async def _llm(system: str, user: str) -> str:
            llm = base["hub"].for_kind("chat") or base["hub"].for_kind("evolve")
            if llm is None:
                raise RuntimeError("模型服务未配置或不可用")
            return await llm.chat([{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                                  json_mode=True, max_tokens=2000)

        try:
            _merged, stats, backup = await generate_and_save(user_home(uid), persona, _llm)
        except Exception as e:
            return f"机械提示词生成失败：{type(e).__name__}: {e}"
        r["mech"] = mech_prompts.load_table(user_home(uid))
        lines = [f"机械提示词已生成：{stats['used']} 条采用角色口吻，"
                 f"{stats['fallback']} 条回退基准。",
                 f"保存：data/{uid}/mech_prompts.json"]
        if stats.get("dropped"):
            lines.append("已丢弃额外键：" + "、".join(stats["dropped"]))
        if backup:
            lines.append(f"旧文件备份：{backup}")
        return "\n".join(lines)

    async def on_reminder_due(evt, data: NotifyTask) -> bool:
        # 所有提醒统一走主会话（ephemeral）现场拟稿/确认，保持连贯；
        # 发送后经 gate 通过即外发；已完成/已提醒可凭 item read(含已完成) 的 done_at 确认。
        # 返回 True = 已处理（可落盘），False = 本次未送达（调度器保留并重试）。
        owner = data.target or master
        r = _ensure_runtime(owner)
        raw = data.content or data.trigger
        if data.draft:
            # 重试只重发、不重新拟稿（draft 已是含标题的最终外发文案）
            text = data.draft
        else:
            cctx = _ctx_for(owner)
            chat = (orch.skills or {}).get("chat")
            # 让模型按“通知栏”格式写：标题是用户视角短标题（如「喝水提醒」），正文一句话；
            # 标题单独放在 <title> 里，不进入 <output>，避免把助手口吻写进通知。
            injected = (
                f"【提醒】{raw}\n"
                "（系统触发，不是用户发言。请输出一条通知栏提醒："
                "先写 <title>用户视角的短标题，以「提醒」结尾，如「喝水提醒」「交作业提醒」</title>，"
                "再写 <output>一句自然正文——用你自己的口吻叫他，可以带一点情绪（催、损、关心都行），"
                "不要官腔；不要复述标题，不要用「提醒你」或「提醒TA」开头。</output>）"
            )
            title, body = "", raw
            if chat:
                try:
                    res = await chat.run(cctx | {"text": injected,
                                                 "target": owner, "ephemeral": True})
                    segs = res.messages or ([res.reply] if res.reply else [])
                    body = "\n".join(segs).strip() or raw
                    title = extract_title(getattr(res, "raw", "") or "")
                except Exception:
                    body = raw
            text = format_reminder(title or derive_title(raw), body,
                                   r["mech"].get("reminder_label", "【{title}】"))
            data.draft = text
        n = NotifyTask(kind="oneoff", content=text, target=owner)
        gate_result = r["gate"].try_notify(n)
        if gate_result is NotifyResult.SENT:
            return await _emit(owner, n.content)
        if gate_result is NotifyResult.REJECT:
            return True   # muted / 静音期：用户主动静默，视为已处理，落盘跳过
        if gate_result is NotifyResult.DROPPED:
            return True   # 无需发出
        return False      # DEFER：保留待重试

    async def on_proactive(evt, owner: str | None):
        """主动关怀：每个白名单用户的独立 scheduler tick(数据=owner) 触发。

        对 owner 自己的 gate/pa（勿扰/日上限/最小间隔各自独立）评估；产出为空或机械兜底文案时静默。
        """
        owner = owner or master
        r = _ensure_runtime(owner)
        pa = r["pa"]
        cfgp = cfg
        today = str(dt.date.today())
        if pa["date"] != today:
            pa["date"], pa["count"] = today, 0
        cap = cfgp.proactive_daily_cap
        if cap > 0 and pa["count"] >= cap:
            return
        interval = cfgp.proactive_min_interval
        now_ts = dt.datetime.now().timestamp()
        if pa["last"] and (now_ts - pa["last"]) < interval * 60:
            return
        chat = (orch.skills or {}).get("chat")
        if not chat:
            return
        cctx = _ctx_for(owner)
        try:
            res = await chat.run(cctx | {"text": _CARE_PROMPT, "target": owner,
                                         "ephemeral": True})
        except Exception as e:
            print(f"[{_ts()}] [错误] 主动关怀失败：{type(e).__name__}: {e}")
            return
        segs = res.messages or ([res.reply] if res.reply else [])
        text = "\n".join(segs).strip()
        if not text or text.startswith("（"):
            return   # 静默或机械兜底文案，不发
        n = NotifyTask(kind="care", content=text, target=owner)
        if r["gate"].try_notify(n) is NotifyResult.SENT:
            if await _emit(owner, n.content):
                pa["count"] += 1
                pa["last"] = now_ts

    event_bus.subscribe(EventType.REMINDER_DUE, on_reminder_due)
    event_bus.subscribe(EventType.PROACTIVE_TICK, on_proactive)
    # 通道驱动 / 调试台任务表（per-uid 调度器任务由 RuntimeRegistry 持有并在 stop() 中取消）
    bg_tasks: list[asyncio.Task] = []
    # 语音合成后台任务表：持有强引用防止任务被 GC（Python 3.10 下 create_task 无引用会被回收，
    # 长任务 await I/O 期间弱引用任务会中途消失 → 语音合成永远不执行）。
    _bg_voice: set[asyncio.Task] = set()

    # ---- 通道装配 + 身份中心 + 出站路由（通道扩展三步法见 开发文档/开发提示与约定.md §7）----
    from qqlife.platform.registry import assemble
    from qqlife.platform.identity import Identity
    from qqlife.platform.outbound import OutboundSender
    bundles = assemble(logger)
    identity = Identity()
    outbound = OutboundSender(bundles, identity)
    # 运行期引用交给模块级：进程退出（Ctrl+C）时 main 用它补发关闭通知
    bootstrap.set_last_app(identity, outbound)
    # 调度告警 / 补发汇总统一走 outbound 广播（registry 不直接依赖 outbound）
    registry.on_notice = lambda uid, text: _emit(uid, text)
    input_drivers = [b.input for b in bundles.values() if b.input is not None]
    if not identity.uids():
        logger.info("身份中心：空表——首次使用请在任一入站通道发送 .create <标识码> 建档")

    # ---- 启动即恢复已绑定身份的后台调度 ----
    # 否则进程重启后，只要用户不发消息，_check_loop / _proactive_loop 就不存在，
    # 当天所有定时提醒与主动关怀都会静默。只恢复“有已验证端点且该通道有出站驱动”的 uid：
    # 无交付通道的空档案不启动，避免到期后白跑一次 LLM 再失败。
    active_channels = set(outbound.output_channels())
    restored = 0
    for uid in identity.uids():
        try:
            if not any(ep.get("channel") in active_channels
                       for ep in identity.endpoints(uid, verified_only=True)):
                continue
            _ensure_runtime(uid)
            restored += 1
        except Exception as e:
            logger.error(f"恢复身份 {uid} 的后台调度失败：{type(e).__name__}: {e}")
    if restored:
        logger.info(f"已恢复 {restored} 个已绑定身份的后台调度")

    # ---- 身份指令族（带副作用，普通指令分发前拦截；对未绑定端点开放）----
    binder = BindingCommands(identity=identity, bundles=bundles,
                             outbound=outbound, registry=registry)
    persona_import = PersonaImport(registry=registry)

    # 连接成功事件：启动强依赖等它置位（10.16 §7）；由通道连接回调填充，主循环等待
    connected_evt = asyncio.Event()
    if input_drivers:
        try:
            from qqlife.platform.dedupe import deduper

            # 端点合并缓冲：同一端点连续消息合并成批（key = channel:remote_id）
            _ep_info: dict[str, tuple[str, str, str]] = {}

            async def _flush(key: str, text: str) -> None:
                channel, remote_id, uid = _ep_info[key]
                await on_batch(channel, remote_id, uid, text)

            async def on_batch(channel, remote_id, uid, text, images=None):
                from qqlife.agent_core.types import InboundMessage
                r = _ensure_runtime(uid)               # per-uid runtime（mech 表 + session）
                mech = r["mech"]
                print(f"[{_ts()}] [收<-{uid}] {text}")
                b = text.strip()
                # `.compact` 手动压缩（async，需 session/hub）
                if is_compact(b):
                    reply = await _manual_compact(uid)
                    print(f"[{_ts()}] [发->{uid}] {reply}")
                    await outbound.reply(channel, remote_id, reply)
                    return
                # `.purge` 清理（文件 + 内存同步重置，必须在 app 层做；只清当前身份）
                if is_purge(b):
                    reply = await _clean_all(uid)
                    print(f"[{_ts()}] [发->{uid}] {reply}")
                    await outbound.reply(channel, remote_id, reply)
                    return
                # `.usage` 用量查询（上次请求 + 累计命中占比，回执走来源通道）
                if is_usage(b):
                    from qqlife import usage_rec
                    reply = usage_rec.report(uid)
                    print(f"[{_ts()}] [发->{uid}] {reply}")
                    await outbound.reply(channel, remote_id, reply)
                    return
                # 所有普通指令（.time/.status/.help 等）在通道端直接执行返回，不误入模型
                cmd = handle_command(b) if _is_command(b) else None
                if cmd:
                    print(f"[{_ts()}] [发->{uid}] {cmd}")
                    await outbound.reply(channel, remote_id, cmd)
                    return
                try:
                    async def _net_notify(text: str):
                        print(f"[{_ts()}] [发->{uid}] {text}")
                        await outbound.reply(channel, remote_id, text)
                    res = await orch.route(InboundMessage(platform=channel, target=uid, text=text, images=images or []),
                                           _ctx_for(uid) | {"text": text, "target": uid, "course": None,
                                                              "images": images or [], "net_notify": _net_notify})
                except RuntimeError as e:
                    # 预算/防循环等拦截：告知用户而非静默，并避免后台任务以异常结束
                    print(f"[{_ts()}] [错误] 处理被拦截：{e}")
                    await outbound.reply(channel, remote_id, mech.get("rate_limited", ""))
                    return
                except Exception as e:
                    # 其余异常（网络故障等）：同样回一句兜底，绝不让用户收不到回复
                    print(f"[{_ts()}] [错误] {type(e).__name__}: {e}")
                    await outbound.reply(channel, remote_id, mech.get("process_error", ""))
                    return
                segs = res.messages or ([res.reply] if res.reply else [])
                if segs:
                    # 长文本拆条：尊重模型 <output> 分块；单块仍超限则按 cfg 上限硬切
                    limit = cfg.message_split_chars
                    pieces = [p for seg in segs for p in _chunks(seg, limit)]
                    gap = cfg.message_send_gap
                    for i, piece in enumerate(pieces):
                        print(f"[{_ts()}] [发->{uid}] {piece}")
                        await outbound.reply(channel, remote_id, piece)
                        # 连发之间停留，避免网络/平台乱序与限流
                        if gap and i < len(pieces) - 1:
                            await asyncio.sleep(gap)
                # 语音段：文本已发，语音后台合成后补发（不阻塞/慢）；不支持语音的通道自动降级跳过
                voices = getattr(res, "voices", None) or []
                if voices:
                    _vsummary = [(v.get("voice") or "默认音色", (v.get("text") or "")[:40]) for v in voices]
                    print(f"[{_ts()}] [语音] 模型产出 {len(voices)} 段语音：{_vsummary}")
                    async def _speak_voices(_channel, _rid, _uid, _voices):
                        for v in _voices:
                            vtext = (v.get("text") or "").strip()
                            if not vtext:
                                print(f"[{_ts()}] [语音] 该段语音文本为空，跳过")
                                continue
                            out_dir = user_home(_uid) / "attachments"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            # 下一次使用 TTS 时清理上一次的 wav 产物（发送后已无用，防磁盘只增不减）
                            for _old in out_dir.glob("tts_*.wav"):
                                try:
                                    _old.unlink()
                                except OSError:
                                    pass
                            out_path = str(out_dir / f"tts_{dt.datetime.now():%H%M%S}_{uuid.uuid4().hex[:6]}.wav")
                            wav = await base["tts"].synthesize(
                                vtext, v.get("voice") or cfg.tts_voice, out_path)
                            if not wav:
                                print(f"[{_ts()}] [语音] 合成未产出音频（原因见上方 [tts] 日志），跳过发送")
                                continue
                            print(f"[{_ts()}] [发->{_uid}] [语音] {vtext}")
                            ok = await outbound.reply_record(_channel, _rid, wav)
                            if not ok:
                                print(f"[{_ts()}] [语音] 发送失败（重试耗尽），文件仍留在 {out_path}")
                    _vtask = asyncio.create_task(_speak_voices(channel, remote_id, uid, voices))
                    _bg_voice.add(_vtask)
                    _vtask.add_done_callback(_bg_voice.discard)
                # 软压缩提醒（10.16 §A3）：turns 达阈值发一次机械提示；fold 后周期自动重置
                n_hint = r["session"].take_compact_hint(uid, cfg.compact_hint_turns)
                if n_hint:
                    hint = mech_prompts.fill(mech.get("compact_hint", ""), n=n_hint)
                    print(f"[{_ts()}] [发->{uid}] {hint}")
                    await outbound.reply(channel, remote_id, hint)

            aggregator = MsgAggregator(cfg.message_merge_seconds, _flush)

            async def on_message(msg):
                # 去重：飞书重推（至少一次投递）会带相同 create_time（毫秒），OneBot 用 time（秒）
                _t = float(msg.raw.get("create_time") or msg.raw.get("time") or 0) if isinstance(msg.raw, dict) else 0.0
                if _t > 1e12:  # 毫秒时间戳（飞书）→ 秒
                    _t = _t / 1000.0
                channel, remote_id = msg.platform, str(msg.target)
                # 去重 key 必须含通道 + 端点：否则不同用户同秒发相同文本会互相吞消息
                if deduper.is_dup(channel, remote_id, msg.text, _t):
                    return
                uid = identity.resolve(channel, remote_id)
                images = getattr(msg, "images", None) or []
                # 人设导入模式：优先于所有指令与图片处理
                if persona_import.active(channel, remote_id):
                    reply = persona_import.feed(channel, remote_id, uid, msg.text, images)
                    if reply is not None:
                        print(f"[{_ts()}] [发->{remote_id}] {reply}")
                        await outbound.reply(channel, remote_id, reply)
                    return
                # PNG 角色卡检测（已绑定端点；非角色卡继续按普通图片走多模态）
                if uid is not None and images:
                    card_reply = persona_import.cache_from_image(channel, remote_id, images[0])
                    if card_reply:
                        print(f"[{_ts()}] [发->{remote_id}] {card_reply}")
                        await outbound.reply(channel, remote_id, card_reply)
                        return
                # 身份指令族：.create 对未绑定端点开放；.link/.profile/.unlink
                bind_reply = await binder.handle(channel, remote_id, uid, msg.text)
                if bind_reply is not None:
                    if command_word(msg.text) in {"create", "link", "unlink"}:
                        persona_import.clear(channel, remote_id)
                    print(f"[{_ts()}] [发->{remote_id}] {bind_reply}")
                    await outbound.reply(channel, remote_id, bind_reply)
                    return
                # 角色卡导入 / 机械提示词生成（未绑定时也给出明确指引）
                persona_reply = await persona_import.handle(channel, remote_id, uid,
                                                            msg.text, images)
                if persona_reply is not None:
                    print(f"[{_ts()}] [发->{remote_id}] {persona_reply}")
                    await outbound.reply(channel, remote_id, persona_reply)
                    return
                if command_word(msg.text) == "mech":
                    reply = ("当前端点未绑定。发送 .create <标识码> 创建身份。"
                             if uid is None else await _run_mech(uid))
                    print(f"[{_ts()}] [发->{remote_id}] {reply}")
                    await outbound.reply(channel, remote_id, reply)
                    return
                if uid is None:
                    # 身份边界：未绑定会话不进任何处理，只回绑定引导（无 uid，只能用内置文案）
                    print(f"[{_ts()}] [隔离] 未绑定端点 {channel}:{remote_id}：{msg.text!r}")
                    await outbound.reply(channel, remote_id, mech_prompts.get("unbound_guide"))
                    return
                key = f"{channel}:{remote_id}"
                _ep_info[key] = (channel, remote_id, uid)
                # 普通指令不参与消息合并：立即执行，避免等待聚合超时
                if command_word(msg.text) in {"compact", "purge", "usage",
                                              "time", "status", "help"}:
                    await on_batch(channel, remote_id, uid, msg.text)
                    return
                # 带图消息不合并：图片本就该整条处理（base64 已就绪），直接走 on_batch。
                # 纯图片无文字时补一句占位，避免会话里落一条空 user 轮、也让模型有上下文。
                if getattr(msg, "images", None):
                    await on_batch(channel, remote_id, uid, msg.text or "（发来一张图片）",
                                   images=msg.images)
                else:
                    await aggregator.put(key, msg.text)

            for drv in input_drivers:
                drv.set_message_handler(on_message)
                if hasattr(drv, "set_connected_callback"):
                    async def _on_conn(ok, _c=drv.channel):
                        if ok:
                            connected_evt.set()
                            print(f"[{_ts()}] [状态] {_c} 通道已连接 → 事件模式（消息实时显示，Ctrl+C 退出）")
                        else:
                            print(f"[{_ts()}] [状态] {_c} 通道断开，正在重连…")
                    drv.set_connected_callback(_on_conn)
                bg_tasks.append(asyncio.create_task(drv.start()))
                logger.info(f"{drv.channel} 通道后台连接中…（断线自动重连）")
        except Exception as e:
            logger.warning(f"入站管线初始化失败：将继续等待连接（Ctrl+C 退出排查）：{e}")
    else:
        logger.info("未配置任何入站通道（INPUT_CHANNELS 空）。")

    # ---- CLI 调试台（10.16 §7：显式开启 DEBUG_CLI=1 才读 stdin；默认终端只输出不接收指令）----
    debug_cli = (os.getenv("DEBUG_CLI") or "").strip().lower() in ("1", "true", "yes", "on")

    async def _debug_console() -> None:
        """stdin 调试台：以 MASTER_UID 直通主会话；打印完整 raw（含思考过程），便于调试。"""
        loop = asyncio.get_event_loop()
        from qqlife.agent_core.types import InboundMessage
        from qqlife import usage_rec
        logger.info(f"调试台开启：stdin 以 {master} 直通主会话（.quit 退出调试台，不影响主流程）")
        while True:
            line = (await loop.run_in_executor(None, input, "调试台> ")).strip()
            if line in (".quit", "。quit"):
                print(f"[{_ts()}] [状态] 调试台已退出（主流程继续，Ctrl+C 结束程序）")
                return
            if persona_import.active("cli", ""):
                reply = persona_import.feed("cli", "", master, line, [])
                if reply is not None:
                    print(f"[{_ts()}] CMD> {reply}")
                continue
            bind_reply = await binder.handle("cli", "", master, line)
            if bind_reply is not None:
                print(f"[{_ts()}] CMD> {bind_reply}")
                continue
            persona_reply = await persona_import.handle("cli", "", master, line, [])
            if persona_reply is not None:
                print(f"[{_ts()}] CMD> {persona_reply}")
                continue
            if command_word(line) == "mech":
                print(f"[{_ts()}] CMD> {await _run_mech(master)}")
                continue
            if is_compact(line):
                print(f"[{_ts()}] CMD> {await _manual_compact(master)}")
                continue
            if is_purge(line):
                print(f"[{_ts()}] CMD> {await _clean_all(master)}")
                continue
            if is_usage(line):
                print(f"[{_ts()}] CMD> {usage_rec.report(master)}")
                continue
            cmd = handle_command(line)
            if cmd is not None:
                print(f"[{_ts()}] CMD> {cmd}")
                continue
            try:
                res = await orch.route(InboundMessage(platform="cli", target=master, text=line),
                                       _ctx_for(master) | {"text": line, "target": master})
            except Exception as e:
                print(f"[{_ts()}] [错误] {type(e).__name__}: {e}")
                continue
            _raw = getattr(res, "raw", "") or "\n".join(res.messages or ([res.reply] if res.reply else []))
            print(f"[{_ts()}] AGENT>", _raw or "(空)")

    if not input_drivers:
        # 无入站通道：不回退交互（强依赖设计，10.16 §7）；用户指令唯一入口=接收端
        if debug_cli:
            try:
                await _debug_console()
            finally:
                await registry.stop()
                for t in bg_tasks:
                    t.cancel()
            return 0
        logger.error("未配置任何入站通道且未开启 DEBUG_CLI：用户指令唯一入口是接收端"
                     "（消息以 . 或 。 开头），无接收端无法运行，退出。")
        return 1

    # ---- 启动强依赖（10.16 §7）：等入站通道连接成功；未连上循环提醒重试，不回退交互 ----
    interval = max(5.0, float(os.getenv("CONNECT_RETRY_INTERVAL") or 30))
    n_try = 0
    while not connected_evt.is_set():
        n_try += 1
        try:
            await asyncio.wait_for(connected_evt.wait(), timeout=interval)
        except asyncio.TimeoutError:
            print(f"[{_ts()}] [状态] 入站通道尚未连接（第 {n_try} 次检查，每 {interval:g}s 一次；"
                  "请检查凭据/网络，Ctrl+C 退出）", flush=True)
    print(f"[{_ts()}] [状态] 已进入事件模式（{('、'.join(d.channel for d in input_drivers))}），"
          "stdin 禁用（终端仅输出调试信息）；Ctrl+C 退出", flush=True)
    # 启动通知：连接建立后向全部已绑定端点报一声（文案取自 mech 表的 startup_notice）
    await broadcast_notice(identity, outbound, "startup_notice")
    if debug_cli:
        bg_tasks.append(asyncio.create_task(_debug_console()))
    try:
        await asyncio.Event().wait()
    finally:
        await registry.stop()
        for t in bg_tasks:
            t.cancel()
        # 正常退出路径补发关闭通知（Ctrl+C 的路径由 main 兜底，notified 保证不重复）
        await bootstrap.shutdown_notice()
    return 0


def main() -> int:
    os.chdir(PROJECT_ROOT)  # 以仓库根为工作目录
    if not acquire_single_instance():
        print("检测到已有实例正在运行，本进程已终止。"
              "（同一 App ID 不允许多实例：通道事件会被多实例分摊，表现酷似丢消息。）",
              flush=True)
        return 1
    from qqlife.agent_core.logger import init_logging, shutdown_logging
    logger = init_logging()
    load_env()
    try:
        rc = asyncio.run(amain(logger))
        print(f"exit_code={rc}")
        return rc
    except KeyboardInterrupt:
        # Ctrl+C：asyncio.run 会中断主协程（amain 的 finally 不一定跑到）→ 此处新开一次短事件循环补发关闭通知
        try:
            asyncio.run(bootstrap.shutdown_notice())
        except Exception as e:
            print(f"[错误] 关闭通知发送失败：{type(e).__name__}: {e}", flush=True)
        return 0
    finally:
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())