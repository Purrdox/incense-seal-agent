"""qqlife/mech_prompts.py：机械提示文本表（程序生成的固定回复文案）。

- **全局基准**：`prompts/mech_prompts_baseline.json`（受版本管理，不依赖任何 UID）。
- **per-uid 覆盖**：`user_home(uid)/mech_prompts.json`（可选，`.mech` 生成）。
- 合并顺序：`BUILTIN → 全局基准 → per-uid 覆盖`。
- `get`：无 ctx 场景读取“全局基准 + BUILTIN”，按 mtime 缓存。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from qqlife._paths import PROMPTS_DIR

BASELINE_PATH = PROMPTS_DIR / "mech_prompts_baseline.json"

# 代码兜底表：与基准同义，纯朴素文案；基准文件缺失/损坏时仍能正常回复。
BUILTIN: dict = {
    "searching": "（正在查询，请稍等。）",
    "rate_limited": "（请求较多，请稍后再试。）",
    "process_error": "（处理时出了点问题，请稍后再试。）",
    "network_fail": "（网络连接不稳定，请稍后再试。）",
    "search_fail": "（查询暂时失败，请稍后再试。）",
    "empty_output": "（没有收到有效回复，请再说一遍。）",
    "tool_loop_limit": "（操作步骤过多，已停止。）",
    "backend_unready": "（模型服务未配置或不可用。）",
    "no_skill": "（暂无可用的处理方式。）",
    "compact_hint": "（当前对话已有 {n} 轮，建议发送 .compact 压缩。）",
    "startup_notice": "（服务已启动。）",
    "shutdown_notice": "（服务已关闭，关闭期间的提醒不会送达。）",
    "unbound_guide": "（当前设备尚未绑定。发送 .create <uid> 创建身份，或 .link <uid> 绑定已有身份。）",
    "probe_test": "（绑定测试：收到本条说明该设备可达。请回复 .link ok 完成验证。）",
    "compact_done": "（压缩完成：{before} 轮对话已整理为 {facts} 条长期记忆。）",
    "compact_none": "（当前没有可压缩的对话。）",
    "compact_fail": "（压缩失败，原对话已保留，请稍后再试。）",
    "backlog_skipped": "（有 {n} 条超过 {hours:g} 小时的过期提醒已跳过。）",
    "delivery_failed": "（有一条提醒多次发送失败，已跳过：{content}）",
    "data_corrupt": "（数据文件损坏：{files}。已备份并按空数据继续，请手动恢复。）",
    "reminder_label": "【{title}】",
}

_BASELINE_CACHE: tuple[float, dict] | None = None


def fill(text: str, **kw) -> str:
    """安全格式化机械提示：模板里的 {占位} 用 kw 填充。

    用户自定义文案里若混入裸花括号（如 "{content}"），format 会抛异常——此时原样返回，
    绝不因为一条文案把回复整个吞掉。空文案返回 ""。
    """
    if not text:
        return ""
    try:
        return text.format(**kw)
    except Exception:
        return text


def baseline_table() -> dict:
    """全局基准：BUILTIN + prompts/mech_prompts_baseline.json（按 mtime 缓存）。"""
    global _BASELINE_CACHE
    try:
        mtime = BASELINE_PATH.stat().st_mtime
        if _BASELINE_CACHE is not None and _BASELINE_CACHE[0] == mtime:
            return dict(_BASELINE_CACHE[1])
        raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        merged = dict(BUILTIN)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if isinstance(v, str)})
        _BASELINE_CACHE = (mtime, merged)
        return dict(merged)
    except Exception:
        return dict(BUILTIN)


def load_table(home: Path) -> dict:
    """加载某 uid 的机械提示表：BUILTIN + 全局基准 + user_home/mech_prompts.json。"""
    merged = baseline_table()
    try:
        user = json.loads((Path(home) / "mech_prompts.json").read_text(encoding="utf-8"))
        if isinstance(user, dict):
            merged.update({k: v for k, v in user.items() if isinstance(v, str)})
    except Exception:
        pass   # 缺文件/损坏 → 使用基准
    return merged


def save_table(home: Path, table: dict) -> str:
    """原子写入 per-uid 机械提示表；旧文件备份为 `.bak-<timestamp>`，返回备份路径。"""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    f = home / "mech_prompts.json"
    backup = ""
    if f.exists():
        backup = str(f.with_name(f"mech_prompts.json.bak-{dt.datetime.now():%Y%m%d-%H%M%S-%f}"))
        f.replace(backup)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return backup


def get(key: str, default: str = "") -> str:
    """无 ctx 的兜底场景：读取全局基准（含内置兜底）。"""
    return baseline_table().get(key, default)
