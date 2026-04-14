"""P2P 模块自定义异常。

用于把底层 HTTP / LLM 客户端异常映射为业务级错误码，
便于编排层 / API 层决定是否向用户显性报错。
"""

from __future__ import annotations


class ReportGenerationError(Exception):
    """报告生成失败。

    - ``code`` 是稳定的业务错误码，前端 / 日志可据此分类
    - ``message`` 是人类可读原因（中文）
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def classify_llm_exception(exc: BaseException) -> str:
    """把 httpx / openai 抛出的异常映射为业务错误码。

    Returns:
        - ``LLM_CONNECTION_ERROR``: TCP / DNS / SSL 连接阶段失败
        - ``LLM_TIMEOUT``: 读写超时
        - ``LLM_API_ERROR``: API 返回 4xx/5xx 等业务错误
        - ``REPORT_GEN_FAILED``: 其他未知失败
    """
    import httpx

    # openai SDK 的封装层，含 APIConnectionError / APITimeoutError
    try:
        import openai  # type: ignore
    except Exception:  # noqa: BLE001
        openai = None  # type: ignore[assignment]

    cls_name = type(exc).__name__
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "LLM_CONNECTION_ERROR"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "LLM_TIMEOUT"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "LLM_CONNECTION_ERROR"
    if openai is not None:
        if isinstance(exc, getattr(openai, "APIConnectionError", ())):
            return "LLM_CONNECTION_ERROR"
        if isinstance(exc, getattr(openai, "APITimeoutError", ())):
            return "LLM_TIMEOUT"
        if isinstance(exc, getattr(openai, "APIStatusError", ())):
            return "LLM_API_ERROR"
    # 名称兜底（某些环境下 httpx 版本差异）
    if "Timeout" in cls_name:
        return "LLM_TIMEOUT"
    if "Connect" in cls_name or "Connection" in cls_name:
        return "LLM_CONNECTION_ERROR"
    return "REPORT_GEN_FAILED"


TRANSIENT_ERROR_CODES = frozenset({"LLM_CONNECTION_ERROR", "LLM_TIMEOUT"})
"""可重试的错误码集合（瞬时错误）。"""
