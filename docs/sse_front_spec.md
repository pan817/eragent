# LLM 流式输出 — 前端改造规范

> 目标读者：前端开发同学
> 配套文档：[sse_issue.md](./sse_issue.md)（后端分析 + 实施方案）、[async_analyze_frontend.md](./async_analyze_frontend.md)（现有异步接口合约，本文档是它的**增量扩展**）

## 1. 背景与目标

当前前端对接的 `POST /analyze/async + SSE /events + GET /tasks/{id}` 三段式接口已经解决了长连接超时、进度反馈、断网不丢结果等问题（见 [async_analyze_frontend.md](./async_analyze_frontend.md)），但用户**从提交到看到报告第一行字的时间**（TTFB）仍然等于整段报告生成完成的时间，典型 60–120 秒，期间只能看到 "AI 正在分析..." 的 pending 气泡。

本次改造在后端开启 LLM 流式输出后，通过在已有 SSE 通道中新增一种 **`chunk` 事件**实现 token 级推送。前端改造完成后：

- **TTFB 目标 < 5 秒**：用户提交后 1–3 秒即可看到报告正文第一行字开始"边写边出"
- **端到端总时长不变**：流式是体验优化，不改变 LLM 生成总耗时
- **降级兼容**：DAG 路径有 chunk 事件；ReAct 兜底路径没有 chunk 事件，依旧按旧协议走完 `pending → done → 拉快照` 路径。前端需统一处理两种情况

本文档只描述**本次改造需要变更**的部分，不重复 [async_analyze_frontend.md](./async_analyze_frontend.md) 里已经讲过的内容。

## 2. 与现有合约的关系

| 接口 / 能力 | 变化情况 |
|---|---|
| `POST /analyze/async` 请求体 | **不变** |
| `POST /analyze/async` 响应体（`AnalysisTaskAck`） | **不变**（`trace_id / user_message_id / assistant_message_id / stream_url / poll_url`）|
| `GET /analyze/tasks/{trace_id}` 响应体（`AnalysisTaskSnapshot`） | **不变** |
| `GET /analyze/tasks/{trace_id}/events` SSE 端点 URL 与 header | **不变** |
| SSE 已有事件类型（`status` / `stage` / `tool` / `dag_task` / `report` / `heartbeat` / `done`） | **不变**，语义与字段完全兼容 |
| SSE 新增事件类型 | **`chunk`**（唯一新增） |
| Last-Event-ID 断线重放 | **不变**，但 `chunk` 事件 `seq=0` 不参与重放（设计使然）|
| `chat_messages.status` 取值 | **不变**（`pending` / `success` / `error`）|

**核心原则**：本次改造是**向下兼容**的增量。如果前端不处理 `chunk` 事件（忽略未知 `type`），所有功能仍按现状工作，只是失去了流式体验。

## 3. `chunk` 事件协议（对前端）

### 3.1 事件格式

SSE 帧示例：

```
event: chunk
id: 0
data: {"type":"chunk","trace_id":"550e8400-...","ts":"2026-04-15T10:00:03.123+08:00","seq":0,"node":"report","message_id":"10087","delta":"## 分析结论\n\n在过去 30 天","index":0,"eos":false}

event: chunk
id: 0
data: {"type":"chunk","trace_id":"550e8400-...","ts":"2026-04-15T10:00:03.175+08:00","seq":0,"node":"report","message_id":"10087","delta":"中，供应商 A 共触发","index":1,"eos":false}

...（N 个 chunk）...

event: chunk
id: 0
data: {"type":"chunk","trace_id":"550e8400-...","ts":"...","seq":0,"node":"report","message_id":"10087","delta":"","index":87,"eos":true}

event: done
id: 42
data: {"type":"done","trace_id":"550e8400-...","seq":42,"status":"ok","duration_ms":67000,"anomaly_count":3}
```

