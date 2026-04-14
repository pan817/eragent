# Analyse 异步接口前端对接文档

> 适用对象：前端开发同学
> 后端设计细节见 [async_analyze_design.md](./async_analyze_design.md)

## 1. 背景与目标

### 1.1 问题
当前 `POST /analyze` 是同步接口，后端链路包含多轮 LLM 调用 + DAG 执行，最长耗时可达 900 秒。现状前端体验有三个痛点：

1. **请求长时间挂起**：HTTP 连接保持 10 分钟以上，用户只能傻等，任何刷新/断网都会丢掉结果
2. **无进度反馈**：聊天气泡只能显示"思考中..."静态文案，无法告诉用户"正在查 PO / 正在分析价格差异"等阶段信息
3. **反代/网关超时**：Nginx 等反向代理 idle timeout 通常 60s，长请求容易被中断

### 1.2 目标
改成异步任务模型：

- 提交请求后**立即返回** `trace_id`（HTTP 202），不再长连接等待
- 前端可选择**两种方式**获取进度与结果：
  - **SSE 推送**（推荐）：服务端主动推阶段事件，体验最好
  - **轮询兜底**：定时 GET 快照接口，适用于 SSE 不可用的场景
- 聊天界面立即显示 assistant 气泡（`status=pending`），随事件/轮询实时更新

### 1.3 向下兼容
旧的同步 `POST /analyze` 接口**继续保留**，行为完全不变。前端可以用 feature flag 分批切换到新接口。

---

## 2. 整体流程

```
前端                                    后端
 │  POST /analyze/async                  │
 │ ─────────────────────────────────────▶│
 │                                       │ 生成 trace_id
 │                                       │ 预落 chat_messages
 │                                       │   user (status=success)
 │                                       │   assistant (status=pending)
 │                                       │ TaskRegistry.submit
 │                                       │
 │  202 {                                │
 │    trace_id,                          │
 │    status: "queued",                  │
 │    session_id,                        │
 │    user_message_id,                   │
 │    assistant_message_id,              │
 │    poll_url, stream_url               │
 │  }                                    │
 │ ◀─────────────────────────────────────│
 │                                       │
 │  渲染 user 气泡 +                      │
 │  pending assistant 气泡                │
 │                                       │
 │  EventSource(stream_url)              │
 │ ─────────────────────────────────────▶│
 │                                       │
 │  ◀── event: status {state:running}  ──│
 │  ◀── event: stage {name:intent_..}  ──│
 │  ◀── event: tool {action:start,..}  ──│
 │  ◀── event: tool {action:end,..}    ──│
 │  ◀── event: heartbeat (每 15s)       ──│
 │  ◀── event: report {anomaly_count}  ──│
 │  ◀── event: done {status:ok, ...}   ──│
 │                                       │
 │  关闭 EventSource                      │
 │  GET /analyze/tasks/{trace_id}        │
 │ ─────────────────────────────────────▶│
 │  200 {result: AnalysisResult, ...}    │
 │ ◀─────────────────────────────────────│
 │                                       │
 │  更新 assistant 气泡：                  │
 │    content = result.report_markdown   │
 │    status = success                    │
```

---

## 3. 后端接口定义

### 3.1 POST /analyze/async — 提交异步分析任务

**请求体**（与旧 `POST /analyze` 完全一致，复用 `AnalysisRequest`）：
```jsonc
{
  "query": "过去 30 天有哪些三路匹配异常？",
  "user_id": "u_001",
  "session_id": "s_abc",            // 可选；不传后端自动生成
  "analysis_type": "three_way_match", // 可选；不传走意图路由
  "output_mode": "markdown",         // 可选
  "auto_persist": true,              // 建议固定 true，走 chat 持久化
  "client_user_message_id": "c_u_1", // 可选；前端侧幂等用
  "client_assistant_message_id": "c_a_1",
  "regenerate_of": null,             // 可选；重新生成场景填已有 assistant_message_id
  "metadata": {}                     // 可选
}
```

**响应体**（HTTP 202）：
```jsonc
{
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",                // queued | running（极少情况提交即 running）
  "session_id": "s_abc",
  "user_message_id": 10086,          // auto_persist=true 时返回
  "assistant_message_id": 10087,     // auto_persist=true 时返回，初始 status=pending
  "poll_url": "/analyze/tasks/550e8400-e29b-41d4-a716-446655440000",
  "stream_url": "/analyze/tasks/550e8400-e29b-41d4-a716-446655440000/events",
  "created_at": "2026-04-14T10:00:00Z"
}
```

