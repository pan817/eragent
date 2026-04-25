"""Tests for multi-schema abstractions: SchemaRegistry, GraphSchema, P2PRepositoryProtocol."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.p2p.schemas import SchemaRegistry, SchemaRegistration
from modules.p2p.schemas.protocol import GraphSchema, P2PRepositoryProtocol


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Save and restore SchemaRegistry state around each test."""
    saved = dict(SchemaRegistry._schemas)
    yield
    SchemaRegistry._schemas = saved


def _make_graph_schema(**overrides: Any) -> GraphSchema:
    defaults: dict[str, Any] = {
        "node_types": {"purchase_order": "PO", "supplier": "Vendor"},
        "edge_types": {"creates_po": "CREATES", "contains_line": "HAS_LINE"},
        "business_id_fields": {"PO": ["po_number"], "Vendor": ["vendor_id"]},
    }
    defaults.update(overrides)
    return GraphSchema(**defaults)


def _make_registration(name: str = "test_schema") -> SchemaRegistration:
    return SchemaRegistration(
        name=name,
        repository_factory=MagicMock(return_value=MagicMock()),
        graph_schema=_make_graph_schema(),
    )


# ===========================================================================
# SchemaRegistry
# ===========================================================================

class TestSchemaRegistryRegisterAndGet:
    def test_register_and_get(self):
        reg = _make_registration("alpha")
        SchemaRegistry.register(reg)
        assert SchemaRegistry.get("alpha") is reg

    def test_get_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown ERP schema 'nope'"):
            SchemaRegistry.get("nope")

    def test_get_unknown_shows_available(self):
        SchemaRegistry._clear()
        SchemaRegistry.register(_make_registration("a"))
        SchemaRegistry.register(_make_registration("b"))
        with pytest.raises(ValueError, match="Available: a, b"):
            SchemaRegistry.get("missing")

    def test_get_unknown_empty_registry(self):
        SchemaRegistry._clear()
        with pytest.raises(ValueError, match="(empty)"):
            SchemaRegistry.get("x")


class TestSchemaRegistryAvailable:
    def test_available_empty_after_clear(self):
        SchemaRegistry._clear()
        assert SchemaRegistry.available() == []

    def test_available_lists_registered(self):
        SchemaRegistry._clear()
        SchemaRegistry.register(_make_registration("x"))
        SchemaRegistry.register(_make_registration("y"))
        assert sorted(SchemaRegistry.available()) == ["x", "y"]


class TestSchemaRegistryOverwrite:
    def test_re_register_overwrites(self):
        reg1 = _make_registration("dup")
        reg2 = _make_registration("dup")
        SchemaRegistry.register(reg1)
        SchemaRegistry.register(reg2)
        assert SchemaRegistry.get("dup") is reg2


class TestSchemaRegistryClear:
    def test_clear_removes_all(self):
        SchemaRegistry.register(_make_registration("z"))
        SchemaRegistry._clear()
        assert SchemaRegistry.available() == []


class TestSchemaRegistryOracleEBS:
    """Verify oracle_ebs is auto-registered by import."""

    def test_oracle_ebs_registered(self):
        reg = SchemaRegistry.get("oracle_ebs")
        assert reg.name == "oracle_ebs"
        assert reg.graph_schema is not None
        assert callable(reg.repository_factory)

    def test_oracle_ebs_in_available(self):
        assert "oracle_ebs" in SchemaRegistry.available()


# ===========================================================================
# GraphSchema
# ===========================================================================

class TestGraphSchemaNode:
    def test_node_returns_type_name(self):
        gs = _make_graph_schema()
        assert gs.node("purchase_order") == "PO"
        assert gs.node("supplier") == "Vendor"

    def test_node_missing_raises_key_error(self):
        gs = _make_graph_schema()
        with pytest.raises(KeyError):
            gs.node("nonexistent")


class TestGraphSchemaEdge:
    def test_edge_returns_type_name(self):
        gs = _make_graph_schema()
        assert gs.edge("creates_po") == "CREATES"
        assert gs.edge("contains_line") == "HAS_LINE"

    def test_edge_missing_raises_key_error(self):
        gs = _make_graph_schema()
        with pytest.raises(KeyError):
            gs.edge("nonexistent")


class TestGraphSchemaBizIdFields:
    def test_returns_field_list(self):
        gs = _make_graph_schema()
        assert gs.biz_id_fields("PO") == ["po_number"]
        assert gs.biz_id_fields("Vendor") == ["vendor_id"]

    def test_missing_returns_empty_list(self):
        gs = _make_graph_schema()
        assert gs.biz_id_fields("Unknown") == []


class TestGraphSchemaFrozen:
    def test_frozen_immutable(self):
        gs = _make_graph_schema()
        with pytest.raises(AttributeError):
            gs.node_types = {}  # type: ignore[misc]


class TestGraphSchemaEmpty:
    def test_empty_defaults(self):
        gs = GraphSchema()
        assert gs.node_types == {}
        assert gs.edge_types == {}
        assert gs.business_id_fields == {}
        assert gs.biz_id_fields("anything") == []


