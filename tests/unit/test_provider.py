"""Unit tests for core/orchestrator/provider.py and modules/p2p/provider.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.orchestrator.provider import ModuleProvider
from modules.p2p.provider import P2PModuleProvider


# ---------------------------------------------------------------------------
# ModuleProvider Protocol — 0% coverage → just import and isinstance check
# ---------------------------------------------------------------------------


class TestModuleProviderProtocol:
    """Cover core/orchestrator/provider.py by importing and using it."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """The Protocol is runtime_checkable — isinstance should work."""
        assert isinstance(P2PModuleProvider(), ModuleProvider)

    def test_protocol_has_expected_methods(self) -> None:
        """Protocol defines expected method names."""
        expected_methods = {
            "get_agent",
            "get_report_agent",
            "get_repository",
            "get_entity_types",
            "get_tools",
            "get_dag_templates",
            "get_reference_patterns",
            "build_memory_content",
            "build_memory_metadata",
            "trim_to_token_budget",
        }
        for method_name in expected_methods:
            assert hasattr(ModuleProvider, method_name)


# ---------------------------------------------------------------------------
# P2PModuleProvider — lines 35-37, 40, 56-58, 64-66, 102-104
# ---------------------------------------------------------------------------


class TestP2PModuleProvider:
    """Cover P2PModuleProvider methods that do lazy imports."""

    def test_get_repository(self) -> None:
        """Lines 35-37: get_repository does a lazy import and returns a repo."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.tools._get_repository") as mock_get_repo:
            mock_get_repo.return_value = MagicMock()
            result = provider.get_repository()
            mock_get_repo.assert_called_once()
            assert result is not None

    def test_get_entity_types(self) -> None:
        """Line 40: returns a list of entity type strings."""
        provider = P2PModuleProvider()
        entity_types = provider.get_entity_types()
        assert isinstance(entity_types, list)
        assert "po_number" in entity_types
        assert "vendor_id" in entity_types
        assert "invoice_num" in entity_types

    def test_get_dag_templates(self) -> None:
        """Lines 56-58: lazy import of dag_templates module."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.dag_templates.get_template_map") as mock_tm:
            with patch("modules.p2p.dag_templates.get_entity_template_map") as mock_etm:
                mock_tm.return_value = {"type1": []}
                mock_etm.return_value = {"entity1": []}
                result = provider.get_dag_templates()
                assert "type_map" in result
                assert "entity_map" in result

    def test_get_reference_patterns(self) -> None:
        """Lines 64-66: lazy import of _REF_PATTERNS."""
        provider = P2PModuleProvider()
        with patch("core.orchestrator.entity._REF_PATTERNS", [("regex", "key", "tpl")]):
            result = provider.get_reference_patterns()
            assert isinstance(result, list)

    def test_trim_to_token_budget(self) -> None:
        """Lines 102-104: lazy import of trim_to_token_budget."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.prompts.trim_to_token_budget") as mock_trim:
            mock_trim.return_value = "trimmed"
            result = provider.trim_to_token_budget("long text", 100, "label")
            mock_trim.assert_called_once_with("long text", 100, "label")
            assert result == "trimmed"

    def test_get_agent(self) -> None:
        """Lines 15-27: get_agent lazy imports P2PAgent."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.agent.P2PAgent") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = provider.get_agent(
                settings=MagicMock(),
                timing_middleware=MagicMock(),
                checkpointer=None,
            )
            mock_cls.assert_called_once()
            assert result is not None

    def test_get_report_agent(self) -> None:
        """Lines 29-31: get_report_agent lazy imports ReportAgent."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.report_agent.ReportAgent") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = provider.get_report_agent(settings=MagicMock())
            mock_cls.assert_called_once()
            assert result is not None

    def test_get_tools(self) -> None:
        """Lines 48-53: get_tools does lazy import and returns tool list."""
        provider = P2PModuleProvider()
        with patch("config.settings.get_settings") as mock_settings:
            mock_settings.return_value.graphiti_etl.query_backend = "postgresql"
            with patch("modules.p2p.tools.get_tools_for_mode") as mock_tools:
                mock_tools.return_value = [MagicMock()]
                result = provider.get_tools()
                assert isinstance(result, list)

    def test_build_memory_content(self) -> None:
        """Lines 68-76: build_memory_content lazy import."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.agent._build_memory_content") as mock_fn:
            mock_fn.return_value = "memory content"
            result = provider.build_memory_content(
                query="test query",
                response="test response",
                summary={"key": "value"},
            )
            mock_fn.assert_called_once()
            assert result == "memory content"

    def test_build_memory_metadata(self) -> None:
        """Lines 78-94: build_memory_metadata lazy import."""
        provider = P2PModuleProvider()
        with patch("modules.p2p.agent._build_memory_metadata") as mock_fn:
            mock_fn.return_value = {"type": "test"}
            result = provider.build_memory_metadata(
                query="test query",
                analysis_type="three_way_match",
                anomalies=[],
                summary={},
                time_range_days=30,
            )
            mock_fn.assert_called_once()
            assert result == {"type": "test"}