**可能的错误**：
- 400：请求体校验失败
- 503：并发队列已满（极端场景，后端会先尝试排队而非拒绝，一般不会出现）

---

### 3.2 GET /analyze/tasks/{trace_id} — 查询任务快照

**响应体**（HTTP 200）：
```jsonc
{
  "trace_id": "550e8400-...",
  "status": "ok",                    // queued | running | ok | error | aborted
  "session_id": "s_abc",
  "user_id": "u_001",
  "created_at": "2026-04-14T10:00:00Z",
  "started_at": "2026-04-14T10:00:02Z",
  "finished_at": "2026-04-14T10:02:15Z",
  "duration_ms": 133000,
  "stage": "report_generated",       // 当前阶段（running 时有值，可选展示）

  // 仅 status=ok 时有值：
  "result": {
    "report_id": "...",
    "trace_id": "...",
    "status": "success",
    "analysis_type": "three_way_match",
    "query": "...",
    "user_id": "u_001",
    "session_id": "s_abc",
    "time_range": "2026-03-15 ~ 2026-04-14",
    "anomalies": [ /* ... */ ],
    "kpis": { /* ... */ },
    "report_markdown": "## 分析报告\n...",
    "duration_ms": 133000,
    "created_at": "...",
    "user_message_id": 10086,
    "assistant_message_id": 10087,
    "session": { /* ChatSession 概览 */ }
  },

  // 仅 status=error / aborted 时有值：
  "error": {
    "code": "TIMEOUT",               // TIMEOUT | API_ERROR | LLM_ERROR | ...
    "message": "分析超时：900s"
  }
}
```

**错误**：
- 404：trace_id 不存在（或已过内存 TTL 且 trace_runs 表也查不到，极少见）

---

### 3.3 GET /analyze/tasks/{trace_id}/events — SSE 事件流

**响应头**：
- `Content-Type: text/event-stream`
- `Cache-Control: no-cache`
- `Connection: keep-alive`
- `X-Accel-Buffering: no`（告诉 Nginx 不要缓冲）

**请求头**（可选）：
- `Last-Event-ID: <seq>` — 断线重连时带上，服务端会从环形缓冲（最近 200 条）重放

**事件格式**：标准 SSE，`data` 字段为 JSON：
```
event: status
id: 1
data: {"type":"status","trace_id":"550e8400...","ts":"2026-04-14T10:00:01Z","seq":1,"state":"running"}

event: stage
id: 2
data: {"type":"stage","trace_id":"...","ts":"...","seq":2,"name":"intent_resolved","attrs":{"analysis_type":"three_way_match","confidence":0.92}}

event: tool
id: 3
data: {"type":"tool","trace_id":"...","ts":"...","seq":3,"action":"start","name":"query_purchase_orders"}

event: tool
id: 4
data: {"type":"tool","trace_id":"...","ts":"...","seq":4,"action":"end","name":"query_purchase_orders","duration_ms":1240,"status":"ok"}

event: heartbeat
id: 5
data: {"type":"heartbeat","trace_id":"...","ts":"...","seq":5}

event: done
id: 42
data: {"type":"done","trace_id":"...","ts":"...","seq":42,"status":"ok","duration_ms":133000,"anomaly_count":3}
```

**通用字段（所有事件必含）**：
| 字段 | 类型 | 说明 |
|------|------|------|
| type | string | 事件类型，见下表 |
| trace_id | string | 任务 ID |
| ts | string | ISO8601 时间戳 |
| seq | number | 单调递增序列号，断线重连用 |

**事件类型清单**：
| event 名 | type 值 | 专属字段 | 用途 |
|----------|--------|---------|------|
| status | status | state: queued/running/ok/error/aborted | 任务状态跃迁 |
| stage | stage | name, attrs? | 关键阶段标记 |
| tool | tool | action (start/end), name, duration_ms?, status? | 工具调用起止 |
| dag_task | dag_task | action, task_name, duration_ms?, status? | DAG 任务起止 |
| report | report | anomaly_count, duration_ms | 报告生成完成 |
| heartbeat | heartbeat | — | 保活，每 15s 一次 |
| done | done | status, duration_ms, anomaly_count?, error? | 终态，服务端随后关闭连接 |

**连接关闭**：
- 正常：后端发送 `event: done` 后主动关闭
- 异常：后端抛异常时直接断开；前端应视作需重连

---

## 4. 前端改造清单

### 4.1 发送请求
- 把原来调用 `POST /analyze` 的入口改为调用 `POST /analyze/async`
- 请求体保持不变（schema 兼容）
- 响应不再等待完整结果，而是拿到 `{trace_id, user_message_id, assistant_message_id, stream_url, poll_url}`

