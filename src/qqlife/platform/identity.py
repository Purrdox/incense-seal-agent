"""platform/identity.py：身份中心（uid 绑定，存储完全 per-uid 家目录化）。

- 每个用户的身份档案 = `user_home(uid)/uid.json`：`{"created":…, "endpoints":[…]}`。
  **端点与身份随家目录迁移**：拷贝 data/users/<uid>/ 即可把通道绑定一并带到新设备。
- 全局只有派生索引（启动时扫描各 uid.json 重建 `(channel, remote_id) → uid`），无全局绑定文件。
- uid：一个人的稳定锚点（`[a-z][a-z0-9_]{0,15}`），全部 per-person 数据按 uid 归档（家目录）。
- 未绑定会话由 app 层拒入并回绑定引导；CLI 免绑定直通 MASTER_UID（物理边界，不入 uid.json）。
- 创建与绑定分离：`.create <uid>` = `create_and_bind`（唯一建档入口）；`.link <uid>` 只绑定已存在 UID。
- 单向出站通道（bark）：`.link bark <token>` 挂到当前身份 → verified=False → 推测试消息
  → 用户回复 `.link ok`（confirm_pending）置 True。绑定过程即连通性自检。
- 兼容：旧的 data/bindings.json（全局单文件布局）在首次加载时自动分拆进各 uid.json 并改名 .bak。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Optional

from qqlife._paths import DATA_DIR

USERS_ROOT = DATA_DIR   # uid 家目录直接挂 data/<uid>/（无 users/ 中间层）
LEGACY_BINDINGS = DATA_DIR / "bindings.json"
_UID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")


def uid_valid(uid: str) -> bool:
    return bool(_UID_RE.match((uid or "").strip()))


class Identity:
    """绑定读写 + 解析（纯数据层；指令交互在 app 层，probe 在 outbound/driver 层）。"""

    def __init__(self, users_root: Path = USERS_ROOT,
                 unidirectional: frozenset[str] = frozenset({"bark"})) -> None:
        self.users_root = Path(users_root)
        # 纯出站通道（无入站路径）：绑定后需 `.link ok` 确认才算 verified
        self.unidirectional = set(unidirectional)
        self._data: dict = {"uids": {}, "index": {}}
        self._load()

    # ---- 存储（per-uid 家目录）----

    def _home(self, uid: str) -> Path:
        home = self.users_root / uid
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _load(self) -> None:
        self._import_legacy()
        if self.users_root.exists():
            for home in self.users_root.iterdir():
                if not home.is_dir():
                    continue
                f = home / "uid.json"
                if not f.exists():
                    continue
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(raw, dict):   # 损坏/异形文件：跳过，不让启动崩溃
                    continue
                uid = home.name
                if not uid_valid(uid):
                    continue
                eps = raw.get("endpoints")
                eps = [e for e in eps if isinstance(e, dict)] if isinstance(eps, list) else []
                rec = {"created": str(raw.get("created", "") or ""), "endpoints": eps}
                self._data["uids"][uid] = rec
                for e in eps:
                    ch = str(e.get("channel", "") or "")
                    rid = str(e.get("remote_id", "") or "")
                    if ch and rid:
                        self._data["index"][self._key(ch, rid)] = uid

    def _import_legacy(self) -> None:
        """旧全局 bindings.json（首轮身份中心布局）→ 分拆进各 uid.json，改名 .bak。"""
        if not LEGACY_BINDINGS.exists():
            return
        try:
            raw = json.loads(LEGACY_BINDINGS.read_text(encoding="utf-8"))
            for uid, u in (raw.get("uids") or {}).items():
                if not uid_valid(uid):
                    continue
                f = self._home(uid) / "uid.json"
                if f.exists():
                    continue
                f.write_text(json.dumps({"created": u.get("created", ""),
                                         "endpoints": u.get("endpoints", [])},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            LEGACY_BINDINGS.rename(LEGACY_BINDINGS.with_suffix(".json.bak"))
        except Exception:
            pass

    def _save_uid(self, uid: str) -> None:
        rec = self._data["uids"].get(uid)
        if rec is None:
            return
        f = self._home(uid) / "uid.json"
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)

    @staticmethod
    def _key(channel: str, remote_id: str) -> str:
        return f"{channel}:{remote_id}"

    # ---- 查询 ----

    def resolve(self, channel: str, remote_id: str) -> Optional[str]:
        return self._data["index"].get(self._key(channel, remote_id))

    def uid_exists(self, uid: str) -> bool:
        return (uid or "").strip() in self._data["uids"]

    def uids(self) -> list[str]:
        return list(self._data["uids"])

    def endpoints(self, uid: str, verified_only: bool = False) -> list[dict]:
        u = self._data["uids"].get((uid or "").strip()) or {}
        eps = list(u.get("endpoints", []))
        if verified_only:
            eps = [e for e in eps if e.get("verified", True)]
        return eps

    # ---- 绑定 ----

    def create(self, uid: str) -> tuple[str, str]:
        """仅建档不绑端点（CLI 预置 / `.create` 在 CLI 下的行为）。

        `.create` 在入站通道走 `create_and_bind`；此处幂等：已存在即报错。
        """
        uid = (uid or "").strip()
        if not uid_valid(uid):
            return "error", "标识码格式：小写字母开头，仅小写字母/数字/下划线，≤16 字符。"
        if self.uid_exists(uid):
            return "error", f"身份 {uid} 已存在（绑定端点请用 .link {uid}）。"
        self._data["uids"][uid] = {"created": dt.datetime.now().isoformat(timespec="seconds"),
                                   "endpoints": []}
        self._save_uid(uid)
        return "ok", (f"已创建身份 {uid}（未绑定端点）。"
                      f"在任一入站通道发送 .link {uid} 即可挂上端点。")

    def create_and_bind(self, channel: str, remote_id: str, uid: str,
                        force: bool = False) -> tuple[str, str]:
        """创建新 UID 并绑定当前端点（`.create <uid>` 的唯一建档入口）。

        status：ok=创建并绑定 / confirm=端点已属他人需确认 / error=拒绝。
        已存在 UID 不走这里（应使用 .link），避免误把老身份当新身份。
        """
        uid = (uid or "").strip()
        if not uid_valid(uid):
            return "error", "标识码格式：小写字母开头，仅小写字母/数字/下划线，≤16 字符。"
        if channel in self.unidirectional:
            return "error", "单向出站通道不能创建身份；请用 .create <uid> 绑定入站端点。"
        if self.uid_exists(uid):
            return "error", f"身份 {uid} 已存在，请用 .link {uid} 绑定。"
        old = self.resolve(channel, remote_id)
        if old == uid:
            return "error", f"当前端点已经是 {uid}。"
        if old is not None and not force:
            return "confirm", f"当前端点已绑定到 {old}。确认新建并切到 {uid} 请发送：.create {uid} confirm"

        now = dt.datetime.now().isoformat(timespec="seconds")
        new_rec = {"created": now, "endpoints": [{"channel": channel, "remote_id": remote_id,
                                                  "bound_at": now, "verified": True}]}
        old_rec = self._data["uids"].get(old) if old is not None else None
        old_endpoint = None
        if old_rec is not None:
            old_endpoint = next((e for e in old_rec.get("endpoints", [])
                                 if e.get("channel") == channel and e.get("remote_id") == remote_id),
                                None)
            old_rec["endpoints"] = [e for e in old_rec.get("endpoints", []) if e is not old_endpoint]
            try:
                self._save_uid(old)   # 先从旧身份摘除，失败则不创建新身份
            except Exception as e:
                if old_endpoint is not None:
                    old_rec["endpoints"].append(old_endpoint)
                return "error", f"创建身份失败（旧身份写入异常：{type(e).__name__}）。"

        self._data["uids"][uid] = new_rec
        self._data["index"][self._key(channel, remote_id)] = uid
        try:
            self._save_uid(uid)
        except Exception as e:
            # 回滚内存与旧身份，避免留下半成品
            self._data["uids"].pop(uid, None)
            try:
                (self._home(uid) / "uid.json").unlink()
            except Exception:
                pass
            if old is not None:
                self._data["index"][self._key(channel, remote_id)] = old
            if old_rec is not None and old_endpoint is not None:
                old_rec["endpoints"].append(old_endpoint)
                try:
                    self._save_uid(old)
                except Exception:
                    pass
            return "error", f"创建身份失败（写入磁盘异常：{type(e).__name__}）。"
        return "ok", f"已创建身份并绑定：{channel}:{remote_id} → {uid}"

    def bind(self, channel: str, remote_id: str, uid: str, force: bool = False) -> tuple[str, str]:
        """把入站端点绑定到**已存在**的 uid（`.link <uid>`）。返回 (status, message)。

        status：ok=绑定成功 / confirm=已属他人需确认 / error=拒绝
        """
        uid = (uid or "").strip()
        if not uid_valid(uid):
            return "error", "标识码格式：小写字母开头，仅小写字母/数字/下划线，≤16 字符。"
        if channel in self.unidirectional:
            return "error", "单向出站通道请用 .link <通道名> <token> 挂到当前身份。"
        old = self.resolve(channel, remote_id)
        if old == uid:
            return "ok", f"当前端点已绑定到 {uid}，无需重复绑定。"
        if old is not None and not force:
            return "confirm", f"当前端点已绑定到 {old}。确认改绑到 {uid} 请发送：.link {uid} confirm"
        u = self._data["uids"].get(uid)
        if u is None:
            return "error", f"身份 {uid} 不存在：请先发送 .create {uid} 创建。"
        now = dt.datetime.now().isoformat(timespec="seconds")
        if old is not None:  # force 改绑：从旧 uid 档案摘除该端点
            ou = self._data["uids"].get(old)
            if ou is not None:
                ou["endpoints"] = [e for e in ou.get("endpoints", [])
                                   if not (e.get("channel") == channel and e.get("remote_id") == remote_id)]
                self._save_uid(old)
        u["endpoints"].append({"channel": channel, "remote_id": remote_id,
                               "bound_at": now, "verified": True})
        self._data["index"][self._key(channel, remote_id)] = uid
        self._save_uid(uid)
        return "ok", f"已绑定：{channel}:{remote_id} → {uid}"

    def bind_endpoint(self, uid: str, channel: str, remote_id: str) -> tuple[str, str]:
        """把单向出站端点挂到已有 uid（.link bark <token>）。verified=False，等 `.link ok` 确认。"""
        uid = (uid or "").strip()
        u = self._data["uids"].get(uid)
        if u is None:
            return "error", f"身份 {uid} 不存在：请先在入站通道执行 .create {uid}。"
        for e in u.get("endpoints", []):
            if e.get("channel") == channel and e.get("remote_id") == remote_id:
                if e.get("verified", True):
                    return "ok", f"{channel} 端点已绑定且验证通过。"
                return "ok", "该端点处于待确认状态：测试消息已发，回复 .link ok 完成验证。"
        now = dt.datetime.now().isoformat(timespec="seconds")
        u["endpoints"].append({"channel": channel, "remote_id": remote_id,
                               "bound_at": now, "verified": False})
        self._data["index"][self._key(channel, remote_id)] = uid
        self._save_uid(uid)
        return "ok", f"已挂载 {channel} 端点到 {uid}（待确认）。"

    def confirm_pending(self, uid: str) -> int:
        """把该 uid 全部未验证端点置 verified（用户回复 `.link ok`），返回确认数量。"""
        u = self._data["uids"].get((uid or "").strip()) or {}
        n = 0
        for e in u.get("endpoints", []):
            if not e.get("verified", True):
                e["verified"] = True
                n += 1
        if n:
            self._save_uid((uid or "").strip())
        return n

    def unbind(self, channel: str, remote_id: str) -> tuple[bool, str]:
        key = self._key(channel, remote_id)
        uid = self._data["index"].pop(key, None)
        if uid is None:
            return False, "当前端点未绑定任何身份。"
        u = self._data["uids"].get(uid)
        if u is not None:
            u["endpoints"] = [e for e in u.get("endpoints", [])
                              if not (e.get("channel") == channel and e.get("remote_id") == remote_id)]
            self._save_uid(uid)
        return True, f"已解绑：{channel}:{remote_id} ← {uid}"
