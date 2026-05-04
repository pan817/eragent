# LLM 流式输出方案设计（SSE）

> 目标读者：后端开发同学、架构评审人
> 配套文档：[sse_front_spec.md](./sse_front_spec.md)（前端改造规范）、[async_analyze_frontend.md](./async_analyze_frontend.md)（现有异步接口合约）、[llm_issue.md](./llm_issue.md)（LLM 性能优化矩阵）

## 1. 背景与现状

eragent 当前的前后端交互采用"提交-订阅/轮询-取结果"三段式异步模型（定义于 [api/routes/analyze_async.py](../api/routes/analyze_async.py)）：

1. `POST /analyze/async` 立即返回 202 + `trace_id`；后台任务走 DAG 或 ReAct 兜底跑分析
2. `GET /analyze/tasks/{trace_id}/events`（SSE）推送 `status / stage / tool / dag_task / report / heartbeat / done` 事件
3. `GET /analyze/tasks/{trace_id}` 拉完整 `AnalysisResult`

这套协议解决了长连接被反代 idle timeout 切断、前端刷新丢结果、无进度反馈等问题，但没有解决体验上最大的痛点：**用户从提交到看到报告正文第一行字的时间（TTFB）等于整段报告生成完成的时间**，典型 60–120 秒。

根因在 [modules/p2p/report_agent.py:183](../modules/p2p/report_agent.py#L183) 的 `response = await llm.ainvoke(prompt)`。这是 DAG 路径的关键路径终点——工具并行跑完后，ReportAgent 把各节点 JSON 整合成 Markdown 报告。整个生成过程是一次性的 `ainvoke`，用户必须等 `max_tokens`（默认 4096）完全生成才能看到任何文字。

同类阻塞也存在于 [modules/p2p/agent.py:562](../modules/p2p/agent.py#L562) 的 ReAct 兜底路径（`await agent.ainvoke(...)`），不过 ReAct 是兜底路径，DAG 才是主路径流量。

业界标准解法是 LLM 流式输出：前端每收到一个 token 就 append 到气泡里，TTFB 从"整段时长"降到"首 token 时间"（典型 1–3 秒），端到端总时长不变但体感改善显著。[docs/llm_issue.md](./llm_issue.md) 的优化矩阵已把流式列为 #2 优先级。

## 2. 可行性结论

**结论：可行，改造面很小（约 1–2 人日核心开发 + 1 人日前端 + 测试）。无架构级风险。**

三个关键支撑：

1. **SSE 侧全链路已生产级就绪**。EventBus 双后端（`MemoryEventBus` + `RedisEventBus`，见 [core/tasks/events.py](../core/tasks/events.py) / [events_redis.py](../core/tasks/events_redis.py)）、Last-Event-ID 断线重放、15 秒 heartbeat、seq 单调递增、`StreamingResponse`、三道健壮性闸（握手前校验 / 连接后首帧合成 / 退出前终态兜底）——这些都不需要改。
2. **技术栈原生支持流式**。LangChain 1.2.0 + langchain-openai ≥0.2.0（见 [pyproject.toml](../pyproject.toml)）原生支持 `llm.astream()` 和 `agent.astream_events(version="v2")`。Qwen DashScope OpenAI 兼容接口支持 SSE 流式返回。
3. **真正的阻塞点只有一处关键路径**。ReportAgent 是单次 `llm.ainvoke` 调用（无 tool-calling 循环），把它替换为 `llm.astream()` + 累加 + 按 chunk 推到 EventBus 即可。ReAct 兜底路径（`agent.py:562`）复杂一些但不是主路径，可以 Phase 2 再做。

预期收益：TTFB 从 60–120s 降到 1–3s（首 token 时间）。端到端总时长不变，但用户体感改善显著；[docs/llm_issue.md](./llm_issue.md) 的优化矩阵里标记为"感知大幅改善"。

## 3. 基础设施盘点

流式改造要接入的所有基础设施均已就绪，盘点如下：

| 能力 | 文件 / 实现 | 当前状态 |
|---|---|---|
| 异步任务生命周期 | [core/tasks/registry.py](../core/tasks/registry.py) `TaskRegistry` | ✅ 5 态（queued/running/ok/error/aborted）+ Semaphore 并发控制 + 内存 TTL + DB 快照回落 |
| 事件总线（单 worker） | [core/tasks/events.py](../core/tasks/events.py) `MemoryEventBus` | ✅ `asyncio.Queue` + 环形缓冲 + 订阅者管理 + 终态 sentinel |
| 事件总线（多 worker） | [core/tasks/events_redis.py](../core/tasks/events_redis.py) `RedisEventBus` | ✅ Pub/Sub + Redis LIST 环形缓冲 + 跨 worker 共享 |
| 事件协议 | [core/tasks/schemas.py](../core/tasks/schemas.py) `BaseEvent` / `StatusEvent` / `StageEvent` / `ToolEvent` / `DagTaskEvent` / `ReportEvent` / `HeartbeatEvent` / `DoneEvent` | ✅ **需新增 `ChunkEvent`** |
| SSE 端点 | [api/routes/analyze_async.py](../api/routes/analyze_async.py) `stream_task_events` | ✅ Last-Event-ID 断线重放 + 15 秒 heartbeat + 三道健壮性闸 + `X-Accel-Buffering: no` |
| 任务快照端点 | [api/routes/analyze_async.py](../api/routes/analyze_async.py) `get_task_snapshot` | ✅ 内存 TTL miss 回落 `trace_runs` 表 |
| LLM 客户端 | [modules/p2p/model_factory.py](../modules/p2p/model_factory.py) `build_chat_model` | ⚠️ `ChatOpenAI` 未显式启用 `streaming=True`，但 `astream()` 仍可工作 |
| LangChain 流式 API | langchain-core ≥1.2 | ✅ `llm.astream()` / `agent.astream_events(v="v2")` |
| 可观测性 | [core/observability/middleware.py](../core/observability/middleware.py) `record_span` | ✅ 支持 `model`/`tool` span；流式下仅需补一条 attribute |
| 持久化 | [core/memory/tables.py](../core/memory/tables.py) `reports.result_json` / `report_markdown` | ✅ 无需改 schema，流式累加后整体落库 |

**结论**：除了新增 `ChunkEvent` 类型和给 `EventBus.publish` 加一个 `ephemeral` 参数，其他基础设施零改动。

## 4. 阻塞点分析

整个分析链路上，"等 LLM 全部返回"的硬同步点只有两处，两处都是 `await ainvoke`。

### 4.1 阻塞点 A：ReportAgent 的 ainvoke（主路径、高影响、低改造成本）

位置：[modules/p2p/report_agent.py:183](../modules/p2p/report_agent.py#L183)

```python
response = await llm.ainvoke(prompt)
content = response.content if hasattr(response, "content") else str(response)
```

上下文：

- ReportAgent 是 DAG 路径的关键路径终点（[core/orchestrator/dag/executor.py](../core/orchestrator/dag/executor.py)），工具并行跑完后串行调用一次 ReportAgent 生成 Markdown
- 单次 LLM 调用，**无 tool-calling 循环**
- 外层包了 tenacity `AsyncRetrying`（3 次重试 + 指数退避 + 240s timeout）
- 走 `record_span("model")` 手动埋点（ReportAgent 不经过 LangChain middleware）

改造代价：**极低**。把 `ainvoke` 替换成 `astream` 并加一个 chunk 累加器 + 推事件总线即可，重试语义保留不变。

### 4.2 阻塞点 B：ReAct 兜底路径的 agent.ainvoke（次要路径）

位置：[modules/p2p/agent.py:562](../modules/p2p/agent.py#L562)

```python
result: dict[str, Any] = await agent.ainvoke(
    {"messages": invoke_messages},
    config=invoke_config,
)
```

上下文：

- 走 LangGraph `create_agent` 构造的 ReAct agent
- 内部是"LLM → tool call → LLM → tool call → ... → 最终消息"多轮循环
- 只有最后一轮不含 `tool_calls` 的 LLM 消息才是给用户看的最终 Markdown

改造代价：**中等**。必须用 `agent.astream_events(version="v2")` 取代 `ainvoke`，并按 `on_chat_model_end` 事件的 message 是否含 `tool_calls` 判断是否 flush 该轮 chunk。但这种做法等于"每轮 LLM 跑完才决定是否发 chunk"，退化为 message-level 而非 token-level 流式。

### 4.3 路径流量占比与改造优先级

- **DAG 路径**（主路径）：前置 L1/L2/L3 意图路由若命中静态模板即走 DAG（覆盖三路匹配、价格差异、付款合规、供应商绩效四大场景）。改造 ReportAgent = 覆盖 DAG 全部流量。
- **ReAct 兜底**（次要）：意图路由未命中 / DAG 执行失败时触发。次要流量。

**优先级**：Phase 1 只做 A（覆盖主路径），Phase 2 视需要再做 B。

## 5. 设计决策

三项已锁定的设计决策（与业务方对齐）：

| 决策 | 选项 | 理由 |
|---|---|---|
| 流式粒度 | **仅最终报告**（ReportAgent 的 Markdown 输出） | ReAct 中间推理的 JSON / tool_call 消息对用户无价值，继续按现有 `stage` / `tool` 事件粗粒度推送即可；token 级流式只给用户真正看到的最终 Markdown |
| 接口合约演进 | **复用 `/analyze/async + /events` 通道 + 新增 `chunk` 事件类型** | 零新端点，前端 SSE 连接不变；chunk 只是事件协议的一个新 `type` |
| 部署形态 | **多 worker（生产）** | 强制使用 `RedisEventBus`；chunk 事件对 Redis Pub/Sub 有特殊处理（见 §7.2） |

## 6. ChunkEvent 协议

在 [core/tasks/schemas.py](../core/tasks/schemas.py) 新增 `ChunkEvent`，字段如下：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | `Literal["chunk"]` | ✓ | 事件类型 |
| `trace_id` | str | ✓ | 继承 `BaseEvent` |
| `ts` | ISO8601 str | ✓ | 继承 `BaseEvent` |
| `seq` | int | ✓ | **固定 0**，不参与全局 seq；不入重放缓冲 |
| `node` | `"report"` \| `"agent_final"` | ✓ | 标识 chunk 来自哪个生成节点；Phase 1 只会出现 `"report"` |
| `message_id` | str | ✓ | 绑定目标 ID。`auto_persist=True` 且 chat 持久化可用时等于 `assistant_message_id`；否则回退为 `trace_id`。前端据此绑定气泡（有 chat 气泡按 assistant_message_id，没有按 trace_id）|
| `delta` | str | ✓ | **增量文本**（非累计），前端 append |
| `index` | int | ✓ | 单 message 内 0-based 序号；调试 + 检测重置 |
| `eos` | bool | — | end-of-stream 标记；默认 `False`，流结束时最后一帧为 `True` |

**关键协议决策**：

1. **`seq = 0`**：与 `heartbeat` 同样语义——不进 ring buffer、不参与 Last-Event-ID 重放。原因：一段 Markdown 报告通常 200–500 chunk，全部塞进 ring buffer 会撑爆 Redis；而重放半段 markdown 是糟糕体验，不如让前端在 `done` 后用 `GET /tasks/{trace_id}` 的 `result.report_markdown` 覆盖兜底。
2. **`index` 单调递增**：同一 `message_id` 内 `index` 从 0 开始单调递增。若前端观察到 `index` 回退到 0 或小于 `lastChunkIndex`，视为"后端重试"（tenacity 触发），必须清空累加 buffer 重新开始。
3. **`delta` 是增量**：不是累计字符串。前端直接 `accumulated += delta`。
4. **示例事件载荷**：

```json
{
  "type": "chunk",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "ts": "2026-04-15T10:00:03.123+08:00",
  "seq": 0,
  "node": "report",
  "message_id": "10087",
  "delta": "## 分析结论\n\n在过去 30 天中，",
  "index": 0,
  "eos": false
}
```

## 7. 分阶段实施方案

### Phase 1：DAG 主路径流式（必做）

按依赖顺序分为 5 个子任务，预计 1–2 人日：

#### 7.1 新增 ChunkEvent schema

文件：[core/tasks/schemas.py](../core/tasks/schemas.py)

在 `BaseEvent` 之后新增：

```python
class ChunkEvent(BaseEvent):
    type: str = "chunk"
    node: str  # "report" | "agent_final"
    message_id: str
    delta: str
    index: int
    eos: bool = False
```

`seq` 继承 `BaseEvent` 但**固定 0**（不参与全局 seq、不入重放缓冲）。

#### 7.2 EventBus 支持 ephemeral 发布

文件：[core/tasks/events.py](../core/tasks/events.py) / [core/tasks/events_redis.py](../core/tasks/events_redis.py)

`EventBusProtocol` 扩展：

```python
def publish(
    self,
    trace_id: str,
    event: dict[str, Any],
    *,
    ephemeral: bool = False,  # 新增
) -> None: ...
```

- `MemoryEventBus.publish`：`ephemeral=True` 时跳过 `buf.append`，只 push 到订阅者 queue
- `RedisEventBus.publish`：`ephemeral=True` 时跳过 `LPUSH+LTRIM`，只 `PUBLISH`
- 默认 `False`，**现有所有调用点行为不变**

**为什么不让 chunk 进缓冲**：

- 一段 markdown 报告通常 200–500 chunks，扩大 buffer 到 5000+ 会撑爆 Redis 内存
- 断线后重放半段 markdown 是糟糕体验，不如让前端 `done` 后拉一次 `GET /tasks/{trace_id}` 拿完整 `report_markdown` 覆盖
- 与"重要事件可重放、轻量事件不重放"的现有语义（heartbeat `seq=0`）一致

#### 7.3 model_factory 启用 streaming

文件：[modules/p2p/model_factory.py](../modules/p2p/model_factory.py)

`build_chat_model` 显式传 `streaming=True`（langchain-openai 支持；Qwen DashScope OpenAI 兼容接口支持 SSE 流式）。保留 non-streaming 分支以防个别 provider 不支持（通过 settings 控制）。

#### 7.4 ReportAgent 流式化

文件：[modules/p2p/report_agent.py](../modules/p2p/report_agent.py)

在 `ReportAgent.generate` 中新增 `_astream_with_publish` 辅助方法：

```python
async def _astream_with_publish(
    self, llm, prompt, trace_id, message_id,
    node="report", flush_interval=0.05, flush_chars=16,
) -> tuple[str, dict | None]:
    """astream + micro-batching publish。返回 (完整文本, usage_metadata)"""
    from core.tasks.events import get_event_bus
    from core.time_utils import now_cn

    bus = get_event_bus()
    accumulated, pending = [], []
    last_flush = time.monotonic()
    chunk_index = 0
    final_meta = None

    def _flush(eos=False):
        nonlocal chunk_index, last_flush
        delta = "".join(pending); pending.clear()
        if bus and (delta or eos):
            bus.publish(trace_id, {
                "type": "chunk", "trace_id": trace_id,
                "ts": now_cn().isoformat(), "seq": 0,
                "node": node, "message_id": message_id,
                "delta": delta, "index": chunk_index, "eos": eos,
            }, ephemeral=True)
            chunk_index += 1; last_flush = time.monotonic()

    async for chunk in llm.astream(prompt):
        text = getattr(chunk, "content", "") or ""
        if text:
            pending.append(text); accumulated.append(text)
        if (sum(len(s) for s in pending) >= flush_chars
                or time.monotonic() - last_flush >= flush_interval):
            _flush()
        final_meta = getattr(chunk, "usage_metadata", None) or final_meta
    _flush(eos=True)
    return "".join(accumulated), final_meta
```

集成进现有 `AsyncRetrying` 循环（流式只要求 `trace_id` 存在；`message_id` 用 `trace_id` 做 fallback）：

```python
_trace_ctx = _current_trace.get()
_trace_id = _trace_ctx.trace_id if _trace_ctx is not None else None
_message_id = get_current_message_id() or _trace_id
streaming_on = bool(settings.llm_fast.streaming_enabled and _trace_id)
async for attempt in AsyncRetrying(...):
    with attempt:
        with record_span("model", str(model_name)) as model_attrs:
            model_attrs["streaming"] = streaming_on
            if streaming_on:
                content, usage = await self._astream_with_publish(
                    llm, prompt, trace_id, message_id, node="report")
            else:
                response = await llm.ainvoke(prompt)
                content = response.content
                usage = getattr(response, "usage_metadata", None)
```

`trace_id` / `message_id` 从 contextvars 拿（与 `record_span` 一致）；[api/routes/analyze_async.py](../api/routes/analyze_async.py) 启动 task 时 set。

#### 7.5 测试

- [tests/unit/test_report_agent_streaming.py](../tests/unit/test_report_agent_streaming.py)：fake `astream` 产生 N 个 chunk，断言 bus 收到 N+1 个 ChunkEvent（含 eos），accumulated 内容 == 最终落库 `report_markdown`
- [tests/integration/test_sse_chunk_flow.py](../tests/integration/test_sse_chunk_flow.py)：启后端 + 真实 SSE 端点，断言 chunk 按 index 递增到达，`ephemeral=True` 不入 `MemoryEventBus.buffered`
- [tests/integration/test_e2e_streaming.py](../tests/integration/test_e2e_streaming.py)：真实 `llm_fast`（见前置依赖），打印 TTFB；对比 Phase 0 baseline

### Phase 2：ReAct 兜底流式（选做）

仅在 Phase 1 上线后、用户反馈 ReAct 兜底场景占比 > 10% 时再做。方案选型：

- **方案 A**：`agent.astream_events(v="v2")` + 按 `on_chat_model_end` 的 `message.tool_calls` 判断是否 flush 该轮 chunk。代价：退化到 message-level 流式。
- **方案 B（推荐）**：ReAct 阶段保持非流式（拿结构化结论），新增一次独立 LLM 调用做 final synthesis，对该调用流式（复用 `_astream_with_publish`，`node="agent_final"`）。代价：多一次 LLM 调用，成本 +30%，但体验最好。

Phase 2 倾向方案 B。

### Phase 3：上线策略

- **配置开关**：`llm.streaming_enabled`（默认 `True`）。上线前置默认 `False`，灰度打开。
- **监控指标**：`model` span 新增 `streaming=true/false` tag；`AnalysisResult.duration_ms` 已有，新增 `first_chunk_ms`（从 task 启动到首个 chunk 发出的时间）。
- **回滚**：发现 Qwen 流式异常（断流、空 chunk、`usage_metadata` 丢失），关闭 `streaming_enabled` 配置即回到原路径。

## 8. 关键文件清单

Phase 1 需要改动的文件：

| 文件 | 改动 | 估计行数 |
|---|---|---|
| [core/tasks/schemas.py](../core/tasks/schemas.py) | 新增 `ChunkEvent` 类 | +10 |
| [core/tasks/events.py](../core/tasks/events.py) | `EventBusProtocol.publish` 加 `ephemeral` 参数；`MemoryEventBus.publish` 实现 ephemeral 分支 | +15 |
| [core/tasks/events_redis.py](../core/tasks/events_redis.py) | `RedisEventBus.publish` 加 `ephemeral` 参数 + 分支 | +10 |
| [config/settings.py](../config/settings.py) | 新增 `llm.streaming_enabled: bool = True` | +5 |
| [config/config.yaml](../config/config.yaml) | 新增同名配置项（注释默认值） | +3 |
| [modules/p2p/model_factory.py](../modules/p2p/model_factory.py) | `build_chat_model` 透传 `streaming=True` | +3 |
| [modules/p2p/report_agent.py](../modules/p2p/report_agent.py) | 新增 `_astream_with_publish`；`generate` 分支调用 | +60 |
| [api/routes/analyze_async.py](../api/routes/analyze_async.py) | 启动 task 时 set context vars（`trace_id` / `message_id`） | +10 |
| [tests/unit/test_report_agent_streaming.py](../tests/unit/test_report_agent_streaming.py) | 新增 | +120 |
| [tests/integration/test_sse_chunk_flow.py](../tests/integration/test_sse_chunk_flow.py) | 新增 | +100 |
| [docs/sse_issue.md](./sse_issue.md) | 本文档 | — |
| [docs/sse_front_spec.md](./sse_front_spec.md) | 前端改造规范 | — |

SSE 端点代码（[api/routes/analyze_async.py](../api/routes/analyze_async.py) 的 `stream_task_events`）**无需改动**——`ChunkEvent` 作为 dict 通过 EventBus 推送，SSE 端点透传 `_format_event` 即可识别。

前端需要改动的文件：见 [docs/sse_front_spec.md](./sse_front_spec.md)。

## 9. 风险与权衡

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| 1 | Qwen DashScope 兼容接口流式偶发断流 / 空 chunk | 中 | 用户看到半段报告后卡住 | `astream` 异常 → tenacity 重试，前端检测 index 倒退即清 buffer；设 `llm.streaming_enabled` 开关支持一键回滚 |
| 2 | `usage_metadata` 在流式下只在最后一个 chunk 里返回 | 高 | 观测 span 里 input/output tokens 可能缺失 | 循环内保留 `final_meta = getattr(chunk, "usage_metadata", None) or final_meta` 累积；最后一个非空值即 usage |
| 3 | tenacity 重试时已发的 chunks 无法撤回 | 中 | 前端显示错乱内容 | 协议层约定：重试首 chunk 的 `index` 从 0 重新开始；前端规则"`index <= lastChunkIndex` 视为重置，清空 buffer" |
| 4 | ReportAgent 被其他地方调用（非 DAG 路径）时 contextvars 未注入 | 低 | 流式降级为 non-streaming | `trace_id` 取不到时走原 `ainvoke` 分支；不影响正确性 |
| 5 | chunk 事件频率过高撑爆 Redis Pub/Sub 带宽 | 低 | Redis CPU 打高 | micro-batching（`flush_interval=50ms` + `flush_chars=16`）；典型长报告约 200 chunk，频率 < 20 Hz，Redis 轻松扛 |
| 6 | 多 worker 场景 chunk 事件在 worker A 发布，订阅者在 worker B | — | 本来就靠 Redis Pub/Sub 跨 worker，无额外风险 | `RedisEventBus` 已实现，无需改 |
| 7 | Last-Event-ID 断线重放时 chunk 丢失 | 高（设计使然） | 断线重连前的 delta 不可恢复 | 协议层明确 chunk `seq=0` 不重放；前端收到 `done` 后拉 task 快照覆盖；最终一致性由 `report_markdown` 保证 |
| 8 | LangChain middleware `wrap_model_call` 与 astream 交互未知 | 低 | ReportAgent 不走 middleware，不受影响 | ReportAgent 手动 `record_span`，与现状一致 |
| 9 | 前端浏览器原生 `EventSource` 无法带 `Last-Event-ID` header | 已知 | 无法做精确重放 | 对 chunk 不影响（本来就不重放）；对 status/stage 重放需用 `fetch + ReadableStream`（现有文档已提示） |
| 10 | 流式开启后 `max_tokens=4096` 消耗仍然很大，Qwen 速率低导致尾延迟 | 中 | 总时长没降，但 TTFB 已经改善 | 流式不解决总时长问题；需配合 [docs/llm_issue.md](./llm_issue.md) #1 + #3（换小模型 + 降 `max_tokens`） |

## 10. 验证方案

### 10.1 单元测试（必须覆盖）

- `tests/unit/test_report_agent_streaming.py`
  - fake LLM 产出 N 个 `AIMessageChunk`，断言 EventBus 收到 N 个 chunk 事件 + 1 个 eos 事件
  - 断言每个 chunk 的 `index` 严格递增，`seq` 固定 0
  - 断言 `accumulated` content 与 `_flush` 累计 delta 拼接结果一致
  - 断言 `accumulated` content 与 `ReportAgent.generate` 返回值一致（用于落库 `report_markdown` 的保证）
  - mock `ephemeral=True` → 断言 `MemoryEventBus.buffered(trace_id)` 不包含 chunk 事件，只有 `status`/`stage`/`done`
  - 流式失败 → tenacity 重试 → 断言重试轮 `chunk.index` 从 0 重置

- `tests/unit/test_event_bus_ephemeral.py`
  - `MemoryEventBus.publish(ephemeral=True)`：live 订阅者能收到，`buffered` 数组不包含该事件
  - `MemoryEventBus.publish(ephemeral=False)`：现有行为不变
  - `RedisEventBus.publish(ephemeral=True)`：mock redis 断言 `PUBLISH` 被调，`LPUSH` / `LTRIM` 未被调

- `tests/unit/test_chunk_event_schema.py`
  - `ChunkEvent` 字段齐全、默认值正确（`eos=False`）
  - `seq=0` 可以 pass Pydantic 校验

### 10.2 集成测试

- `tests/integration/test_sse_chunk_flow.py`
  - 启动 FastAPI test client + `MemoryEventBus`
  - 模拟 ReportAgent 跑流式（fake LLM）
  - 订阅 `/analyze/tasks/{trace_id}/events`，断言按顺序收到：`status` → `stage × N` → `chunk × M` → `chunk(eos)` → `done`
  - 断言 chunk 事件的 `type` / `node` / `message_id` / `delta` / `index` / `eos` 字段

### 10.3 端到端测试

- `tests/integration/test_e2e_streaming.py`
  - 真实 Qwen `llm_fast` 调用（前置依赖：已切换到真小模型）
  - `POST /analyze/async` 提交一个简单 query
  - `curl -N` 订阅 SSE
  - 记录：从提交到首个 chunk 到达的时间（TTFB） vs 从提交到 done 的时间（总时长）
  - 断言 TTFB < 5s（目标），总时长与 Phase 0 baseline 相当

### 10.4 手动联调

```bash
# 终端 1：启服务（关闭流式 baseline）
LLM_STREAMING_ENABLED=false uvicorn api.main:app --reload
curl -X POST http://localhost:8000/analyze/async -d '{"query":"..."}'
# 记录 TTFB

# 终端 2：启服务（打开流式）
LLM_STREAMING_ENABLED=true uvicorn api.main:app --reload
curl -X POST http://localhost:8000/analyze/async -d '{"query":"..."}'
curl -N http://localhost:8000/analyze/tasks/{trace_id}/events
# 观察 chunk 事件按 50ms 节奏到达
```

### 10.5 覆盖率要求

整个 Phase 1 新增 / 改动代码的单元测试覆盖率 ≥ 90%（保持项目现有标准）。

## 11. 前置依赖

**强烈建议在开启流式之前先落地**：

- **`llm_fast` 切换到真小模型**（qwen-flash / qwen-turbo / qwen3-30b-a3b 等）。见 [docs/llm_issue.md](./llm_issue.md)#L7。当前 `llm_fast` 的 YAML 注释掉了大部分字段，`Settings.from_yaml` 会把 `llm` 的字段全量镜像过来，导致 ReportAgent 实际跑的是 `qwen3-max`（和主 Agent 同一个大模型）。不先换小模型，流式带来的 TTFB 改善会被"大模型慢"抵消大半。
- **Redis 部署验证**：生产已部署 Redis（异步任务、trace 存储复用），无需额外搬砖；仅需确认 `async_analysis.event_backend: redis` 配置生效。

**可选的配合项（不阻塞 Phase 1 上线）**：

- 降 `report.max_output_tokens` 到 2000（[docs/llm_issue.md](./llm_issue.md) #3）
- Prompt caching（[docs/llm_issue.md](./llm_issue.md) #4）