### 4.2 立即渲染气泡
- 用 `user_message_id` 渲染 user 气泡（内容来自请求 query）
- 用 `assistant_message_id` 渲染一个占位 assistant 气泡：
  - 内容建议显示"AI 正在分析..."或 loading 动画
  - 可以标记 `status=pending` 样式（如变灰 + 旋转图标）
- 这两个 id 将作为后续更新气泡内容的 key

### 4.3 订阅 SSE
推荐实现：
```js
const es = new EventSource(ack.stream_url);

es.addEventListener('status',   e => handleStatus(JSON.parse(e.data)));
es.addEventListener('stage',    e => handleStage(JSON.parse(e.data)));
es.addEventListener('tool',     e => handleTool(JSON.parse(e.data)));
es.addEventListener('dag_task', e => handleDagTask(JSON.parse(e.data)));
es.addEventListener('report',   e => handleReport(JSON.parse(e.data)));
es.addEventListener('heartbeat',e => handleHeartbeat(JSON.parse(e.data)));
es.addEventListener('done',     e => {
  const evt = JSON.parse(e.data);
  es.close();
  fetchFinalResult(ack.trace_id, evt.status);
});

es.onerror = () => {
  // 连接异常，EventSource 会自动重连；若持续失败可降级到轮询
};
```

**注意 `EventSource` 的限制**：
- 原生 `EventSource` 不支持设置请求头（不能带 `Authorization`、`Last-Event-ID`）。如需这两项，改用 `fetch` + `ReadableStream` 手写 SSE 解析
- 浏览器对同一 origin 的 EventSource 有并发上限（通常 6 个），若同一页面可能开多个任务需注意复用

### 4.4 拉取最终结果
收到 `event: done` 后：
```js
async function fetchFinalResult(traceId, doneStatus) {
  const snapshot = await fetch(`/analyze/tasks/${traceId}`).then(r => r.json());
  if (snapshot.status === 'ok') {
    updateAssistantMessage(snapshot.result.assistant_message_id, {
      content: snapshot.result.report_markdown,
      status: 'success',
      duration_ms: snapshot.duration_ms,
    });
  } else {
    updateAssistantMessage(assistantMessageId, {
      content: snapshot.error?.message ?? '任务失败',
      status: 'error',
    });
  }
}
```

### 4.5 SSE 不可用时的降级（兜底）
以下场景需要降级为轮询：
- `EventSource` 连接建立失败（CSP / 代理不支持 / 其他）
- 连续 30 秒没收到任何事件（连 heartbeat 都没有）

降级策略：
```js
async function pollUntilDone(traceId) {
  while (true) {
    const s = await fetch(`/analyze/tasks/${traceId}`).then(r => r.json());
    if (['ok', 'error', 'aborted'].includes(s.status)) {
      return s;
    }
    await sleep(2000);  // 2 秒轮询一次
  }
}
```

### 4.6 断线重连（可选，高级）
若采用 fetch + ReadableStream 自行实现 SSE，可发送 `Last-Event-ID` 请求头让服务端重放中间事件：
```js
const res = await fetch(streamUrl, {
  headers: { 'Last-Event-ID': String(lastSeq) },
});
// 然后解析 res.body 的 text/event-stream 流
```
环形缓冲每 trace_id 最近 200 条事件；超出范围的 seq 拿不回。

### 4.7 重新生成（regenerate）
- 请求体带 `regenerate_of: <已有 assistant_message_id>`
- 后端不新建消息，而是在分析完成后 update 既有 assistant 消息（status: pending → success/error）
- 前端：收到 202 后把对应气泡状态置回 pending，其余流程同上

### 4.8 错误处理速查表
| 场景 | 后端表现 | 前端处理 |
|------|---------|---------|
| 提交 4xx | `POST /analyze/async` 返回 4xx | 提示用户检查输入 |
| 提交成功但从未收到事件 | EventSource 连接建立后 30s 无事件 | 关闭 EventSource → 走 2s 轮询 |
| SSE 收到 done, status=ok | `event: done`, then 连接关闭 | 关 EventSource → GET snapshot → 更新气泡 |
| SSE 收到 done, status=error | 同上，`error` 字段有值 | 同上，气泡 status=error，显示 error.message |
| 轮询 404 | trace_id 不存在/过期 | 提示"任务记录已过期，请重新发起" |
| 轮询 status=aborted | 进程重启导致的中止 | 气泡 status=error，内容："任务因服务重启中断，请重新发起"；可展示"重新生成"按钮 |
| 提交时收到 503 | 队列满（极少） | 提示"系统繁忙，请稍后重试" |

