"""MemoryMiddleware：ReAct 循环内的 LLM 输入裁剪。

在每次 LLM 调用前，通过 ``wrap_model_call`` 截断早期 ToolMessage 的内容，
降低实际发送给模型的 token 数量，同时 **不修改** graph state——checkpoint
中保留完整的原始消息，确保审计可追溯。

裁剪策略：
- 最近 K 轮对话（keep_recent_rounds）的所有消息保持完整。
- 更早的 ToolMessage 内容截断到 max_chars 字符。
- 其他消息类型（Human / AI / System）不截断。

设计要点：
- 注册顺序：MemoryMiddleware 在 TimingMiddleware **之前**（外层），
  这样 TimingMiddleware 记录的 estimated_input_tokens 为裁剪后的真实值。
- 记录 ``memory_trim`` span，形成 "context_budget → memory_trim → model"
  完整的 token 消耗追溯链。
"""

from __future__ import annotations

import logging
from copy import copy
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# 截断后缀，提示 LLM 完整数据已保留在系统中
_TRUNCATION_SUFFIX = "…(已截断，完整数据保留在系统记录中)"


class MemoryMiddleware(AgentMiddleware):  # type: ignore[type-arg]
    """ReAct 循环内 LLM 输入裁剪中间件。

    通过 ``wrap_model_call`` 拦截 ModelRequest，对早期 ToolMessage
    内容做截断后传给 LLM，原始 state / checkpoint 不受影响。

    Args:
        enabled: 裁剪总开关，关闭后直接透传。
        keep_recent_rounds: 保留最近几轮完整对话（1 轮 = Human → AI → Tool(s) → AI）。
        tool_content_max_chars: 早期 ToolMessage 截断字符数。0 表示完全清空内容。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        keep_recent_rounds: int = 2,
        tool_content_max_chars: int = 500,
    ) -> None:
        self._enabled = enabled
        self._keep_recent_rounds = max(1, keep_recent_rounds)
        self._tool_content_max_chars = max(0, tool_content_max_chars)

    @property
    def name(self) -> str:  # noqa: D401
        return "MemoryMiddleware"

    # ------------------------------------------------------------------
    # wrap_model_call（同步 + 异步）
    # ------------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        if not self._enabled:
            return handler(request)

        trimmed_request, trim_info = self._trim_request(request)
        self._record_trim_span(trim_info)
        return handler(trimmed_request)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        if not self._enabled:
            return await handler(request)

        trimmed_request, trim_info = self._trim_request(request)
        self._record_trim_span(trim_info)
        return await handler(trimmed_request)

    # ------------------------------------------------------------------
    # 核心裁剪逻辑
    # ------------------------------------------------------------------

    def _trim_request(
        self, request: ModelRequest,
    ) -> tuple[ModelRequest, dict[str, Any]]:
        """裁剪 request.messages 中的早期 ToolMessage 内容。

        返回 (裁剪后的新 ModelRequest, trim_info dict)。
        原始 request 不被修改。
        """
        messages = request.messages
        if not messages:
            return request, self._no_trim_info()

        # ── 识别"轮次"边界：从尾部往前找 HumanMessage 作为轮次起点 ──
        boundary_idx = self._find_keep_boundary(messages)

        # ── 遍历消息，截断 boundary 之前的 ToolMessage 内容 ──
        original_chars = 0
        trimmed_chars = 0
        truncated_count = 0
        new_messages = []

        for idx, msg in enumerate(messages):
            content = getattr(msg, "content", "") or ""
            content_len = len(content) if isinstance(content, str) else len(str(content))
            original_chars += content_len

            if (
                idx < boundary_idx
                and isinstance(msg, ToolMessage)
                and isinstance(content, str)
                and content_len > self._tool_content_max_chars
            ):
                # 截断：创建副本，修改 content
                truncated_content = self._truncate_content(content)
                new_msg = copy(msg)
                new_msg.content = truncated_content
                new_messages.append(new_msg)
                trimmed_chars += len(truncated_content)
                truncated_count += 1
            else:
                new_messages.append(msg)
                trimmed_chars += content_len

        if truncated_count == 0:
            return request, self._no_trim_info()

        from core.observability.middleware import estimate_tokens

        trim_info: dict[str, Any] = {
            "trimmed": True,
            "original_message_count": len(messages),
            "trimmed_message_count": len(new_messages),
            "original_total_chars": original_chars,
            "trimmed_total_chars": trimmed_chars,
            "original_estimated_tokens": estimate_tokens("x" * original_chars),
            "trimmed_estimated_tokens": estimate_tokens("x" * trimmed_chars),
            "truncated_tool_messages": truncated_count,
            "keep_recent_rounds": self._keep_recent_rounds,
            "tool_content_max_chars": self._tool_content_max_chars,
        }

        logger.info(
            "memory_trim: truncated %d ToolMessages, "
            "chars %d → %d, est_tokens %d → %d",
            truncated_count,
            original_chars,
            trimmed_chars,
            trim_info["original_estimated_tokens"],
            trim_info["trimmed_estimated_tokens"],
        )

        return request.override(messages=new_messages), trim_info

    def _find_keep_boundary(self, messages: list) -> int:
        """从尾部往前找到第 K 个 HumanMessage 的位置索引。

        boundary 之前的 ToolMessage 会被截断，boundary 及之后的保持完整。
        """
        rounds_found = 0
        for idx in range(len(messages) - 1, -1, -1):
            if isinstance(messages[idx], HumanMessage):
                rounds_found += 1
                if rounds_found >= self._keep_recent_rounds:
                    return idx
        # 不足 K 轮，不截断
        return 0

    def _truncate_content(self, content: str) -> str:
        """截断 ToolMessage 内容到配置的最大字符数。"""
        if self._tool_content_max_chars <= 0:
            return _TRUNCATION_SUFFIX
        return content[: self._tool_content_max_chars] + _TRUNCATION_SUFFIX

    @staticmethod
    def _no_trim_info() -> dict[str, Any]:
        return {"trimmed": False}

    @staticmethod
    def _record_trim_span(trim_info: dict[str, Any]) -> None:
        """记录 memory_trim span 到当前活跃 trace。"""
        if not trim_info.get("trimmed"):
            return

        try:
            from core.observability.middleware import record_span

            with record_span(
                "memory_trim",
                "react_tool_content_trim",
                **{k: v for k, v in trim_info.items() if k != "trimmed"},
            ):
                pass  # 纯记录，无业务逻辑
        except Exception:  # noqa: BLE001
            logger.info("memory_trim span recording failed", exc_info=True)
