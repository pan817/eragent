"""会话历史表定义（SQLAlchemy Core）。

chat_sessions：会话元信息；chat_messages：会话消息记录。
软删除通过 deleted_at 列实现，列表查询默认过滤已删除记录。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
)

from core.memory.tables import metadata_obj

chat_sessions_table = Table(
    "chat_sessions",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("title", String(120), nullable=False, server_default="新对话"),
    Column("title_auto", Boolean, nullable=False, server_default=sa.text("true")),
    Column("message_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("last_message_preview", String(120), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

Index(
    "ix_chat_sessions_user_updated",
    chat_sessions_table.c.user_id,
    chat_sessions_table.c.updated_at.desc(),
    postgresql_where=chat_sessions_table.c.deleted_at.is_(None),
)

chat_messages_table = Table(
    "chat_messages",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("session_id", String(36), nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("status", String(16), nullable=False, server_default="success"),
    Column("duration_ms", Integer, nullable=True),
    Column("trace_id", String(64), nullable=True),
    Column("metadata_", JSON, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)

Index(
    "ix_chat_messages_session_created",
    chat_messages_table.c.session_id,
    chat_messages_table.c.created_at,
)
