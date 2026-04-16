# ReAct 流式（Phase 2）后端答复前端待确认事项

> 回复方：后端
> 对应问题：[sse_react_backend_questions.md](./sse_react_backend_questions.md)
> 配套规范：[sse_react_backend.md](./sse_react_backend.md) / [sse_react_frontend.md](./sse_react_frontend.md)
> 回复日期：2026-04-16

## 摘要

**全部 9 项可对齐**，其中 4 处需要前端规范 `sse_react_frontend.md` 做小幅修订（详见本文末尾"§10 前端规范建议修订"），契约本身无分歧。

---

## §1 文档位置

**✅ 两篇文档均已在后端仓库落盘**：

- [docs/sse_react_issue.md](./sse_react_issue.md) — 总体方案与可行性论证（387 行，11 章节）
- [docs/sse_react_backend.md](./sse_react_backend.md) — 后端实施规范（334 行，6 章节）

这两份与 `sse_react_frontend.md` 在同一 `eragent/docs/` 目录下。请前端直接拉取后端仓库对应路径，或通过代码仓库的文档镜像同步。`sse_react_backend.md §2 接口契约` 与 `sse_react_frontend.md §2` **已确认字段与规范完全镜像**（diff 只有跨引链接与视角措辞差异，非规范差异）。

---

## §2 ChunkEvent 字段契约

### 2.1 `node` 实际枚举值

**✅ ReAct 路径只会是 `"agent_final"` 一个值**。

- 不会有 `"agent_reasoning"` / `"agent_tool_text"` 等其他值
- 推理内容（`<think>...</think>`）在后端流式状态机内部吞掉，不会推送到前端（复用 Phase 1 的 `_astream_with_publish` 的 `<think>` 抑制机制）
- Tool turn 的 token 完全不推送（first-chunk 模式检测 → 判定为 tool → 丢弃整轮）
- 前端白名单 `{"report", "agent_final"}` **正确**，未知值告警丢弃的策略**正确**

### 2.2 `message_id` 生成规则

**✅ 与 Phase 1 完全一致**：

- 后端从 `current_assistant_message_id` ContextVar 取值（由异步任务启动时注入）
- 有 chat 持久化 → `assistant_message_id`（字符串化后的整数 ID）
- 无 chat 持久化 → 回退 `trace_id`
- **保证同一条 assistant 气泡在一个 trace 里只有唯一的 `message_id`**：ContextVar 在任务启动时 set 一次，整个生命周期不变

### 2.3 `delta` 严格增量

**✅ 严格增量，绝不累计**。后端实现（`_astream_with_publish` 及 Phase 2 的 `_astream_react_with_publish`）的 `_flush` 逻辑：
```python
delta = "".join(pending); pending.clear()
```
每次 flush 后 `pending` 清空，不可能产生累计帧。

### 2.4 `seq` 固定 0 & 不进重放

**✅ 确认两点**：

- Phase 2 chunk `seq` 固定 0（与 Phase 1 一致）
- **不进入 Last-Event-ID 重放队列**：`EventBus.publish(..., ephemeral=True)` 明确跳过 ring buffer（见 [core/tasks/events.py](../core/tasks/events.py) 的 `_ring_push` 分支）

### 2.5 `eos` 语义

需要分三种场景回复：

- **正常结束**：**✅ 每个 `message_id` 生命周期都会以 `eos=true` 结束**，前端可依赖它停止打字光标
- **`eos=true` 帧的 `delta` 不一定是空串**：⚠️ 这是与前端提问的偏差点。后端实现 `_flush(eos=True)` 时若 `pending` 尚有残留字符，会与 `eos=true` 一起发出——即 `delta` 可能非空。**前端必须在 `eos=true` 帧先 `accumulated += delta` 再停止累加**，不能简单忽略 eos 帧的 delta
- **`error` / `aborted` 场景**：❌ **不保证发 `eos=true`**。任务中途失败或被中止时后端直接进入 `done(status=error|aborted)`，不补发 eos。前端必须把 `done` 事件作为停止打字的**最终权威信号**，不能只依赖 `eos`

---

## §3 `index` 重置协议

### 3.1 两条重置路径在 ReAct 下是否同样适用

**✅ 是**。`_astream_react_with_publish` 严格复用 Phase 1 的 index 语义：

