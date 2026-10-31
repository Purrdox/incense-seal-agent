"""agent_core/logger.py：极简日志（控制台 + 轮转文件）。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from qqlife._paths import LOGS_DIR as _LOGS_DIR


def _fmt() -> logging.Formatter:
    return logging.Formatter("[%(asctime)s %(levelname)s %(name)s] %(message)s")


def init_logging(name: str = "default", *, verbose: bool = True) -> logging.Logger:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(_fmt())
    fh = RotatingFileHandler(_LOGS_DIR / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(_fmt())
    lg.addHandler(sh)
    lg.addHandler(fh)
    return lg


def get_logger(name: str = "default") -> logging.Logger:
    return logging.getLogger(name)


def shutdown_logging() -> None:
    for h in list(logging.root.handlers) + [h for _, lg in logging.Logger.manager.loggerDict.items() if isinstance(lg, logging.Logger) for h in lg.handlers]:
        try:
            h.close()
        except Exception:
            pass