"""P2PAgent ReAct 流式输出单元测试（Phase 2）。

覆盖 ``_astream_react_with_publish`` 的核心行为：

- first-chunk 模式检测（text vs tool）
- 仅对 text turn 推送 chunk
- 多轮 tool + 单轮 text 的累计语义
- 混输 rollback（text 轮中冒出 tool_call_chunks）
- ambiguous chunk 计数
- ``on_chain_end`` 缺失时 accumulated 重构 AIMessage 兜底
- ``usage_metadata`` 仅末 chunk 出现的采集

test 标号对应 ``docs/sse_react_backend.md §5.1`` 的用例表。
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import Settings


# ---------------------------------------------------------------------------
# 通用 fakes
# ---------------------------------------------------------------------------


class _FakeChunk:
    """模拟 LangChain ``AIMessageChunk``。

    - ``content``：文本内容（str / list[dict] / 空串）
    - ``tool_call_chunks``：tool 调用增量列表，非空表示 tool 模式
    - ``usage_metadata``：仅在 turn 末 chunk 上出现
    - ``additional_kwargs``：兼容 Qwen3 reasoning_content 兜底
    """

    def __init__(
        self,
        content: Any = "",
        tool_call_chunks: list | None = None,
        usage_metadata: Any = None,
        additional_kwargs: dict | None = None,
    ) -> None:
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []
        self.usage_metadata = usage_metadata
        self.additional_kwargs = additional_kwargs or {}


def _evt_chat_start() -> dict[str, Any]:
    return {"event": "on_chat_model_start", "name": "ChatModel", "data": {}}


def _evt_chat_stream(chunk: _FakeChunk) -> dict[str, Any]:
    return {
        "event": "on_chat_model_stream",
        "name": "ChatModel",
        "data": {"chunk": chunk},
    }


def _evt_chat_end() -> dict[str, Any]:
    return {"event": "on_chat_model_end", "name": "ChatModel", "data": {}}


def _evt_chain_end_with_messages(messages: list[Any]) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "LangGraph",
        "data": {"output": {"messages": messages}},
    }


class _FakeAgent:
    """模拟 LangGraph CompiledStateGraph，仅实现 ``astream_events``。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.calls = 0

    async def astream_events(
        self,
        invoke_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        version: str = "v2",
    ):
        self.calls += 1
        for ev in self._events:
            yield ev


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings()