### 3.2 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | `"chunk"` | 事件类型，固定值 |
| `trace_id` | string | 任务 ID |
| `ts` | ISO8601 | 时间戳 |
| `seq` | **固定 0** | **不参与全局 seq，不参与 Last-Event-ID 重放**（与 heartbeat 一致） |
| `node` | `"report"` \| `"agent_final"` | 生成节点标识；Phase 1 只会出现 `"report"` |
| `message_id` | string | 绑定目标 ID。若请求 `auto_persist=True` 且 chat 持久化可用，等于 `assistant_message_id`（转字符串）；否则回退为 `trace_id`。前端需要同时处理两种情况：有 chat 气泡时按 `assistant_message_id` 匹配，没有时按 `trace_id` 匹配 |
| `delta` | string | **增量文本**，前端直接 append。不是累计字符串 |
| `index` | number | 该 `message_id` 内的 0-based 序号，**单调递增**；用于检测"后端重试导致的重置" |
| `eos` | boolean | `true` 表示这是该 message 的最后一个 chunk；默认 `false`。`eos=true` 的 chunk 的 `delta` 通常为空串 |

### 3.3 与其他事件的时序关系

典型 DAG 路径完整事件序列：

```
status(state=running)
  → stage(name=intent_resolved)
  → stage(name=dag_plan_ready)
  → tool(action=start, name=query_purchase_orders)
  → tool(action=end, name=query_purchase_orders)
  → ... (更多 tool) ...
  → stage(name=report_generating)
  → chunk(index=0, delta="...")          ← 首个 chunk（TTFB 计时终点）
  → chunk(index=1, delta="...")
  → chunk(index=2, delta="...")
  → ... (N 个 chunk) ...
  → chunk(index=N, eos=true, delta="")
  → report(anomaly_count=3, duration_ms=...)
  → done(status=ok, duration_ms=...)
```

ReAct 兜底路径：**没有 chunk 事件**，直接从 `stage` 跳到 `report` / `done`。前端必须支持"任务没有任何 chunk 就结束"的情况。

## 4. 前端气泡状态机

```
                                            ┌─ done.status=ok  ──────▶ SUCCESS
                                            │    （拉 task 快照，用
                                            │    result.report_markdown
                    ┌─ 收到首个 chunk ─────┐│    覆盖 chunkBuffer）
PENDING ────────────┤                       ▼│
(灰底 + spinner)    │                  STREAMING
                    │                (实时 append delta)
                    │                       │
                    │                       └─ done.status=error/aborted ─▶ ERROR
                    │                               （清空 chunkBuffer，
                    │                                显示 error.message）
                    │
                    └─ 直接收到 done ─▶ （按 done.status 分支）
                       （ReAct 兜底路径）
```

### 4.1 气泡 state 定义（伪 TS）

```ts
type AssistantBubble = {
  status: 'pending' | 'streaming' | 'success' | 'error';
  content: string;          // 最终渲染内容（done 后才填）
  chunkBuffer: string;      // streaming 中的累加 buffer
  lastChunkIndex: number;   // 已处理的最大 index，检测重试重置
  traceId: string;
  messageId: string;        // 用于匹配 chunk.message_id
  errorMsg?: string;
};
```

### 4.2 状态迁移规则

| 当前状态 | 事件 | 动作 |
|---|---|---|
| PENDING | 收到 `chunk`（首个） | `status → streaming`；处理 chunk（见 §4.3） |
| PENDING | 收到 `done(ok)` | 拉 `GET /tasks/{trace_id}`，用 `result.report_markdown` 填 `content`；`status → success`（ReAct 兜底路径走这条）|
| PENDING | 收到 `done(error/aborted)` | `status → error`；显示 `error.message` |
| STREAMING | 收到 `chunk` | 处理 chunk（见 §4.3） |
| STREAMING | 收到 `done(ok)` | 拉快照覆盖 `chunkBuffer` → `content`；`status → success` |
| STREAMING | 收到 `done(error/aborted)` | 清空 `chunkBuffer`；`status → error`；显示 `error.message`（见 §7.3） |

