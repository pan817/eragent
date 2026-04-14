"""core.time_utils 单元测试。"""

from __future__ import annotations

from datetime import timedelta, timezone
from urllib.parse import unquote

import pytest

from core.time_utils import (
    DEFAULT_TIMEZONE,
    configure_timezone,
    get_timezone,
    get_timezone_name,
    now_cn,
)


def test_default_timezone_is_asia_shanghai() -> None:
    assert get_timezone_name() == DEFAULT_TIMEZONE == "Asia/Shanghai"


def test_now_cn_returns_aware_beijing_time() -> None:
    ts = now_cn()
    assert ts.tzinfo is not None, "now_cn() must return aware datetime"
    assert ts.utcoffset() == timedelta(hours=8)


def test_now_cn_absolute_time_matches_utc_now() -> None:
    """CN 时间与 UTC 时间对应同一时刻,差值小于 1 秒。"""
    from datetime import datetime as _dt

    utc_ref = _dt.now(timezone.utc)
    cn = now_cn()
    delta = abs((cn - utc_ref).total_seconds())
    assert delta < 1.0


def test_configure_timezone_roundtrip() -> None:
    try:
        configure_timezone("UTC")
        assert get_timezone_name() == "UTC"
        assert now_cn().utcoffset() == timedelta(0)
    finally:
        configure_timezone(DEFAULT_TIMEZONE)
    assert get_timezone_name() == DEFAULT_TIMEZONE


def test_configure_timezone_rejects_invalid() -> None:
    with pytest.raises(Exception):  # ZoneInfoNotFoundError
        configure_timezone("Not/A_Real_Zone_XYZ")


def test_conninfo_includes_url_encoded_timezone() -> None:
    """PostgresSaver conninfo 必须注入 URL 编码的 TimeZone options。"""
    from config.settings import PostgreSQLSettings

    pg = PostgreSQLSettings(host="h", port=1, database="d", username="u", password="p")
    conninfo = pg.conninfo
    assert "options=" in conninfo
    # 解析 ?options=... 并解码,应得到 '-c TimeZone=<tz>'
    options_part = conninfo.split("?options=", 1)[1]
    decoded = unquote(options_part)
    assert decoded == f"-c TimeZone={get_timezone_name()}"


def test_pg_engine_connect_args_contains_timezone() -> None:
    """get_engine 为 PG 注入的 connect_args 必须包含 TimeZone。"""
    from core.database.engine import _pg_timezone_connect_args

    args = _pg_timezone_connect_args()
    assert "options" in args
    assert f"TimeZone={get_timezone_name()}" in args["options"]
