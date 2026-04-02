"""
记忆管理模块。

提供短期记忆（会话内多轮对话上下文）和长期记忆（PostgreSQL 持久化）两种机制。
短期记忆负责维护单次会话的对话历史并支持自动压缩；
长期记忆通过 SQLAlchemy Core 将记忆和分析报告持久化到 PostgreSQL。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSON


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------

metadata_obj = MetaData()

memories_table = Table(
    "memories",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("session_id", String(128), nullable=False, index=True),
    Column("memory_type", String(64), nullable=False),
    Column("content", Text, nullable=False),
    Column("metadata", JSON, nullable=True),
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
    Column("user_id", String(128), nullable=False, index=True),
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


# ---------------------------------------------------------------------------
# ShortTermMemory
# ---------------------------------------------------------------------------


class ShortTermMemory:
    """管理单个会话的短期记忆（多轮对话上下文）。

    内部维护一个有序消息列表和可选的摘要文本。当消息数量超过
    ``summary_threshold`` 时，可通过外部提供的摘要函数将早期消息
    压缩为一段摘要，从而在保持上下文连贯性的同时控制 token 消耗。

    Attributes:
        max_messages: 保留的最大原始消息数量。
        summary_threshold: 触发压缩的消息数量阈值。
        messages: 当前会话的消息列表。
        summary: 压缩后的早期对话摘要文本。
    """

    def __init__(
        self,
        max_messages: int = 20,
        summary_threshold: int = 15,
    ) -> None:
        """初始化短期记忆。

        Args:
            max_messages: 压缩后保留的最近消息条数。
            summary_threshold: 当消息总数达到此值时 ``needs_compression`` 返回 True。
        """
        self.max_messages: int = max_messages
        self.summary_threshold: int = summary_threshold
        self.messages: list[dict[str, str]] = []
        self.summary: str = ""

    def add_message(self, role: str, content: str) -> None:
        """向对话历史中追加一条消息。

        Args:
            role: 消息角色，例如 ``"user"``、``"assistant"``、``"system"``。
            content: 消息文本内容。
        """
        self.messages.append({"role": role, "content": content})

    def get_context(self) -> list[dict[str, str]]:
        """返回当前上下文，包含摘要（如有）和近期消息。

        如果存在压缩摘要，会在消息列表前插入一条 ``system`` 角色的摘要消息，
        以便 LLM 获取早期对话的关键信息。

        Returns:
            上下文消息列表，每条消息为 ``{"role": ..., "content": ...}`` 字典。
        """
        context: list[dict[str, str]] = []
        if self.summary:
            context.append({
                "role": "system",
                "content": f"以下是早期对话的摘要：\n{self.summary}",
            })
        context.extend(self.messages)
        return context

    def needs_compression(self) -> bool:
        """判断当前消息数量是否已达到压缩阈值。

        Returns:
            如果消息数量 >= ``summary_threshold`` 则返回 True。
        """
        return len(self.messages) >= self.summary_threshold

    def compress(self, summarizer_fn: Callable[[list[dict[str, str]]], str]) -> None:
        """将早期消息压缩为摘要，仅保留最近的消息。

        压缩逻辑：
        1. 计算需要保留的最近消息数量（``max_messages`` 的一半，至少保留 5 条）。
        2. 将早期消息通过 ``summarizer_fn`` 生成摘要文本。
        3. 若已有摘要则与新摘要合并。
        4. 仅保留最近的消息。

        Args:
            summarizer_fn: 接收消息列表并返回摘要字符串的可调用对象，
                          通常是调用 LLM 的函数。
        """
        keep_count: int = max(len(self.messages) // 2, 5)
        if keep_count >= len(self.messages):
            return

        early_messages: list[dict[str, str]] = self.messages[:-keep_count]
        new_summary: str = summarizer_fn(early_messages)

        if self.summary:
            self.summary = f"{self.summary}\n\n{new_summary}"
        else:
            self.summary = new_summary

        self.messages = self.messages[-keep_count:]

    def clear(self) -> None:
        """清空所有消息和摘要，重置短期记忆。"""
        self.messages.clear()
        self.summary = ""


# ---------------------------------------------------------------------------
# LongTermMemory
# ---------------------------------------------------------------------------


class LongTermMemory:
    """基于 PostgreSQL 的长期记忆持久化。

    使用 SQLAlchemy Core 管理两张表：
    - ``memories``：通用记忆存储，支持按用户和关键词检索。
    - ``reports``：分析报告存储，支持按用户列出和按 ID 查询。

    Attributes:
        dsn: PostgreSQL 连接字符串。
        engine: SQLAlchemy 引擎实例。
    """

    def __init__(self, dsn: str) -> None:
        """初始化长期记忆，创建数据库引擎。

        Args:
            dsn: PostgreSQL 连接字符串，
                 例如 ``"postgresql+psycopg2://user:pass@host:5432/db"``。
        """
        self.dsn: str = dsn
        self.engine: sa.engine.Engine = create_engine(
            dsn,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )

    def init_tables(self) -> None:
        """创建数据库表（如果不存在）。

        此方法是幂等的，可安全地多次调用。使用
        ``CREATE TABLE IF NOT EXISTS`` 语义。
        """
        metadata_obj.create_all(self.engine, checkfirst=True)

    def save_memory(
        self,
        user_id: str,
        session_id: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> str:
        """保存一条记忆记录。

        Args:
            user_id: 用户唯一标识。
            session_id: 会话唯一标识。
            memory_type: 记忆类型，例如 ``"conversation"``、``"insight"``。
            content: 记忆文本内容。
            metadata: 附加元数据，以 JSON 格式存储。

        Returns:
            新创建记录的 UUID 字符串。
        """
        memory_id: str = str(uuid.uuid4())
        stmt = memories_table.insert().values(
            id=memory_id,
            user_id=user_id,
            session_id=session_id,
            memory_type=memory_type,
            content=content,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()
        return memory_id

    def search_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按关键词检索用户的记忆记录。

        MVP 阶段使用 SQL LIKE 进行简单的文本匹配，
        后续可替换为向量检索。

        Args:
            user_id: 用户唯一标识。
            query: 搜索关键词。
            limit: 返回的最大记录数，默认 5。

        Returns:
            匹配的记忆记录列表，每条记录为字典格式，包含
            ``id``、``user_id``、``session_id``、``memory_type``、
            ``content``、``metadata``、``created_at`` 字段。
        """
        stmt = (
            sa.select(memories_table)
            .where(
                sa.and_(
                    memories_table.c.user_id == user_id,
                    memories_table.c.content.like(f"%{query}%"),
                )
            )
            .order_by(memories_table.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result]

    def save_report(
        self,
        user_id: str,
        session_id: str,
        query: str,
        analysis_type: str,
        result_json: str,
        report_markdown: str,
        anomaly_count: int,
    ) -> str:
        """保存一份分析报告。

        Args:
            user_id: 用户唯一标识。
            session_id: 会话唯一标识。
            query: 用户的原始查询文本。
            analysis_type: 分析类型，例如 ``"three_way_match"``、``"payment_compliance"``。
            result_json: 分析结果的 JSON 字符串。
            report_markdown: Markdown 格式的分析报告。
            anomaly_count: 检测到的异常数量。

        Returns:
            新创建报告的 UUID 字符串。
        """
        report_id: str = str(uuid.uuid4())
        stmt = reports_table.insert().values(
            id=report_id,
            user_id=user_id,
            session_id=session_id,
            query=query,
            analysis_type=analysis_type,
            result_json=result_json,
            report_markdown=report_markdown,
            anomaly_count=anomaly_count,
            created_at=datetime.now(timezone.utc),
        )
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()
        return report_id

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """按 ID 获取单份分析报告。

        Args:
            report_id: 报告的 UUID 字符串。

        Returns:
            报告字典（包含所有字段），如果不存在则返回 None。
        """
        stmt = sa.select(reports_table).where(reports_table.c.id == report_id)
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            row = result.fetchone()
            if row is None:
                return None
            return dict(row._mapping)

    def list_reports(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """列出用户的分析报告，按创建时间倒序排列。

        Args:
            user_id: 用户唯一标识。
            limit: 返回的最大记录数，默认 20。

        Returns:
            报告记录列表，每条记录为字典格式。
        """
        stmt = (
            sa.select(reports_table)
            .where(reports_table.c.user_id == user_id)
            .order_by(reports_table.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result]
