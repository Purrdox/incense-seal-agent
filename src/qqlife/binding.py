"""qqlife/binding.py：身份指令族（.create / .link / .profile / .unlink）。

带副作用的指令在普通指令分发前拦截；`.create` 对未绑定端点开放，
`.link` 只绑定已存在 UID，不再隐式建档。
"""
from __future__ import annotations

import os

from qqlife import mech_prompts
from qqlife._paths import user_home


def persona_label(path: str) -> str:
    """回执里的人设标签：缺失时明确说明走内置 ROLE，不伪造「通人人格」。"""
    return os.path.basename(path) if path else "默认（内置 ROLE）"


class BindingCommands:
    def __init__(self, *, identity, bundles, outbound, registry) -> None:
        self.identity = identity
        self.bundles = bundles
        self.outbound = outbound
        self.registry = registry

    def _bound_receipt(self, uid: str, head: str) -> str:
        r = self.registry.ensure(uid)
        n_active = sum(1 for it in r["store"].items if it.status == "active")
        return (head + f"\n人设已加载：{persona_label(r['persona_path'])}；"
                f"在办提醒 {n_active} 条。提醒将广播到你已验证的全部端点。")

    async def handle(self, channel: str, remote_id: str, uid: str | None, text: str) -> str | None:
        t = (text or "").strip()
        if not t.startswith((".", "。")):   # 显式指令才处理，避免普通词误触发
            return None
        parts = t[1:].strip().split()
        low = (parts[0].lower() if parts else "")
        identity = self.identity
        bundles = self.bundles
        outbound = self.outbound

        if low == "profile":
            if uid is None:
                return "当前端点未绑定。发送 .create <标识码> 创建身份。"
            if channel == "cli":
                return f"CLI 直通身份：{uid}（免绑定端点）"
            eps = identity.endpoints(uid)
            rows = "\n".join(f"· {e['channel']}:{str(e['remote_id'])[:18]}"
                             + ("" if e.get("verified", True) else "（待确认：回复 .link ok）")
                             for e in eps)
            r = self.registry.ensure(uid)
            n_active = sum(1 for it in r["store"].items if it.status == "active")
            return (f"当前身份：{uid}\n"
                    f"人设：{persona_label(r['persona_path'])}\n"
                    f"在办提醒：{n_active} 条\n已绑定端点：\n{rows or '（无）'}")

        if low == "unlink":
            if channel == "cli":
                return "CLI 为免绑定直通端点，不可解绑（身份由 MASTER_UID 配置）。"
            ok, msg = identity.unbind(channel, remote_id)
            return msg

        if low == "create":
            # .create <标识码> [confirm]：创建新身份并绑定当前端点（唯一建档入口）
            force = len(parts) == 3 and parts[2].lower() == "confirm"
            if len(parts) != 2 and not force:
                return "用法：.create <标识码> [confirm]（创建身份并绑定当前端点）"
            target = parts[1]
            if channel == "cli":
                if force:
                    return "CLI 无端点，.create 只建档，不需要 confirm。"
                return identity.create(target)[1]
            status, msg = identity.create_and_bind(channel, remote_id, target, force=force)
            if status in ("confirm", "error"):
                return msg
            return self._bound_receipt(target, msg + "\n下一步：.persona 导入角色卡；.mech 生成机械提示词。")

        if low != "link":
            return None

        # ---- .link 分支（原 .bind 的全部子形态，只改名）----
        if len(parts) == 2 and parts[1].lower() == "ok":
            # .link ok：确认当前身份全部待验证端点
            if uid is None:
                return "当前端点未绑定，无法确认。先发送 .create <标识码>。"
            n = identity.confirm_pending(uid)
            return (f"已确认 {n} 个端点验证通过，提醒将从该端点送达。"
                    if n else "当前没有待确认的端点。")

        if len(parts) >= 3 and parts[2].lower() != "confirm":
            # .link <通道> <token>：单向出站端点挂到当前身份（如 .link bark <token>）
            ep_channel, token = parts[1].lower(), parts[2]
            b = bundles.get(ep_channel)
            if b is None or getattr(b, "output", None) is None:
                return f"未知或无出站能力的通道：{ep_channel}（可用：{'、'.join(outbound.output_channels()) or '无'}）"
            if uid is None:
                return "当前端点未绑定，无法挂载设备。先发送 .create <标识码>。"
            status, msg = identity.bind_endpoint(uid, ep_channel, token)
            if status != "ok":
                return msg
            # 探测消息文案也来自该 uid 机械提示表（probe_test 可 per-uid 定制）
            probe_text = mech_prompts.load_table(user_home(uid)).get("probe_test", "")
            if not await outbound.probe(ep_channel, token, probe_text):
                identity.unbind(ep_channel, token)   # 探测失败回滚，不留坏端点
                return "测试推送发送失败：请检查 token 是否正确、设备网络是否可达，然后重试。"
            return msg + " 测试消息已发出——在该设备上看到后，回到任一通道回复 .link ok 完成验证。"

        if len(parts) == 2 or (len(parts) == 3 and parts[2].lower() == "confirm"):
            # .link <标识码> [confirm]：绑定到已存在身份
            if channel == "cli":
                return "CLI 免绑定（身份由 .env 的 MASTER_UID 指定）。挂载出站设备：.link bark <token>"
            target, force = parts[1], (len(parts) == 3)
            if target.lower() in identity.unidirectional:
                # 单向通道名不随 confirm 放宽：否则 `.link bark confirm` 会把入站端点绑成名为 bark 的身份
                return f"「{target}」是单向出站通道名。挂载设备请用：.link {target} <token>"
            status, msg = identity.bind(channel, remote_id, target, force=force)
            if status in ("confirm", "error"):
                return msg
            return self._bound_receipt(target, msg)

        return ("身份指令用法：\n"
                "· .create <标识码>  创建身份并绑定当前端点\n"
                "· .create <标识码> confirm  已绑定时确认切换\n"
                "· .link <标识码>  绑定到已存在身份\n"
                "· .link <标识码> confirm  改绑确认\n"
                "· .link bark <token>  挂载推送设备\n"
                "· .link ok  确认待验证端点\n"
                "· .profile / .unlink")
