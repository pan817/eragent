"""Tests for ETLPipeline and ETLScheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.etl.models import LoadResult, SyncResult
from core.etl.pipeline import ETLPipeline
from core.etl.scheduler import ETLScheduler
from core.etl.state import SyncStateManager


# ============================================================
# Helpers
# ============================================================


def _make_mock_extractor(domain: str, tables: list[str]):
    ext = MagicMock()
    ext.domain.return_value = domain
    ext.table_names.return_value = tables

    async def _full(table_name):
        yield [{"vendor_id": "SUP-001", "vendor_name": "Test", "last_update_date": datetime.now(timezone.utc)}]

    async def _incr(table_name, since):
        yield [{"vendor_id": "SUP-001", "vendor_name": "Test", "last_update_date": datetime.now(timezone.utc)}]

    ext.extract_full = _full
    ext.extract_incremental = _incr
    return ext


def _make_mock_transformer():
    from core.etl.models import GraphitiNode, TransformResult

    t = MagicMock()
    t.transform.return_value = TransformResult(
        nodes=[GraphitiNode(entity_type="Supplier", entity_id="1", properties={})],
        edges=[],
        free_text_records=[],
    )
    return t


def _make_mock_loader():
    loader = MagicMock()
    loader.load = AsyncMock(return_value=LoadResult(loaded=1))
    loader.create_edges = AsyncMock(return_value=0)
    loader._client = MagicMock()
    loader._client.clear_graph = AsyncMock()
    return loader


# ============================================================
# ETLPipeline
# ============================================================


class TestETLPipeline:
    @pytest.fixture()
    def pipeline(self, db_session_factory):
        ext = _make_mock_extractor("master_data", ["AP_SUPPLIERS"])
        transformer = _make_mock_transformer()
        loader = _make_mock_loader()
        state_mgr = SyncStateManager(db_session_factory)
        return ETLPipeline(
            extractors={"master_data": ext},
            transformer=transformer,
            loader=loader,
            state_manager=state_mgr,
        )

    def test_needs_full_sync_initially_true(self, pipeline):
        result = asyncio.get_event_loop().run_until_complete(
            pipeline.needs_full_sync()
        )
        assert result is True

    def test_run_full_sync(self, pipeline):
        result = asyncio.get_event_loop().run_until_complete(
            pipeline.run_full_sync()
        )
        assert isinstance(result, SyncResult)
        assert result.sync_type == "full"
        assert result.total_rows >= 1
        assert "AP_SUPPLIERS" in result.succeeded_tables
        assert result.failed_tables == []

    def test_needs_full_sync_false_after_sync(self, pipeline):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(pipeline.run_full_sync())
        result = loop.run_until_complete(pipeline.needs_full_sync())
        assert result is False

    def test_run_incremental_sync(self, pipeline):
        loop = asyncio.get_event_loop()
        # First full sync to set watermarks
        loop.run_until_complete(pipeline.run_full_sync())
        # Then incremental
        result = loop.run_until_complete(pipeline.run_incremental_sync())
        assert result.sync_type == "incremental"
        assert result.total_rows >= 1

    def test_full_sync_multi_domain(self, db_session_factory):
        ext1 = _make_mock_extractor("master_data", ["AP_SUPPLIERS"])
        ext2 = _make_mock_extractor("purchasing", ["PO_HEADERS_ALL"])
        transformer = _make_mock_transformer()
        loader = _make_mock_loader()
        state_mgr = SyncStateManager(db_session_factory)

        pipeline = ETLPipeline(
            extractors={"master_data": ext1, "purchasing": ext2},
            transformer=transformer,
            loader=loader,
            state_manager=state_mgr,
        )
        result = asyncio.get_event_loop().run_until_complete(
            pipeline.run_full_sync()
        )
        assert len(result.succeeded_tables) == 2
        assert result.total_rows >= 2

    def test_sync_table_failure_recorded(self, db_session_factory):
        ext = MagicMock()
        ext.domain.return_value = "master_data"
        ext.table_names.return_value = ["BAD_TABLE"]

        async def _explode(table_name):
            raise RuntimeError("DB down")
            yield  # pragma: no cover — makes it an async generator

        ext.extract_full = _explode

        transformer = _make_mock_transformer()
        loader = _make_mock_loader()
        state_mgr = SyncStateManager(db_session_factory)

        pipeline = ETLPipeline(
            extractors={"master_data": ext},
            transformer=transformer,
            loader=loader,
            state_manager=state_mgr,
        )
        result = asyncio.get_event_loop().run_until_complete(
            pipeline.run_full_sync()
        )
        assert "BAD_TABLE" in result.failed_tables


# ============================================================
# ETLScheduler
# ============================================================


class TestETLScheduler:
    @pytest.fixture()
    def scheduler(self, db_session_factory):
        ext = _make_mock_extractor("master_data", ["AP_SUPPLIERS"])
        transformer = _make_mock_transformer()
        loader = _make_mock_loader()
        state_mgr = SyncStateManager(db_session_factory)
        pipeline = ETLPipeline(
            extractors={"master_data": ext},
            transformer=transformer,
            loader=loader,
            state_manager=state_mgr,
        )
        return ETLScheduler(
            pipeline=pipeline,
            state_manager=state_mgr,
            interval_seconds=3600,  # long interval so loop doesn't fire
            full_sync_on_startup=True,
        )

    def test_start_and_shutdown(self, scheduler):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(scheduler.start())
        assert scheduler._running is True
        assert scheduler._task is not None
        loop.run_until_complete(scheduler.shutdown())
        assert scheduler._running is False

    def test_manual_trigger_full(self, scheduler):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(scheduler.start())
        result = loop.run_until_complete(
            scheduler.trigger_manual_sync("full")
        )
        assert result["sync_type"] == "full"
        loop.run_until_complete(scheduler.shutdown())

    def test_manual_trigger_incremental(self, scheduler):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(scheduler.start())
        result = loop.run_until_complete(
            scheduler.trigger_manual_sync("incremental")
        )
        assert result["sync_type"] == "incremental"
        loop.run_until_complete(scheduler.shutdown())

    def test_start_without_full_sync(self, db_session_factory):
        ext = _make_mock_extractor("master_data", ["AP_SUPPLIERS"])
        transformer = _make_mock_transformer()
        loader = _make_mock_loader()
        state_mgr = SyncStateManager(db_session_factory)
        pipeline = ETLPipeline(
            extractors={"master_data": ext},
            transformer=transformer,
            loader=loader,
            state_manager=state_mgr,
        )
        scheduler = ETLScheduler(
            pipeline=pipeline,
            state_manager=state_mgr,
            interval_seconds=3600,
            full_sync_on_startup=False,  # skip
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(scheduler.start())
        assert scheduler._running is True
        loop.run_until_complete(scheduler.shutdown())