### 4.3 chunk 处理规则（核心逻辑）

伪代码：

```ts
function handleChunk(bubble: AssistantBubble, chunk: ChunkEvent) {
  // 规则 1：message_id 不匹配直接丢弃
  if (chunk.message_id !== bubble.messageId) return;

  // 规则 2：index 倒退视为"后端重试重置" → 清空 buffer 重新累加
  if (chunk.index <= bubble.lastChunkIndex && chunk.index === 0) {
    bubble.chunkBuffer = '';
  }

  // 规则 3：正常 append
  bubble.chunkBuffer += chunk.delta;
  bubble.lastChunkIndex = chunk.index;

  // 规则 4：首次 chunk 切换状态到 streaming
  if (bubble.status === 'pending') {
    bubble.status = 'streaming';
  }

  // 规则 5：触发 UI 重渲染（50ms debounce，见 §5）
  requestRerender(bubble);
}
```

**关键约定**：

- 规则 2 的触发条件是 `chunk.index === 0 && bubble.lastChunkIndex > 0`，表示"后端 tenacity 重试"（ReportAgent 的 `_astream_with_publish` 在每次 retry 都从 `chunk_index=0` 开始）
- 如果观察到 `chunk.index > lastChunkIndex + 1` 的 gap（中间丢失），**不要尝试猜测缺失内容**，直接按规则 3 继续 append，置 `bubble.chunkBroken=true`；`done` 到来时必须用快照覆盖（见 §6）

## 5. 增量渲染策略

### 5.1 性能问题

直接"每个 chunk 触发一次 markdown 解析 + re-render"在长报告下（几千字 + 表格 + 代码块）会导致浏览器卡顿。原因：markdown-to-HTML 解析复杂度 O(n)，每个 chunk 都重新解析整段会退化为 O(n²)。

### 5.2 推荐渲染方案（按优先级）

**方案 A：流式显示纯文本 + done 后切 markdown（推荐，Phase 1）**

```tsx
function AssistantMessage({ bubble }: { bubble: AssistantBubble }) {
  if (bubble.status === 'streaming') {
    // 纯文本显示，等宽字体 + 光标动画
    return <StreamingText text={bubble.chunkBuffer} />;
  }
  if (bubble.status === 'success') {
    // done 后一次性渲染完整 markdown
    return <ReactMarkdown>{bubble.content}</ReactMarkdown>;
  }
  // ... pending / error
}
```

- 优点：实现简单，性能稳定
- 缺点：流式过程不显示 markdown 格式（代码块、表格、标题样式）；done 后有一个"从纯文本切到 markdown"的跳变

**方案 B：50ms debounce + 实时 markdown（次选）**

```tsx
const [rendered, setRendered] = useState('');

useEffect(() => {
  const timer = setTimeout(() => setRendered(bubble.chunkBuffer), 50);
  return () => clearTimeout(timer);
}, [bubble.chunkBuffer]);

return <ReactMarkdown>{rendered}</ReactMarkdown>;
```

- 优点：流式过程即时看到 markdown 格式
- 缺点：markdown 半截状态（代码块未闭合、表格表头残缺）视觉上不友好；长报告仍有卡顿风险

**方案 C：专用流式 markdown 库**

如果团队有预算引入 `react-streaming-markdown` / `streamdown` 等支持增量 AST 的库，可以用。Phase 1 不强求。

### 5.3 UI 节奏与后端 micro-batching 的关系

后端 `_astream_with_publish` 已经做了 micro-batching（`flush_interval=50ms`, `flush_chars=16`），所以 chunk 事件到达前端的频率**约 20 Hz**。前端无需再做 throttle（方案 A），或仅需 50ms debounce（方案 B）。

## 6. 断线重连与一致性

### 6.1 核心原则

> **chunk 是体验优化，task 快照才是事实来源。任何不确定的状态都查 `GET /analyze/tasks/{trace_id}`。**