class _CapturingBus:
    """记录所有 publish 调用，便于断言 chunk 推送序列。

    _astream_react_with_publish 通过 publish_chunk_event 调用 bus.publish，
    本类只关心被调记录，不实现 subscribe。
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.ephemeral_flags: list[bool] = []

    def publish(
        self,
        trace_id: str,  # noqa: ARG002
        event: dict[str, Any],
        *,
        ephemeral: bool = False,
    ) -> None:
        self.events.append(event)
        self.ephemeral_flags.append(ephemeral)

    def chunks(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == "chunk"]


@pytest.fixture()
def event_bus() -> _CapturingBus:
    """每个用例独立 bus，避免事件泄漏。注入到 events 模块单例位置。"""
    from core.tasks import events as events_mod

    bus = _CapturingBus()
    events_mod._bus = bus  # noqa: SLF001
    try:
        yield bus
    finally:
        events_mod._bus = None  # noqa: SLF001


# ---------------------------------------------------------------------------
# UT-B01: 单轮 text turn 正常推送
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b01_single_text_turn_streams_all_chunks(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """单一 text turn：所有 chunk 都按 micro-batch 推出，末尾带 eos=true，
    accumulated 拼接 == 完整回复，``index`` 严格递增。
    """
    # 构造 16 个字符触发首次 flush（_STREAM_FLUSH_CHARS=16）+ 第二次 flush
    chunks_text = ["这是分析报告的第一段内容。", "继续输出第二段内容收尾。"]
    events = [
        _evt_chat_start(),
        *[_evt_chat_stream(_FakeChunk(content=t)) for t in chunks_text],
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content="".join(chunks_text))]),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    result = await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b01",
        message_id="m-b01",
        span_attrs=span_attrs,
    )

    # 推送的 chunk 至少有 1 帧 + 1 帧 eos
    chunks = event_bus.chunks()
    assert len(chunks) >= 1, "应至少推出一帧"
    assert chunks[-1]["eos"] is True, "末帧必须 eos=True"
    assert chunks[-1]["node"] == "agent_final"
    assert chunks[-1]["message_id"] == "m-b01"
    assert chunks[-1]["seq"] == 0, "seq 固定 0"

    # index 严格 +1 递增
    indices = [c["index"] for c in chunks]
    assert indices == list(range(len(indices))), f"index 必须递增: {indices}"

    # ephemeral=True
    assert all(event_bus.ephemeral_flags), "所有 chunk 必须 ephemeral=True"
    # replay_safe=False
    assert all(c["replay_safe"] is False for c in chunks), "chunk 必须 replay_safe=False"

    # accumulated delta 拼接 == 完整文本
    accumulated = "".join(c["delta"] for c in chunks)
    assert accumulated == "".join(chunks_text)

    # span 指标
    assert span_attrs["text_turns"] == 1
    assert span_attrs["tool_turns"] == 0
    assert span_attrs["ambiguous_chunks"] == 0
    assert span_attrs["rollback_triggered"] is False
    assert "first_chunk_ms" in span_attrs

    # 返回值含 messages
    assert result["messages"], "result.messages 非空"


# ---------------------------------------------------------------------------
# UT-B02: 1 轮 tool → 1 轮 text，tool 轮不推送
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b02_tool_turn_followed_by_text_turn(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """tool turn 的 chunk 完全不推送，仅 text turn 的 chunk 进入 EventBus。"""
    text_payload = "最终 markdown 报告内容片段甲乙丙丁。"
    events = [
        # turn 1: tool（首 chunk 即 tool_call_chunks）
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"name": "query_po"}])),
        _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"args": '{"id":1}'}])),
        _evt_chat_end(),
        # turn 2: text
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content=text_payload)),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=text_payload)]),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b02",
        message_id="m-b02",
        span_attrs=span_attrs,
    )

    chunks = event_bus.chunks()
    accumulated = "".join(c["delta"] for c in chunks)
    assert accumulated == text_payload, "tool turn 不应混入推送内容"

    assert span_attrs["text_turns"] == 1
    assert span_attrs["tool_turns"] == 1
    assert span_attrs["rollback_triggered"] is False


# ---------------------------------------------------------------------------
# UT-B03: 多轮 tool（3 轮）+ 单轮 text，turn 计数正确
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b03_multiple_tool_turns_then_text(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """3 轮 tool + 1 轮 text：text_turns=1, tool_turns=3，仅最后一轮推送。"""
    final_text = "综合分析结论：风险整体可控。"
    events: list[dict[str, Any]] = []
    # 三轮连续 tool
    for tool_name in ("query_po", "run_3wm", "calc_kpi"):
        events.extend([
            _evt_chat_start(),
            _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"name": tool_name}])),
            _evt_chat_end(),
        ])
    # 最终 text turn
    events.extend([
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content=final_text)),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=final_text)]),
    ])

    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b03",
        message_id="m-b03",
        span_attrs=span_attrs,
    )

    chunks = event_bus.chunks()
    accumulated = "".join(c["delta"] for c in chunks)
    assert accumulated == final_text

    assert span_attrs["text_turns"] == 1
    assert span_attrs["tool_turns"] == 3


# ---------------------------------------------------------------------------
# UT-B04: 混输 rollback — text 轮中冒出 tool_call_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b04_mixed_text_then_tool_triggers_rollback(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """text 轮已推过若干 chunk 后冒出 tool_call_chunks：
    必须发 ``index=0, delta=""`` 重置帧；后续真正 text turn 重新推送。
    """
    events = [
        # 误判轮：首 chunk 是 content（被判为 text），后续混入 tool_call
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content="让我先查询一下采购订单……")),
        _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"name": "query_po"}])),
        _evt_chat_end(),
        # 真正的最终 text turn
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content="基于查询结果，结论如下。")),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content="基于查询结果，结论如下。")]),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b04",
        message_id="m-b04",
        span_attrs=span_attrs,
    )

    chunks = event_bus.chunks()
    # 序列里应包含一帧 index=0 + delta="" 的重置帧
    rollback_frames = [
        c for c in chunks if c["index"] == 0 and c["delta"] == "" and not c["eos"]
    ]
    assert rollback_frames, "应发出至少一帧 index=0/delta='' 的 rollback"

    # rollback 之后 text turn 的 chunk 重新从 index=0 开始
    # 找最后一组连续递增序列
    final_seq = []
    for c in chunks:
        if c["index"] == 0 and c["delta"] != "":
            final_seq = [c]
        elif final_seq and c["index"] == final_seq[-1]["index"] + 1:
            final_seq.append(c)
    assert final_seq, "rollback 后应有新的 text turn chunks"
    assert "结论如下" in "".join(c["delta"] for c in final_seq)

    assert span_attrs["rollback_triggered"] is True


# ---------------------------------------------------------------------------
# UT-B05: 首 chunk 既无 content 也无 tool_call → ambiguous_chunks 计数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b05_ambiguous_first_chunks_then_text(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """首个 chunk 完全空（无 content 也无 tool_call_chunks）→ 不判模式，
    继续等下一个 chunk。``ambiguous_chunks`` 计数递增。
    """
    final_text = "经过几轮空 chunk 后才出现的真实回复。"
    events = [
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content="")),  # ambiguous
        _evt_chat_stream(_FakeChunk(content="")),  # ambiguous
        _evt_chat_stream(_FakeChunk(content=final_text)),  # 进入 text 模式
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=final_text)]),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b05",
        message_id="m-b05",
        span_attrs=span_attrs,
    )

    chunks = event_bus.chunks()
    accumulated = "".join(c["delta"] for c in chunks)
    assert accumulated == final_text

    assert span_attrs["ambiguous_chunks"] == 2, "前两个空 chunk 应计入 ambiguous"
    assert span_attrs["text_turns"] == 1


# ---------------------------------------------------------------------------
# UT-B09: on_chain_end 事件缺失 → 用 accumulated 重构 AIMessage 兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b09_chain_end_missing_reconstructs_aimessage(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """LangGraph 的 on_chain_end 事件未携带 messages（或 name 不匹配）：
    返回值的 messages 必须由 accumulated 文本重构出来，长度 1 的 AIMessage。
    """
    text = "重构出来的最终回复内容。"
    events = [
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content=text)),
        _evt_chat_end(),
        # 故意省略 on_chain_end / name 不匹配
        {"event": "on_chain_end", "name": "OtherChain", "data": {"output": {}}},
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    result = await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b09",
        message_id="m-b09",
        span_attrs=span_attrs,
    )

    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert getattr(msg, "content", "") == text


# ---------------------------------------------------------------------------
# UT-B10: usage_metadata 仅末 chunk 出现，final_meta 采集成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b10_usage_metadata_only_on_last_chunk(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """LangChain 聚合规则：usage_metadata 仅在 turn 最后一个 chunk 上有值。
    实现里通过 ``final_meta = ... or final_meta`` 累计保留。
    返回 dict 的 ``usage_metadata`` 字段必须等于末 chunk 的 usage。
    """
    expected_usage = {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}
    events = [
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content="第一段", usage_metadata=None)),
        _evt_chat_stream(_FakeChunk(content="第二段", usage_metadata=None)),
        _evt_chat_stream(_FakeChunk(content="第三段", usage_metadata=expected_usage)),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content="第一段第二段第三段")]),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    result = await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b10",
        message_id="m-b10",
        span_attrs=span_attrs,
    )

    assert result["usage_metadata"] == expected_usage


# ---------------------------------------------------------------------------
# UT-B11: <think> 标签在 ReAct 路径同样被状态机抑制
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b11_think_tags_suppressed_in_react_stream(
    p2p_agent, event_bus: "_CapturingBus"
) -> None:
    """text turn 内出现 ``<think>...</think>`` 推理段：
    推送给前端的 chunk 拼起来不应包含推理内容；最终重构 messages 也已 strip。
    """
    events = [
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content="开头可见。")),
        _evt_chat_stream(_FakeChunk(content="<think>这是模型推理")),
        _evt_chat_stream(_FakeChunk(content="不应外泄</think>")),
        _evt_chat_stream(_FakeChunk(content="结尾可见。")),
        _evt_chat_end(),
    ]
    fake_agent = _FakeAgent(events)
    span_attrs: dict[str, Any] = {}

    result = await p2p_agent._astream_react_with_publish(
        fake_agent,
        {"messages": []},
        {},
        trace_id="t-b11",
        message_id="m-b11",
        span_attrs=span_attrs,
    )

    chunks = event_bus.chunks()
    accumulated_visible = "".join(c["delta"] for c in chunks)
    assert "推理" not in accumulated_visible
    assert "外泄" not in accumulated_visible
    assert "<think>" not in accumulated_visible
    assert "</think>" not in accumulated_visible
    assert "开头可见。" in accumulated_visible
    assert "结尾可见。" in accumulated_visible

    # accumulated 重构出来的 message 也已 strip
    msg_content = getattr(result["messages"][0], "content", "") if result["messages"] else ""
    assert "推理" not in msg_content
    assert "<think>" not in msg_content


# ---------------------------------------------------------------------------
# UT-B06: streaming_enabled=False 时走 ainvoke 原路径，EventBus 零 chunk
# ---------------------------------------------------------------------------


class _FakeAgentAinvoke:
    """模拟 LangGraph CompiledStateGraph 的 ainvoke 接口。"""

    def __init__(self, content: str = "ainvoke 路径回复") -> None:
        self.content = content
        self.calls = 0
        self.astream_events_calls = 0

    async def ainvoke(self, invoke_input: dict[str, Any], config: dict[str, Any] | None = None):
        self.calls += 1
        from langchain_core.messages import AIMessage

        return {"messages": [AIMessage(content=self.content)]}

    async def astream_events(self, *args, **kwargs):  # noqa: D401
        """若被错误调用立即抛错；不应进入此路径。"""
        self.astream_events_calls += 1
        raise AssertionError("streaming_on=False 时不应调用 astream_events")
        yield  # 让方法成为 async generator


@pytest.mark.asyncio
async def test_b06_streaming_disabled_falls_back_to_ainvoke(
    settings: Settings, event_bus: "_CapturingBus"
) -> None:
    """关 streaming 开关时：analyze() 不调用 astream_events，
    EventBus 不收到任何 chunk 事件。
    """
    from unittest.mock import patch

    from core.observability.tracing import _current_trace, _TraceContext
    from modules.p2p.agent import P2PAgent

    # 关 streaming
    settings.llm.streaming_enabled = False
    # 关长期记忆 / 短期记忆截断（避免外部依赖）
    settings.memory.long_term_enabled = False
    settings.memory.short_term_max_messages = 0

    agent = P2PAgent(settings=settings)
    fake_graph = _FakeAgentAinvoke(content="ainvoke 输出")

    # 即便注入 trace_id，配置开关关上仍走 ainvoke
    trace_ctx = _TraceContext(
        agent_name="p2p_agent",
        session_id="s",
        user_id="u",
        trace_id="t-b06",
    )
    token = _current_trace.set(trace_ctx)
    try:
        with patch.object(agent, "_get_or_build_agent", return_value=fake_graph):
            result = await agent.analyze(
                query="测试", user_id="u", session_id="s",
                time_range_days=7,
            )
    finally:
        _current_trace.reset(token)

    assert fake_graph.calls == 1, "ainvoke 应被调用一次"
    assert fake_graph.astream_events_calls == 0, "不应触发 astream_events"
    assert event_bus.chunks() == [], "不应有任何 chunk 事件"
    assert result.report_markdown == "ainvoke 输出"


# ---------------------------------------------------------------------------
# UT-B07: trace_id 缺失（_current_trace 未设置）→ 自动降级到 ainvoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b07_missing_trace_context_degrades_to_ainvoke(
    settings: Settings, event_bus: "_CapturingBus"
) -> None:
    """开关打开但没有 trace 上下文（独立单测场景）→ 应降级到 ainvoke。"""
    from unittest.mock import patch

    from modules.p2p.agent import P2PAgent

    settings.llm.streaming_enabled = True  # 配置开着
    settings.memory.long_term_enabled = False
    settings.memory.short_term_max_messages = 0

    agent = P2PAgent(settings=settings)
    fake_graph = _FakeAgentAinvoke(content="降级路径回复")

    # 故意不 set _current_trace
    with patch.object(agent, "_get_or_build_agent", return_value=fake_graph):
        result = await agent.analyze(
            query="测试", user_id="u", session_id="s", time_range_days=7,
        )

    assert fake_graph.calls == 1
    assert fake_graph.astream_events_calls == 0
    assert event_bus.chunks() == []
    assert result.report_markdown == "降级路径回复"


# ---------------------------------------------------------------------------
# UT-B08: 重试场景 chunk_index 从 0 重置（前端协议复用）
# ---------------------------------------------------------------------------


class _FlakyStreamingAgent:
    """首次 astream_events 抛异常，第二次正常返回。"""

    def __init__(self, success_events: list[dict[str, Any]]) -> None:
        self._success_events = success_events
        self.astream_calls = 0

    async def astream_events(
        self,
        invoke_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        version: str = "v2",
    ):
        self.astream_calls += 1
        if self.astream_calls == 1:
            # 首次失败：先推一段超过 flush 阈值的内容（>16 字符），让 chunk 真正
            # 落到 EventBus，再抛异常模拟"已部分推送后中断"。
            yield _evt_chat_start()
            yield _evt_chat_stream(
                _FakeChunk(content="首次尝试已经成功推出的部分前缀文本ABCDEFG")
            )
            raise RuntimeError("transient")
        for ev in self._success_events:
            yield ev


@pytest.mark.asyncio
async def test_b08_retry_resets_chunk_index_from_zero(
    settings: Settings, event_bus: "_CapturingBus"
) -> None:
    """重试触发后，新一轮的首 chunk index=0；前端按"index <= lastChunkIndex"清 buffer。"""
    from unittest.mock import patch

    from core.observability.tracing import _TraceContext, _current_trace
    from modules.p2p.agent import P2PAgent

    settings.llm.streaming_enabled = True
    settings.memory.long_term_enabled = False
    settings.memory.short_term_max_messages = 0
    settings.llm.max_retries = 3
    settings.agent_runtime.retry_backoff_base_seconds = 0.0  # 提速
    settings.agent_runtime.retry_backoff_max_seconds = 0.0

    final_text = "重试成功后的最终回复。"
    success_events = [
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content=final_text)),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=final_text)]),
    ]
    fake_graph = _FlakyStreamingAgent(success_events)

    agent = P2PAgent(settings=settings)
    trace_ctx = _TraceContext(
        agent_name="p2p_agent", session_id="s", user_id="u",
        trace_id="t-b08",
    )
    token = _current_trace.set(trace_ctx)
    try:
        with patch.object(agent, "_get_or_build_agent", return_value=fake_graph):
            result = await agent.analyze(
                query="测试", user_id="u", session_id="s", time_range_days=7,
            )
    finally:
        _current_trace.reset(token)

    assert fake_graph.astream_calls == 2, "应触发 1 次重试"

    chunks = event_bus.chunks()
    # 第一次推过部分内容（index=0,1,...） + 失败 → 第二次重新推（index=0,...）
    indices = [c["index"] for c in chunks]
    # 至少应该包含两次"index=0"出现，第二次是重置标志
    assert indices.count(0) >= 2, f"应至少两次 index=0（重试重置），实际 indices={indices}"

    # 最终结果是第二次的内容
    assert result.report_markdown == final_text


# ---------------------------------------------------------------------------
# UT-B12: 端到端 — analyze() 全链路下 accumulated delta == result.report_markdown
# ---------------------------------------------------------------------------


class _FakeStreamingAgentForAnalyze:
    """供 analyze() 重试循环使用的 streaming fake agent。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.astream_calls = 0

    async def astream_events(
        self,
        invoke_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        version: str = "v2",
    ):
        self.astream_calls += 1
        for ev in self._events:
            yield ev


