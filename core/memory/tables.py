"""长期记忆表定义（SQLAlchemy Core）。

memories：通用记忆存储；reports：分析报告存储。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, Text
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
    Column("entity_id", String(128), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("consolidated_at", DateTime(timezone=True), nullable=True),
    Column(
        "is_consolidated",
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    ),
    Column("source_ids", JSON, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)

# memories: type + user composite index (type-aware queries)
Index("memories_type_user", memories_table.c.memory_type, memories_table.c.user_id)

# memories: entity_id partial index (entity_profile exact lookup)
Index(
    "memories_entity",
    memories_table.c.user_id,
    memories_table.c.entity_id,
    postgresql_where=memories_table.c.entity_id.isnot(None),
)

# memories: expires_at partial index (TTL purge scan)
Index(
    "memories_expires",
    memories_table.c.expires_at,
    postgresql_where=memories_table.c.expires_at.isnot(None),
)

# memories: consolidation candidate query
Index(
    "memories_consolidation",
    memories_table.c.user_id,
    memories_table.c.is_consolidated,
    memories_table.c.memory_type,
)

reports_table = Table(
    "reports",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("session_id", String(128), nullable=False, index=True),
    # trace_id: 分析请求的全链路 trace。一次 analyze 产生唯一一份报告，
    # 所以 (trace_id) 是候选键（见 reports_trace_id UNIQUE 索引）。
    # nullable 是为了容纳 `is_recall=True` / 极端情况下 `_persist_report` 失败的遗留，
    # 并让 UNIQUE 索引在这些场景下多个 NULL 共存（PG 语义：NULL 间不比较）。
    Column("trace_id", String(36), nullable=True),
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

# UNIQUE 索引：按 trace_id 反查单条报告（/analyze/tasks/{trace_id} 回落 DB 时用）。
# UNIQUE 语义表达"一个 trace 最多一份报告"的业务约束；PG 允许多个 NULL 共存，
# 不影响历史行（trace_id=NULL）的存在。
Index("reports_trace_id", reports_table.c.trace_id, unique=True)


# ── 记忆整合日志 ─────────────────────────────────────────────────────
# 记录每次整合的执行状态，用于触发条件判定 + 审计追溯 + per-user 锁。

memory_consolidation_log_table = Table(
    "memory_consolidation_log",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False),  # running / completed / failed
    Column("input_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("merged_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("pruned_count", Integer, nullable=False, server_default=sa.text("0")),
    Column(
        "llm_used",
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    ),
    Column("error_message", Text, nullable=True),
    Column("details", JSON, nullable=True),
)

Index(
    "consolidation_user_status",
    memory_consolidation_log_table.c.user_id,
    memory_consolidation_log_table.c.status,
)
Index(
    "consolidation_completed",
    memory_consolidation_log_table.c.user_id,
    memory_consolidation_log_table.c.completed_at.desc(),
)


# ── 会话实体上下文 ───────────────────────────────────────────────────
# 结构化存储每个 session 当前讨论的业务实体（po_number / supplier_id 等），
# 不依赖从消息文本正则提取。每轮分析后 upsert 更新。

session_entities_table = Table(
    "session_entities",
    metadata_obj,
    Column("session_id", String(36), primary_key=True),
    Column("entities", JSON, nullable=False, server_default=sa.text("'{}'")),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)
