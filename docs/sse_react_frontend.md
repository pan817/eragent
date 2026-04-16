# ReAct 流式输出 — 前端实施规范（Phase 2）

> 目标读者：前端开发同学
> 配套文档：
> - [sse_react_issue.md](./sse_react_issue.md) — 总体方案与可行性论证
> - [sse_react_backend.md](./sse_react_backend.md) — 后端实施规范（**本文 §2 接口契约与之完全镜像**）
> - [sse_front_spec.md](./sse_front_spec.md) — Phase 1 前端改造规范（chunk 消费器已就绪）

## 1. 范围与前置

### 1.1 本次改动范围

**仅前端、仅扩展 chunk 事件处理器**：Phase 1 已经实现完整的 SSE 消费 + chunk 累加 + 气泡绑定链路。Phase 2 只需让 chunk 处理器能够识别 `node="agent_final"` 并绑定到同一套气泡与打字 UI，**不新增 HTTP 调用、不新增 SSE 连接、不改协议字段**。

### 1.2 Phase 1 基础设施复用清单（**不改动**）

| 能力 | Phase 1 已实现 |
|---|---|
| SSE 连接建立（`fetch + ReadableStream`，带 `Last-Event-ID`）| ✅ |
| 事件分发器（按 `type` 路由到 handler）| ✅ |
| chunk 累加 buffer + index 单调性校验 | ✅ |
| 重试重置（`index <= lastChunkIndex` → 清 buffer）| ✅ |
| `done` 事件后拉取 `GET /analyze/tasks/{trace_id}` 快照 | ✅ |
| `message_id` → 气泡 DOM 绑定 | ✅ |
| 打字动画 / 占位骨架 | ✅ |
| 异常态展示（error / timeout）| ✅ |

### 1.3 本次新增 / 修改文件

| 文件 | 动作 | 说明 |
|---|---|---|
| 事件分发器（Phase 1 chunk handler 所在文件）| 修改 | 增加 `node === "agent_final"` 分支，复用同一条气泡绑定管线 |
| 气泡组件（若按 node 区分过样式）| 修改 | 确认 `"report"` 和 `"agent_final"` UI 行为一致 |
| 单元测试（chunk handler spec）| 扩展 | 新增 `agent_final` 场景用例 |
| E2E 测试（Playwright）| 扩展 | 新增 ReAct 兜底场景用例 |

### 1.4 前置依赖

