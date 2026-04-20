"""Tests for core/etl — ETLSyncState table, SyncStateManager, GraphitiClient, config."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from core.etl.config import GraphitiETLSettings
from core.etl.tables import ETLSyncState
from core.etl.state import SyncStateManager
from core.etl.client import GraphitiClient, EpisodeData


# ============================================================
# Config
# ============================================================


class TestGraphitiETLSettings:
    def test_defaults(self):
        cfg = GraphitiETLSettings()
        assert cfg.enabled is True
        assert cfg.query_backend == "graphiti"
        assert cfg.sync_interval_seconds == 600
        assert cfg.batch_size == 500
        assert cfg.retry_max_attempts == 3
        assert cfg.llm_extraction_enabled is False
        assert cfg.max_nodes_per_sync == 50_000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ETL_ENABLED", "false")
        monkeypatch.setenv("ETL_BATCH_SIZE", "100")
        cfg = GraphitiETLSettings()
        assert cfg.enabled is False
        assert cfg.batch_size == 100

    def test_from_yaml_integration(self):
        from config.settings import Settings

        s = Settings.from_yaml()
        assert isinstance(s.graphiti_etl, GraphitiETLSettings)
        assert s.graphiti_etl.enabled is True


# ============================================================
# ETLSyncState ORM table
# ============================================================


class TestETLSyncStateTable:
    def test_table_created(self, db_engine):
        """etl_sync_state table should be created by init_database."""
        from sqlalchemy import inspect

        insp = inspect(db_engine)
        tables = insp.get_table_names()
        assert "etl_sync_state" in tables

    def test_insert_and_query(self, db_session_factory):
        now = datetime.now(timezone.utc)
        with db_session_factory() as session:
            row = ETLSyncState(
                table_name="PO_HEADERS_ALL",
                domain="purchasing",
                last_sync_at=now,
                last_watermark=now,
                last_sync_status="SUCCESS",
                rows_synced=100,
                total_rows_synced=100,
                sync_type="FULL",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()

            result = session.execute(
                select(ETLSyncState).where(
                    ETLSyncState.table_name == "PO_HEADERS_ALL"
                )
            ).scalar_one()
            assert result.domain == "purchasing"
            assert result.rows_synced == 100
            assert result.last_sync_status == "SUCCESS"

    def test_unique_table_name(self, db_session_factory):
        now = datetime.now(timezone.utc)
        with db_session_factory() as session:
            session.add(
                ETLSyncState(
                    table_name="AP_INVOICES_ALL",
                    domain="payables",
                    last_sync_at=now,
                    last_watermark=now,
                    last_sync_status="SUCCESS",
                    rows_synced=0,
                    total_rows_synced=0,
                    sync_type="FULL",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with pytest.raises(Exception):
            with db_session_factory() as session:
                session.add(
                    ETLSyncState(
                        table_name="AP_INVOICES_ALL",
                        domain="payables",
                        last_sync_at=now,
                        last_watermark=now,
                        last_sync_status="SUCCESS",
                        rows_synced=0,
                        total_rows_synced=0,
                        sync_type="FULL",
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.commit()


# ============================================================
# SyncStateManager
# ============================================================


class TestSyncStateManager:
    @pytest.fixture()
    def manager(self, db_session_factory) -> SyncStateManager:
        return SyncStateManager(db_session_factory)

    def test_get_watermark_empty(self, manager):
        result = asyncio.get_event_loop().run_until_complete(
            manager.get_watermark("NONEXISTENT_TABLE")
        )
        assert result is None

    def test_update_and_get_watermark(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            manager.update_watermark(
                table_name="PO_HEADERS_ALL",
                domain="purchasing",
                watermark=now,
                rows_synced=50,
                sync_type="FULL",
                status="SUCCESS",
            )
        )
        result = loop.run_until_complete(manager.get_watermark("PO_HEADERS_ALL"))
        assert result is not None

    def test_needs_full_sync_true(self, manager):
        result = asyncio.get_event_loop().run_until_complete(
            manager.needs_full_sync(["PO_HEADERS_ALL", "AP_INVOICES_ALL"])
        )
        assert result is True

    def test_needs_full_sync_false_after_update(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        for table in ["PO_HEADERS_ALL", "AP_INVOICES_ALL"]:
            loop.run_until_complete(
                manager.update_watermark(
                    table_name=table,
                    domain="test",
                    watermark=now,
                    rows_synced=10,
                    sync_type="FULL",
                    status="SUCCESS",
                )
            )
        result = loop.run_until_complete(
            manager.needs_full_sync(["PO_HEADERS_ALL", "AP_INVOICES_ALL"])
        )
        assert result is False

    def test_update_accumulates_total(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        for i in range(3):
            loop.run_until_complete(
                manager.update_watermark(
                    table_name="PO_LINES_ALL",
                    domain="purchasing",
                    watermark=now,
                    rows_synced=10,
                    sync_type="INCREMENTAL",
                    status="SUCCESS",
                )
            )
        states = loop.run_until_complete(manager.get_all_states())
        po_lines = [s for s in states if s["table_name"] == "PO_LINES_ALL"]
        assert len(po_lines) == 1
        assert po_lines[0]["total_rows_synced"] == 30
        assert po_lines[0]["rows_synced"] == 10

    def test_get_all_states(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            manager.update_watermark(
                table_name="AP_CHECKS_ALL",
                domain="payables",
                watermark=now,
                rows_synced=5,
                sync_type="FULL",
                status="SUCCESS",
            )
        )
        states = loop.run_until_complete(manager.get_all_states())
        assert len(states) >= 1
        row = states[0]
        assert "table_name" in row
        assert "lag_seconds" in row

    def test_recover_stale_running(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            manager.update_watermark(
                table_name="STALE_TABLE",
                domain="test",
                watermark=now,
                rows_synced=0,
                sync_type="FULL",
                status="RUNNING",
            )
        )
        count = loop.run_until_complete(manager.recover_stale_running())
        assert count == 1

        states = loop.run_until_complete(manager.get_all_states())
        stale = [s for s in states if s["table_name"] == "STALE_TABLE"]
        assert stale[0]["last_sync_status"] == "FAILED"

    def test_error_message_stored(self, manager):
        now = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            manager.update_watermark(
                table_name="FAIL_TABLE",
                domain="test",
                watermark=now,
                rows_synced=0,
                sync_type="INCREMENTAL",
                status="FAILED",
                error_message="Connection refused",
            )
        )
        states = loop.run_until_complete(manager.get_all_states())
        fail = [s for s in states if s["table_name"] == "FAIL_TABLE"]
        assert fail[0]["error_message"] == "Connection refused"


# ============================================================
# GraphitiClient
# ============================================================


class TestGraphitiClient:
    def test_init_not_connected(self, settings):
        client = GraphitiClient(settings)
        assert not client.is_connected

    def test_ensure_connected_raises(self, settings):
        client = GraphitiClient(settings)
        with pytest.raises(RuntimeError, match="not initialised"):
            asyncio.get_event_loop().run_until_complete(
                client.search("test")
            )

    def test_close_safe_when_not_connected(self, settings):
        client = GraphitiClient(settings)
        asyncio.get_event_loop().run_until_complete(client.close())

    def test_episode_data_defaults(self):
        ep = EpisodeData(name="test", body="body text")
        assert ep.source == "etl"
        assert ep.metadata == {}