### 6.2 典型场景处理

| 场景 | 后端行为 | 前端处理 |
|---|---|---|
| **网络抖动**：streaming 中 EventSource 自动重连 | 重连后服务端先发 `status` 快照作为首帧 + 继续 live 流。**chunk 不重放** | 检测 `chunk.index` 与 `lastChunkIndex` 是否连续（`gap > 1`）；不连续则置 `bubble.chunkBroken=true`，停止 append（保留已有内容 + spinner），等 `done` 拉快照覆盖 |
| **关闭重开浏览器，任务运行中** | 前端 localStorage 拿 `trace_id` 重连 SSE → 服务端发当前 `status` 快照 + 后续 chunk | 走"快照覆盖"路径；不展示 streaming 动画；等 `done` 后拉完整 `result` 显示 |
| **关闭重开浏览器，任务已完成** | SSE 立即发 `status(ok)` + `done` | 直接 `GET /tasks/{trace_id}` 拉 `result` 渲染；无 streaming 体验但内容完整 |
| **任务失败 mid-stream** | `done(status=error)` | 清空已累加的 `chunkBuffer`；气泡置 `error`；显示 `error.message`（见 §7.3） |
| **ReportAgent 重试**（chunk index 倒退） | 重试首个 chunk `index=0` | 检测 `chunk.index === 0 && lastChunkIndex > 0` → 清空 buffer 重新累加 |

### 6.3 何时主动拉 task 快照

1. **收到 `done` 事件后（无条件，强制）**：用 `result.report_markdown` 覆盖 `chunkBuffer`。这是保证最终一致性的核心保障——即使流式过程中有 chunk 丢失、重放截断、编码异常，最终气泡内容都以快照为准。
2. **检测到 chunk gap 时**：等 `done` 一起拉（不要重复拉）
3. **30 秒无任何事件（连 heartbeat 都没有）**：拉一次确认任务存活（防 SSE 死链）

### 6.4 与 Last-Event-ID 重放的关系

- `status` / `stage` / `tool` / `dag_task` / `report` / `done` 事件 `seq > 0` → **走 ring buffer 重放**，Last-Event-ID 有效
- `chunk` / `heartbeat` 事件 `seq = 0` → **不重放**

即使 SSE 断线，重连后 `status` / `done` 总能正确恢复；最终内容一致性靠 task 快照保证。

### 6.5 与 EventSource 限制的兼容

浏览器原生 `EventSource` 不支持设置 `Last-Event-ID` 请求头。对本次改造的影响：

- `chunk` 事件本来就不走 Last-Event-ID 重放，**不受影响**
- 如果前端需要让 `status/stage/tool` 等事件的精确重放生效，需要用 `fetch + ReadableStream` 手写 SSE 解析（这是现有 [async_analyze_frontend.md](./async_analyze_frontend.md) §4.6 已经讨论过的选项，本次改造不引入新要求）

## 7. 错误处理与降级

### 7.1 降级优先级

| 场景 | 前端表现 |
|---|---|
| 浏览器不支持 SSE（极少） | 走 `pollUntilDone(traceId)` 轮询（现有降级逻辑，2 秒/次） |
| SSE 连接建立但收不到任何事件 30s | 关闭 EventSource → 走轮询兜底 |
| 收到所有事件但无 chunk（ReAct 兜底路径） | 正常等 `done` → 拉快照显示 |
| 收到 chunk 但中途断线 | 见 §6.2 网络抖动场景 |
| `done.status=error` | 见 §7.3 |

### 7.2 未知事件类型的兼容

如果后端将来新增其他事件类型（例如 Phase 2 的 `node="agent_final"` chunk），前端应该：

- **忽略未知事件类型**，不报错不崩溃
- 不假设所有 chunk 的 `node` 都是 `"report"`——用 `chunk.message_id === bubble.messageId` 作为匹配主键

### 7.3 任务失败 mid-stream 的处理

