# ReAct 分支流式输出方案设计（SSE Phase 2）

> 目标读者：后端开发同学、架构评审人
> 配套文档：[sse_issue.md](./sse_issue.md)（Phase 1 DAG 路径流式 + 基础设施盘点）、[sse_front_spec.md](./sse_front_spec.md)（前端改造规范）、[async_analyze_frontend.md](./async_analyze_frontend.md)（异步接口合约）、[llm_issue.md](./llm_issue.md)（LLM 性能优化矩阵）

## 1. 背景与现状

Phase 1 已经完成 DAG 主路径的流式改造：`ReportAgent.generate` 使用 `_astream_with_publish` 把 Markdown 报告按 token 增量推到 `EventBus`，前端 SSE 通道收到 `chunk` 事件 append 到气泡。TTFB 从 60–120s 降到 1–3s，DAG 流量全部覆盖。

剩下的兜底路径是 ReAct：当三级意图路由未命中静态 DAG 模板、或 DAG 执行失败时，[core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py) 的 `_execute_react` 会调用 P2PAgent 的 `run → analyze`，在 [modules/p2p/agent.py:562](../modules/p2p/agent.py#L562) 通过 `await agent.ainvoke(...)` 跑完整的多轮 tool-calling ReAct 循环，把最后一条不含 `tool_calls` 的消息作为 Markdown 报告返回。

该路径目前**没有流式**。阻塞点有两层：
1. `agent.ainvoke` 本身是一次性等整轮 LangGraph 子图完成
2. 即便每个 LLM turn 都开 `streaming=True`，LangGraph 聚合后仍是单次返回

ReAct 路径是次要流量（主要流量已被 DAG 吸走），但在以下场景仍会触发：
- 用户提出 DAG 模板未覆盖的探索类问题
- 意图路由 L3 分类返回 `COMPREHENSIVE` 且缺少核心实体
- DAG 模板校验失败的降级兜底

这些场景通常伴随较长的 ReAct 循环（2–5 轮 tool 调用），总时长 30–90s，TTFB 完全等于总时长。用户体验劣于 DAG 路径。

Phase 2 的目标：**把 ReAct 兜底路径的最终 LLM 回复改成 token-level 流式，复用 Phase 1 的全部基础设施（ChunkEvent / EventBus / SSE 端点），不新增端点也不改协议**。

## 2. 方案候选与选型

三个候选方案都是围绕"如何在 ReAct 多轮循环中只流式推送最终回复文本、不泄露中间 tool-call 噪声"这一核心难题展开。

### 2.1 方案对比

| 维度 | 方案 A：`astream_events` + 末端过滤 | 方案 B：非流式 + 二次 synthesis | 方案 C：`astream_events` + first-chunk 模式检测 |
|---|---|---|---|
| 原理 | 用 `agent.astream_events(v="v2")` 订阅 `on_chat_model_end` 事件，判断该轮 message 是否含 `tool_calls`，仅在"最后一轮 + 无 tool_calls"时把完整 message 内容 flush | `agent.ainvoke` 正常跑完拿结构化结论，再用一次独立 `llm.astream()` 调用做 Markdown synthesis，对该调用流式（复用 `_astream_with_publish`，`node="agent_final"`） | 订阅 `on_chat_model_stream` 事件，用每轮 LLM turn 的**第一个非空 chunk** 判定 turn 模式：`tool_call_chunks` → tool 模式丢弃；`content` → text 模式实时推送 |
| TTFB 粒度 | Message-level（等整轮跑完） | Token-level（仅 synthesis 阶段） | Token-level（最终 turn 实时） |
| 额外 LLM 调用 | 0 | +1（Markdown→Markdown 重写 ≈ +30% 成本） | 0 |
| 前端协议改动 | 无 | 无 | 无 |
| 代码复杂度 | 低 | 低（复用 `_astream_with_publish`） | 中（新增 ~120 行 stream 处理） |
| 风险 | 不改善 TTFB，只是把 `ainvoke` 换了个形状 | ReAct 已产出 Markdown，再做一次 Markdown synthesis 是纯粹浪费 | Qwen 偶发 text+tool_call 混输，需要靠 index 重置兜底 |

### 2.2 关键观察

查看 [modules/p2p/prompts.py:191-196](../modules/p2p/prompts.py#L191) 的 system prompt 明确要求"以 Markdown 格式组织报告"，[modules/p2p/agent.py:580-593](../modules/p2p/agent.py#L580) 的 JSON 解析只是**兜底**——主要输出形态就是 Markdown。这意味着：

- **方案 B 是负价值改造**：Markdown 作为 ReAct 输出 → 再跑一次 LLM 把 Markdown 转成 Markdown，除了加成本什么都没得到。
- **方案 A 的 TTFB 收益为零**：最终 turn 本身就是报告生成 turn，整段 Markdown 的生成时间 = 报告的端到端时长，等它结束再 flush 等于不做流式。
- **只有方案 C 提供真正的 token-level 流式**：在最终 turn 内 token-by-token 推送，TTFB = 首个非空 content chunk 到达时间（典型 1–3s）。

### 2.3 选型结论

**选择方案 C**。理由：

1. 唯一能给出真正 TTFB 改善的方案
2. 零额外 LLM 调用，无成本增加
3. Qwen 混输风险有成熟兜底（复用 Phase 1 的 index 重置协议）
4. 与 Phase 1 的 `_astream_with_publish` 风格一致，前后端协议完全不动

## 3. 最终方案（Approach C）

### 3.1 核心思路

LangGraph `create_agent` 返回的 `CompiledStateGraph` 继承 `Runnable`，原生支持 `astream_events(version="v2")`。该方法会在图执行期间按事件流输出：

- `on_chain_start` / `on_chain_end`：子图节点进出
- `on_chat_model_start`：一次 LLM turn 开始
- `on_chat_model_stream`：该 turn 的每个增量 chunk（`AIMessageChunk`）
- `on_chat_model_end`：该 turn 结束，完整 message 可拿
- `on_tool_start` / `on_tool_end`：工具调用（已由 Phase 1 的 `stage` / `tool` 事件覆盖，不重复）

OpenAI 兼容 tool calling 协议下，每个 LLM turn 只会走两条路之一：
- **Tool-call turn**：`chunk.tool_call_chunks` 持续有内容，`chunk.content` 始终为空
- **Text turn**：`chunk.content` 持续有内容，`chunk.tool_call_chunks` 始终为空

**关键洞察**：turn 的"模式"在第一个非空 chunk 即可判定。Text turn 必然是 ReAct 循环中的"最终回复 turn"（不再需要调工具）。

### 3.2 First-chunk 模式检测算法

```python
async def _astream_react_with_publish(
    self, agent, invoke_input, invoke_config,
    trace_id: str, message_id: str,
    *, node: str = "agent_final",
    flush_interval: float = 0.05, flush_chars: int = 16,
) -> dict[str, Any]:
    """用 astream_events 跑 ReAct，仅对最终 text turn 做 token-level 流式推送。

    返回与 agent.ainvoke 等价的 result dict（含 messages 列表）。
    """
    from core.tasks.events import get_event_bus
    from core.time_utils import now_cn
    import time as _time

    bus = get_event_bus()
    accumulated: list[str] = []
    pending: list[str] = []
    last_flush = _time.monotonic()
    chunk_index = 0
    current_turn_mode: str | None = None  # None | "text" | "tool"
    final_messages: list[Any] = []
    final_meta: Any = None

    def _publish(delta: str, eos: bool = False) -> None:
        nonlocal chunk_index, last_flush
        if bus is None or (not delta and not eos):
            return
        bus.publish(trace_id, {
            "type": "chunk", "trace_id": trace_id,
            "ts": now_cn().isoformat(), "seq": 0,
            "node": node, "message_id": message_id,
            "delta": delta, "index": chunk_index, "eos": eos,
        }, ephemeral=True)
        chunk_index += 1
        last_flush = _time.monotonic()

    def _flush(eos: bool = False) -> None:
        delta = "".join(pending); pending.clear()
        _publish(delta, eos=eos)

    async for event in agent.astream_events(
        invoke_input, config=invoke_config, version="v2"
    ):
        et = event["event"]
        if et == "on_chat_model_start":
            current_turn_mode = None  # reset per turn
        elif et == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            # 模式判定（仅首个非空 chunk）
            if current_turn_mode is None:
                if getattr(chunk, "tool_call_chunks", None):
                    current_turn_mode = "tool"
                elif getattr(chunk, "content", ""):
                    current_turn_mode = "text"
                # 两者都空：继续等下一个 chunk
            # 仅 text 模式推送
            if current_turn_mode == "text":
                text = getattr(chunk, "content", "") or ""
                if text:
                    pending.append(text); accumulated.append(text)
                if (sum(len(s) for s in pending) >= flush_chars
                        or _time.monotonic() - last_flush >= flush_interval):
                    _flush()
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                final_meta = meta
        elif et == "on_chat_model_end":
            # text turn 正常结束：flush 尾部并发 eos
            if current_turn_mode == "text":
                _flush(eos=True)
        elif et == "on_chain_end" and event.get("name") == "LangGraph":
            # 顶层图执行结束，拿 final state
            final_messages = event["data"].get("output", {}).get("messages", [])

    return {"messages": final_messages, "usage_metadata": final_meta}
```

### 3.3 集成到 `P2PAgent.analyze`

在 [modules/p2p/agent.py](../modules/p2p/agent.py) 的重试循环内部（当前 `agent.ainvoke` 调用点）：

```python
_trace_ctx = _current_trace.get()
_trace_id = _trace_ctx.trace_id if _trace_ctx is not None else None
_message_id = get_current_message_id() or _trace_id
_bus_ready = get_event_bus() is not None
streaming_on = bool(
    self._settings.llm.streaming_enabled
    and _trace_id
    and _bus_ready
)

with record_span("model", ...) as model_attrs:
    model_attrs["react_streaming"] = streaming_on
    if streaming_on:
        result = await self._astream_react_with_publish(
            agent, {"messages": invoke_messages}, invoke_config,
            trace_id=_trace_id, message_id=_message_id,
            node="agent_final",
        )
    else:
        result = await agent.ainvoke({"messages": invoke_messages}, config=invoke_config)
```

后续 `messages[-1].content` 提取、JSON 解析、长期记忆写入、`AnalysisResult` 构造——全部逻辑不变。

## 4. 与现有基础设施的衔接

### 4.1 LangGraph 兼容性

- `create_agent`（[modules/p2p/agent.py:383](../modules/p2p/agent.py#L383)）返回 `CompiledStateGraph`，继承 `Runnable`，`astream_events(version="v2")` 为官方稳定 API
- 调用时 `config={"configurable": {"thread_id": effective_thread}}` 与 `ainvoke` 完全一致，checkpointer 的 thread 状态加载/持久化行为不变
- 事件流输出期间，LangGraph 内部仍然会触发 state 更新、checkpointer 写入、工具执行等副作用——我们只"旁观"事件，不影响图执行语义

### 4.2 Middleware 正交性

- **`MemoryMiddleware`**（[core/memory/middleware.py](../core/memory/middleware.py)）：在 state 转换层（`wrap_model_call` 前）裁剪 tool message 内容，工作在 LangGraph 节点层级，与事件流订阅完全正交
- **`TimingMiddleware`**（[core/observability/middleware.py](../core/observability/middleware.py)）：通过 `wrap_model_call` 包裹 model 调用。streaming 模式下，model span 的 duration 仍然覆盖完整一次 model 调用（start → end），与 non-streaming 行为一致。仅需给 span 加 `react_streaming=true/false` attr

### 4.3 Checkpointer 一致性

- PostgresSaver（`core/observability/checkpointer.py`）在 `astream_events` 路径下的 thread 加载/保存逻辑与 `ainvoke` 完全一致，由 LangGraph 底层保障
- Checkpoint 写入时机 = 每个 super-step 结束（节点完成），不受事件订阅影响
- 短期记忆写回、session 断点续传等行为不需要任何改动

### 4.4 输出契约：JSON vs Markdown 保留

- 当前 `agent.py:586` 的 `json.loads(content)` 是兜底解析，Markdown 场景会落到 `except` 分支静默忽略
- Approach C 推送给前端的是 `chunk.content` 原始文本（Markdown 或 JSON 均可），前端展示形态不受影响
- 若某次 ReAct 输出恰好是纯 JSON（例如 system prompt 被测试覆盖改写），前端气泡会显示 JSON 源码——这是当前行为的等价迁移，不是退化

### 4.5 `usage_metadata` 采集

- LangChain 聚合规则：`usage_metadata` 只在每个 turn 的最后一个 chunk 返回
- 循环内 `final_meta = getattr(chunk, "usage_metadata", None) or final_meta` 保留最后一次非空值
- 多 turn 场景下只记录最终 text turn 的 usage；中间 tool turn 的 token 消耗通过 TimingMiddleware 的 model span 独立记录（已有行为）

## 5. 重试与 streaming 交互协议

[modules/p2p/agent.py:559](../modules/p2p/agent.py#L559) 的重试循环（`max_retries=3`，指数退避）在 streaming 模式下的行为：

1. **首次尝试**：`_astream_react_with_publish` 开始推 chunk，`index=0,1,2,...`
2. **首次失败**（例如 LangGraph 超时 / tool 异常 / Qwen 断流）：已发的 chunks 留在前端 buffer
3. **退避等待**：无事件推送
4. **重试尝试**：新一轮 `_astream_react_with_publish`，`chunk_index` 重新从 0 开始
5. **前端处理**（现有协议 [docs/sse_issue.md §6](./sse_issue.md#6-chunkevent-协议)）：观察到 `chunk.index <= lastChunkIndex` → 视为"后端重试"，**清空累加 buffer 重新开始**

这条协议 Phase 1 已为 ReportAgent tenacity 重试设计并验证。**Phase 2 完全复用，无新增协议字段**。

**同一 turn 内的 Qwen 混输兜底**（罕见）：若某个 text turn 的首 chunk 是 content 但后续追加了 `tool_call_chunks`，意味着模型在文本中途决定调工具。此时 `_astream_react_with_publish` 已经推过若干 text chunk。处理：
- 检测到该 turn 结束后 LangGraph 进入下一轮 tool 调用（即总轮数 > 1 且末轮仍有 tool_calls），视为"推错"
- 主动发一条 `index=0, delta=""` 的重置 chunk 让前端清 buffer
- 下一个真正的 text turn 重新推送

这个兜底 bug 只在同一 turn 内 content 和 tool_call 并存时触发（Qwen 已观察到偶发），概率低但需预防。

## 6. 配置与回滚

### 6.1 配置开关

P2PAgent 的 model 构造走的是主模型（[modules/p2p/agent.py:361](../modules/p2p/agent.py#L361) 的 `build_chat_model(self._settings.llm)`），因此**复用 `settings.llm.streaming_enabled`**，不新增配置项。

启用条件（短路组合，任一不满足则降级到 `ainvoke`）：

```python
streaming_on = bool(
    self._settings.llm.streaming_enabled  # 全局开关
    and _trace_id                          # 必须有 trace 上下文（来自 _current_trace ContextVar）
    and _bus_ready                         # EventBus 已初始化
)
```

### 6.2 回滚路径

- **一键关闭**：`LLM_STREAMING_ENABLED=false` 环境变量 → `streaming_on=False` → 回到 `await agent.ainvoke(...)` 原始代码路径
- **降级不降维**：回退后，Phase 1 DAG 路径的流式**继续保持**（DAG 走 `llm_fast.streaming_enabled`，与 `llm.streaming_enabled` 独立）
- **无需重启**：配置通过 `get_settings()` 读取，可热更新（取决于部署侧缓存策略）

## 7. 监控

### 7.1 新增 span 属性

在 `_astream_react_with_publish` 外层的 `record_span("model", ...)` 上挂：

| 属性 | 类型 | 说明 |
|---|---|---|
| `react_streaming` | bool | 本次 ReAct 调用是否启用流式 |
| `first_chunk_ms` | float | 从 `astream_events` 开始到首个 `text` 模式 chunk 推出的毫秒数（仅 streaming_on=True 且发生过 text turn 时记录）|
| `text_turns` | int | 本次 ReAct 中 text 模式 turn 数（期望=1，>1 视为异常）|
| `tool_turns` | int | 本次 ReAct 中 tool 模式 turn 数（用于观察 ReAct 循环深度）|
| `ambiguous_chunks` | int | 首 chunk 既无 content 也无 tool_call_chunks 的计数（LangChain 版本兼容性监测指标）|

### 7.2 告警阈值建议

- `first_chunk_ms` P95 > 8000 → 告警（可能 Qwen 首 token 延迟异常 / ReAct 前置 tool 轮过多）
- `text_turns > 1` 出现率 > 1% → 告警（模型行为异常，可能需要调整 prompt）
- `ambiguous_chunks > 0` 出现率 > 5% → 告警（LangChain / LangGraph 版本升级引入了新的 chunk 形态）

### 7.3 与 Phase 1 指标对齐

Phase 1 已经在 ReportAgent 的 `record_span("model", ...)` 上记录 `streaming=true/false` 和 usage tokens。Phase 2 的 `react_streaming` 保持独立属性名，避免混淆两条路径的流量统计。

## 8. 实施清单

Phase 2 需要改动的文件：

| 文件 | 改动 | 估计行数 |
|---|---|---|
| [modules/p2p/agent.py](../modules/p2p/agent.py) | 新增 `_astream_react_with_publish` 方法 + `analyze` 内部 streaming 分支 + span 属性采集 | +120 |
| [tests/unit/test_react_streaming.py](../tests/unit/test_react_streaming.py) | 新增：fake `astream_events` 序列覆盖 tool-only / text-only / mixed / ambiguous 四种模式 | +150 |
| [tests/integration/test_sse_react_chunk_flow.py](../tests/integration/test_sse_react_chunk_flow.py) | 新增：触发 ReAct 兜底路径，断言 SSE 通道收到 chunk 事件 + 顺序正确 | +100 |
| [tests/integration/test_e2e_streaming.py](../tests/integration/test_e2e_streaming.py) | 扩展：新增 ReAct 场景测试用例，采集 `first_chunk_ms` baseline | +40 |
| [docs/sse_react_issue.md](./sse_react_issue.md) | 本文档 | — |

**不需要改动的文件**：
- `core/tasks/schemas.py`（ChunkEvent 已定义）
- `core/tasks/events.py` / `events_redis.py`（ephemeral 已支持）
- `api/routes/analyze_async.py`（SSE 端点对 chunk 事件透传）
- `config/settings.py` / `config/config.yaml`（复用 `llm.streaming_enabled`）
- `modules/p2p/model_factory.py`（`streaming=True` 已透传）
- `core/observability/middleware.py`（TimingMiddleware 已兼容 astream_events）

总代码改动 ≈ **410 行**（含测试），预计 **1 人日核心开发 + 0.5 人日测试**。

## 9. 测试矩阵

### 9.1 单元测试 `tests/unit/test_react_streaming.py`

| 用例 | 输入（fake astream_events 序列）| 断言 |
|---|---|---|
| **纯 text turn** | 1 轮 text chunks，无 tool_calls | 所有 chunk 推出，`index` 递增，末尾 `eos=True`，accumulated == 完整回复 |
| **先 tool 后 text** | turn1=tool chunks，turn2=text chunks | turn1 所有 chunk **不推送**，turn2 按 micro-batch 推送 |
| **多轮 tool + 一轮 text** | turn1=tool, turn2=tool, turn3=text | 仅 turn3 chunk 推送，`text_turns=1`, `tool_turns=2` |
| **混输（content + tool_call 同 turn）** | turn1 首 chunk content，后续追加 tool_call_chunks | 检测到异常后发 `index=0` 重置 chunk |
| **空 chunk 流** | 所有 chunk 的 content 和 tool_call_chunks 均空 | `ambiguous_chunks > 0` 计数递增，最终无 text 推送（`text_turns=0`）|
| **streaming 禁用** | `settings.llm.streaming_enabled=False` | 走 `agent.ainvoke` 分支，EventBus 无 chunk 事件 |
| **trace_id 缺失** | `_current_trace.get() is None` | 自动降级到 `ainvoke`，`react_streaming=False` |
| **重试触发** | 首次 astream 抛异常，重试成功 | 第二轮 chunk `index` 从 0 重新开始，前端能识别重置 |

### 9.2 集成测试 `tests/integration/test_sse_react_chunk_flow.py`

- 启动 FastAPI test client + `MemoryEventBus`
- 构造 query 让意图路由落到 `COMPREHENSIVE` 且无核心实体，触发 ReAct 兜底
- 订阅 `GET /analyze/tasks/{trace_id}/events`，断言事件序列：
  `status → stage×N → tool×M → chunk×K → chunk(eos) → done`
- 断言 chunk 事件字段：`node="agent_final"`, `message_id` 正确绑定，`index` 严格递增，`seq=0`
- 断言 `MemoryEventBus.buffered(trace_id)` **不包含 chunk 事件**（ephemeral 语义）

### 9.3 端到端测试 `tests/integration/test_e2e_streaming.py`（扩展）

- 真实 Qwen `llm` 主模型
- 构造一个 DAG 无法覆盖的探索类 query（例如 `"帮我找最近有异常的前 3 家供应商并说明原因"`，故意让 DAG 模板命中失败）
- `POST /analyze/async` 提交
- `curl -N` 订阅 SSE，记录：
  - TTFB（提交 → 首个 `chunk` 事件）
  - 首 tool 调用完成时间
  - 端到端时长（提交 → `done` 事件）
- 断言 TTFB < 10s（容许 ReAct 前置 tool 调用占用），端到端时长与关闭 streaming 的 baseline 相当

### 9.4 覆盖率

Phase 2 新增/改动代码单元测试覆盖率 ≥ 90%（保持项目现有标准）。

## 10. 风险与缓解

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| 1 | Qwen 同一 turn 内 content 与 tool_call 混输 | 低 | 前端显示被中途撤销 | §5 约定的 `index=0` 重置 chunk，前端既有协议自动清 buffer |
| 2 | `astream_events(v="v2")` 在 LangChain/LangGraph 版本升级后事件结构变化 | 低 | 模式判定失效 | 监控 `ambiguous_chunks` 指标；单元测试覆盖事件序列解析；pin `langchain-core>=1.2,<2.0` |
| 3 | `_astream_react_with_publish` 与 TimingMiddleware 的 `wrap_model_call` 交互异常 | 低 | model span 采集错乱 | 集成测试检查 span 完整性；失败时 middleware 不阻塞主流程（仅记 warning）|
| 4 | 多轮 ReAct 深度过大（tool_turns > 5）导致 TTFB 仍然大 | 中 | streaming 收益被前置 tool 轮抵消 | 本质是 ReAct 循环深度问题，与 streaming 无关；通过 intent 路由命中率提升减少 ReAct 触发率 |
| 5 | 重试期间已推 chunk 污染前端 | 中 | 前端显示不一致 | 复用 Phase 1 的 index 重置协议，前端见到 `index <= lastChunkIndex` 即清 buffer |
| 6 | `ambiguous_chunks`（首 chunk 既无 content 也无 tool_call_chunks）频繁出现 | 低 | 模式判定延后，TTFB 略有抖动 | 检测循环正确处理：继续等下一个 chunk，不强制判定；监控指标告警 |
| 7 | `astream_events` 输出的 `on_chain_end` 顶层事件 payload 结构变化 | 低 | 无法提取 final messages | fallback：若 `final_messages` 为空，退化到从 accumulated 文本重构 AIMessage |
| 8 | Checkpointer 在 astream_events 下的写入时机与 ainvoke 不一致 | 低 | 短期记忆丢失或重复 | LangGraph 官方保证一致，集成测试覆盖跨 session 续传 |
| 9 | 流式开启后 `usage_metadata` 仅在 turn 最后一个 chunk 出现 | 高 | 单 turn 场景 token 统计正常；多 turn 场景仅最终 text turn 的 usage 被记 | 与 Phase 1 保持一致的循环内 `final_meta = ... or final_meta` 策略 |
| 10 | 并发高峰期 chunk 事件压爆 Redis Pub/Sub | 低 | Redis CPU 打高 | micro-batching（`flush_interval=50ms`, `flush_chars=16`）把 chunk 频率控制在 < 20 Hz/连接；与 Phase 1 策略一致 |

## 11. 前置依赖与后续优化

### 11.1 前置依赖（必须满足）

- **Phase 1 已上线**：ChunkEvent schema / EventBus ephemeral / SSE 端点 / 前端 chunk 消费逻辑全部就绪
- **Redis 部署验证**：生产已部署 Redis，`async_analysis.event_backend: redis` 配置生效
- **`llm.streaming_enabled=True`** 已在生产启用（Phase 1 上线时已开）

### 11.2 后续优化方向（不阻塞 Phase 2 上线）

- **Tool 中间观察事件**：将 `on_tool_start` / `on_tool_end` 转成更细粒度的 `tool` 事件（当前已有但频率较粗），提升用户对"后台在做什么"的感知
- **Text turn 边界的 think-tag 抑制**：Phase 1 的 `_astream_with_publish` 已实现 `<think>` 标签状态机抑制；Phase 2 需要同步引入（直接 import 复用），避免未来 Qwen3 开启 thinking 后泄露推理过程
- **自适应 flush 阈值**：根据网络/订阅者数量动态调整 `flush_interval` / `flush_chars`，在低并发场景下提升流畅度
- **ReAct 触发率降低**：持续优化 intent 路由的 L1/L2 命中率，把更多流量引向 DAG 路径（DAG 路径 TTFB 更稳定）

### 11.3 上线策略

与 Phase 1 一致：
1. **灰度**：先在 staging 环境开 `llm.streaming_enabled=True`，观察 1 周监控指标
2. **上线**：生产环境配置开关，初始置 `False`
3. **放量**：按 user_id 哈希灰度 10% → 50% → 100%
4. **回滚**：任一关键指标（`first_chunk_ms` P95 / `ambiguous_chunks` 率 / 错误率）超阈值，立刻置 `False` 回滚










