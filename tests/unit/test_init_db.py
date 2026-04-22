"""Unit tests for core/database/init_db.py — uncovered lines."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from core.database.init_db import (
    _parse_date_opt,
    _parse_datetime,
    _parse_datetime_opt,
    create_tables,
)


# ---------------------------------------------------------------------------
# _parse_datetime: line 49
# ---------------------------------------------------------------------------


class TestParseDatetime:
    def test_basic(self) -> None:
        result = _parse_datetime("2024-01-15")
        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_with_time(self) -> None:
        result = _parse_datetime("2024-06-01T10:30:00")
        assert result.hour == 10
        assert result.minute == 30


# ---------------------------------------------------------------------------
# _parse_datetime_opt: line 55
# ---------------------------------------------------------------------------


class TestParseDatetimeOpt:
    def test_none_input(self) -> None:
        assert _parse_datetime_opt(None) is None

    def test_valid_input(self) -> None:
        result = _parse_datetime_opt("2024-03-20")
        assert isinstance(result, datetime)


# ---------------------------------------------------------------------------
# _parse_date_opt
# ---------------------------------------------------------------------------


class TestParseDateOpt:
    def test_none_input(self) -> None:
        assert _parse_date_opt(None) is None

    def test_valid_input(self) -> None:
        result = _parse_date_opt("2024-12-25")
        assert isinstance(result, date)


# ---------------------------------------------------------------------------
# create_tables — PostgreSQL path (lines 420-464)
# ---------------------------------------------------------------------------


class TestCreateTablesPostgres:
    """Cover the alembic-based PostgreSQL migration path.

    Since the function does lazy imports (from alembic import command, etc.),
    we patch at the source module level: alembic.command, alembic.config.Config,
    sqlalchemy.inspect.
    """

    def _make_pg_engine(
        self,
        *,
        has_business_tables: bool = False,
        has_alembic_version: bool = False,
        alembic_version_empty: bool = False,
    ) -> MagicMock:
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.url = "postgresql://user:pass@localhost/test"

        inspector = MagicMock()
        tables = set()
        if has_business_tables:
            tables.add("ap_suppliers")
        if has_alembic_version:
            tables.add("alembic_version")
        inspector.get_table_names.return_value = list(tables)

        # Connection mock for SELECT version_num
        conn_mock = MagicMock()
        if alembic_version_empty:
            conn_mock.execute.return_value.first.return_value = None
        else:
            conn_mock.execute.return_value.first.return_value = ("0013",)
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        engine.connect.return_value = conn_mock

        return engine, inspector

    @patch("alembic.command")
    @patch("alembic.config.Config")
    @patch("sqlalchemy.inspect")
    def test_fresh_database_no_stamp(
        self, mock_inspect: MagicMock, mock_config_cls: MagicMock,
        mock_command: MagicMock,
    ) -> None:
        """No business tables → just upgrade, no stamp."""
        engine, inspector = self._make_pg_engine()
        mock_inspect.return_value = inspector
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        create_tables(engine)

        mock_command.stamp.assert_not_called()
        mock_command.upgrade.assert_called_once_with(mock_cfg, "head")

    @patch("alembic.command")
    @patch("alembic.config.Config")
    @patch("sqlalchemy.inspect")
    def test_existing_db_without_alembic_stamps_head(
        self, mock_inspect: MagicMock, mock_config_cls: MagicMock,
        mock_command: MagicMock,
    ) -> None:
        """Business tables exist but no alembic_version → stamp head first."""
        engine, inspector = self._make_pg_engine(has_business_tables=True)
        mock_inspect.return_value = inspector
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        create_tables(engine)

        mock_command.stamp.assert_called_once_with(mock_cfg, "head")
        mock_command.upgrade.assert_called_once()

    @patch("alembic.command")
    @patch("alembic.config.Config")
    @patch("sqlalchemy.inspect")
    def test_existing_db_empty_alembic_version(
        self, mock_inspect: MagicMock, mock_config_cls: MagicMock,
        mock_command: MagicMock,
    ) -> None:
        """Business tables + alembic_version empty → stamp head."""
        engine, inspector = self._make_pg_engine(
            has_business_tables=True,
            has_alembic_version=True,
            alembic_version_empty=True,
        )
        mock_inspect.return_value = inspector
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        create_tables(engine)

        mock_command.stamp.assert_called_once_with(mock_cfg, "head")
        mock_command.upgrade.assert_called_once()

    @patch("alembic.command")
    @patch("alembic.config.Config")
    @patch("sqlalchemy.inspect")
    def test_existing_db_with_alembic_version(
        self, mock_inspect: MagicMock, mock_config_cls: MagicMock,
        mock_command: MagicMock,
    ) -> None:
        """Business tables + populated alembic_version → no stamp."""
        engine, inspector = self._make_pg_engine(
            has_business_tables=True,
            has_alembic_version=True,
            alembic_version_empty=False,
        )
        mock_inspect.return_value = inspector
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        create_tables(engine)

        mock_command.stamp.assert_not_called()
        mock_command.upgrade.assert_called_once()
