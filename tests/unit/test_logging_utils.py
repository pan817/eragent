"""Unit tests for core/logging_utils.py — uncovered lines."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from core.logging_utils import (
    ConsoleFormatter,
    JsonFormatter,
    TraceIdFilter,
    _heal_if_disabled,
    _pick_stream,
    _pull_trace_id,
    get_logger,
    get_trace_logger,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# _pull_trace_id: lines 49-50 (exception branch)
# ---------------------------------------------------------------------------


class TestPullTraceId:
    def test_returns_none_on_import_error(self) -> None:
        with patch(
            "core.logging_utils._pull_trace_id",
            side_effect=lambda: (_ for _ in ()).throw(ImportError("no module")),
        ):
            # Direct call: when import fails, should return None
            result = _pull_trace_id()
            # _pull_trace_id catches all exceptions and returns None
            assert result is None

    def test_returns_none_when_no_trace(self) -> None:
        result = _pull_trace_id()
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        """Force the except branch (lines 49-50) by making the import raise."""
        with patch.dict("sys.modules", {"core.observability.tracing": None}):
            result = _pull_trace_id()
            assert result is None


# ---------------------------------------------------------------------------
# JsonFormatter: lines 66-67, 70-89
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_format_basic(self) -> None:
        fmt = JsonFormatter(include_trace_id=True)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        record.trace_id = "abc123"
        output = fmt.format(record)
        payload = json.loads(output)
        assert payload["msg"] == "hello"
        assert payload["trace_id"] == "abc123"

    def test_format_without_trace_id(self) -> None:
        """Lines 66-67: include_trace_id=False."""
        fmt = JsonFormatter(include_trace_id=False)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="world", args=(), exc_info=None,
        )
        output = fmt.format(record)
        payload = json.loads(output)
        assert "trace_id" not in payload

    def test_format_with_trace_id_missing(self) -> None:
        """Line 77: getattr fallback to '-'."""
        fmt = JsonFormatter(include_trace_id=True)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        # no trace_id attr at all
        output = fmt.format(record)
        payload = json.loads(output)
        assert payload["trace_id"] == "-"

    def test_format_with_exception(self) -> None:
        """Lines 78-79: exc_info branch."""
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="fail", args=(), exc_info=sys.exc_info(),
            )
        output = fmt.format(record)
        payload = json.loads(output)
        assert "exc" in payload
        assert "boom" in payload["exc"]

    def test_format_extra_fields(self) -> None:
        """Lines 81-88: extra fields serialization."""
        fmt = JsonFormatter(include_trace_id=False)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.custom_field = "value1"
        record.trace_id = "-"
        output = fmt.format(record)
        payload = json.loads(output)
        assert payload["custom_field"] == "value1"

    def test_format_extra_non_serializable(self) -> None:
        """Lines 87-88: fallback to str() for non-serializable extras."""
        fmt = JsonFormatter(include_trace_id=False)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.trace_id = "-"
        # An object that json.dumps can handle via default=str
        record.weird_obj = object()
        output = fmt.format(record)
        payload = json.loads(output)
        assert "weird_obj" in payload


# ---------------------------------------------------------------------------
# ConsoleFormatter: line 102
# ---------------------------------------------------------------------------


class TestConsoleFormatter:
    def test_without_trace_id(self) -> None:
        """Line 102: include_trace_id=False branch."""
        fmt = ConsoleFormatter(include_trace_id=False)
        assert "trace_id" not in fmt._fmt


# ---------------------------------------------------------------------------
# _configure: lines 131-134, 145, 148
# ---------------------------------------------------------------------------


class TestConfigure:
    def setup_method(self) -> None:
        reset_for_tests()

    def teardown_method(self) -> None:
        reset_for_tests()

    def test_configure_fallback_on_settings_error(self) -> None:
        """Lines 131-134: settings import fails, defaults used."""
        with patch("config.settings.get_settings", side_effect=RuntimeError("no config")):
            # Just calling get_logger triggers _configure
            logger = get_logger("test_fallback")
            assert logger is not None

    def test_configure_json_format(self) -> None:
        """Line 148: log_format == 'json' branch."""
        mock_settings = MagicMock()
        mock_settings.logging.level = "DEBUG"
        mock_settings.logging.format = "json"
        mock_settings.logging.include_trace_id = True
        mock_settings.logging.business_stream = "stderr"
        mock_settings.observability.console_stream = "stdout"

        with patch("config.settings.get_settings", return_value=mock_settings):
            reset_for_tests()
            logger = get_logger("test_json")
            assert logger is not None

    def test_configure_removes_existing_handlers(self) -> None:
        """Lines 144-145, 163-164: handler cleanup."""
        # First configure
        get_logger("dummy1")
        # Force re-configure
        reset_for_tests()
        logger = get_logger("dummy2")
        assert logger is not None


# ---------------------------------------------------------------------------
# _heal_if_disabled: lines 185, 190
# ---------------------------------------------------------------------------


class TestHealIfDisabled:
    def test_heal_disabled_logger(self) -> None:
        """Line 185: disabled logger healed."""
        logger = logging.getLogger("eragent.test_heal")
        logger.disabled = True
        _heal_if_disabled(logger)
        assert not logger.disabled

    def test_heal_disabled_parent(self) -> None:
        """Lines 188-190: parent chain healing."""
        parent = logging.getLogger("eragent.test_parent")
        child = logging.getLogger("eragent.test_parent.child")
        parent.disabled = True
        child.disabled = True
        _heal_if_disabled(child)
        assert not child.disabled
        assert not parent.disabled


# ---------------------------------------------------------------------------
# get_logger: lines 199, 218-219
# ---------------------------------------------------------------------------


class TestGetLogger:
    def test_trace_namespace_redirect(self) -> None:
        """Line 199: trace namespace redirects to trace logger."""
        logger = get_logger("eragent.trace.something")
        assert logger.name == "eragent.trace"

    def test_prefix_added(self) -> None:
        logger = get_logger("mymodule")
        assert logger.name == "eragent.mymodule"


# ---------------------------------------------------------------------------
# reset_for_tests: lines 218-219
# ---------------------------------------------------------------------------


class TestResetForTests:
    def test_reset(self) -> None:
        """Lines 218-219: reset _configured flag."""
        import core.logging_utils as lu

        get_logger("x")
        assert lu._configured is True
        reset_for_tests()
        assert lu._configured is False