- Phase 1 前端已上线
- 后端已完成 [sse_react_backend.md §6](./sse_react_backend.md#6-验收标准) 的 10 项验收

**无新第三方依赖**。

## 2. 前后端接口契约（规范性）

> ⚠️ 本章节与 [sse_react_backend.md §2](./sse_react_backend.md#2-前后端接口契约规范性) **完全镜像**。任何一方改动必须同步另一方，否则视为契约破坏。

### 2.1 ChunkEvent 事件载荷

前端从 SSE 通道收到的 `data:` 行 JSON 反序列化后，字段严格如下：

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

### 3.1 chunk handler 扩展

Phase 1 已有的 chunk handler 结构（伪代码）：

```typescript
function handleChunk(event: ChunkEvent, ctx: StreamContext) {
  if (event.node !== "report") return;  // Phase 1 仅处理 DAG
  // ... 累加、绑定气泡、打字动画
}
```

**Phase 2 修改**：把 node 判断改为白名单：

```typescript
const ACCEPTED_CHUNK_NODES = new Set(["report", "agent_final"] as const);

function handleChunk(event: ChunkEvent, ctx: StreamContext) {
  if (!ACCEPTED_CHUNK_NODES.has(event.node)) {
    console.warn("unknown chunk.node, ignored:", event.node);
    return;
  }
  // 后续逻辑完全复用 Phase 1 的累加 + 气泡绑定
}
```

**关键原则**：不按 `node` 值分派到不同 handler，而是走**统一累加管线**。`node` 仅用于调试日志与监控埋点。

### 3.2 气泡绑定 by message_id

- Phase 1 已按 `event.message_id` 查找或创建对应 assistant 气泡组件
- Phase 2 **无需扩展**：`agent_final` 的 `message_id` 与 `report` 使用相同绑定规则
- 首次收到新 `message_id` 的 chunk 时，组件进入"typing"状态（保留 Phase 1 行为）

### 3.3 index 回退检测（已有，需 Phase 2 验证）

Phase 1 已实现，Phase 2 必须确认该逻辑对 `agent_final` 同样生效：

```typescript
if (event.index <= ctx.lastChunkIndex[event.message_id] ?? -1) {
  // 重置：清 buffer、index 追踪归零
  ctx.buffer[event.message_id] = "";
  ctx.lastChunkIndex[event.message_id] = event.index;
}
ctx.buffer[event.message_id] += event.delta;
ctx.lastChunkIndex[event.message_id] = event.index;
```

**关键触发路径**：
1. tenacity 重试 → 后端重新发 `index=0` → 前端清 buffer
2. 混输 rollback → 后端发 `index=0, delta=""` → 前端清 buffer
3. 两种场景处理**完全相同**，前端无需区分

### 3.4 eos 处理

```typescript
if (event.eos) {
  ctx.streamComplete[event.message_id] = true;
  // 触发"typing 结束"动画（停止光标闪烁 / 渐出骨架）
}
```

后端保证 eos 后不再发同 `message_id` 的 chunk，但前端**必须幂等**：若错误收到后续 chunk，应忽略并告警。

### 3.5 `done` 事件与快照兜底

Phase 1 已有逻辑：

```typescript
function handleDone(event: DoneEvent) {
  // 1. 停止 SSE 连接
  // 2. 拉 GET /analyze/tasks/{trace_id}
  // 3. 用 result.report_markdown 覆盖气泡（保证最终一致性）
}
```

**Phase 2 无需改动**。关键不变量：
- 如果 streaming 正常，`accumulated delta === report_markdown`，覆盖是无感的
- 如果 streaming 中途断流，快照覆盖保证用户看到完整报告
- 如果 `streaming_enabled=false`，buffer 为空，快照直接填充

## 4. UI 行为规范

### 4.1 typing 状态

- 首个 `chunk`（无论 `node`）到达 → 进入 typing 状态，显示光标 / 省略号动画
- 累加文本按 Markdown 实时渲染（保持 Phase 1 节流策略：16ms / 50ms 任选）
- `eos=true` 到达 → 退出 typing 状态

### 4.2 占位骨架

- `POST /analyze/async` 返回 `trace_id` 后，立即在气泡位置显示骨架 / "分析中…" 占位
- 首个 `chunk` 到达时替换为真实文本（不要等 `status=running`）

### 4.3 异常态

| 场景 | UI 表现 |
|---|---|
| 长时间无 chunk（> 30s 且无 `tool` / `stage` 事件）| 占位骨架保留，文案切换为"模型响应较慢，请稍候…" |
| 收到 `error` 事件 | 清空 typing 光标，渲染错误卡片（复用 Phase 1 错误组件）|
| 收到 `done(status=failed)` | 同上；若 buffer 非空，保留 buffer 文本 + 错误标记 |
| SSE 连接断开（浏览器事件）| 尝试重连，重连时带 `Last-Event-ID`（chunk 本身不重放，仅恢复 status/stage 流）|

### 4.4 与 Phase 1 DAG 的 UX 统一

**硬性要求**：用户**无法从 UI 区分**当前流式来源是 DAG (`report`) 还是 ReAct (`agent_final`)。
- 气泡样式一致
- typing 动画一致
- Markdown 渲染规则一致
- 错误态样式一致

**唯一允许的差异**：开发者模式下 `node` 可打印到控制台供调试。

## 5. 测试清单

### 5.1 单元测试（Jest / Vitest）

针对 chunk handler 扩展：

| 用例 ID | 输入 | 断言 |
|---|---|---|
| UT-F01 | 连续 N 个 `node="agent_final"` chunk，index 0..N-1，末尾 `eos=true` | buffer == 全量 delta 拼接；typing 状态正确切换 |
| UT-F02 | `node="report"` + `node="agent_final"` 分别绑定两个 `message_id` | 两个气泡独立累加，互不干扰 |
| UT-F03 | 同一 `message_id` 的 chunk 先 index=0,1,2,3 再回退到 index=0 | buffer 清空后重新累加 |
| UT-F04 | 混输 rollback：收到 `{index:0, delta:"", eos:false}` | buffer 清空，保持 typing 状态 |
| UT-F05 | `eos=true` 之后又收到同 `message_id` chunk | 忽略，告警日志 |
| UT-F06 | 未知 `node` 值（例如 `"unknown"`）| 忽略，告警日志，不污染任何 buffer |
| UT-F07 | `done` 事件到达后拉快照，`report_markdown` 覆盖 buffer | 最终显示以快照为准 |
| UT-F08 | 无任何 chunk 的场景（`streaming_enabled=false`）| typing 状态始终不进入，`done` 后快照直接渲染 |

### 5.2 集成测试（mock SSE server）

- 启动本地 mock SSE server，预设 Phase 2 事件序列（含 `agent_final` chunks）
- 运行前端 app，断言：
  - 气泡按 `message_id` 正确绑定
  - 打字动画流畅（帧率稳定）
  - `done` 后 HTTP 请求 `GET /analyze/tasks/{trace_id}` 被发起且响应被应用

### 5.3 E2E 测试（Playwright）

新增两个场景：

| 场景 ID | 操作 | 断言 |
|---|---|---|
| E2E-F01 | 提交触发 ReAct 兜底的 query（绕过 DAG 模板）| 气泡内首个字符出现时间 < 10s；最终文本 == 后端 `report_markdown` |
| E2E-F02 | 提交后服务端模拟 tenacity 重试 | 文本完整无乱序；index 重置用户无感知（或有"重试中"提示）|

### 5.4 回归测试

**必须通过**：
- Phase 1 DAG 流式所有既有测试用例全绿
- 关闭 streaming 的非流式路径无回归
- 移动端 / 桌面端两套布局打字动画表现一致

## 6. 验收标准

前端交付必须同时满足以下条件才能上线：

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | chunk handler 正确处理 `node="agent_final"` | UT-F01 |
| 2 | `report` 和 `agent_final` 两种 node 的气泡 UI 完全一致 | 目视 + 截图比对 |
| 3 | index 回退场景 buffer 正确清空 | UT-F03、UT-F04 |
| 4 | `eos` 后幂等忽略后续同 `message_id` chunk | UT-F05 |
| 5 | 未知 `node` 不污染任何 buffer | UT-F06 |
| 6 | `done` 后快照覆盖逻辑对 `agent_final` 生效 | UT-F07 |
| 7 | `streaming_enabled=false` 时前端 UX 与 Phase 1 一致 | UT-F08 |
| 8 | E2E 场景 TTFB 可观测（浏览器 DevTools Network 面板 SSE 通道首个 `chunk` 事件时间）| E2E-F01 |
| 9 | 重试场景用户最终看到完整文本、无乱序 | E2E-F02 |
| 10 | Phase 1 DAG 流式**无回归** | 既有 Phase 1 测试全绿 |

**联调节奏**：
1. 后端完成 [sse_react_backend.md §6](./sse_react_backend.md#6-验收标准) 的 10 项
2. 前端基于本规范独立实现，先用 mock SSE server 自测（UT + 集成测试）
3. 前后端联调，跑通 E2E-F01、E2E-F02
4. 生产灰度按 `llm.streaming_enabled` 开关控制（后端侧）

**验收通过后**：PR 合并、更新 MEMORY / CHANGELOG、关闭对应技术债条目。