@pytest.mark.asyncio
async def test_b12_accumulated_delta_equals_report_markdown_via_analyze(
    settings: Settings, event_bus: "_CapturingBus"
) -> None:
    """analyze() 全链路：streaming 推送的所有 chunk delta 拼接 ==
    最终 ``AnalysisResult.report_markdown``。这是前端"streaming
    accumulated 与 done 后快照覆盖语义等价"的根本保证。
    """
    from unittest.mock import patch

    from core.observability.tracing import _TraceContext, _current_trace
    from modules.p2p.agent import P2PAgent

    settings.llm.streaming_enabled = True
    settings.memory.long_term_enabled = False
    settings.memory.short_term_max_messages = 0

    # 构造一段会被分批 flush 的内容
    parts = [
        "## 分析报告\n\n",
        "本次共发现 ",
        "**3 项** 异常：\n",
        "1. 价格偏差 PO-001\n",
        "2. 三路不匹配 PO-002\n",
        "3. 付款延迟 INV-003\n",
    ]
    full = "".join(parts)
    events = [
        # 先 1 轮 tool（不应推送）
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"name": "query_po"}])),
        _evt_chat_end(),
        # 最终 text turn
        _evt_chat_start(),
        *[_evt_chat_stream(_FakeChunk(content=p)) for p in parts],
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=full)]),
    ]
    fake_graph = _FakeStreamingAgentForAnalyze(events)

    agent = P2PAgent(settings=settings)
    trace_ctx = _TraceContext(
        agent_name="p2p_agent", session_id="s", user_id="u",
        trace_id="t-b12",
    )
    token = _current_trace.set(trace_ctx)
    try:
        with patch.object(agent, "_get_or_build_agent", return_value=fake_graph):
            result = await agent.analyze(
                query="找异常", user_id="u", session_id="s", time_range_days=30,
            )
    finally:
        _current_trace.reset(token)

    chunks = event_bus.chunks()
    accumulated = "".join(c["delta"] for c in chunks)

    assert accumulated == full, (
        "streaming accumulated 必须等于 result.report_markdown，"
        "否则前端 done 后快照覆盖会出现内容跳变"
    )
    assert result.report_markdown == full
    assert result.status.value == "success"
    # tool turn 期间没有产生任何 chunk
    assert all(c["node"] == "agent_final" for c in chunks)


