"""
SQLAlchemy 引擎与 Session 工厂管理。

提供数据库连接引擎创建和 Session 工厂，支持 PostgreSQL（生产）和 SQLite（测试）。

时区一致性：
    生产 PostgreSQL 连接通过 libpq ``options`` 参数注入
    ``-c TimeZone=<app.timezone>``，让 ``now()`` / ``CURRENT_TIMESTAMP``
    以及 TIMESTAMPTZ 列的显示全部走业务时区（默认 Asia/Shanghai）。

    测试的 SQLite 连接由 :func:`install_sqlite_timezone_hook` 注入 Python
    层级的时间函数覆写，见 ``tests/conftest.py`` 调用。
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config.settings import PostgreSQLSettings
from core.time_utils import get_timezone_name, now_cn


def _pg_timezone_connect_args(tz_name: str | None = None) -> dict[str, str]:
    """构造 psycopg 使用的 ``connect_args``，注入会话时区。"""
    tz = tz_name or get_timezone_name()
    return {"options": f"-c TimeZone={tz}"}


def get_engine(settings: PostgreSQLSettings) -> Engine:
    """基于配置创建 SQLAlchemy Engine（PostgreSQL，附带会话时区）。"""
    return create_engine(
        settings.dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        connect_args=_pg_timezone_connect_args(),
        echo=False,
    )


def create_engine_from_dsn(dsn: str, **kwargs: object) -> Engine:
    """从 DSN 字符串直接创建引擎（用于测试等场景）。

    - PostgreSQL DSN 自动注入 ``connect_args.options`` 的 TimeZone 设置；
      调用方若显式传入 ``connect_args``，其字段会覆盖默认值。
    - SQLite 等其他方言不做注入，由调用方通过
      :func:`install_sqlite_timezone_hook` 显式模拟时区。
    """
    if dsn.startswith("postgresql"):
        default_args = _pg_timezone_connect_args()
        user_args = kwargs.get("connect_args")
        if isinstance(user_args, dict):
            merged = {**default_args, **user_args}
        else:
            merged = default_args
        kwargs["connect_args"] = merged
    return create_engine(dsn, **kwargs)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建 Session 工厂。"""
    return sessionmaker(bind=engine, expire_on_commit=False)


def install_sqlite_timezone_hook(engine: Engine) -> None:
    """为 SQLite engine 注入时区模拟。

    SQLite 内建 ``CURRENT_TIMESTAMP`` / ``datetime('now')`` 固定返回 UTC，
    无法通过会话参数覆盖。本项目约定业务表在 Python 侧设置 ``default=now_cn``，
    写入路径已走业务时区，无需改动 DDL。

    本 hook 额外注册一个自定义 SQL 函数 ``now_cn()``，返回业务时区的
    字符串时间戳，便于在原生 SQL 中直接使用。
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register_now_cn(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        dbapi_conn.create_function(
            "now_cn",
            0,
            lambda: now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        )