class TestOracleEBSGraphSchema:
    """Verify the production oracle_ebs GraphSchema has all expected mappings."""

    @pytest.fixture()
    def gs(self):
        from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
        return ORACLE_EBS_GRAPH_SCHEMA

    EXPECTED_NODES = [
        "purchase_order", "supplier", "invoice", "payment", "receipt",
        "material", "po_line", "contract", "auction", "supplier_site",
        "contract_line",
    ]
    EXPECTED_EDGES = [
        "creates_po", "contains_line", "receives_line", "invoices_line",
        "belongs_to_invoice", "pays_invoice", "submits_invoice", "has_site",
        "bids_on", "has_bid", "orders_material", "contract_covers",
        "contains_contract_line",
    ]

    def test_all_node_concepts_present(self, gs):
        for concept in self.EXPECTED_NODES:
            assert concept in gs.node_types, f"missing node concept: {concept}"

    def test_all_edge_concepts_present(self, gs):
        for rel in self.EXPECTED_EDGES:
            assert rel in gs.edge_types, f"missing edge concept: {rel}"

    def test_node_count(self, gs):
        assert len(gs.node_types) == 11

    def test_edge_count(self, gs):
        assert len(gs.edge_types) == 13

    def test_business_id_fields_cover_nodes(self, gs):
        for node_type in gs.node_types.values():
            if node_type == "ContractLine":
                continue
            assert gs.biz_id_fields(node_type), (
                f"node type {node_type} has no business_id_fields entry"
            )


class TestNewERPSchemaRegistration:
    """Verify new_erp is auto-registered by import."""

    def test_new_erp_registered(self):
        reg = SchemaRegistry.get("new_erp")
        assert reg.name == "new_erp"
        assert reg.graph_schema is not None
        assert callable(reg.repository_factory)

    def test_new_erp_in_available(self):
        assert "new_erp" in SchemaRegistry.available()

    def test_both_schemas_coexist(self):
        avail = SchemaRegistry.available()
        assert "oracle_ebs" in avail
        assert "new_erp" in avail


class TestNewERPGraphSchema:
    """Verify the new_erp GraphSchema has all expected mappings."""

    @pytest.fixture()
    def gs(self):
        from modules.p2p.schemas.new_erp.graph_schema import NEW_ERP_GRAPH_SCHEMA
        return NEW_ERP_GRAPH_SCHEMA

    EXPECTED_NODES = [
        "purchase_order", "supplier", "invoice", "payment", "receipt",
        "material", "po_line", "contract", "auction", "supplier_site",
        "contract_line",
    ]
    EXPECTED_EDGES = [
        "creates_po", "contains_line", "receives_line", "invoices_line",
        "belongs_to_invoice", "pays_invoice", "submits_invoice", "has_site",
        "bids_on", "has_bid", "orders_material", "contract_covers",
        "contains_contract_line",
    ]

    def test_all_node_concepts_present(self, gs):
        for concept in self.EXPECTED_NODES:
            assert concept in gs.node_types, f"missing node concept: {concept}"

    def test_all_edge_concepts_present(self, gs):
        for rel in self.EXPECTED_EDGES:
            assert rel in gs.edge_types, f"missing edge concept: {rel}"

    def test_node_count(self, gs):
        assert len(gs.node_types) == 11

    def test_edge_count(self, gs):
        assert len(gs.edge_types) == 13

    def test_node_types_differ_from_ebs(self, gs):
        from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
        assert gs.node("purchase_order") != ORACLE_EBS_GRAPH_SCHEMA.node("purchase_order")

    def test_edge_types_differ_from_ebs(self, gs):
        from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
        assert gs.edge("creates_po") != ORACLE_EBS_GRAPH_SCHEMA.edge("creates_po")

    def test_business_id_fields_cover_nodes(self, gs):
        for node_type in gs.node_types.values():
            if node_type == "NewContractLine":
                continue
            assert gs.biz_id_fields(node_type), (
                f"node type {node_type} has no business_id_fields entry"
            )


class TestNewERPRepositoryProtocol:
    def test_new_erp_repo_satisfies_protocol(self):
        from modules.p2p.schemas.new_erp.repository import NewERPRepository
        assert issubclass(NewERPRepository, P2PRepositoryProtocol)


# ===========================================================================
# P2PRepositoryProtocol
# ===========================================================================

class TestP2PRepositoryProtocol:
    def test_oracle_ebs_repo_satisfies_protocol(self):
        from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository
        assert issubclass(OracleEBSRepository, P2PRepositoryProtocol)

    def test_runtime_checkable(self):
        mock = MagicMock(spec=[
            "query_purchase_orders",
            "query_receipts",
            "query_invoices",
            "query_payments",
            "query_suppliers",
            "get_contract_prices",
            "get_flattened_purchase_orders",
            "get_flattened_receipts",
            "get_flattened_invoices",
            "get_flattened_payments",
            "invalidate_contract_price_cache",
            "analyze_receipt_anomalies",
            "detect_duplicate_invoices",
            "analyze_discount_utilization",
            "analyze_vendor_concentration",
            "calculate_po_cycle_time",
        ])
        assert isinstance(mock, P2PRepositoryProtocol)


# ===========================================================================
# SchemaRegistration
# ===========================================================================

class TestSchemaRegistration:
    def test_repository_factory_called_with_session_factory(self):
        factory = MagicMock(return_value=MagicMock())
        reg = SchemaRegistration(
            name="test",
            repository_factory=factory,
            graph_schema=_make_graph_schema(),
        )
        fake_session_factory = MagicMock()
        repo = reg.repository_factory(fake_session_factory)
        factory.assert_called_once_with(fake_session_factory)
        assert repo is factory.return_value

    def test_graph_backend_factory_optional(self):
        reg = SchemaRegistration(
            name="test",
            repository_factory=MagicMock(),
            graph_schema=_make_graph_schema(),
        )
        assert reg.graph_backend_factory is None

    def test_graph_backend_factory_provided(self):
        backend_factory = MagicMock()
        reg = SchemaRegistration(
            name="test",
            repository_factory=MagicMock(),
            graph_schema=_make_graph_schema(),
            graph_backend_factory=backend_factory,
        )
        assert reg.graph_backend_factory is backend_factory