# ---------------------------------------------------------------------------
# UT-B13: 监控 span 属性正确写入 trace context（最终持久化到 trace_spans 表）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b13_streaming_span_attributes_recorded(
    settings: Settings, event_bus: "_CapturingBus"
) -> None:
    """analyze() 完成后，trace_ctx.spans 必须包含一个 type="model" 的 span，
    其 attributes 含完整 streaming 监控指标（这些 attributes 后续由
    TraceStore 持久化到 trace_spans 表）。
    """
    from unittest.mock import patch

    from core.observability.tracing import _TraceContext, _current_trace
    from modules.p2p.agent import P2PAgent

    settings.llm.streaming_enabled = True
    settings.memory.long_term_enabled = False
    settings.memory.short_term_max_messages = 0

    final_text = "包含若干 token 的最终回复内容供测试。"
    events = [
        # tool 轮 + text 轮，让 turn 计数有真实分布
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(tool_call_chunks=[{"name": "tool1"}])),
        _evt_chat_end(),
        _evt_chat_start(),
        _evt_chat_stream(_FakeChunk(content=final_text)),
        _evt_chat_end(),
        _evt_chain_end_with_messages([_FakeChunk(content=final_text)]),
    ]
    fake_graph = _FakeStreamingAgentForAnalyze(events)

    agent = P2PAgent(settings=settings)
    trace_ctx = _TraceContext(
        agent_name="p2p_agent", session_id="s", user_id="u",
        trace_id="t-b13",
    )
    token = _current_trace.set(trace_ctx)
    try:
        with patch.object(agent, "_get_or_build_agent", return_value=fake_graph):
            await agent.analyze(
                query="测试 span", user_id="u", session_id="s", time_range_days=7,
            )
    finally:
        _current_trace.reset(token)

    # 找到 ReAct streaming 对应的 model span
    model_spans = [
        sp for sp in trace_ctx.spans
        if sp.span_type == "model" and sp.name == "p2p_agent.react"
    ]
    assert model_spans, f"应有 p2p_agent.react span; 实际 spans={[s.name for s in trace_ctx.spans]}"

    sp = model_spans[-1]
    attrs = sp.attributes
    # 全套监控指标必须在场
    assert attrs.get("react_streaming") is True
    assert attrs.get("text_turns") == 1
    assert attrs.get("tool_turns") == 1
    assert attrs.get("ambiguous_chunks") == 0
    assert attrs.get("rollback_triggered") is False
    assert "first_chunk_ms" in attrs
    assert isinstance(attrs["first_chunk_ms"], (int, float))
    assert sp.status == "ok"
    assert sp.duration_ms is not None and sp.duration_ms > 0


# ---------------------------------------------------------------------------
# fixtures（续）
# ---------------------------------------------------------------------------


@pytest.fixture()
def p2p_agent(settings: Settings):
    """构造一个 P2PAgent 实例，避免触发 LLM/checkpointer 真实初始化。

    ``_astream_react_with_publish`` 不依赖 ``_get_or_build_agent`` 的产出
    （fake_agent 由 fixture/test 直接传入），因此这里只需要构造 P2PAgent
    实例本身。
    """
    from modules.p2p.agent import P2PAgent

    return P2PAgent(settings=settings)


