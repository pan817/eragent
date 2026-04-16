# ReAct 流式输出 — 后端实施规范（Phase 2）

> 目标读者：后端开发同学
> 配套文档：
> - [sse_react_issue.md](./sse_react_issue.md) — 总体方案与可行性论证
> - [sse_react_frontend.md](./sse_react_frontend.md) — 前端实施规范（**本文 §2 接口契约与之完全镜像**）
> - [sse_issue.md](./sse_issue.md) — Phase 1 DAG 流式方案（基础设施盘点）

## 1. 范围与前置

### 1.1 本次改动范围

**仅后端、仅 ReAct 兜底路径**：把 [modules/p2p/agent.py:562](../modules/p2p/agent.py#L562) 的 `await agent.ainvoke(...)` 改造为基于 LangGraph `astream_events(version="v2")` 的流式推送，最终产生与 Phase 1 ReportAgent 同协议的 `ChunkEvent`。

### 1.2 Phase 1 基础设施复用清单（**不改动**）

| 组件 | 文件 | 用途 |
|---|---|---|
| `ChunkEvent` schema | [core/tasks/schemas.py](../core/tasks/schemas.py) | 事件协议 |
| `EventBus.publish(ephemeral=True)` | [core/tasks/events.py](../core/tasks/events.py) / [events_redis.py](../core/tasks/events_redis.py) | 跳过 ring buffer、只推订阅者 |
| SSE 端点 `/analyze/tasks/{trace_id}/events` | [api/routes/analyze_async.py](../api/routes/analyze_async.py) | 透传 chunk 事件 |
| `_current_trace` ContextVar | [core/observability/middleware.py](../core/observability/middleware.py) | trace_id 上下文 |
| `current_assistant_message_id` ContextVar | [core/tasks/context.py](../core/tasks/context.py) | message_id 上下文 |
| `settings.llm.streaming_enabled` | [config/settings.py](../config/settings.py) | 流式总开关（P2PAgent 走主 llm）|
| `build_chat_model(streaming=...)` | [modules/p2p/model_factory.py](../modules/p2p/model_factory.py) | 已透传 `streaming=True` |
| `record_span("model", ...)` | [core/observability/middleware.py](../core/observability/middleware.py) | span 埋点 |

### 1.3 本次新增 / 修改文件

| 文件 | 动作 | 说明 |
|---|---|---|
| [modules/p2p/agent.py](../modules/p2p/agent.py) | 修改 | 新增 `_astream_react_with_publish` 方法；`analyze()` 重试循环内按 `streaming_on` 分支调用 |
| [tests/unit/test_react_streaming.py](../tests/unit/test_react_streaming.py) | 新增 | 模式检测单元测试 |
| [tests/integration/test_sse_react_chunk_flow.py](../tests/integration/test_sse_react_chunk_flow.py) | 新增 | SSE 端到端集成测试 |
| [tests/integration/test_e2e_streaming.py](../tests/integration/test_e2e_streaming.py) | 扩展 | 新增 ReAct 场景用例 |

### 1.4 前置依赖

- Phase 1 已上线（满足）
- Redis 生产部署（满足）
- `llm.streaming_enabled=True` 已在生产启用（满足）

**无新前置依赖**。

## 2. 前后端接口契约（规范性）

> ⚠️ 本章节与 [sse_react_frontend.md §2](./sse_react_frontend.md#2-前后端接口契约规范性) **完全镜像**。任何一方改动必须同步另一方，否则视为契约破坏。

### 2.1 ChunkEvent 事件载荷

后端推送到 EventBus（ephemeral=True）、SSE 通道透传给前端，字段严格如下：

| 字段 | 类型 | 必填 | 值域 / 规范 |
|---|---|---|---|
| `type` | string | ✓ | 固定字面量 `"chunk"` |
| `trace_id` | string | ✓ | 与 `POST /analyze/async` 返回的 `trace_id` 一致 |
| `ts` | string | ✓ | ISO8601（Asia/Shanghai）— 后端产出时间 |
| `seq` | int | ✓ | **固定 0**，不参与全局 seq、不入 ring buffer、不参与 Last-Event-ID 重放 |
| `node` | string | ✓ | 枚举：`"report"`（Phase 1 DAG 路径）\| `"agent_final"`（Phase 2 ReAct 路径）|
| `message_id` | string | ✓ | 气泡绑定 ID：有 chat 持久化时等于 `assistant_message_id`；否则回退为 `trace_id` |
| `delta` | string | ✓ | **增量文本**（非累计）；前端 `accumulated += delta` |
| `index` | int | ✓ | 单 `message_id` 内 0-based 单调递增 |
| `eos` | bool | — | 默认 `false`；stream 结束时最后一帧为 `true` |

**示例载荷**：
```json
{
  "type": "chunk",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "ts": "2026-04-16T10:00:03.123+08:00",
  "seq": 0,
  "node": "agent_final",
  "message_id": "10087",
  "delta": "## 分析结论\n\n在过去 30 天中，",
  "index": 0,
  "eos": false
}
```

### 2.2 事件到达顺序保证

单个 `trace_id` 生命周期内，前端会收到的事件序列：

```
status(running) → stage × N → tool × M → chunk × K → chunk(eos=true) → done
                              ↑          ↑
                              heartbeat 每 15s 一帧（与 chunk 并行无序）
```

**后端保证**：
- 同一 `message_id` 的 chunk 按 `index` 单调递增发出
- `eos=true` 之后不会再发同 `message_id` 的 chunk
- `done` 事件在所有 chunk（含 eos）之后

**不保证**：
- 跨 `trace_id` 的顺序
- chunk 与 heartbeat 之间的相对顺序

### 2.3 index 单调性 & 重置协议

- **正常场景**：`index` 从 0 开始严格 +1 递增
- **重试重置**：tenacity 重试触发 → 新一轮推送的首 chunk `index` **重新从 0 开始**
- **混输 rollback**：首 chunk 被误判为 text 后续发现是 tool turn → 后端主动发 `{index: 0, delta: "", eos: false}` 重置帧

**前端契约**：观察到 `chunk.index <= lastChunkIndex` → **清空 buffer、从 0 开始重新累加**。

### 2.4 node 枚举语义

| node 值 | 路径 | message_id 绑定 | 首次出现时机 |
|---|---|---|---|
| `"report"` | DAG → ReportAgent | `assistant_message_id` 优先 | DAG 各 task 完成后 |
| `"agent_final"` | ReAct → P2PAgent 最终 text turn | `assistant_message_id` 优先 | ReAct 最后一轮 LLM 调用开始 |

**前端契约**：两种 node 绑定到**同一个** assistant 气泡（由 `message_id` 决定），UI 行为完全一致。

### 2.5 回退路径

| 场景 | 后端行为 | 前端契约 |
|---|---|---|
| `llm.streaming_enabled=false` | **不发 chunk 事件**，正常走 `done` | 收到 `done` 后拉 `GET /analyze/tasks/{trace_id}` 的 `result.report_markdown` 整体渲染 |
| streaming 开启但失败（EMPTY_RESPONSE 等） | 发 `error` 事件 + `done(status=failed)` | 展示错误态，不累加不完整内容 |
| EventBus 不可用 | 降级为 non-streaming，同上 | 透明，前端无感知 |

### 2.6 禁止项（明确排除）

- ❌ 后端**不得**推送 `delta` 为累计字符串（只能是增量）
- ❌ 后端**不得**跨 `message_id` 复用 `index`
- ❌ 后端**不得**在 `eos=true` 后继续发同 message_id 的 chunk
- ❌ 前端**不得**将 chunk 入 Last-Event-ID 重放队列（seq=0 的语义就是不重放）
- ❌ 前端**不得**基于 `ts` 排序（`ts` 仅供展示与调试）

## 3. 实现要点

### 3.1 新增方法：`_astream_react_with_publish`

位置：[modules/p2p/agent.py](../modules/p2p/agent.py) `P2PAgent` 类内部，紧邻现有 `analyze` 方法。

**职责**：
- 用 `agent.astream_events(version="v2")` 替代 `agent.ainvoke`
- 按 first-chunk 模式检测判定每个 LLM turn 是 text 还是 tool
- 仅对 text turn 按 micro-batch 推送 chunk
- 返回与 `ainvoke` 等价的 `{"messages": [...], "usage_metadata": ...}`

**算法要点**：

1. **模式判定**：`on_chat_model_start` 事件重置 `current_turn_mode=None`；在 `on_chat_model_stream` 的首个非空 chunk 判定：
   - `tool_call_chunks` 非空 → `tool`，丢弃本轮
   - `content` 非空 → `text`，本轮起实时推送
   - 两者皆空 → 继续等（计入 `ambiguous_chunks` 指标）

2. **micro-batching**（与 Phase 1 `_astream_with_publish` 参数一致）：
   - `flush_interval = 0.05` 秒
   - `flush_chars = 16` 字符
   - 任一阈值触达 → `_flush()`

3. **eos 时机**：`on_chat_model_end` 触发且 `current_turn_mode == "text"` → 发 `eos=true`

4. **最终结果提取**：`on_chain_end` 事件 `name == "LangGraph"`（顶层图）→ 从 `event["data"]["output"]["messages"]` 拿完整消息列表；若缺失则用 accumulated 文本重构 `AIMessage`

5. **usage_metadata**：循环内 `final_meta = getattr(chunk, "usage_metadata", None) or final_meta`

### 3.2 集成到 `analyze()` 重试循环

**位置**：[modules/p2p/agent.py:559-674](../modules/p2p/agent.py#L559) 的 `for attempt in range(max_retries):` 循环内部，在 `result = await agent.ainvoke(...)` 处分支。

**分支守卫**（短路组合，任一不满足降级到 `ainvoke`）：

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
        result = await agent.ainvoke(
            {"messages": invoke_messages}, config=invoke_config,
        )
```

**不变动的逻辑**：
- 重试循环本体（`max_retries`, 指数退避）
- `result["messages"]` 提取、`content` JSON 兜底解析、长期记忆写入、`AnalysisResult` 构造

### 3.3 混输 rollback 实现

在 `_astream_react_with_publish` 的循环尾部（`on_chat_model_end` 之后），若检测到**当前 turn 模式为 text 但随后 LangGraph 又进入 tool 调用**（即下一次 `on_chat_model_start` 前出现 `on_tool_start`），说明模式被误判：

```python
# 伪代码
if current_turn_mode == "text" and chunk_index > 0 and next_is_tool:
    # 发重置帧
    bus.publish(trace_id, {
        "type": "chunk", "trace_id": trace_id,
        "ts": now_cn().isoformat(), "seq": 0,
        "node": "agent_final", "message_id": message_id,
        "delta": "", "index": 0, "eos": False,
    }, ephemeral=True)
    accumulated.clear()
    chunk_index = 0
```

### 3.4 `<think>` 标签抑制（复用 Phase 1 状态机）

直接从 [modules/p2p/report_agent.py](../modules/p2p/report_agent.py) 的 `_astream_with_publish` 抽取 `<think>` 状态机（`_process_text` 函数），**不复制代码**：

- **推荐方案**：把 `_process_text` + 边界缓冲逻辑提到 `core/tasks/stream_utils.py`，两处 import
- **次选方案**：`P2PAgent._astream_react_with_publish` 内联一份，标注来源和同步义务

**本次实施推荐方案**（新增 `core/tasks/stream_utils.py`，代价 ~30 行，换来单点维护）。

## 4. 配置、回滚、监控

### 4.1 配置

**复用 `settings.llm.streaming_enabled`**（不新增配置项）：

| 配置 | 环境变量 | 默认 | 作用 |
|---|---|---|---|
| `llm.streaming_enabled` | `LLM_STREAMING_ENABLED` | `true` | 主 LLM（P2PAgent ReAct）流式总开关 |
| `llm_fast.streaming_enabled` | `LLM_FAST_STREAMING_ENABLED` | `true` | 小模型（ReportAgent）流式总开关（Phase 1）|

两个开关**相互独立**：关闭 `llm.streaming_enabled` 不影响 DAG 路径流式。

### 4.2 回滚路径

| 触发 | 动作 | 影响面 |
|---|---|---|
| ReAct 流式异常 | `LLM_STREAMING_ENABLED=false` | 回到 `agent.ainvoke`；DAG 路径不受影响 |
| 前端解析 chunk 异常 | 同上 | 同上 |
| EventBus 失联 | 代码内自动降级（`_bus_ready=False`）| 本次调用不推 chunk，下次恢复后自愈 |

**回滚不需重启服务**（`get_settings()` 读取时效取决于部署侧缓存）。

### 4.3 监控指标

在 `_astream_react_with_publish` 外层 `record_span("model", ...)` 上挂：

| 属性 | 类型 | 含义 |
|---|---|---|
| `react_streaming` | bool | 本次调用是否启用流式 |
| `first_chunk_ms` | float | 从 `astream_events` 开始到首个 text chunk 推出的毫秒数 |
| `text_turns` | int | 本次 ReAct 中 text turn 数（期望 = 1）|
| `tool_turns` | int | 本次 ReAct 中 tool turn 数（循环深度指标）|
| `ambiguous_chunks` | int | 首 chunk 既无 content 也无 tool_call_chunks 的计数 |
| `rollback_triggered` | bool | 是否因混输触发过 index 重置 |

### 4.4 告警阈值

| 指标 | 阈值 | 行动 |
|---|---|---|
| `first_chunk_ms` P95 | > 8000ms | 排查 ReAct 前置 tool 深度 / Qwen 首 token 延迟 |
| `text_turns > 1` 出现率 | > 1% | 排查 prompt 稳定性，可能模型反复补充 |
| `ambiguous_chunks` 出现率 | > 5% | 排查 LangChain 版本兼容性 |
| `rollback_triggered` 出现率 | > 2% | 排查 Qwen 混输发生率 |

## 5. 测试清单

### 5.1 单元测试 `tests/unit/test_react_streaming.py`

mock `agent.astream_events` 返回预设事件序列，验证推送行为：

| 用例 ID | fake 事件序列 | 断言 |
|---|---|---|
| UT-B01 | 单轮 text chunks | 所有 chunk 按 index 递增推出，末尾 `eos=true`，accumulated == 完整回复 |
| UT-B02 | 1 轮 tool → 1 轮 text | tool 轮不推送，text 轮正常推送 |
| UT-B03 | 3 轮 tool → 1 轮 text | 仅最后一轮推送，`text_turns=1`, `tool_turns=3` |
| UT-B04 | text 轮内混入 tool_call_chunks | 触发 rollback，发 `index=0,delta=""` 重置帧 |
| UT-B05 | 首 chunk 空 content + 空 tool_call | `ambiguous_chunks` 计数递增，下一个非空 chunk 正确判定 |
| UT-B06 | `streaming_enabled=false` | 不调用 `astream_events`，走 `ainvoke` 原路径，EventBus 零事件 |
| UT-B07 | `_current_trace.get() is None` | 自动降级到 `ainvoke` |
| UT-B08 | 首次 astream 抛异常，重试成功 | 第二轮 `chunk_index` 从 0 重新开始 |
| UT-B09 | `on_chain_end` 缺失 messages | 用 accumulated 重构 `AIMessage`，返回值仍有效 |
| UT-B10 | `usage_metadata` 仅末 chunk 出现 | `final_meta` 采集成功 |

### 5.2 集成测试 `tests/integration/test_sse_react_chunk_flow.py`

- 启动 FastAPI test client + `MemoryEventBus`
- 构造触发 ReAct 兜底的 query（意图路由 L3 → `COMPREHENSIVE` 无实体）
- 订阅 SSE 事件流，断言：
  - 事件序列：`status → stage×N → tool×M → chunk×K → chunk(eos) → done`
  - chunk 事件字段：`node="agent_final"`，`message_id` 正确绑定，`index` 严格递增，`seq=0`
  - `MemoryEventBus.buffered(trace_id)` **不包含 chunk 事件**（ephemeral 语义）
- 验证 `done` 事件后拉 `GET /analyze/tasks/{trace_id}` 返回的 `report_markdown == accumulated delta`

### 5.3 端到端测试 `tests/integration/test_e2e_streaming.py`（扩展）

- 真实 Qwen `llm` 主模型
- 构造 DAG 无法覆盖的探索类 query
- `POST /analyze/async` + `curl -N` 订阅 SSE
- 采集：TTFB（提交 → 首个 chunk）、首 tool 完成时间、总时长（提交 → done）
- 断言：
  - TTFB < 10s（容许 ReAct 前置 tool 占用）
  - 总时长与关闭 streaming 的 baseline ±10% 之内

### 5.4 覆盖率要求

Phase 2 新增/改动代码单元测试覆盖率 **≥ 90%**（保持项目现有标准）。

## 6. 验收标准

后端交付必须同时满足以下条件才能进入前端联调：

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | `_astream_react_with_publish` 按 §2 契约推送 ChunkEvent | UT-B01–UT-B10 全绿 |
| 2 | ReAct 兜底路径在 `streaming_enabled=true` 时产出 `node="agent_final"` chunk | 集成测试 |
| 3 | 重试场景 `index` 从 0 重置 | UT-B08 |
| 4 | 混输 rollback 发重置帧 | UT-B04 |
| 5 | `streaming_enabled=false` 回到 `ainvoke` 原路径，EventBus 零 chunk | UT-B06 |
| 6 | `accumulated delta == result.report_markdown` | 集成测试 |
| 7 | `first_chunk_ms` / `react_streaming` / `text_turns` / `tool_turns` / `ambiguous_chunks` / `rollback_triggered` 指标可在 trace 表查询 | 手动查 `trace_spans` |
| 8 | Phase 1 DAG 路径流式**无回归** | 既有 Phase 1 测试全绿 |
| 9 | 新增/改动代码覆盖率 ≥ 90% | `pytest --cov --cov-fail-under=90` |
| 10 | 真实 Qwen e2e 场景 TTFB < 10s | `test_e2e_streaming.py` 人工跑 5 次取 P95 |

**验收通过后**：标注 `llm.streaming_enabled` 生产默认值，与前端团队约定联调窗口。





