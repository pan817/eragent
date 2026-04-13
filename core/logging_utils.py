"""统一日志工具：基于 stdlib logging 的轻量封装。

避免引入额外依赖（structlog 后续再补）。
按 LoggingSettings.level 配置日志级别，所有模块通过 ``get_logger(__name__)`` 获取。
"""

from __future__ import annotations

import logging
import sys
from threading import Lock

_configured = False
_lock = Lock()


def _configure() -> None:
    global _configured
    with _lock:
        if _configured:
            return
        try:
            from config.settings import get_settings

            level_name = get_settings().logging.level
        except Exception as exc:
            print(f"[eragent] logging config load failed, defaulting to INFO: {exc}", file=sys.stderr)
            level_name = "INFO"
        level = getattr(logging, level_name.upper(), logging.INFO)
        root = logging.getLogger("eragent")
        if not root.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s [%(name)s] %(message)s"
                )
            )
            root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取命名空间化的 logger。"""
    _configure()
    if not name.startswith("eragent"):
        name = f"eragent.{name}"
    return logging.getLogger(name)