当 streaming 中收到 `done.status=error` 或 `done.status=aborted`：

```ts
function handleDoneError(bubble: AssistantBubble, done: DoneEvent) {
  // 清空已累加内容（不展示半截失败输出）
  bubble.chunkBuffer = '';
  bubble.content = done.error?.message ?? '任务失败，请重新生成';
  bubble.status = 'error';
  bubble.errorMsg = done.error?.message;
}
```

**UX 建议**：用户在失败前已经看到半段输出，UI 上建议：

- 把已显示的 streaming 内容**灰化 + 划删除线**保留 2 秒
- 然后覆盖为 error 提示 + "重新生成" 按钮
- 避免"突然消失"的视觉不连续感

### 7.4 错误处理速查表（扩展自 async_analyze_frontend.md §4.8）

| 场景 | 后端表现 | 前端处理 |
|---|---|---|
| chunk 收到但 `message_id` 不匹配 | 极少，可能是逻辑 bug | 静默丢弃，上报监控 |
| chunk 的 `delta` 是空串且 `eos=false` | 后端 micro-batch 空刷（理论上不会） | 忽略该 chunk |
| chunk 的 `index` 大幅跳跃（gap > 1） | 断线重连导致 | 置 `chunkBroken=true`；等 `done` 拉快照覆盖 |
| chunk 的 `index` === 0 且 `lastChunkIndex` > 0 | 后端 tenacity 重试 | 清空 `chunkBuffer`，重新累加 |
| 收到 `chunk(eos=true)` 但一直没 `done` | 极少，可能 SSE 异常 | 视同 streaming 完成，等 `done`；30s 内无 `done` 则拉快照 |
| 收到 `done` 但未收到过任何 chunk | ReAct 兜底路径 / 或流式失败降级 | 按现有流程：拉快照 → 拿 `result.report_markdown` 渲染 |

## 8. UI / UX 要求

### 8.1 必做项

- **pending 气泡**：灰底 + spinner + "AI 正在分析..." 文案（现状保持）
- **streaming 气泡**：白底 + 动态打字机光标（闪烁的 `▍` 或类似）追加在最后一个字符后面；文字随 chunk 增长"边写边出"
- **success 气泡**：完整 markdown 渲染（代码块、表格、标题）；右下角显示耗时（`duration_ms`）
- **error 气泡**：红底或左边框红条；显示 `error.message`；提供"重新生成"按钮（调用 `POST /analyze/async` 带 `regenerate_of` 参数）
- **stage 事件显示**：在 streaming 开始之前（pending → 收到首 chunk 的窗口期），把 `stage.name` 翻译成人话显示在 spinner 文案里：
  - `intent_resolved` → "理解意图中..."
  - `dag_plan_ready` → "规划分析任务..."
  - `tool.start` → "正在调用：[工具名]"
  - `report_generating` → "正在生成报告..."（此阶段之后很快会收到首个 chunk）

### 8.2 推荐项

- **首 chunk 抵达时的平滑过渡**：从"AI 正在分析..."直接切成流式文本会有跳变。建议 pending 文案淡出 100ms 后再开始显示 chunk 内容
- **长报告的滚动跟随**：streaming 过程中如果用户没有主动滚动，自动滚到底部；一旦用户手动滚了就停止跟随
- **chunk 丢失的视觉提示**：当 `bubble.chunkBroken=true` 时，在光标位置显示一个小的"..."占位符，提示用户"等完整结果"

### 8.3 不做项

- 不要显示 chunk 的 `index` / `seq` / `ts` 等内部字段（只用于调试）
- 不要给每个 chunk 添加动画效果（性能爆炸）
- 不要尝试"在 streaming 过程中把 markdown 半截语法修复成合法状态"（方案 A 下根本不渲染 markdown；方案 B 下让 markdown 库自己处理）

## 9. 联调与自测清单

### 9.1 后端联调命令

