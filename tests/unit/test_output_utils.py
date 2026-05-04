"""Unit tests for modules/p2p/tools/_output.py — uncovered lines."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _get_tool_logger: lines 14-17
# ---------------------------------------------------------------------------


class TestGetToolLogger:
    def test_lazy_logger_init(self) -> None:
        """Lines 14-17: _get_tool_logger creates logger on first call."""
        import modules.p2p.tools._output as mod

        original = mod._tool_logger
        mod._tool_logger = None
        try:
            with patch("core.logging_utils.get_logger") as mock_get_logger:
                mock_logger = MagicMock()
                mock_get_logger.return_value = mock_logger
                result = mod._get_tool_logger()
                mock_get_logger.assert_called_once()
                assert result is mock_logger
        finally:
            mod._tool_logger = original

    def test_returns_cached_logger(self) -> None:
        """Lines 14-17: returns cached logger on subsequent calls."""
        import modules.p2p.tools._output as mod

        original = mod._tool_logger
        mock_logger = MagicMock()
        mod._tool_logger = mock_logger
        try:
            result = mod._get_tool_logger()
            assert result is mock_logger
        finally:
            mod._tool_logger = original


# ---------------------------------------------------------------------------
# _clip_and_dump: lines 73-82, 86
# ---------------------------------------------------------------------------


def _make_tool_output_cfg(
    max_items: int = 200, max_chars: int = 100, query_max_rows: int = 5000,
) -> MagicMock:
    """Create a mock P2PSettings with tool_output config."""
    mock_settings = MagicMock()
    mock_settings.tool_output.max_items = max_items
    mock_settings.tool_output.max_chars = max_chars
    mock_settings.tool_output.query_max_rows = query_max_rows
    return mock_settings


class TestClampQueryLimit:

    def test_limit_zero_returns_max_rows(self) -> None:
        from modules.p2p.tools._output import _clamp_query_limit
        mock = _make_tool_output_cfg(query_max_rows=5000)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock):
            assert _clamp_query_limit(0) == 5000

    def test_limit_within_max(self) -> None:
        from modules.p2p.tools._output import _clamp_query_limit
        mock = _make_tool_output_cfg(query_max_rows=5000)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock):
            assert _clamp_query_limit(100) == 100

    def test_limit_exceeds_max(self) -> None:
        from modules.p2p.tools._output import _clamp_query_limit
        mock = _make_tool_output_cfg(query_max_rows=5000)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock):
            assert _clamp_query_limit(10000) == 5000

    def test_max_rows_zero_disables_clamp(self) -> None:
        from modules.p2p.tools._output import _clamp_query_limit
        mock = _make_tool_output_cfg(query_max_rows=0)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock):
            assert _clamp_query_limit(0) == 0
            assert _clamp_query_limit(99999) == 99999


class TestClipAndDump:
    """Cover lines 73-82 (dict with list values exceeding max_chars)
    and line 86 (hard-cut fallback)."""

    def test_list_items_truncated(self) -> None:
        """Lines 40-51: list with more items than max_items gets truncated."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=2, max_chars=0)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
            result = _clip_and_dump(data)

        parsed = json.loads(result)
        # 2 items + 1 truncation marker = 3 items
        assert len(parsed) == 3
        assert parsed[-1]["_truncated"] is True
        assert parsed[-1]["dropped"] == 2

    def test_dict_with_list_truncated(self) -> None:
        """Lines 56-60: dict with list values gets list items truncated."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=1, max_chars=0)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = {"items": [{"id": 1}, {"id": 2}, {"id": 3}], "total": 3}
            result = _clip_and_dump(data)

        parsed = json.loads(result)
        assert len(parsed["items"]) == 2  # 1 item + 1 truncation marker
        assert parsed["items"][-1]["_truncated"] is True
        assert parsed["total"] == 3

    def test_char_limit_list_shrink(self) -> None:
        """Lines 69-72: list shrinks when exceeding max_chars."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=0, max_chars=50)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = [{"data": "a" * 30}, {"data": "b" * 30}]
            result = _clip_and_dump(data)

        # Should be within budget or have hard-cut marker
        assert len(result) <= 50 or '"_budget_truncated"' in result

    def test_char_limit_dict_shrink(self) -> None:
        """Lines 73-82: dict with list values shrinks lists to fit max_chars."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=0, max_chars=80)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = {
                "results": [
                    {"data": "a" * 30},
                    {"data": "b" * 30},
                    {"data": "c" * 30},
                ],
            }
            result = _clip_and_dump(data)

        assert len(result) <= 80 or '"_budget_truncated"' in result

    def test_hard_cut_fallback(self) -> None:
        """Line 86: when no list can be shrunk further, hard-cut."""
        from modules.p2p.tools._output import _clip_and_dump

        # max_chars must be > 50 to allow room for the truncation suffix
        # (the code does text[:max_chars - 50] + '..."_budget_truncated":true}')
        mock_settings = _make_tool_output_cfg(max_items=0, max_chars=80)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            # A dict without lists that exceeds max_chars
            data = {"key": "v" * 200}
            result = _clip_and_dump(data)

        assert '"_budget_truncated":true}' in result
        assert len(result) <= 80

    def test_no_truncation_needed(self) -> None:
        """Normal case: data fits within budget."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=100, max_chars=10000)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = {"key": "value"}
            result = _clip_and_dump(data)

        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_max_chars_zero_no_char_truncation(self) -> None:
        """When max_chars=0, no char-level truncation."""
        from modules.p2p.tools._output import _clip_and_dump

        mock_settings = _make_tool_output_cfg(max_items=0, max_chars=0)
        with patch("modules.p2p.settings.get_p2p_settings", return_value=mock_settings):
            data = {"key": "v" * 10000}
            result = _clip_and_dump(data)

        parsed = json.loads(result)
        assert parsed["key"] == "v" * 10000
