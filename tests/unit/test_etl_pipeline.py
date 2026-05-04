"""Tests for ETL pipeline components: extractors, transformer, loader, registry, pipeline."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.etl.models import (
    EdgeMapping,
    GraphitiEdge,
    GraphitiNode,
    LoadResult,
    SyncResult,
    TableMapping,
    TextRecord,
    TransformResult,
)
from core.etl.transformers.registry import MAPPING_REGISTRY
from core.etl.transformers.structured import StructuredTransformer
from core.etl.loaders.graphiti_loader import GraphitiLoader
from core.etl.pipeline import ETLPipeline


# ============================================================
# Data models
# ============================================================


class TestDataModels:
    def test_graphiti_node(self):
        node = GraphitiNode(
            entity_type="Supplier",
            entity_id="SUP-001",
            properties={"vendor_name": "Test"},
        )
        assert node.entity_type == "Supplier"
        assert node.valid_from is None

    def test_graphiti_edge(self):
        edge = GraphitiEdge(
            edge_type="CREATES_PO",
            source_type="Supplier",
            source_id="SUP-001",
            target_type="PurchaseOrder",
            target_id="1",
        )
        assert edge.created_at is None

    def test_transform_result_defaults(self):
        result = TransformResult()
        assert result.nodes == []
        assert result.edges == []
        assert result.free_text_records == []

    def test_load_result(self):
        lr = LoadResult(loaded=10, skipped=2, failed=1)
        assert lr.loaded == 10

    def test_sync_result_to_dict(self):
        sr = SyncResult(sync_type="full", total_rows=100, total_nodes=50)
        d = sr.to_dict()
        assert d["sync_type"] == "full"
        assert d["total_rows"] == 100

    def test_table_mapping(self):
        m = TableMapping(
            node_type="Supplier",
            id_field="vendor_id",
            property_mapping={"name": "vendor_name"},
            temporal={"valid_from": None, "valid_to": None, "last_updated": None},
        )
        assert m.free_text_fields == []
        assert m.edges == []


# ============================================================
# Mapping registry
# ============================================================


class TestMappingRegistry:
    def test_all_20_tables_registered(self):
        assert len(MAPPING_REGISTRY) == 20

    def test_all_entries_are_table_mappings(self):
        for name, m in MAPPING_REGISTRY.items():
            assert isinstance(m, TableMapping), f"{name} is not a TableMapping"
            assert m.node_type, f"{name} has empty node_type"
            assert m.id_field, f"{name} has empty id_field"

    def test_known_tables(self):
        expected = {
            "AP_SUPPLIERS", "AP_SUPPLIER_SITES_ALL", "MTL_SYSTEM_ITEMS_B",
            "PO_HEADERS_ALL", "PO_LINES_ALL", "PO_LINE_LOCATIONS_ALL", "PO_DISTRIBUTIONS_ALL",
            "RCV_SHIPMENT_HEADERS", "RCV_SHIPMENT_LINES", "RCV_TRANSACTIONS",
            "AP_INVOICES_ALL", "AP_INVOICE_LINES_ALL", "AP_INVOICE_DISTRIBUTIONS_ALL",
            "AP_CHECKS_ALL", "AP_INVOICE_PAYMENTS_ALL", "AP_PAYMENT_SCHEDULES_ALL",
            "PON_AUCTION_HEADERS_ALL", "PON_BID_HEADERS",
            "OKC_K_HEADERS_B", "OKC_K_LINES_B",
        }
        assert set(MAPPING_REGISTRY.keys()) == expected

    def test_po_headers_mapping(self):
        m = MAPPING_REGISTRY["PO_HEADERS_ALL"]
        assert m.node_type == "PurchaseOrder"
        assert m.id_field == "po_header_id"
        assert len(m.edges) == 1
        assert m.edges[0].edge_type == "CREATES_PO"
        assert "comments" in m.free_text_fields

    def test_ap_invoices_mapping(self):
        m = MAPPING_REGISTRY["AP_INVOICES_ALL"]
        assert m.node_type == "Invoice"
        assert m.temporal["valid_from"] == "invoice_date"
        assert m.temporal["valid_to"] == "cancelled_date"

    def test_edge_count(self):
        total_edges = sum(len(m.edges) for m in MAPPING_REGISTRY.values())
        assert total_edges >= 15  # at least 15 edge mappings across all tables


# ============================================================
# StructuredTransformer
# ============================================================


class TestStructuredTransformer:
    @pytest.fixture()
    def transformer(self) -> StructuredTransformer:
        return StructuredTransformer(MAPPING_REGISTRY)

    def test_transform_supplier(self, transformer):
        rows = [
            {
                "vendor_id": "SUP-001",
                "vendor_name": "Test Corp",
                "segment1": "V001",
                "vendor_type_lookup_code": "VENDOR",
                "terms_id": "NET30",
                "enabled_flag": "Y",
                "standard_industry_class": "MFG",
                "small_business_flag": "N",
                "women_owned_flag": "N",
                "start_date_active": date(2020, 1, 1),
                "end_date_active": None,
                "last_update_date": datetime(2026, 1, 1),
            }
        ]
        result = transformer.transform("AP_SUPPLIERS", rows)
        assert len(result.nodes) == 1
        node = result.nodes[0]
        assert node.entity_type == "Supplier"
        assert node.entity_id == "SUP-001"
        assert node.properties["vendor_name"] == "Test Corp"
        assert node.valid_from == datetime(2020, 1, 1)
        assert node.valid_to is None
        assert len(result.edges) == 0  # AP_SUPPLIERS has no edges

    def test_transform_po_header_with_edge(self, transformer):
        rows = [
            {
                "po_header_id": 1,
                "po_number": "PO-2024-0001",
                "vendor_id": "SUP-001",
                "status": "APPROVED",
                "type_lookup_code": "STANDARD",
                "authorization_status": "APPROVED",
                "total_amount": 50000.00,
                "currency": "CNY",
                "revision_num": 0,
                "buyer_id": 101,
                "closed_code": "OPEN",
                "comments": "Urgent order for Q2",
                "creation_date": date(2026, 3, 15),
                "last_update_date": datetime(2026, 3, 15, 10, 30),
            }
        ]
        result = transformer.transform("PO_HEADERS_ALL", rows)
        assert len(result.nodes) == 1
        assert result.nodes[0].entity_type == "PurchaseOrder"
        assert result.nodes[0].properties["po_number"] == "PO-2024-0001"

        # CREATES_PO edge
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.edge_type == "CREATES_PO"
        assert edge.source_type == "Supplier"
        assert edge.source_id == "SUP-001"
        assert edge.target_type == "PurchaseOrder"
        assert edge.target_id == "1"

        # free text
        assert len(result.free_text_records) == 1
        assert result.free_text_records[0].text == "Urgent order for Q2"

    def test_transform_empty_rows(self, transformer):
        result = transformer.transform("AP_SUPPLIERS", [])
        assert result.nodes == []
        assert result.edges == []

    def test_transform_unknown_table(self, transformer):
        result = transformer.transform("UNKNOWN_TABLE", [{"id": 1}])
        assert result.nodes == []

    def test_conditional_edge_skipped(self, transformer):
        rows = [
            {
                "po_line_id": 1,
                "po_header_id": 10,
                "line_num": 1,
                "item_id": None,  # no item → ORDERS_MATERIAL should be skipped
                "item_description": "Custom item",
                "quantity": 100,
                "unit_price": 10.0,
                "amount": 1000.0,
                "category_id": "CAT-01",
                "standard_price": 10.0,
                "unit_meas_lookup_code": "EA",
                "closed_code": None,
                "last_update_date": None,
            }
        ]
        result = transformer.transform("PO_LINES_ALL", rows)
        assert len(result.nodes) == 1
        # CONTAINS_LINE should exist, ORDERS_MATERIAL should be skipped
        edge_types = [e.edge_type for e in result.edges]
        assert "CONTAINS_LINE" in edge_types
        assert "ORDERS_MATERIAL" not in edge_types

    def test_type_coercion(self, transformer):
        rows = [
            {
                "po_header_id": 2,
                "po_number": "PO-2024-0002",
                "vendor_id": "SUP-002",
                "status": "APPROVED",
                "type_lookup_code": None,
                "authorization_status": None,
                "total_amount": "12345.67",  # string → should be coerced to float
                "currency": "USD",
                "revision_num": None,
                "buyer_id": None,
                "closed_code": None,
                "comments": None,
                "creation_date": "2026-01-01",  # string date
                "last_update_date": None,
            }
        ]
        result = transformer.transform("PO_HEADERS_ALL", rows)
        assert result.nodes[0].properties["total_amount"] == 12345.67
        assert result.nodes[0].valid_from == datetime(2026, 1, 1)


# ============================================================
# Extractors — import & structure
# ============================================================


class TestExtractors:
    def test_master_data_extractor_structure(self):
        from core.etl.extractors.master_data import MasterDataExtractor
        ext = MasterDataExtractor.__new__(MasterDataExtractor)
        assert ext.domain() == "master_data"
        assert len(ext.table_names()) == 3
        assert "AP_SUPPLIERS" in ext.table_names()

    def test_purchasing_extractor_structure(self):
        from core.etl.extractors.purchasing import PurchasingExtractor
        ext = PurchasingExtractor.__new__(PurchasingExtractor)
        assert ext.domain() == "purchasing"
        assert len(ext.table_names()) == 4

    def test_receiving_extractor_structure(self):
        from core.etl.extractors.receiving import ReceivingExtractor
        ext = ReceivingExtractor.__new__(ReceivingExtractor)
        assert ext.domain() == "receiving"
        assert len(ext.table_names()) == 3

    def test_payables_extractor_structure(self):
        from core.etl.extractors.payables import PayablesExtractor
        ext = PayablesExtractor.__new__(PayablesExtractor)
        assert ext.domain() == "payables"
        assert len(ext.table_names()) == 6

    def test_sourcing_extractor_structure(self):
        from core.etl.extractors.sourcing import SourcingExtractor
        ext = SourcingExtractor.__new__(SourcingExtractor)
        assert ext.domain() == "sourcing"
        assert len(ext.table_names()) == 4

    def test_all_extractors_cover_20_tables(self):
        from core.etl.extractors.master_data import MasterDataExtractor
        from core.etl.extractors.purchasing import PurchasingExtractor
        from core.etl.extractors.receiving import ReceivingExtractor
        from core.etl.extractors.payables import PayablesExtractor
        from core.etl.extractors.sourcing import SourcingExtractor

        all_tables: list[str] = []
        for cls in [MasterDataExtractor, PurchasingExtractor, ReceivingExtractor,
                     PayablesExtractor, SourcingExtractor]:
            ext = cls.__new__(cls)
            all_tables.extend(ext.table_names())
        assert len(all_tables) == 20
        assert len(set(all_tables)) == 20  # no duplicates

    def test_master_data_extract_from_db(self, db_session_factory):
        from core.etl.extractors.master_data import MasterDataExtractor
        ext = MasterDataExtractor(db_session_factory, batch_size=10)

        batches = []
        loop = asyncio.get_event_loop()

        async def collect():
            async for batch in ext.extract_full("AP_SUPPLIERS"):
                batches.append(batch)

        loop.run_until_complete(collect())
        assert len(batches) >= 1
        row = batches[0][0]
        assert "vendor_id" in row
        assert "vendor_name" in row

    def test_max_rows_per_table_caps_output(self, db_session_factory):
        """max_rows_per_table should cap the total rows yielded."""
        from core.etl.extractors.master_data import MasterDataExtractor

        # DB has 5 suppliers; cap at 3
        ext = MasterDataExtractor(db_session_factory, batch_size=100, max_rows_per_table=3)

        all_rows: list[dict] = []
        loop = asyncio.get_event_loop()

        async def collect():
            async for batch in ext.extract_full("AP_SUPPLIERS"):
                all_rows.extend(batch)

        loop.run_until_complete(collect())
        assert len(all_rows) == 3

    def test_max_rows_zero_means_unlimited(self, db_session_factory):
        """max_rows_per_table=0 should return all rows."""
        from core.etl.extractors.master_data import MasterDataExtractor

        ext = MasterDataExtractor(db_session_factory, batch_size=100, max_rows_per_table=0)

        all_rows: list[dict] = []
        loop = asyncio.get_event_loop()

        async def collect():
            async for batch in ext.extract_full("AP_SUPPLIERS"):
                all_rows.extend(batch)

        loop.run_until_complete(collect())
        assert len(all_rows) == 5  # all suppliers

    def test_config_default_is_10(self):
        """Default max_rows_per_table should be 10."""
        from core.etl.config import GraphitiETLSettings
        cfg = GraphitiETLSettings()
        assert cfg.max_rows_per_table == 10


# ============================================================
# GraphitiLoader — unit tests with mock client
# ============================================================


class TestGraphitiLoader:
    def test_node_to_episode(self):
        node = GraphitiNode(
            entity_type="Supplier",
            entity_id="SUP-001",
            properties={"vendor_name": "Test"},
            last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        episode = GraphitiLoader._node_to_episode(node, [])
        assert "Supplier" in episode.name
        assert "Test" in episode.body
        assert episode.source == "etl"

    def test_node_to_episode_excludes_edges(self):
        """Episode body should NOT contain edge info (edges are created via Cypher)."""
        node = GraphitiNode(
            entity_type="PurchaseOrder",
            entity_id="1",
            properties={"po_number": "PO-001"},
        )
        edges = [
            GraphitiEdge(
                edge_type="CREATES_PO",
                source_type="Supplier",
                source_id="SUP-001",
                target_type="PurchaseOrder",
                target_id="1",
            ),
        ]
        episode = GraphitiLoader._node_to_episode(node, edges)
        assert "PO-001" in episode.body
        # Edges are NOT embedded in episode body
        assert "CREATES_PO" not in episode.body

    def test_merge_nodes_to_episode_helper(self):
        """Test backward-compat episode helper (used for free text)."""
        nodes = [
            GraphitiNode(entity_type="Supplier", entity_id="SUP-001", properties={"name": "A"}),
            GraphitiNode(entity_type="Supplier", entity_id="SUP-002", properties={"name": "B"}),
        ]
        episode = GraphitiLoader._merge_nodes_to_episode(nodes, [])
        assert "batch(2)" in episode.name
        assert "SUP-001" in episode.body
        assert "SUP-002" in episode.body

    def test_load_writes_via_cypher(self):
        """load() should write nodes via batch Cypher UNWIND, not add_episode."""
        mock_client = MagicMock()
        mock_client.execute_cypher = AsyncMock()
        mock_client.add_episode = AsyncMock()
        loader = GraphitiLoader(mock_client, max_concurrency=3)

        nodes = [
            GraphitiNode(entity_type="Supplier", entity_id="1", properties={"name": "A"}),
            GraphitiNode(entity_type="Supplier", entity_id="2", properties={"name": "B"}),
        ]

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(loader.load(nodes, []))
        assert result.loaded == 2
        assert result.failed == 0
        # Batch UNWIND: one Cypher call for the entire batch
        assert mock_client.execute_cypher.await_count == 1
        cypher_arg = mock_client.execute_cypher.call_args[0][0]
        assert "UNWIND" in cypher_arg
        # add_episode should NOT be called for structured data
        mock_client.add_episode.assert_not_awaited()

    def test_load_handles_cypher_failure(self):
        mock_client = MagicMock()
        mock_client.execute_cypher = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        loader = GraphitiLoader(mock_client)

        nodes = [GraphitiNode(entity_type="X", entity_id="1", properties={})]

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(loader.load(nodes, []))
        assert result.loaded == 0
        assert result.failed == 1

    def test_load_free_text_uses_add_episode(self):
        """load_free_text() should call add_episode (LLM path)."""
        mock_client = MagicMock()
        mock_client.add_episode = AsyncMock()
        loader = GraphitiLoader(mock_client, episode_batch_size=50)

        records = [
            TextRecord(
                source_node_type="PurchaseOrder",
                source_node_id="1",
                field_name="comments",
                text="Urgent delivery needed",
            ),
        ]

        loop = asyncio.get_event_loop()
        loaded = loop.run_until_complete(loader.load_free_text(records))
        assert loaded == 1
        mock_client.add_episode.assert_awaited_once()


# ============================================================
# ETLPipeline orchestration
# ============================================================


def _make_extractor(domain: str, tables: list[str]) -> MagicMock:
    """Create a mock BaseExtractor."""
    ext = MagicMock()
    ext.domain.return_value = domain
    ext.table_names.return_value = tables

    async def _full(table_name: str):
        yield [{"id": 1, "last_update_date": datetime(2025, 1, 1, tzinfo=timezone.utc)}]

    ext.extract_full = _full
    ext.extract_incremental = _full
    return ext


def _make_pipeline(
    extractors: dict[str, MagicMock],
    *,
    llm_extractor: MagicMock | None = None,
    has_edges: bool = False,
    has_free_text: bool = False,
) -> tuple[ETLPipeline, MagicMock, MagicMock]:
    """Build a pipeline with mock loader/state/transformer."""
    transformer = MagicMock()
    edge_list = (
        [GraphitiEdge(edge_type="HAS", source_type="A", source_id="1",
                       target_type="B", target_id="2")]
        if has_edges else []
    )
    free_text = (
        [TextRecord(source_node_type="PO", source_node_id="1",
                    field_name="note", text="sample")]
        if has_free_text else []
    )
    transformer.transform.return_value = TransformResult(
        nodes=[GraphitiNode(entity_type="Test", entity_id="1", properties={"k": "v"})],
        edges=edge_list,
        free_text_records=free_text,
    )

    mock_client = MagicMock()
    mock_client.execute_cypher = AsyncMock()
    mock_client.clear_graph = AsyncMock()
    loader = GraphitiLoader(mock_client)
    loader.create_edges = AsyncMock(return_value=len(edge_list))
    loader.load = AsyncMock(return_value=LoadResult(loaded=1, failed=0))
    loader.load_free_text = AsyncMock(return_value=1)

    state = MagicMock()
    state.update_watermark = AsyncMock()
    state.get_watermark = AsyncMock(return_value=None)
    state.needs_full_sync = AsyncMock(return_value=True)

    pipeline = ETLPipeline(
        extractors=extractors,
        transformer=transformer,
        loader=loader,
        state_manager=state,
        llm_extractor=llm_extractor,
    )
    return pipeline, loader, state


class TestETLPipelineOrchestration:
    """Tests for pipeline-level orchestration (P0/P1/P2)."""

    def test_full_sync_master_data_first(self):
        """P1: master_data completes before other domains start."""
        call_order: list[str] = []

        md_ext = _make_extractor("master_data", ["AP_SUPPLIERS"])
        pur_ext = _make_extractor("purchasing", ["PO_HEADERS_ALL"])
        rec_ext = _make_extractor("receiving", ["RCV_SHIPMENT_HEADERS"])

        extractors = {
            "master_data": md_ext,
            "purchasing": pur_ext,
            "receiving": rec_ext,
        }
        pipeline, loader, state = _make_pipeline(extractors)

        orig_sync_domain = pipeline._sync_domain.__func__

        async def _tracking_sync(self_inner, extractor, **kwargs):
            domain = extractor.domain()
            call_order.append(f"start:{domain}")
            await orig_sync_domain(self_inner, extractor, **kwargs)
            call_order.append(f"end:{domain}")

        loop = asyncio.get_event_loop()
        with patch.object(type(pipeline), "_sync_domain", _tracking_sync):
            result = loop.run_until_complete(pipeline.run_full_sync())

        assert call_order.index("end:master_data") < call_order.index("start:purchasing")
        assert call_order.index("end:master_data") < call_order.index("start:receiving")
        assert not result.failed_tables

    def test_domain_edges_deferred_until_all_nodes_written(self):
        """P2: edges created only after all table nodes in the domain are done."""
        md_ext = _make_extractor("master_data", ["AP_SUPPLIERS", "HR_ALL_ORGANIZATION_UNITS"])
        pipeline, loader, state = _make_pipeline({"master_data": md_ext}, has_edges=True)

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(pipeline.run_full_sync())

        assert len(result.succeeded_tables) == 2
        assert not result.failed_tables
        loader.create_edges.assert_awaited()

    def test_llm_extraction_skipped_when_no_extractor(self):
        """P0: free_text_records don't trigger load_free_text when llm_extractor is None."""
        md_ext = _make_extractor("master_data", ["OKC_K_HEADERS_B"])
        pipeline, loader, state = _make_pipeline(
            {"master_data": md_ext}, llm_extractor=None, has_free_text=True,
        )

        loop = asyncio.get_event_loop()
        loop.run_until_complete(pipeline.run_full_sync())

        loader.load_free_text.assert_not_awaited()

    def test_llm_extraction_called_when_extractor_present(self):
        """P0: free_text_records trigger load_free_text when llm_extractor is set."""
        md_ext = _make_extractor("master_data", ["OKC_K_HEADERS_B"])
        mock_llm = MagicMock()
        pipeline, loader, state = _make_pipeline(
            {"master_data": md_ext}, llm_extractor=mock_llm, has_free_text=True,
        )

        loop = asyncio.get_event_loop()
        loop.run_until_complete(pipeline.run_full_sync())

        loader.load_free_text.assert_awaited()

    def test_table_failure_does_not_block_domain(self):
        """A failing table should appear in failed_tables, others succeed."""
        md_ext = _make_extractor("master_data", ["AP_SUPPLIERS", "BAD_TABLE"])

        async def _failing_full(table_name: str):
            if table_name == "BAD_TABLE":
                raise RuntimeError("DB error")
            yield [{"id": 1, "last_update_date": datetime(2025, 1, 1, tzinfo=timezone.utc)}]

        md_ext.extract_full = _failing_full
        pipeline, loader, state = _make_pipeline({"master_data": md_ext})

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(pipeline.run_full_sync())

        assert "BAD_TABLE" in result.failed_tables
        assert "AP_SUPPLIERS" in result.succeeded_tables

    def test_incremental_sync_all_domains_parallel(self):
        """Incremental sync runs all domains in parallel via asyncio.gather."""
        md_ext = _make_extractor("master_data", ["AP_SUPPLIERS"])
        pur_ext = _make_extractor("purchasing", ["PO_HEADERS_ALL"])
        extractors = {"master_data": md_ext, "purchasing": pur_ext}
        pipeline, loader, state = _make_pipeline(extractors)

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(pipeline.run_incremental_sync())

        assert result.total_nodes == 2
        assert len(result.succeeded_tables) == 2
        assert not result.failed_tables