### 4.9 UI/UX 建议（非硬性要求）
- `status=pending` 气泡用 loading 动画占位（"AI 正在分析..."）
- 收到 `stage` 事件可把阶段名翻译成人类可读的状态文案（"正在理解意图" / "正在并行执行分析任务" 等）
- 收到 `tool` 事件可显示"正在调用：查询采购订单"之类的细粒度提示
- `heartbeat` 仅用于判活，不必在 UI 上展示
- 失败气泡显示"重新生成"按钮，点击后走 `regenerate_of` 流程

---

## 5. 接口对比速查

| 能力 | 旧 `POST /analyze`（同步） | 新 `POST /analyze/async`（异步） |
|------|-----------|----------|
| 响应时机 | 分析完成后返回（最长 900s） | 立即返回（<100ms） |
| 响应内容 | 完整 `AnalysisResult` | `AnalysisTaskAck`（trace_id 等） |
| 进度反馈 | 无 | SSE 阶段事件 / 轮询快照 |
| 反代超时风险 | 高（长连接） | 无（短请求 + SSE 心跳） |
| 前端刷新/断网 | 结果丢失 | trace_id 仍可查 |
| chat 持久化 | 完成后一次写入 | 提交即落 pending，完成后 update |
| 推荐使用场景 | 批处理脚本、简单调试 | Web UI / 用户交互 |

---

## 6. 时序与状态机

### 6.1 后端 status 状态机
```
  [提交]
     │
     ▼
  queued  ──（semaphore acquire）──▶  running  ──▶  ok
                                              └──▶  error
                                              └──▶  aborted（仅服务重启）
```
- `queued` 和 `running` 都属于"进行中"，前端 UI 可统一显示 loading
- `ok / error / aborted` 都是终态，前端应停止订阅/轮询

### 6.2 chat_messages.status 取值
| 值 | 来源 | 何时出现 |
|----|------|---------|
| success | 同步接口 or 异步完成 | user 消息总是 success；assistant 成功时 |
| error | 同步接口 or 异步完成 | assistant 失败时 |
| pending | **仅异步接口** | assistant 消息在任务完成前的占位状态 |

前端需要感知 `pending` 这个新值并渲染 loading 样式。

---

## 7. 本地联调建议

### 7.1 启后端
```bash
cd eragent
uvicorn api.main:app --reload --port 8000
```

### 7.2 用 curl 手动跑一遍
```bash
# 1. 提交
curl -X POST http://localhost:8000/analyze/async \
  -H "Content-Type: application/json" \
  -d '{"query":"过去30天三路匹配异常","user_id":"u1","auto_persist":true}'
# → {"trace_id":"abc...", ...}

# 2. 立即查快照
curl http://localhost:8000/analyze/tasks/abc...

# 3. 订阅 SSE
curl -N http://localhost:8000/analyze/tasks/abc.../events

# 4. 完成后再查快照拿 result
curl http://localhost:8000/analyze/tasks/abc...
```

### 7.3 注意 CORS
后端已通过现有 middleware 配置允许跨域；如遇 CORS 报错，确认 `api/main.py` 的 CORS 配置是否包含你的前端域名。

---

## 8. FAQ

**Q1：SSE 和轮询一定要二选一吗？**
不是。推荐用 SSE 做实时体验，同时保留"收到 done 后 GET 一次快照拿完整 result"的二次请求。兜底场景可以短时间轮询。

**Q2：如果前端刷新了页面，之前的任务还能继续吗？**
能。重新用 trace_id 订阅 SSE 或轮询快照即可。trace_id 建议持久化到 localStorage + 服务端 chat_messages.trace_id 字段。

**Q3：SSE 事件会不会丢？**
正常情况下不丢。断线重连时用 `Last-Event-ID` 可从环形缓冲（200 条）重放；万一丢了，done 之后 GET 快照总是能拿到最终结果。

**Q4：同一用户并发提交多个任务会怎样？**
会依次进入队列，每个任务独立有自己的 trace_id 和 SSE 流。后端并发上限 5，超过即 queued 排队。

**Q5：为什么不直接推完整的 result？**
SSE 只推轻量进度事件（< 1KB）。完整报告 markdown 可能几百 KB 不适合走 SSE，走 GET 快照拉一次更合理。

---

## 9. 变更对接联系人

- 后端接口变更：在本文档 PR 里 comment，或找后端同学对齐
- 交付物：本文档（`docs/async_analyze_frontend.md`） + 后端实现
- 上线策略：后端先发布（兼容旧接口），前端用 feature flag 逐步切换

