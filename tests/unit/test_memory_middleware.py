"""MemoryMiddleware 单元测试。

验证 ReAct 循环内 LLM 输入裁剪的核心行为：
- 早期 ToolMessage 被截断，最近 K 轮保持完整
- 非 ToolMessage（Human / AI / System）不受影响
- checkpoint / state 不被修改（通过 request.override 创建新对象）
- 关闭开关后直接透传
- memory_trim span 正确记录
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from core.memory.trimmer import MemoryMiddleware, _TRUNCATION_SUFFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeModelRequest:
    """模拟 ModelRequest，支持 override() 方法。"""

    messages: list[Any]
    system_message: Any = None
    tools: list[Any] = field(default_factory=list)
    model: Any = None
    state: dict[str, Any] = field(default_factory=dict)

    def override(self, **kwargs: Any) -> "_FakeModelRequest":
        new_req = _FakeModelRequest(
            messages=kwargs.get("messages", self.messages),
            system_message=kwargs.get("system_message", self.system_message),
            tools=kwargs.get("tools", self.tools),
            model=kwargs.get("model", self.model),
            state=kwargs.get("state", self.state),
        )
        return new_req


def _make_messages(rounds: int, tool_content_len: int = 2000) -> list[Any]:
    """构造多轮 ReAct 对话消息列表。

    每轮：HumanMessage → AIMessage(tool_calls) → ToolMessage → AIMessage(final)
    """
    msgs: list[Any] = []
    for i in range(rounds):
        msgs.append(HumanMessage(content=f"user query round {i}"))
        msgs.append(AIMessage(
            content="",
            tool_calls=[{"id": f"tc-{i}", "name": "query_pos", "args": {}}],
        ))
        msgs.append(ToolMessage(
            content="x" * tool_content_len,
            tool_call_id=f"tc-{i}",
        ))
        msgs.append(AIMessage(content=f"analysis result round {i}"))
    return msgs


def _request_with_messages(msgs: list[Any]) -> _FakeModelRequest:
    return _FakeModelRequest(messages=msgs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFindKeepBoundary:
    """测试轮次边界识别逻辑。"""

    def test_boundary_with_3_rounds_keep_2(self):
        mw = MemoryMiddleware(keep_recent_rounds=2)
        msgs = _make_messages(3, tool_content_len=100)
        # 3 轮消息，每轮 4 条，共 12 条
        # 从尾部数第 2 个 HumanMessage 在 index 4（第 2 轮起点）
        boundary = mw._find_keep_boundary(msgs)
        assert boundary == 4

    def test_boundary_with_1_round_keep_2(self):
        """不足 K 轮时，boundary=0，不截断任何内容。"""
        mw = MemoryMiddleware(keep_recent_rounds=2)
        msgs = _make_messages(1, tool_content_len=100)
        boundary = mw._find_keep_boundary(msgs)
        assert boundary == 0

    def test_boundary_with_5_rounds_keep_1(self):
        mw = MemoryMiddleware(keep_recent_rounds=1)
        msgs = _make_messages(5, tool_content_len=100)
        boundary = mw._find_keep_boundary(msgs)
        # 最后一个 HumanMessage 在 index 16（第 5 轮起点）
        assert boundary == 16

    def test_empty_messages(self):
        mw = MemoryMiddleware(keep_recent_rounds=2)
        boundary = mw._find_keep_boundary([])
        assert boundary == 0


class TestTrimRequest:
    """测试消息裁剪核心逻辑。"""

    @patch("core.memory.trimmer.logger")
    def test_early_tool_messages_truncated(self, _mock_logger):
        """早期 ToolMessage 内容被截断到 max_chars。"""
        mw = MemoryMiddleware(keep_recent_rounds=1, tool_content_max_chars=100)
        msgs = _make_messages(3, tool_content_len=2000)
        req = _request_with_messages(msgs)
        original_msgs = deepcopy(msgs)

        trimmed_req, info = mw._trim_request(req)

        # 原始 request 未被修改
        assert len(req.messages) == len(original_msgs)
        for orig, curr in zip(original_msgs, req.messages):
            assert orig.content == curr.content

        # 第 1、2 轮的 ToolMessage 被截断（index 2, 6）
        assert info["trimmed"] is True
        assert info["truncated_tool_messages"] == 2
        assert trimmed_req.messages[2].content == "x" * 100 + _TRUNCATION_SUFFIX
        assert trimmed_req.messages[6].content == "x" * 100 + _TRUNCATION_SUFFIX

        # 第 3 轮（最近 1 轮）的 ToolMessage 保持完整（index 10）
        assert len(trimmed_req.messages[10].content) == 2000

    def test_recent_rounds_preserved(self):
        """最近 K 轮的所有消息保持完整。"""
        mw = MemoryMiddleware(keep_recent_rounds=2, tool_content_max_chars=50)
        msgs = _make_messages(3, tool_content_len=1000)
        req = _request_with_messages(msgs)

        trimmed_req, info = mw._trim_request(req)

        # 只有第 1 轮的 ToolMessage 被截断
        assert info["truncated_tool_messages"] == 1
        # 第 2、3 轮保持完整
        assert len(trimmed_req.messages[6].content) == 1000
        assert len(trimmed_req.messages[10].content) == 1000

    def test_non_tool_messages_not_truncated(self):
        """Human / AI / System 消息不受截断影响。"""
        mw = MemoryMiddleware(keep_recent_rounds=1, tool_content_max_chars=50)
        msgs = _make_messages(3, tool_content_len=1000)
        req = _request_with_messages(msgs)

        trimmed_req, _ = mw._trim_request(req)

        for idx, msg in enumerate(trimmed_req.messages):
            if not isinstance(msg, ToolMessage):
                assert msg.content == msgs[idx].content

    def test_short_tool_messages_not_truncated(self):
        """内容短于 max_chars 的 ToolMessage 不截断。"""
        mw = MemoryMiddleware(keep_recent_rounds=1, tool_content_max_chars=500)
        msgs = _make_messages(3, tool_content_len=100)
        req = _request_with_messages(msgs)

        trimmed_req, info = mw._trim_request(req)

        assert info["trimmed"] is False

    def test_no_messages(self):
        """空消息列表直接返回。"""
        mw = MemoryMiddleware()
        req = _request_with_messages([])

        trimmed_req, info = mw._trim_request(req)

        assert info["trimmed"] is False

    def test_zero_max_chars_clears_content(self):
        """max_chars=0 时完全清空 ToolMessage 内容，只留截断后缀。"""
        mw = MemoryMiddleware(keep_recent_rounds=1, tool_content_max_chars=0)
        msgs = _make_messages(2, tool_content_len=1000)
        req = _request_with_messages(msgs)

        trimmed_req, info = mw._trim_request(req)

        assert info["truncated_tool_messages"] == 1
        assert trimmed_req.messages[2].content == _TRUNCATION_SUFFIX


class TestWrapModelCall:
    """测试 wrap_model_call / awrap_model_call 集成。"""

    def test_enabled_trims_and_calls_handler(self):
        mw = MemoryMiddleware(
            enabled=True, keep_recent_rounds=1, tool_content_max_chars=50,
        )
        msgs = _make_messages(3, tool_content_len=1000)
        req = _request_with_messages(msgs)
        handler = MagicMock(return_value="result")

        with patch.object(mw, "_record_trim_span"):
            result = mw.wrap_model_call(req, handler)

        assert result == "result"
        handler.assert_called_once()
        # handler 收到的是裁剪后的 request
        called_req = handler.call_args[0][0]
        assert called_req.messages[2].content.endswith(_TRUNCATION_SUFFIX)

    def test_disabled_passes_through(self):
        mw = MemoryMiddleware(enabled=False)
        msgs = _make_messages(3, tool_content_len=1000)
        req = _request_with_messages(msgs)
        handler = MagicMock(return_value="result")

        result = mw.wrap_model_call(req, handler)

        assert result == "result"
        handler.assert_called_once_with(req)  # 原始 request 直接传入

    @pytest.mark.asyncio
    async def test_async_enabled_trims(self):
        mw = MemoryMiddleware(
            enabled=True, keep_recent_rounds=1, tool_content_max_chars=50,
        )
        msgs = _make_messages(3, tool_content_len=1000)
        req = _request_with_messages(msgs)

        async def async_handler(r: Any) -> str:
            return "async_result"

        with patch.object(mw, "_record_trim_span"):
            result = await mw.awrap_model_call(req, async_handler)

        assert result == "async_result"

    @pytest.mark.asyncio
    async def test_async_disabled_passes_through(self):
        mw = MemoryMiddleware(enabled=False)
        msgs = _make_messages(2, tool_content_len=1000)
        req = _request_with_messages(msgs)

        async def async_handler(r: Any) -> str:
            assert r is req  # 原始 request
            return "async_result"

        result = await mw.awrap_model_call(req, async_handler)
        assert result == "async_result"


class TestTrimSpanRecording:
    """测试 memory_trim span 记录。"""

    def test_no_span_when_not_trimmed(self):
        """未发生裁剪时不记录 span。"""
        with patch(
            "core.observability.middleware.record_span"
        ) as mock_record:
            MemoryMiddleware._record_trim_span({"trimmed": False})
            mock_record.assert_not_called()

    def test_span_recorded_when_trimmed(self):
        """发生裁剪时记录 memory_trim span。"""
        trim_info = {
            "trimmed": True,
            "original_message_count": 12,
            "trimmed_message_count": 12,
            "original_total_chars": 6000,
            "trimmed_total_chars": 1200,
            "original_estimated_tokens": 4000,
            "trimmed_estimated_tokens": 800,
            "truncated_tool_messages": 2,
            "keep_recent_rounds": 2,
            "tool_content_max_chars": 500,
        }
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=None)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch(
            "core.observability.middleware.record_span", return_value=mock_ctx
        ) as mock_record:
            MemoryMiddleware._record_trim_span(trim_info)
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args
            assert call_kwargs[0][0] == "memory_trim"
            assert call_kwargs[0][1] == "react_tool_content_trim"
            assert call_kwargs[1]["truncated_tool_messages"] == 2


class TestMiddlewareName:
    """测试 middleware name 属性。"""

    def test_name(self):
        mw = MemoryMiddleware()
        assert mw.name == "MemoryMiddleware"


class TestConfigDefaults:
    """测试默认配置值和边界处理。"""

    def test_keep_recent_rounds_minimum_1(self):
        mw = MemoryMiddleware(keep_recent_rounds=0)
        assert mw._keep_recent_rounds == 1

    def test_max_chars_minimum_0(self):
        mw = MemoryMiddleware(tool_content_max_chars=-1)
        assert mw._tool_content_max_chars == 0
