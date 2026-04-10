"""长期记忆表定义（SQLAlchemy Core）。

memories：通用记忆存储；reports：分析报告存储。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects.postgresql import JSON


metadata_obj = MetaData()

memories_table = Table(
    "memories",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("session_id", String(128), nullable=False, index=True),
    Column("memory_type", String(64), nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(16), nullable=True, index=False),
    Column("attrs", JSON, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)

reports_table = Table(
    "reports",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("session_id", String(128), nullable=False, index=True),
    Column("query", Text, nullable=False),
    Column("analysis_type", String(64), nullable=False),
    Column("time_range", String(128), nullable=True),
    Column("result_json", Text, nullable=True),
    Column("report_markdown", Text, nullable=True),
    Column("anomaly_count", Integer, nullable=False, default=0),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)

# 复合索引：加速 list_reports(user_id, ORDER BY created_at DESC LIMIT N)
# 单列 user_id 索引已被此复合索引取代，无需重复建立
Index("reports_user_created", reports_table.c.user_id, reports_table.c.created_at.desc())
