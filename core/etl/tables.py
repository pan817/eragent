"""ETL sync state persistence model: etl_sync_state.

Tracks per-table watermark (last_update_date high-water mark) and sync
status for incremental ETL synchronisation from PostgreSQL to Graphiti.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.models import Base
from core.time_utils import now_cn


class ETLSyncState(Base):
    """Per-table ETL sync watermark and status."""

    __tablename__ = "etl_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(30), nullable=False)
    last_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_watermark: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_sync_status: Mapped[str] = mapped_column(String(20), nullable=False)
    rows_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_rows_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_cn
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_cn, onupdate=now_cn
    )

    __table_args__ = (
        Index("ix_etl_sync_state_domain", "domain"),
        Index("ix_etl_sync_state_status", "last_sync_status"),
    )