- **tenacity 重试**（`P2PAgent.analyze` 重试循环）：新一轮推送首 chunk `index=0`
- **混输 rollback**（first-chunk 模式误判后发现是 tool）：后端主动发 `{index: 0, delta: "", eos: false}` 重置帧

### 3.2 重置帧 `delta` 是否一定空串

⚠️ **分两种场景，前端处理逻辑一致但需知晓差异**：

- **混输 rollback 重置帧**：`delta` **固定空串**（见 `sse_react_backend.md §3.3` 伪代码）
- **tenacity 重试的 index=0**：**不是"重置帧"，而是新一轮推送的正常第一帧**，`delta` 是该轮的第一个真实增量，**可能非空**

**对前端意味着**：无需区分两种情况。统一处理规则：
```
if (event.index <= lastChunkIndex[msg_id]) {
    accumulated[msg_id] = "";       // 清 buffer
    lastChunkIndex[msg_id] = -1;    // 重置追踪
}
accumulated[msg_id] += event.delta;  // append（空串也安全）
lastChunkIndex[msg_id] = event.index;
```

### 3.3 重置是否只发生在同一 `message_id` 内

**✅ 是**。`message_id` 来自 ContextVar，在任务启动时 set 一次，整个 trace 生命周期不变。不会换 `message_id`。

### 3.4 `index` 是否可能非连续递增（gap）

**❌ 后端保证严格 +1 递增，正常场景不会有 gap**。原因：`_flush` 内 `chunk_index` 仅在 `bus.publish` 实际发生时自增：

```python
if bus is not None and (delta or eos):
    bus.publish(...)
    chunk_index += 1
```

- 空 `pending` 且非 eos → 不发事件 → index 不动
- 事件实际推出 → index 严格 +1

**但前端当前的"遇到 gap 继续 append 并打 `chunkBroken` 标记、done 快照覆盖兜底"的策略 → ✅ 可接受**。因为：
1. 网络丢包、SSE 中间代理重组都可能导致前端观察到 gap（即使后端发出的是连续 index）
2. 这个兜底策略不会丢内容（最终以 `report_markdown` 为准），不会污染 UI

---

## §4 事件顺序与 ReAct 路径下其他事件

### 4.1 ReAct 下 `stage` / `tool` / `dag_task` 事件是否还发

| 事件 | ReAct 路径 | 说明 |
|---|---|---|
| `stage` | **✅ 会发** | Orchestrator 按阶段发 stage 事件（意图路由 / 执行 / 完成）|
| `tool` | **✅ 会发** | TimingMiddleware 为每个 tool 调用发 `action=start` 和 `action=end`，ReAct 比 DAG 触发更多 tool 事件 |
| `dag_task` | **❌ 不会发** | ReAct 路径没有 DAG 任务概念，该事件仅 DAG 路径使用 |

**前端时间线折叠面板不会空**：ReAct 下中间进度由 `stage` + 多个 `tool` 事件呈现，用户能看到"正在查 PO / 正在跑三路匹配 / 正在生成报告"这类进度。

### 4.2 `node=report` 与 `node=agent_final` 是否互斥

**✅ 完全互斥**。一次 trace 的执行路径在 Orchestrator 层级就已经二选一（见 [core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py) `_analyze_inner` 的路由决策）：

- DAG 路径命中 → 全程走 ReportAgent，只会有 `node="report"` chunks
- ReAct 兜底触发 → 全程走 P2PAgent.analyze，只会有 `node="agent_final"` chunks

**不会同时出现两种 node 的 chunk**。

### 4.3 `done` 是否在最后一帧 `eos=true` 之后发

**✅ 保证**。后端发送序列：

```
... → chunk(eos=true) → done(status=ok|error|aborted)
```

`done` 事件总是在 EventBus 订阅链路的最后，`_astream_react_with_publish` 返回到 `analyze()` → 继续跑 `AnalysisResult` 构造 → 任务框架发 `done`。这中间 chunk 流已经完全结束。

---

## §5 `error` 事件形态

### 5.1 是否存在独立 `type: "error"` 事件

