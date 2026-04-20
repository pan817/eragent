"""Base extractor interface for ETL domains.

Each domain (master_data, purchasing, receiving, payables, sourcing) provides
a concrete subclass that knows which ORM models to query and how to iterate
over them in batches.
"""

from __future__ import annotations

import asyncio
from core.logging_utils import get_logger
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

_logger = get_logger(__name__)

# Default batch size — overridable via GraphitiETLSettings.batch_size
_DEFAULT_BATCH_SIZE = 500


class BaseExtractor(ABC):
    """Abstract base for domain-level data extraction."""

    def __init__(
        self,
        session_factory: sessionmaker,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        max_rows_per_table: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._max_rows_per_table = max_rows_per_table  # 0 = unlimited

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def table_names(self) -> list[str]:
        """Ordered list of logical table names this extractor covers."""

    @abstractmethod
    def domain(self) -> str:
        """Domain name: purchasing / payables / receiving / master_data / sourcing."""

    @abstractmethod
    def _model_for_table(self, table_name: str) -> type:
        """Return the ORM model class for *table_name*."""

    @abstractmethod
    def _row_to_dict(self, table_name: str, row: Any) -> dict[str, Any]:
        """Convert an ORM instance to a plain dict."""

    # ------------------------------------------------------------------
    # Concrete extraction (shared logic)
    # ------------------------------------------------------------------

    async def extract_full(self, table_name: str) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of rows for a full-table scan."""
        async for batch in self._paginated_query(table_name, since=None):
            yield batch

    async def extract_incremental(
        self, table_name: str, since: datetime
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of rows updated after *since*."""
        async for batch in self._paginated_query(table_name, since=since):
            yield batch

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _paginated_query(
        self, table_name: str, since: datetime | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Run a paginated query in a thread and yield batches.

        Respects ``max_rows_per_table``: once the cumulative row count
        reaches the cap, stops yielding further batches.
        """
        offset = 0
        total_yielded = 0
        cap = self._max_rows_per_table  # 0 = unlimited

        while True:
            batch = await asyncio.to_thread(
                self._fetch_batch, table_name, since, offset
            )
            if not batch:
                break

            # Enforce per-table row cap
            if cap > 0 and total_yielded + len(batch) > cap:
                remaining = cap - total_yielded
                if remaining > 0:
                    yield batch[:remaining]
                break

            yield batch
            total_yielded += len(batch)

            if len(batch) < self._batch_size:
                break
            offset += self._batch_size

    def _fetch_batch(
        self,
        table_name: str,
        since: datetime | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        model = self._model_for_table(table_name)
        with self._session_factory() as session:
            stmt = select(model)

            if since is not None and hasattr(model, "last_update_date"):
                stmt = stmt.where(model.last_update_date > since)
                stmt = stmt.order_by(model.last_update_date.asc())

            stmt = stmt.offset(offset).limit(self._batch_size)
            rows = session.scalars(stmt).all()
            return [self._row_to_dict(table_name, r) for r in rows]
