"""统一时区时间工具。

全系统禁止直接调用 ``datetime.utcnow()`` 或 ``datetime.now(timezone.utc)``,
所有业务时间戳通过本模块的 :func:`now_cn` 产生,默认时区 Asia/Shanghai。

时区可通过 ``APP_TIMEZONE`` 环境变量或 config.yaml 的 ``app.timezone``
切换,切换后进程需重启或显式调用 :func:`configure_timezone` 刷新。
"""

from __future__ import annotations

import threading
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"

_lock = threading.Lock()
_tz_name: str = DEFAULT_TIMEZONE
_tz: tzinfo = ZoneInfo(DEFAULT_TIMEZONE)


def configure_timezone(name: str) -> None:
    """显式设置业务时区。

    一般由 :func:`config.settings.get_settings` 首次加载后调用。
    不合法的时区名会抛 ``ZoneInfoNotFoundError``。
    """
    global _tz_name, _tz
    tz = ZoneInfo(name)
    with _lock:
        _tz_name = name
        _tz = tz


def get_timezone() -> tzinfo:
    """返回当前业务时区对象。"""
    return _tz


def get_timezone_name() -> str:
    """返回当前业务时区 IANA 名称,例如 ``Asia/Shanghai``。"""
    return _tz_name


def now_cn() -> datetime:
    """返回带业务时区信息的当前时间(aware datetime)。

    名称保留 ``_cn`` 后缀是因为项目实际场景使用北京时间;
    若运行时切到其他时区,返回值跟随配置。
    """
    return datetime.now(_tz)