**❌ 不存在**。已核对 [core/tasks/schemas.py:79-158](../core/tasks/schemas.py#L79)，事件类型白名单：

```
status / stage / tool / dag_task / report / heartbeat / chunk / done
```

**没有 `ErrorEvent` 类**。Phase 1 和 Phase 2 都通过 `done(status=error, error=ErrorInfo)` 传递错误。

### 5.2 错误传递路径

**统一走 `DoneEvent`**：

```json
{
  "type": "done",
  "trace_id": "...",
  "seq": N,
  "status": "error",
  "duration_ms": 12345.6,
  "error": {
    "code": "AGENT_INVOKE_FAILED",
    "message": "P2P 分析失败: LLM 调用超时",
    "retry_count": 3
  }
}
```

**前端现有实现（switch 无 `case 'error'`、靠 `done(status=error)` + 熔断/超时）→ ✅ 正确，无需改动**。

### 5.3 ReAct 路径典型错误码

| 错误码 | 触发场景 | 建议 UX 文案 |
|---|---|---|
| `AGENT_INVOKE_FAILED` | ReAct 3 次重试均失败（[agent.py:698](../modules/p2p/agent.py#L698)） | "分析未能完成，请稍后重试" |
| `EMPTY_RESPONSE` | 流式返回空文本（Qwen 偶发兼容性异常） | "模型响应为空，请稍后重试" |
| `LLM_TIMEOUT` | LLM 单次调用超过 `report.timeout_seconds` | "模型响应超时，请稍后重试" |
| `CANCELLED` | 用户主动取消任务 | "已取消" |

其他更细粒度的 tool 级错误（SQL 失败、规则引擎异常）不会透到 `done.error`，而是作为 `tool` 事件的 `status="error"` 被前端已有逻辑消费。

### 5.4 `error` 之后是否仍会发 `done`

**不适用**（不存在独立 error 事件）。`done(status=error)` 本身就是终态事件，后续不会再发任何事件。

---

## §6 回退路径：`streaming_enabled=false`

### 6.1 前端是否需要感知、传参

**✅ 前端完全不需要感知、不需要传参**。

- 开关由后端 `settings.llm.streaming_enabled`（环境变量 `LLM_STREAMING_ENABLED`）控制
- 前端不感知后端是否开启流式；开关关闭时后端自动回到 `agent.ainvoke` 路径，EventBus 不发 chunk
- 前端已有的 `done` + 快照兜底逻辑自动覆盖该场景

### 6.2 灰度切换是否会导致同一 trace 分裂

**✅ 不会**。守卫条件在 trace 启动时快照决定：

```python
streaming_on = bool(
    self._settings.llm.streaming_enabled  # 启动时读取
    and _trace_id
    and _bus_ready
)
```

- 同一 trace 生命周期内 `streaming_on` 不变
- 即使灰度放量过程中配置被改，已经启动的 trace 沿用启动时的决策
- 不会出现"前半段发 chunk、后半段切成非流式"的分裂

### 6.3 快照接口 schema 是否变

**✅ Phase 2 下快照接口返回 schema 完全不变**：

- `GET /analyze/tasks/{trace_id}` 仍返回完整 `AnalysisResult`
- `result.report_markdown` 仍是最终完整文本
- 不新增不删除字段

---

## §7 Last-Event-ID 契约分歧

前端指出 `sse_react_frontend.md §1.2` 声称"fetch + ReadableStream，带 Last-Event-ID"与前端实际使用 `EventSource` 的实现不符。**确认前端描述正确，后端规范里的措辞有误，需修订**（见 §10）。

### 7.1 后端是否基于浏览器自动发送的 `Last-Event-ID` 做 ring buffer 重放

**✅ 是**。SSE 端点（[api/routes/analyze_async.py](../api/routes/analyze_async.py) `stream_task_events`）的逻辑：

- 解析请求头 `Last-Event-ID`（浏览器 EventSource 断线重连时自动携带）
- 基于该值从 EventBus 的 ring buffer 中回放 `seq > Last-Event-ID` 的事件
- 仅对 `seq > 0` 的持久事件生效（status / stage / tool / dag_task / report / done）

### 7.2 断线重连恢复策略

**✅ 双方一致**：

- `chunk` 事件 `seq=0` + `ephemeral=True` → **绝不进 ring buffer 也绝不重放**
- 断线重连**只恢复 `status/stage/tool/dag_task` 事件**
- 丢失的 chunk 部分靠 `done` 事件后拉 `GET /analyze/tasks/{trace_id}` 的 `report_markdown` 覆盖补齐
- `heartbeat` 同样 `seq=0` 不重放（行为一致）

### 7.3 "带 Last-Event-ID" 的主体

**✅ 浏览器自动带，不是前端主动 set**。前端使用 `EventSource` 的情况下：

- `EventSource` 规范：浏览器内核在断线重连时自动在请求头加 `Last-Event-ID`
- 前端代码**不需要**也**不应**手动 set（`EventSource` 不支持自定义请求头）
- 后端契约与浏览器行为一致，无需特殊处理

### 7.4 对"重放"的精确定义

- 后端 ring buffer 存储所有 `seq > 0` 的事件（按 trace_id 分片，容量可配）
- 重连请求携带 `Last-Event-ID: N` 时，后端补发 `seq in (N, N+K]` 的历史事件，然后继续实时流
- 若 `N` 过旧导致 buffer 已淘汰对应事件，后端会降级为"只发实时流"，前端可通过快照接口兜底

---

## §8 联调准备

### 8.1 mock SSE fixture

**⚠️ 将在后端完成 Phase 2 开发后提供**，具体交付清单：

- `tests/integration/fixtures/sse_react_agent_final_normal.jsonl` — 正常流程（单 text turn）
- `tests/integration/fixtures/sse_react_agent_final_retry.jsonl` — tenacity 重试场景（index 从 0 重置）
- `tests/integration/fixtures/sse_react_agent_final_rollback.jsonl` — 混输 rollback（index=0 空帧）
- `tests/integration/fixtures/sse_react_agent_final_error.jsonl` — 错误终态（无 eos + `done(status=error)`）

每个 fixture 是一行一个 JSON 事件，可直接灌入前端 mock SSE server 回放。交付时机：后端 UT-B01–B10 全绿后 24 小时内。

### 8.2 本地联调环境

**✅ 可提供**：

- 后端开启 `LLM_STREAMING_ENABLED=true` 环境变量
- 绕过 DAG 走 ReAct 的测试 query 示例：

  ```bash
  curl -X POST http://localhost:8000/analyze/async \
    -H "Content-Type: application/json" \
    -d '{"query": "帮我找最近有异常的前 3 家供应商并综合说明原因和趋势", "user_id": "dev"}'
  ```

  这类探索型 query 不会命中 L1/L2 意图路由的静态模板，L3 分类为 `COMPREHENSIVE` 且缺少核心实体 → 走 ReAct 兜底

- 若需强制走 ReAct（用于测试），可通过 debug 参数 `"_force_react": true`（联调期临时提供，上线前移除）

### 8.3 观测点与日志对齐

**✅ 可提供以下对齐能力**：

1. **trace_spans 表查询**：`GET /traces/{trace_id}/spans` 返回本次 trace 的所有 span，其中 `model` span 的 attrs 包含：
   - `react_streaming` / `first_chunk_ms` / `text_turns` / `tool_turns` / `ambiguous_chunks` / `rollback_triggered`
2. **后端日志字段**：`_astream_react_with_publish` 内每次 publish 均打 debug 日志（开 `LOG_LEVEL=DEBUG`）：
   ```
   chunk.publish trace_id=... message_id=... index=3 delta_chars=16 eos=false
   ```
   前端抓包时按 `index` 对齐，定位丢包/乱序点
3. **span 导出**：联调期可开启 `EXPORT_SPANS_TO_STDOUT=true` 让后端把 span 以 JSON 格式打到 stdout，便于并行抓后端+前端日志

---

## §9 非阻塞但希望对齐的细节

### 9.1 心跳频率

**✅ 与 Phase 1 一致：15 秒一帧**。

- 实现位置：[api/routes/analyze_async.py](../api/routes/analyze_async.py) `stream_task_events` 的 heartbeat 调度
- 配置项：`async_analysis.heartbeat_interval_seconds`（默认 15）
- Phase 2 不改变此行为；前端 30s 无业务事件熔断逻辑**可继续依赖**

### 9.2 `ts` 时区与精度

**✅ 与 Phase 1 一致**：

- 时区：`Asia/Shanghai`（由 `core.time_utils.now_cn()` 产出，全链路统一）
- 精度：微秒级 ISO8601（例如 `2026-04-16T10:00:03.123456+08:00`）
- 前端仅用于展示/调试、不参与排序 → ✅ 正确，后端不保证跨事件的 `ts` 严格递增（micro-batch flush 间隔 50ms，同批内可能 ts 相等或倒序）

---

## §10 前端规范建议修订

核对答复后发现 `sse_react_frontend.md` 有 4 处措辞需修订。后端规范 `sse_react_backend.md` 同步调整。**这些修订不改变实质协议，只是校正描述的准确性**：

| # | 文件 / 位置 | 原文 | 建议修订 |
|---|---|---|---|
| 1 | `sse_react_frontend.md §1.2` | "SSE 连接建立（fetch + ReadableStream，带 Last-Event-ID）" | "SSE 连接建立（浏览器 EventSource；断线重连时 `Last-Event-ID` 由浏览器自动携带）" |
| 2 | `sse_react_frontend.md §3.4` | "后端保证 eos 后不再发同 `message_id` 的 chunk，但前端必须幂等" | 补充："eos=true 帧的 `delta` 可能非空（尾部残留），前端必须先 `accumulated += delta` 再停止累加；`error/aborted` 场景不保证发 eos，以 `done` 为最终停止信号" |
| 3 | `sse_react_frontend.md §4.3` | "收到 `error` 事件 → 清空 typing 光标，渲染错误卡片" | "收到 `done(status=error)` → 清空 typing 光标，渲染错误卡片（项目无独立 `error` 事件类型）" |
| 4 | `sse_react_frontend.md §3.3` | 保持不变（index 重置规则本身正确） | 可在注释里补一句："index=0 既可能来自 tenacity 重试（delta 非空），也可能来自混输 rollback（delta 为空串），前端处理逻辑统一无需区分" |

**接下来的动作**：
- 若前端同意以上修订，请后端同步更新 `sse_react_frontend.md` 与 `sse_react_backend.md`（保持 §2 镜像一致）
- 后端会在下一轮 push 把修订一并提交，PR 标题 `docs(sse): Phase 2 契约校正与前端答复`

---

## 回复摘要表（便于快速过）

| § | 问题 | 答复 |
|---|---|---|
| §1 | 缺失的两篇文档在哪 | 后端仓库 `docs/sse_react_issue.md` + `docs/sse_react_backend.md` |
| §2.1 | node 枚举 | 仅 `"agent_final"` |
| §2.2 | message_id 生成 | 与 Phase 1 一致，trace 内唯一 |
| §2.3 | delta 增量 | 严格增量 |
| §2.4 | seq=0 不重放 | 确认 |
| §2.5 | eos 语义 | 正常结束必发；delta 可能非空；error/aborted 不保证发 |
| §3.1 | 重置路径适用 | ✅ |
| §3.2 | 重置帧 delta 空串 | rollback 为空；retry 为新帧内容可能非空 |
| §3.3 | 重置在同 message_id 内 | ✅ |
| §3.4 | index gap 兜底策略 | ✅ 后端保证无 gap；前端兜底策略可接受 |
| §4.1 | ReAct 下 stage/tool/dag_task | stage/tool 发；dag_task 不发 |
| §4.2 | node=report vs agent_final 互斥 | ✅ 完全互斥 |
| §4.3 | done 在 eos 之后 | ✅ |
| §5 | error 事件形态 | 无独立 error 事件；靠 `done(status=error)` |
| §6.1 | 前端是否感知开关 | ❌ 不感知 |
| §6.2 | 灰度切换分裂 | 不会 |
| §6.3 | 快照 schema 变更 | 无变更 |
| §7 | Last-Event-ID | 浏览器自动携带，chunk 不重放靠快照兜底 |
| §8 | 联调准备 | fixture / 环境 / 观测点均可提供 |
| §9.1 | 心跳频率 | 15s |
| §9.2 | ts 时区 | Asia/Shanghai 微秒级 |
| §10 | 前端规范修订 | 4 处小幅校正（不改协议）|

**所有 9 项可对齐，无阻塞性分歧。请前端基于本答复启动独立开发，后端将在完成 §10 修订后交付 mock fixture。**








