"""agents/mantools.py：主会话暴露给模型的工具（OpenAI function-calling）。

工具由程序执行（act 不 talk），模型只选路填参。当前工具：
- item               事项统一管理（add/read/update/delete；常规+临时，含备注/笔记/勿扰 focus）
- expense            记账（add/read/update/delete）
- list_courses       列出常规事项（"课程/例行"）
- weather            天气查询（中国境内，可到区县，uapis.cn 免费无 key）
- remember_user      记用户记忆（一句话进 session 的待固化区，fold 时并入长期记忆）
- web_search         联网搜索（内部子对话提炼答案，<>污染不进主会话）

当前时间不再单独给模型取：主会话上下文固定注入【当前时间】。勿扰统一由 item 表达。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

from qqlife.agents.promptkit import render as render_prompt


def _web_search_sync(query: str, max_results: int = 5, region: str = "cn-zh",
                     backend: str = "auto", timeout: float = 8.0, proxy: str = "") -> list:
    from ddgs import DDGS  # 无 key 免费库；未安装则抛异常，由上层转为提示
    with DDGS(timeout=int(max(1, timeout)), proxy=proxy or None) as d:
        return list(d.text(query, region=region if region else "cn-zh", max_results=max_results,
                           backend=backend or "auto"))


def _weather_sync(city: str, timeout: float = 8.0) -> dict:
    """全国 3000+ 城市/区县天气（uapis.cn 免费 API，无需 key）。返回原始 JSON dict。"""
    import urllib.parse
    import urllib.request
    url = "https://uapis.cn/api/v1/misc/weather?city=" + urllib.parse.quote(city)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 子对话提炼提示词：把原始搜索结果/天气原始数据提炼成一段简短答案，回主会话
_SUB_EXTRACT_SYS = render_prompt("sub_extract")

_DUE_PAST_TOLERANCE = 60.0   # 秒；刚建就到的"马上提醒"不算过期


def _due_ts(value: str) -> float | None:
    """解析 once 的 due（ISO 时刻，兼容结尾 Z）；无法解析返回 None。"""
    try:
        return dt.datetime.fromisoformat((value or "").strip().replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _due_past_error(due: str) -> str | None:
    """once 的到期时刻是否有问题；有问题返回给模型的拒绝文案，没问题返回 None。

    - 空 due：纯待办（没有到期时刻，永远不会触发），放行——保持旧行为，别把"记一下"堵死；
    - 无法解析：拒绝并给格式提示；
    - 已经过去：拒绝，让模型改期或用 regular（否则调度会立刻触发/或永远不触发）。
    """
    s = (due or "").strip()
    if not s:
        return None
    ts = _due_ts(s)
    if ts is None:
        return ("due 无法解析（要 ISO 时刻，如 2026-11-01T20:00）；"
                "周期事项请用 type=regular + sched。未写入。")
    if ts < dt.datetime.now().timestamp() - _DUE_PAST_TOLERANCE:
        return (f"due={due} 已经过去了（现在是 {dt.datetime.now():%Y-%m-%d %H:%M}）。"
                "请改到下一次的时间，或改用 type=regular + sched。未写入。")
    return None


SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "item",
        "description": "统一管理事项（add/read/update/delete）。type=regular常规(周期进行,给 sched)/once临时(一次性,给 due ISO)。**once 的 due 若给出，必须是尚未过去的时刻**（已过去会写入失败并提示改期）；不给 due 表示无到期时刻的待办（不会触发提醒）。周期事项用 regular + sched。add 需 content（用户视角的事项短名，如「喝水」「交作业」，不写“提醒我/提醒TA”）；**勿扰**加 focus=true 并给 start/end 时段（小睡/午睡/上课/会议/夜间睡眠都这样表达：时段内不主动打扰，到点自动解除，无需再改任何东西）。**status**：active=进行中，done=完成·终态（提前做完就 update 置 status=done，就不会再提醒，也不要再提）。**read 默认绝不返回已完成（done）的事项，只返回进行中的**；仅当用户明确要看历史/已完成时才传 include_done=true（含 done_at）。**所有提醒到点都会进入我的主对话现场确认**。update 可改 content/type/sched/due/start/end/focus/note/status；给 note_id+content 时改其中某条开放笔记；delete 删 id。id 来自 read。",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"}, "id": {"type": "string"},
                                      "content": {"type": "string"}, "type": {"type": "string"},
                                      "sched": {"type": "string"}, "due": {"type": "string"},
                                      "start": {"type": "string"}, "end": {"type": "string"},
                                      "focus": {"type": "boolean"},
                                      "note": {"type": "string"}, "note_id": {"type": "string"},
                                      "status": {"type": "string"}, "include_done": {"type": "boolean"}},
                       "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "expense",
        "description": "记账（add/read/update/delete）。add=记一笔(amount 数字+category 类别+可选 purpose)；read=查(date 可选 YYYY-MM-DD，缺省全部)；update=改(id+要改字段/note)；delete=删(id)。id 来自 read 明细。",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"}, "id": {"type": "string"},
                                      "date": {"type": "string"}, "amount": {"type": "number"},
                                      "category": {"type": "string"}, "purpose": {"type": "string"},
                                      "note": {"type": "string"}},
                       "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "list_courses",
        "description": "列出常规事项（每周/每月例行，如课程），含周期与时段。用来识别«XX课/XX例行»相关的内容。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "weather",
        "description": "查中国境内某地实时天气（精确到城市/区县）。用户问天气/温度/下雨时用。返回已提炼的中文描述（含温度/天气/湿度/风向等）。city 填城市或区县名，如「北京」「深圳福田区」。",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "remember_user",
        "description": "记录关于用户本人的一条记忆（如称呼、作息、习惯、偏好、关键事实）。field 是主题名（需要时新建，如『作息』『饮食偏好』），text 是内容。这句记忆会立即注入后续对话；在下次总结（.compact/自动压缩）时合并进长期记忆，复用同名 field 便于合并更新。",
        "parameters": {"type": "object",
                       "properties": {"field": {"type": "string"},
                                      "text": {"type": "string"}},
                       "required": ["field", "text"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "用 DuckDuckGo 联网搜索（免费、无需 key）。查你不确定的事实/最新信息；把 query 写成要查的关键词，拿结果后组织进回复。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
]


class ToolExecutor:
    def __init__(self, store=None, hub=None, owner: str = "",
                 session=None, cfg=None, mech: dict | None = None) -> None:
        self.store = store
        self.hub = hub
        self.owner = owner
        self.session = session
        self.cfg = cfg
        self.mech = mech if isinstance(mech, dict) else None

    async def call(self, name: str, args: dict) -> str:
        if name == "list_courses":
            rows = [{"name": i.content, "sched": i.sched, "start": i.start, "end": i.end}
                    for i in self.store.items if i.type == "regular" and not i.is_done]
            return json.dumps(rows, ensure_ascii=False)
        if name == "weather":
            return await self._weather(args)
        if name == "item":
            return await self._item(args)
        if name == "expense":
            return await self._expense(args)
        if name == "remember_user":
            # 10.16 §A2：一句话进 session 待固化区（每轮注入），fold 时并入长期记忆
            if self.session is None:
                return "（会话模块未就绪，无法写入。）"
            field = (args.get("field") or "").strip()
            text = (args.get("text") or "").strip()
            if not (field and text):
                return "field 或 text 为空，未写入。"
            self.session.add_new(self.owner, f"{field}：{text}")
            return f"已记下（{field}）：{text}"
        if name == "web_search":
            q = (args.get("query") or "").strip()
            if not q:
                return "query 为空。"
            retries = max(0, self.cfg.search_retries) if self.cfg else 0
            timeout = self.cfg.search_timeout if self.cfg else 8.0
            backend = self.cfg.search_backend if self.cfg else "auto"
            proxy = self.cfg.search_proxy if self.cfg else ""
            last = None
            raw = None
            for attempt in range(retries + 1):
                try:
                    results = await asyncio.to_thread(
                        _web_search_sync, q, region="cn-zh", backend=backend, timeout=timeout, proxy=proxy)
                except ImportError:
                    return "搜索库 ddgs 未安装，请 pip install ddgs 后重试。"
                except Exception as e:
                    last = e
                    if attempt < retries:
                        await asyncio.sleep(1.0)
                        if "auto" in backend:
                            backend = "brave,google"
                        continue
                    return f"__SEARCH_FAIL__: {type(e).__name__}"
                if not results:
                    return "没有搜到相关结果。"
                raw = "\n\n".join(
                    f"标题:{r.get('title', '')}\n链接:{r.get('href', '')}\n摘要:{(r.get('body') or '')[:300]}"
                    for r in results[:5])
                break
            if raw is None:
                return f"__SEARCH_FAIL__: {(type(last).__name__ if last else '未知')}"
            # 子对话提炼：原始结果不直接进主会话，先提炼成简短答案
            return await self._extract(f"用户的查询：{q}\n\n搜到的资料：\n{raw}")
        return f"未知工具:{name}"

    async def _item(self, args: dict) -> str:
        action = (args.get("action") or "").lower()
        st = self.store
        if action == "add":
            if args.get("type") != "regular":   # once：到期时刻必须还没过去，否则会一存就响
                err = _due_past_error(args.get("due") or "")
                if err:
                    return err
            iid = st.add_item(owner=self.owner, content=args.get("content", ""),
                              type_="regular" if args.get("type") == "regular" else "once",
                              due=args.get("due", ""), sched=args.get("sched", ""),
                              start=args.get("start", ""), end=args.get("end", ""),
                              focus=bool(args.get("focus")),
                              note=args.get("note", ""))
            return f"已新增事项 #{iid}：{args.get('content')}"
        if action == "read":
            include_done = bool(args.get("include_done"))
            rows = [{"id": i.id, "content": i.content, "type": i.type, "sched": i.sched, "due": i.due,
                     "start": i.start, "end": i.end, "focus": i.focus, "note": i.note, "notes": i.notes,
                     "status": i.status, "done_at": i.done_at}
                    for i in st.items if include_done or not i.is_done]
            return json.dumps(rows, ensure_ascii=False)
        if action == "update":
            iid = args.get("id", "")
            it = next((x for x in st.items if x.id == iid), None)
            if it is None:
                return f"未找到事项：{iid}"
            note_id = args.get("note_id")
            if note_id:
                for n in it.notes:
                    if n.get("id") == note_id:
                        n["content"] = args.get("content", "")
                        st.save_items()
                        return "已更新该笔记。"
                return f"未找到笔记：{note_id}"
            was_done = it.is_done
            # 改成/保持 once 并给了 due 时，同样要保证到期时刻还没过去（否则改完立刻触发）
            new_type = "regular" if str(args.get("type", it.type)) == "regular" else "once"
            if new_type != "regular" and (args.get("due") is not None or args.get("type") is not None):
                err = _due_past_error(str(args.get("due", it.due)))
                if err:
                    return err.replace("未写入", "未修改")
            if args.get("content") is not None:
                it.content = args["content"]
            if args.get("type") is not None:
                it.type = "regular" if args["type"] == "regular" else "once"
            if args.get("sched") is not None:
                it.sched = args["sched"]
            if args.get("due") is not None:
                it.due = args["due"]
            if args.get("start") is not None:
                it.start = args["start"]
            if args.get("end") is not None:
                it.end = args["end"]
            if args.get("focus") is not None:
                it.focus = bool(args["focus"])
            if args.get("note") is not None:
                it.note = args["note"]
            if args.get("status") is not None:
                it.status = args["status"]
            # 首次置为 done 时记完成时刻（供确认已提醒/几点完成）
            if it.is_done and not was_done and not it.done_at:
                it.done_at = dt.datetime.now().isoformat(timespec="seconds")
            st.save_items()
            return "已更新该事项。"
        if action == "delete":
            iid = args.get("id", "")
            before = len(st.items)
            st.items = [x for x in st.items if x.id != iid]
            if len(st.items) != before:
                st.save_items()
                return "已删除该事项。"
            return f"未找到事项：{iid}"
        return "item 的 action 需为 add/read/update/delete"

    async def _expense(self, args: dict) -> str:
        action = (args.get("action") or "").lower()
        st = self.store
        if action == "add":
            amount = args.get("amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return "amount 需要是数字。"
            if amount < 0:
                return "金额不能为负。"
            st.add_expense(amount=amount, category=args.get("category", ""), purpose=args.get("purpose", ""))
            return f"已记一笔：{amount:.2f}（{args.get('category') or '其他'}）{args.get('purpose', '')}"
        if action == "read":
            return json.dumps(st.list_expenses(date=(args.get("date") or "").strip()), ensure_ascii=False)
        if action == "update":
            ok = st.update_expense(args.get("id", ""), amount=args.get("amount"),
                                   category=args.get("category"), purpose=args.get("purpose"),
                                   note=args.get("note"))
            return "已修改该笔支出。" if ok else f"未找到该笔：{args.get('id')}"
        if action == "delete":
            ok = st.delete_expense(args.get("id", ""))
            return "已删除该笔支出。" if ok else f"未找到该笔：{args.get('id')}"
        return "expense 的 action 需为 add/read/update/delete"

    # ---- 子对话提炼：原始资料（搜索结果/天气 JSON）不直接进主会话，提炼成简短答案 ----
    async def _extract(self, user_content: str) -> str:
        # 程序化提炼：显式关思考——思考模式下 reasoning 与正文共享 max_tokens，
        # 300 的额度会被思考吃光，正文为空后只能退化回原始 JSON。
        llm = self.hub.for_kind("search") if self.hub else None
        if not (llm and llm.is_available()):
            return user_content   # 模型不可用则原样返回，不阻断
        usage: dict = {}
        try:
            out = await llm.chat([{"role": "system", "content": _SUB_EXTRACT_SYS},
                                  {"role": "user", "content": user_content}], max_tokens=300,
                                 usage=usage, thinking=False)
            return (out or "").strip() or user_content
        except Exception:
            return user_content
        finally:
            if usage:
                from qqlife import usage_rec
                usage_rec.record(self.owner, usage)

    async def _weather(self, args: dict) -> str:
        city = (args.get("city") or "").strip()
        if not city:
            return "city 不能为空。"
        try:
            data = await asyncio.to_thread(_weather_sync, city, timeout=8.0)
        except Exception as e:
            return f"__WEATHER_FAIL__: {type(e).__name__}: {e}"
        # 提炼成可读中文（结合原查询城市，避免模型对着 adcode 乱猜）
        n = data.get("city") or city
        user = (f"用户问的是「{city}」的天气。以下为天气数据 JSON：\n{json.dumps(data, ensure_ascii=False)}"
                f"\n请用中文概括「{n}」当前的天气、温度、风向风力、湿度，按用户的问题组织，2-3 句。")
        return await self._extract(user)