"""modules.p2p.errors 单元测试。"""

from __future__ import annotations

import httpx
import pytest

from modules.p2p.errors import (
    TRANSIENT_ERROR_CODES,
    ReportGenerationError,
    classify_llm_exception,
)


def test_report_generation_error_carries_code_and_message() -> None:
    err = ReportGenerationError("LLM_CONNECTION_ERROR", "conn reset")
    assert err.code == "LLM_CONNECTION_ERROR"
    assert err.message == "conn reset"
    assert "LLM_CONNECTION_ERROR" in str(err)
    assert "conn reset" in str(err)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("dns fail"), "LLM_CONNECTION_ERROR"),
        (httpx.ConnectTimeout("handshake"), "LLM_CONNECTION_ERROR"),
        (httpx.ReadError("reset"), "LLM_CONNECTION_ERROR"),
        (httpx.WriteError("w"), "LLM_CONNECTION_ERROR"),
        (httpx.RemoteProtocolError("rp"), "LLM_CONNECTION_ERROR"),
        (httpx.ReadTimeout("rt"), "LLM_TIMEOUT"),
        (httpx.WriteTimeout("wt"), "LLM_TIMEOUT"),
        (httpx.PoolTimeout("pt"), "LLM_TIMEOUT"),
        (RuntimeError("biz"), "REPORT_GEN_FAILED"),
        (ValueError("bad input"), "REPORT_GEN_FAILED"),
    ],
)
def test_classify_llm_exception(exc: BaseException, expected: str) -> None:
    assert classify_llm_exception(exc) == expected


def test_classify_by_class_name_fallback() -> None:
    class CustomConnectionError(Exception):
        pass

    class CustomTimeoutError(Exception):
        pass

    assert classify_llm_exception(CustomConnectionError("x")) == "LLM_CONNECTION_ERROR"
    assert classify_llm_exception(CustomTimeoutError("x")) == "LLM_TIMEOUT"


def test_transient_codes_contains_connection_and_timeout() -> None:
    assert "LLM_CONNECTION_ERROR" in TRANSIENT_ERROR_CODES
    assert "LLM_TIMEOUT" in TRANSIENT_ERROR_CODES
    assert "REPORT_GEN_FAILED" not in TRANSIENT_ERROR_CODES
    assert "LLM_API_ERROR" not in TRANSIENT_ERROR_CODES


def test_openai_api_connection_error_classified() -> None:
    try:
        import openai
    except Exception:  # pragma: no cover
        pytest.skip("openai 未安装")

    if hasattr(openai, "APIConnectionError"):
        exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
        assert classify_llm_exception(exc) == "LLM_CONNECTION_ERROR"
