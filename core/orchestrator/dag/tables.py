"""DAG 案例表定义（SQLAlchemy Core）。

dag_cases：成功执行的 DAG 案例持久化存储，PostgreSQL 为权威数据源，
Chroma 为检索缓存，服务启动时从 PG 全量加载到 Chroma。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import JSON, Column, DateTime, Float, Index, Integer, String, Table, Text

from core.memory.tables import metadata_obj
from core.time_utils import now_cn

dag_cases_table = Table(
    "dag_cases",
    metadata_obj,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("query_hash", String(16), unique=True, nullable=False),
    Column("query", Text, nullable=False),
    Column("analysis_type", String(64), nullable=False),
    Column("dag_definition", JSON, nullable=False),
    Column("route_type", String(32), nullable=False),
    Column("task_count", Integer, nullable=False),
    Column("duration_sec", Float, nullable=True),
    # Python-side default 确保无论 SQLite（无时区）还是 PG 都写入业务时区时间。
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=now_cn,
        server_default=sa.func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        default=now_cn,
        onupdate=now_cn,
        server_default=sa.func.now(),
    ),
)

Index("dag_cases_analysis_type", dag_cases_table.c.analysis_type)
