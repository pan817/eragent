"""
SQLAlchemy 引擎与 Session 工厂管理。

提供数据库连接引擎创建和 Session 工厂，支持 PostgreSQL（生产）和 SQLite（测试）。
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import PostgreSQLSettings


def get_engine(settings: PostgreSQLSettings) -> Engine:
    """基于配置创建 SQLAlchemy Engine。

    Args:
        settings: PostgreSQL 配置对象。

    Returns:
        配置好的 SQLAlchemy Engine。
    """
    return create_engine(
        settings.dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        echo=False,
    )


def create_engine_from_dsn(dsn: str, **kwargs: object) -> Engine:
    """从 DSN 字符串直接创建引擎（用于测试等场景）。

    Args:
        dsn: 数据库连接字符串。
        **kwargs: 传递给 create_engine 的额外参数。

    Returns:
        SQLAlchemy Engine。
    """
    return create_engine(dsn, **kwargs)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建 Session 工厂。

    Args:
        engine: SQLAlchemy Engine。

    Returns:
        绑定到引擎的 sessionmaker。
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