```bash
# 启动后端（流式打开）
cd eragent
LLM_STREAMING_ENABLED=true uvicorn api.main:app --reload --port 8000

# 提交任务
curl -X POST http://localhost:8000/analyze/async \
  -H "Content-Type: application/json" \
  -d '{"query":"过去30天三路匹配异常","user_id":"u1","auto_persist":true}'
# → 获得 trace_id

# 订阅 SSE，观察 chunk 事件按 50ms 节奏到达
curl -N http://localhost:8000/analyze/tasks/{trace_id}/events
```

### 9.2 前端自测清单

必做用例：

- [ ] 提交一个分析请求（走 DAG 静态模板），观察 pending → streaming → success 状态切换
- [ ] streaming 过程中 `chunkBuffer` 内容实时增长，UI 平滑渲染不卡顿
- [ ] `done` 后气泡最终内容 === `GET /tasks/{trace_id}` 返回的 `result.report_markdown`
- [ ] 提交一个命中 ReAct 兜底的请求（意图模糊的 query，或关闭 DAG 配置），观察 pending → success 无 streaming 直接完成
- [ ] 流式过程中刷新页面：重新用 `trace_id` 订阅 SSE，最终气泡内容完整
- [ ] 模拟网络抖动（DevTools 断网 2 秒再恢复）：断网前的 chunk 保留，断网后的 chunk 丢失，`done` 后快照覆盖为完整内容
- [ ] 提交一个后端会报错的请求：收到 `done(error)` 后气泡切到 error 状态，显示 error.message
- [ ] "重新生成"按钮：带 `regenerate_of=<id>` 提交，气泡状态切回 pending 继续走流程
- [ ] 多任务并发：同一页面连续提交 2 个任务，两个气泡独立 streaming 互不干扰
- [ ] 未知事件类型向前兼容：mock 后端发一个 `type=xxx` 的事件，前端忽略不崩

推荐用例：

- [ ] 超长报告（5000+ token）不卡浏览器
- [ ] 长时间（30+ 秒）流式过程中 heartbeat 正常到达，前端不误判断线
- [ ] 浏览器 tab 切走再切回，SSE 连接不中断

## 10. 与 async_analyze_frontend.md 的差异汇总

本文档是 [async_analyze_frontend.md](./async_analyze_frontend.md) 的**增量扩展**。以下是两份文档在前端实现上的差异点：

| 方面 | async_analyze_frontend.md | 本文档（增量） |
|---|---|---|
| SSE 事件类型 | status / stage / tool / dag_task / report / heartbeat / done | **新增 chunk** |
| pending 气泡 | "AI 正在分析..." 静态文案直到 done | 首个 chunk 到达后切 streaming，"边写边出" |
| 消息状态字段 | status: pending / success / error | **新增客户端内部状态 streaming**（不改变 `chat_messages.status` 字段取值） |
| done 后动作 | 调 `GET /tasks/{id}` 拉 result，渲染 `report_markdown` | **不变**；流式下 `report_markdown` 用于覆盖 chunkBuffer，做最终一致性保证 |
| 气泡渲染组件 | 单一 markdown 渲染 | 新增 "StreamingText"（纯文本 + 光标）组件用于 streaming 状态 |
| chunk / message_id 绑定 | — | 新增；用 `assistant_message_id` 关联 |
| 重试重置检测 | — | 新增；`chunk.index === 0 && lastChunkIndex > 0` → 清 buffer |
| chunk gap 检测 | — | 新增；`chunkBroken` 标记 + 快照兜底 |

**实现路径建议**：

1. 先实现 `chunk` 事件的解析和 `chunkBuffer` 累加，但暂不显示（黑盒验证数据链通）
2. 再加 `StreamingText` 组件渲染 `chunkBuffer`，观察打字机效果
3. 最后加重置检测 / gap 检测 / 错误处理的边界逻辑

完成后删除 pending → success 的直连分支是不必要的——**保留它作为 ReAct 兜底路径的处理**。
