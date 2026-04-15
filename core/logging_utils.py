"""双通道日志基建：业务日志 + trace 日志物理隔离。

- 业务日志 logger: ``eragent`` → 业务 handler（默认 stderr），支持 JSON / console
  两种格式，通过 ``TraceIdFilter`` 注入当前 trace_id，便于生产排查。
- Trace 日志 logger: ``eragent.trace`` → 独立 handler（默认 stdout），纯文本
  message-only 格式，``propagate=False`` 不走业务 handler。observability 中间件
  把一次 trace 的树 + 汇总拼成单个字符串后一次性 ``info(msg)``，借助
  ``logging.Handler`` 内置锁保证 **一条 trace 块原子写入**，不会被并发的业务
  日志切断。

所有模块通过 ``get_logger(__name__)`` 获取业务 logger，trace 模块通过
``get_trace_logger()`` 获取专用 logger。
"""

from __future__ import annotations

import json
import logging
import sys
from threading import Lock
from typing import Any, TextIO

_configured = False
_lock = Lock()

# stdlib LogRecord 自带字段；JsonFormatter 序列化 extra 时用它排除标准字段。
_STD_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName", "trace_id",
    }
)


def _pull_trace_id() -> str | None:
    """尽力从 observability 中间件的 contextvar 取出当前 trace_id。

    observability 未初始化或无活跃 trace 时返回 None。此函数不抛异常，
    以保证日志路径永远不被自己的依赖打挂。
    """
    try:
        from core.observability.middleware import _current_trace  # noqa: WPS433

        ctx = _current_trace.get()
        if ctx is not None:
            return ctx.trace_id
    except Exception:  # noqa: BLE001
        return None
    return None


class TraceIdFilter(logging.Filter):
    """把当前 trace_id 写进 LogRecord.trace_id，供 formatter 读取。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _pull_trace_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志，便于采集/检索（ELK、Loki、Datadog 等）。"""

    def __init__(self, *, include_trace_id: bool = True) -> None:
        super().__init__()
        self._include_trace_id = include_trace_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self._include_trace_id:
            payload["trace_id"] = getattr(record, "trace_id", None) or "-"
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 额外 extra 字段（logger.info("...", extra={...})）
        for key, val in record.__dict__.items():
            if key in _STD_RECORD_FIELDS or key in payload:
                continue
            try:
                json.dumps(val, default=str, ensure_ascii=False)
                payload[key] = val
            except Exception:  # noqa: BLE001
                payload[key] = str(val)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """人读友好格式，带 trace_id 短 ID 列。"""

    def __init__(self, *, include_trace_id: bool = True) -> None:
        if include_trace_id:
            fmt = (
                "%(asctime)s %(levelname)s trace=%(trace_id).8s "
                "[%(name)s] %(message)s"
            )
        else:
            fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        super().__init__(fmt)


def _pick_stream(name: str) -> TextIO:
    return sys.stdout if name == "stdout" else sys.stderr


def _configure() -> None:
    global _configured
    with _lock:
        if _configured:
            return

        level_name = "INFO"
        log_format = "console"
        include_trace_id = True
        business_stream_name = "stderr"
        trace_stream_name = "stdout"

        try:
            from config.settings import get_settings

            settings = get_settings()
            level_name = settings.logging.level
            log_format = settings.logging.format
            include_trace_id = settings.logging.include_trace_id
            business_stream_name = settings.logging.business_stream
            trace_stream_name = settings.observability.console_stream
        except Exception as exc:  # noqa: BLE001
            # 启动早期 / 配置加载失败时，用安全默认值继续跑，避免日志自毁导致
            # 真正的错误堆栈也看不到。
            print(
                f"[eragent] logging config load failed, defaulting to "
                f"INFO/console: {exc}",
                file=sys.stderr,
            )

        level = getattr(logging, level_name.upper(), logging.INFO)

        # --- 业务 logger：eragent ---
        business = logging.getLogger("eragent")
        for h in list(business.handlers):
            business.removeHandler(h)
        business_handler = logging.StreamHandler(_pick_stream(business_stream_name))
        if log_format == "json":
            business_formatter: logging.Formatter = JsonFormatter(
                include_trace_id=include_trace_id
            )
        else:
            business_formatter = ConsoleFormatter(include_trace_id=include_trace_id)
        business_handler.setFormatter(business_formatter)
        business_handler.addFilter(TraceIdFilter())
        business.addHandler(business_handler)
        business.setLevel(level)
        business.propagate = False

        # --- Trace logger：eragent.trace ---
        # 独立 handler、独立 stream、独立锁；一次 info(msg) = 一次原子 write + flush，
        # 即便多任务并发也只会在不同 trace 块之间交错，不会在行内插花。
        trace_logger = logging.getLogger("eragent.trace")
        for h in list(trace_logger.handlers):
            trace_logger.removeHandler(h)
        trace_handler = logging.StreamHandler(_pick_stream(trace_stream_name))
        trace_handler.setFormatter(logging.Formatter("%(message)s"))
        trace_logger.addHandler(trace_handler)
        trace_logger.setLevel(logging.INFO)
        trace_logger.propagate = False  # 不走业务 handler

        _configured = True


def _heal_if_disabled(logger: logging.Logger) -> None:
    """防御性恢复被 ``dictConfig``/``fileConfig`` 第三方调用标记为 disabled 的 logger。

    典型触发场景：alembic 在 ``migrations/env.py`` 里调用
    ``logging.config.fileConfig(...)``（默认 ``disable_existing_loggers=True``），
    会把 ini 中未列出的 logger（包括 ``eragent.*``）全部置为 ``disabled=True``，
    导致后续 ``logger.info(...)`` 静默丢失。我们也在 env.py 里传了
    ``disable_existing_loggers=False`` 作为根治，此处为第二道防线：应用内
    只要任何模块通过 ``get_logger`` 取用，就能即时把状态纠回来。
    """
    if logger.disabled:
        logger.disabled = False
    # 子 logger 被 disable 时父 logger 通常也被 disable，顺带恢复
    parent = logger.parent
    while parent is not None and parent is not logging.getLogger():
        if parent.disabled:
            parent.disabled = False
        parent = parent.parent


def get_logger(name: str) -> logging.Logger:
    """获取业务 logger。命名空间化到 ``eragent.<name>``。"""
    _configure()
    if name == "eragent.trace" or name.startswith("eragent.trace."):
        # trace 命名空间保留给专用 logger，避免业务模块误用
        return get_trace_logger()
    if not name.startswith("eragent"):
        name = f"eragent.{name}"
    logger = logging.getLogger(name)
    _heal_if_disabled(logger)
    return logger


def get_trace_logger() -> logging.Logger:
    """获取 trace 专用 logger。调用方负责把一整块 trace 文本拼好后一次 info(msg)。"""
    _configure()
    logger = logging.getLogger("eragent.trace")
    _heal_if_disabled(logger)
    return logger


def reset_for_tests() -> None:
    """测试辅助：强制下次 get_logger 重新读取配置并重建 handler。"""
    global _configured
    with _lock:
        _configured = False
