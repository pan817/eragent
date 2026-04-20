"""Alembic 环境配置。

target_metadata 聚合两个 metadata 容器：
- ``Base.metadata``  —— 业务表 + 可观测性 trace 表（Declarative）
- ``memory_metadata`` —— 长期记忆 memories / reports 表（Core）

数据库 URL 从 ``config.settings.get_settings()`` 注入，与运行时一致。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 migrations/ 内可以 import 项目根目录下的模块
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import get_settings  # noqa: E402
from core.database.models import Base  # noqa: E402
from core.memory.tables import metadata_obj as memory_metadata  # noqa: E402

# 触发 Declarative 子类注册（trace_runs / trace_spans 等）
import core.observability.tables  # noqa: E402,F401

# 触发 Core Table 注册到 memory_metadata（dag_cases / chat_sessions / chat_messages）
import core.chat.tables  # noqa: E402,F401
import core.orchestrator.dag.tables  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False 是关键：alembic.ini 的 fileConfig 默认会把
    # 所有未在 ini 中声明的 logger（如 eragent.*）设为 disabled=True，导致应用
    # 内 create_tables 触发 alembic 后，所有业务 / trace 日志静默消失。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 注入运行时 DSN
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.postgresql.dsn)

# 多 metadata：alembic 1.7+ 支持 list[MetaData]
target_metadata = [Base.metadata, memory_metadata]


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。

    使用 PostgreSQL advisory lock 防止多 worker 并发执行迁移。
    第一个 worker 获取锁并执行迁移，其他 worker 阻塞等待后发现
    已是最新版本，直接跳过。SQLite 等不支持 advisory lock 的方言
    直接执行迁移。
    """
    from sqlalchemy import text

    # advisory lock key：所有 worker 竞争同一把锁
    _MIGRATION_LOCK_ID = 20260413

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        use_advisory_lock = connection.dialect.name == "postgresql"

        if use_advisory_lock:
            connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _MIGRATION_LOCK_ID})

        migration_ok = False
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
            migration_ok = True
        finally:
            if use_advisory_lock:
                if not migration_ok:
                    # rollback failed transaction before releasing advisory lock
                    connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _MIGRATION_LOCK_ID})
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
