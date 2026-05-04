# ReAct 流式（Phase 2）前端待确认事项

> 发起方：前端
> 对应规范：[sse_react_frontend.md](./sse_react_frontend.md)
> 背景：前端已完成可行性核验，Phase 1 基础设施（chunk 累加 / message_id 绑定 / index 重试重置 / done 快照兜底）均已就绪，且 `onChunk` 未按 `node` 过滤、`ChunkEvent.node` 类型已声明为 `'report' | 'agent_final' | string`。联调前需要就以下契约点与后端对齐。

---

## 1. 缺失文档

前端仓库里只存在 [sse_react_frontend.md](./sse_react_frontend.md)，文档顶部引用的以下两篇在前端仓库**不存在**：

- `sse_react_issue.md` — 总体方案与可行性论证
- `sse_react_backend.md` — 后端实施规范

**请确认**：
- 这两篇是否在后端仓库？请给出具体路径或内容，前端需要对照 `sse_react_backend.md §2 前后端接口契约` 做镜像校验。
- 若尚未成文，请同步后端目前实际实现了/计划实现哪些事件与字段。

---

## 2. `ChunkEvent` 字段契约

前端目前的类型定义（[src/types/api.ts:349-361](../src/types/api.ts#L349-L361)）：

```ts
interface ChunkEvent {
  type: 'chunk';
  trace_id: string;
  ts: string;
  seq: number;              // 期望固定 0
  node: 'report' | 'agent_final' | string;
  message_id: string;
  delta: string;            // 增量，非累计
  index: number;            // 单 message_id 内 0-based 单调递增
  eos?: boolean;
}
```

**请逐项确认**：

1. **`node` 的实际枚举值**：ReAct 路径只会是 `"agent_final"` 一个值，还是可能有其他值（例如 `"agent_reasoning"` / `"agent_tool_text"` 之类）？前端会把白名单限定为 `{"report", "agent_final"}`，未知值会告警并丢弃。
2. **`message_id` 生成规则**：Phase 2 的 `message_id` 是否**与 Phase 1 `report` 走相同逻辑**（有 chat 持久化时等于 `assistant_message_id`，否则回退 `trace_id`）？是否保证同一条 assistant 气泡在一个 trace 里只有**唯一**的 `message_id`？
3. **`delta` 是否严格为增量**：保证**绝不**出现某一帧是累计全文？
4. **`seq` 固定为 0**：Phase 2 chunk 是否一定 `seq=0`，并且**不进入 Last-Event-ID 重放队列**？
5. **`eos` 语义**：
   - 是否每个 `message_id` 生命周期**都会**以 `eos=true` 结束（即前端可以依赖它停止打字光标）？还是可能只有 `done` 事件、没有 `eos=true` 帧？
   - `eos=true` 帧的 `delta` 是否固定为空串？
   - 发生 `error` / `aborted` 时，是否还会发 `eos=true`？

---

## 3. `index` 重置协议

前端会把 `chunk.index <= lastChunkIndex` 视为"清 buffer 重新累加"。涉及两条触发路径：

1. **tenacity 重试**：重试后新一轮推送的首 chunk `index` 从 0 开始。
2. **混输 rollback**：后端主动发 `{index: 0, delta: "", eos: false}` 重置帧。

**请确认**：

- 这两条路径在 ReAct（`node="agent_final"`）下**同样适用**？
- 重置帧的 `delta` 一定是空串，不会携带内容？
- 重试重置**只发生在同一个 `message_id` 内**，不会换 `message_id`？
- 是否存在"同一 `message_id` 内 `index` 非连续递增"（gap）的正常场景？前端当前的实现会继续 append 并打 `chunkBroken` 标记，最终靠 done 快照覆盖兜底——能否接受？

---

## 4. 事件顺序与 ReAct 路径下其他事件

Phase 1 的事件序列是：

```
status(running) → stage × N → tool × M → chunk × K → chunk(eos=true) → done
```

**请确认**：

- Phase 2 ReAct 路径下，`stage` / `tool` / `dag_task` 事件**还会不会发**？前端 UI 的时间线折叠面板依赖这些事件展示推理进度。如果 ReAct 下只有 chunk，用户只会看到占位骨架直接变成打字动画，没有中间进度。
- 是否会出现"同一个 trace 内既发 `node=report` 又发 `node=agent_final`"的场景？还是两者**互斥**（一次 trace 只会是其中一种）？
- `done` 事件在 `agent_final` 的最后一帧（`eos=true`）之后发出，这一点是否保证？

---

## 5. `error` 事件形态

前端分发器（[src/services/analysisStream.ts:338-397](../src/services/analysisStream.ts#L338-L397)）当前的 switch **没有 `case 'error'` 分支**，错误走的是 `done(status=error)` + 熔断/超时检测路径。但 `sse_react_frontend.md §4.3` 提到"收到 `error` 事件"。

**请确认**：

- 后端是否会推送独立的 `type: "error"` 事件？还是错误只通过 `done` 的 `status=error` + `error` 字段传递？
- 如果有 `error` 事件：它的 schema 是什么？是否在 `error` 之后仍会发 `done`？
- ReAct 路径下典型的错误码（例如 `EMPTY_RESPONSE`、`LLM_TIMEOUT`、`TOOL_FAILED`）有哪些？前端需要映射到用户文案。

---

## 6. 回退路径：`streaming_enabled=false`

文档 §2.5 说此时"**不发 chunk 事件**，正常走 done"，前端依赖 `GET /analyze/tasks/{trace_id}` 的 `result.report_markdown` 整体渲染。

**请确认**：

- 这个开关由后端配置（例如 `llm.streaming_enabled`），**前端不需要感知、不需要传参**？
- 灰度开启/关闭时，**不会**出现"同一个 trace 先发了 chunk 后来又切成非流式"的分裂状态？
- 快照接口 `GET /analyze/tasks/{trace_id}` 的返回 schema 在 Phase 2 下**无变化**，`result.report_markdown` 仍是最终完整文本？

---

## 7. Last-Event-ID 的契约分歧（重要）

`sse_react_frontend.md §1.2` 声称"Phase 1 SSE 连接建立（fetch + ReadableStream，带 Last-Event-ID）✅"，但前端实际实现用的是**浏览器原生 `EventSource`**（[analysisStream.ts:413](../src/services/analysisStream.ts#L413)），这带来的限制：

- `EventSource` **不支持自定义请求头**，所以前端**无法主动发送** `Last-Event-ID`。
- 断线重连时浏览器会自动带 `Last-Event-ID` 头（这是 EventSource 规范），但前端代码里没有做额外处理。
- 这一限制在 [sse_front_spec.md:255-257](./sse_front_spec.md#L255-L257) 中也有说明。

**请确认**：

- 后端是否基于浏览器自动发送的 `Last-Event-ID`（通过 `/analyze/events/{trace_id}` 的请求头）做 ring buffer 重放？
- Phase 2 `chunk` 因为 `seq=0` 本身不进重放队列——这一点双方一致吗？也就是说断线重连**只恢复 `status/stage/tool` 事件**，chunk 丢失的部分靠最终 done 快照覆盖补齐，对吗？
- `sse_react_frontend.md §4.3` 说"重连时带 Last-Event-ID（chunk 本身不重放，仅恢复 status/stage 流）"——此处的"带"指浏览器自动带，不是前端主动 set？

---

## 8. 联调准备

为了前端能独立自测，请后端提供：

1. **mock SSE fixture**：一份 `node="agent_final"` 的完整事件序列样例（含重试重置、gap、eos、done），最好是 `.ndjson` 或 `.jsonl` 可回放文件。
2. **本地联调环境**：后端能打开 `streaming_enabled=true` 并绕过 DAG 走 ReAct 兜底的测试 query 或参数。
3. **观测点**：后端侧是否有日志字段可以让前端在抓包时对齐 `index` / `message_id`（便于排查"前端收到的 chunk 顺序和后端发出顺序是否一致"）？

---

## 9. 非阻塞但希望对齐的细节

- **心跳频率**：`sse_react_frontend.md §2.2` 说"heartbeat 每 15s 一帧"，这是否和 Phase 1 一致？前端的 30s 无业务事件熔断逻辑依赖它。
- **`ts` 时区**：契约写的是 `Asia/Shanghai`，ReAct 路径下后端产出这个字段的时间戳精度与 Phase 1 一致吗？前端只用于展示/调试，不参与排序。

---

## 回复格式建议

每个问题可按 `§编号 → 是/否 + 简短说明` 回复即可，无需长篇。如需会议对齐的，请标注 `[讨论]`。
