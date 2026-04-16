# 异步分析 SSE 事件流缺少终态事件 —— 后端 Bug 报告

> 报告时间：2026-04-14
> 报告人：前端
> 影响接口：`GET /api/v1/ptp-agent/analyze/tasks/{trace_id}/events`（SSE）
> 相关后端文档：[async_analyze_frontend.md §3.3](./async_analyze_frontend.md) / `async_analyze_design.md`
> 严重程度：**高** —— 所有走异步接口的请求都无法在前端完成（只能靠前端 15 分钟全局超时兜底失败）

## 1. 现象

前端走 `POST /analyze/async` → `GET /analyze/tasks/{trace_id}/events` 的异步链路时：

- ✅ 后端任务能正常跑完（日志里看到完整的 trace summary，任务耗时 ~12s）
- ✅ SSE 连接能成功建立
- ❌ **SSE 流里只推 heartbeat，从头到尾没有 `done` / `status` / `stage` / `tool` / `report` 等业务事件**
- ❌ 前端等不到 `done`，就不会去拉最终 snapshot，气泡永远停在"分析中"

结果：**用户界面所有异步请求都表现为"一直转圈直到 15 分钟后失败"**。

## 2. 复现案例

### 2.1 后端日志（任务已成功完成）

```
trace_id: 058282f5-dc2f-4771-8161-6a616525ec7b
user query: 查看最近30天的采购订单异常，按严重等级排序

=== trace summary ===
├─ [intent]  route_decision             2443ms
├─ [model]   qwen3-max  msgs=?          2293ms
├─ [model]   qwen3-max  msgs=2          2725ms
├─ [tool]    query_purchase_orders         3.4ms
├─ [tool]    run_three_way_match           6.6ms
├─ [model]   qwen3-max  msgs=5          6780ms
└─ ... checkpoints / memory writes
--- summary: total=12011.6ms ---
```

任务在后端视角是完全正常完成的（有最终报告输出）。

### 2.2 SSE 流实际输出（curl 抓到的内容）

```
event: heartbeat
data: {"type": "heartbeat", "trace_id": "058282f5-dc2f-4771-8161-6a616525ec7b"}

event: heartbeat
data: {"type": "heartbeat", "trace_id": "058282f5-dc2f-4771-8161-6a616525ec7b"}

...（持续 N 次 heartbeat，无其他事件）...
```

- **预期**：至少应有 `event: status {state: running}` → 各类 `event: stage` / `event: tool` → 最终 `event: done {status: "ok", ...}`
- **实际**：整个生命周期**只有 heartbeat**

## 3. 问题定位

### 3.1 接口：`GET /api/v1/ptp-agent/analyze/tasks/{trace_id}/events`

这是问题的唯一入口。该接口的实现（后端 SSE broker / EventBus）**没有把任务进度事件和终态事件发布到 SSE 订阅者**。

### 3.2 可能的后端原因（请后端同学按优先级排查）

#### 优先级 P0 —— **`done` 事件完全没发**
最关键的问题。任务从 running → ok / error / aborted 的状态跃迁时，必须向该 trace_id 对应的 event bus 发布一条 `event: done`。请检查：

- **位置**：任务完成/失败/中止后的收尾逻辑（推测在 `TaskRegistry` / `AsyncAnalyzeRunner` 或类似模块）
- **怀疑点**：
  1. 有没有 `publish(done_event)` 的调用？可能根本漏写
  2. 如果有，是不是走错了 topic（发到了一个没有订阅者的 bus）？
  3. 是不是 publish 在异常分支里被跳过了？（比如 `try { ... publish(done) } except: ...` 导致异常路径不 publish）
  4. SSE generator 是不是在收到 `done` 前就因为超时 / 连接检测逻辑提前 yield 了 heartbeat 然后跳过 done？

#### 优先级 P1 —— **`stage` / `tool` / `dag_task` 进度事件也没发**
这不单独阻塞前端（有 `done` 兜底就能显示），但严重降低用户体验（前端无法展示"正在查采购订单"等真阶段文案，只能继续跑假轮播）。

- 请确认任务内部的 stage 埋点 / tool start-end hook 是否**真的调用了** event bus publish
- 可能的原因：埋点只写了 `logger.info` 但忘了 `await bus.publish(stage_event)`

#### 优先级 P2 —— **heartbeat payload 缺必填字段**
文档 §3.3 要求所有事件（含 heartbeat）必须带：
- `type` ✅（有）
- `trace_id` ✅（有）
- `ts` ❌ **缺失**
- `seq` ❌ **缺失**

实际输出：
```json
{"type": "heartbeat", "trace_id": "058282f5-dc2f-4771-8161-6a616525ec7b"}
```

应为：
```json
{"type": "heartbeat", "trace_id": "058282f5-...", "ts": "2026-04-14T10:00:00Z", "seq": 42}
```

影响：
- `seq` 缺失会让断点续传（`Last-Event-ID`）机制失效 —— 前端重连后服务端无法判断从哪里开始重放
- `ts` 缺失只是轻微影响日志和调试

## 4. 验证方法（修复后请后端自测）

### 4.1 用 curl 订阅 SSE 并跑一次完整任务

```bash
# Terminal A：订阅事件流（先挂起等事件）
TID=$(curl -s -X POST http://localhost:3000/api/v1/ptp-agent/analyze/async \
  -H "Content-Type: application/json" \
  -d '{"query":"查看最近30天的采购订单异常","user_id":"u1","auto_persist":false}' \
  | jq -r .trace_id)
echo "trace_id=$TID"

curl -N "http://localhost:3000/api/v1/ptp-agent/analyze/tasks/$TID/events"
```

### 4.2 预期输出（最小可接受版本）

```
event: status
id: 1
data: {"type":"status","trace_id":"...","ts":"...","seq":1,"state":"running"}

event: heartbeat
id: 2
data: {"type":"heartbeat","trace_id":"...","ts":"...","seq":2}

... （中间可以有更多 stage / tool / heartbeat） ...

event: done
id: N
data: {"type":"done","trace_id":"...","ts":"...","seq":N,"status":"ok","duration_ms":12011}
```

### 4.3 验收标准

- [ ] 每条事件都有 `type` / `trace_id` / `ts` / `seq` 四个基本字段
- [ ] 任务完成时一定有 `event: done`（无论 ok / error / aborted）
- [ ] `done` 事件发出后，SSE 连接被服务端主动关闭（而不是继续推 heartbeat）
- [ ] 任务进行中至少能收到 1 条 `event: stage`（可以逐步补齐更多）

## 5. 前端侧的临时兜底（已实施）

在后端修复之前，前端做了一个最小改动防止界面彻底挂死：

**改动**：[src/services/analysisStream.ts](../src/services/analysisStream.ts) 的 watchdog 逻辑改为 **heartbeat 事件不再重置 "最后事件时间戳"**。

**效果**：
- 原逻辑：heartbeat 会刷新 watchdog 计时器 → 当前这种"只发 heartbeat"的场景下 watchdog 永远不触发 → 界面挂到 15 分钟全局超时
- 新逻辑：heartbeat 只代表连接存活，不代表任务有进展 → 30s 内没有任何"业务事件"（即使心跳在跳）→ 前端降级为每 2s 轮询 `GET /analyze/tasks/{trace_id}` 快照 → 任务完成后前端能通过快照拿到结果

**代价**：
- 任务处于 `queued` 状态排队 >30s 时，前端会误判并提前降级到轮询（功能上仍然正确，只是多了几次 HTTP 请求）
- 这个代价远小于"界面挂死 15 分钟"

**这个兜底不替代后端修复**。后端修好后，前端行为会自然回归到 SSE 事件驱动（只要 `done` 事件正常发出，watchdog 就不会触发降级）。

## 6. 联系人 / 后续

- 后端修复后请在此文档末尾追加修复说明（改了哪些文件 / 变更的 commit hash）
- 修复上线后前端会做一次联调验证：
  - [ ] 能收到 `event: done`，气泡正确变成报告内容
  - [ ] 能收到至少一条 `event: stage`，气泡阶段文案有真实更新
  - [ ] 正常任务下前端不触发轮询降级（Network 面板无 `/analyze/tasks/{id}` 快照请求，除非最终 `done` 后那一次）

---

**附：本次复现的 trace_id**：`058282f5-dc2f-4771-8161-6a616525ec7b`（后端如需查具体一次调用的上下文）

---

# Issue 2：SSE 与快照接口对任务终态的可见性不一致

> 报告时间：2026-04-14
> 报告人：前端
> 影响接口：`GET /api/v1/ptp-agent/analyze/tasks/{trace_id}`（同步快照）
> 严重程度：**高** —— 前端会把实际成功的任务误报为"分析失败"
> 状态：前端已做兜底（3 次 × 1s 重试 + done 事件兜底），但根因在后端

## 1. 现象

前端走异步链路完整跑通：SSE 收到 status/stage/tool/done 全套事件，`event: done` 明确给出 `status: "ok"`。此时前端按设计应调 `GET /analyze/tasks/{trace_id}` 拿完整 `result.report_markdown`。

但**紧跟在 done 事件之后立即查同步快照接口，返回的 `status` 仍是 `running`，`result` 仍是 `null`**：

```json
{
  "trace_id": "5b29c774-3165-4b87-bf84-a89573e2a3fe",
  "status": "running",
  "session_id": "f80974ea-1519-48b0-8686-52e7b5584d50",
  "user_id": "xiegp",
  "created_at": "2026-04-14T18:20:59.220348+08:00",
  "started_at": "2026-04-14T18:20:59.220348+08:00",
  "finished_at": null,
  "duration_ms": null,
  "stage": null,
  "result": null,
  "error": null
}
```

前端原先的代码**盲信**这个快照的 `status`，当成任务没完成；叠加 `result=null` 走到失败分支显示"分析失败"。

**用户观感**：后端日志里任务圆满完成，界面上却是红色的"分析失败"，毫无指向性。

## 2. 复现案例

- **trace_id**：`5b29c774-3165-4b87-bf84-a89573e2a3fe`
- **时间线**：
  - `18:20:59.149` 后端推 `event: status state=queued`
  - `18:21:46.639` 后端推 `event: done status=ok duration_ms=47469`
  - `18:21:46.6xx`（紧随其后）前端 `GET /analyze/tasks/{trace_id}` 返回 `status=running, result=null`

SSE 和快照接口对同一 trace_id 的状态描述**冲突**。

## 3. 问题定位（后端侧）

两种可能的后端实现问题（按怀疑度排序）：

### P0 —— done 事件发布早于快照落库

后端很可能在"任务完成"这一刻做了两件事：
1. 向 event bus publish `done` 事件 → SSE 立刻推给前端
2. 把 `result` 写入快照存储

如果**步骤 1 早于步骤 2 提交**（异步、不同 goroutine / task，或者 1 是内存队列推送、2 是数据库写入），就会出现"前端已收到 done，但快照还没 result"的 race window。

**建议修复**：把"publish done"放到"result 落库已提交"之后，或者让两个动作变成原子事务。

### P1 —— 快照接口读的是旧副本

如果 `GET /analyze/tasks/{trace_id}` 读的是某个 read replica / 缓存层（如 Redis 缓存、查询副本库），主写路径提交后副本同步有延迟，也会出现这种观察。

**建议排查**：确认快照接口的数据源是否有 replication lag。如有，该接口应该强制读主库，或者提供 `?consistent=true` 参数。

### P2 —— 状态更新漏写

如果后端任务状态机有"ok 之后应该把 status 字段更新为 ok"这一步，但漏写了一部分字段（只写 result，没写 status），就会出现这种观察。

**建议排查**：查看任务完成的收尾代码，确认 `status` 字段是否跟 `result` 一起写入。

## 4. 验证方法

```bash
# 用 curl 同时订阅 SSE 和轮询快照
TID=$(curl -s -X POST http://localhost:3000/api/v1/ptp-agent/analyze/async \
  -H "Content-Type: application/json" \
  -d '{"query":"查看最近30天的采购订单异常","user_id":"u1","auto_persist":false}' \
  | jq -r .trace_id)

# Terminal A：盯 SSE
curl -N "http://localhost:3000/api/v1/ptp-agent/analyze/tasks/$TID/events"

# Terminal B：在看到 Terminal A 的 "event: done" 后立即手动跑一次
curl -s "http://localhost:3000/api/v1/ptp-agent/analyze/tasks/$TID" | jq
```

**验收标准**：
- [ ] 收到 `event: done` 后，**立即**（< 100ms）调快照接口，`status` 必须是 `ok` / `error` / `aborted`（而不是 `running` / `queued`）
- [ ] 如果 `status` 是 `ok`，`result` 字段必须包含完整报告（`report_markdown` 非空）
- [ ] 如果 `status` 是 `error`，`error` 字段必须有 `{code, message}`

## 5. 前端侧的兜底（已实施）

**改动**：[src/services/analysisStream.ts](../src/services/analysisStream.ts) 的 `'done'` 事件处理引入 `fetchSnapshotUntilTerminal`。

**策略**：
- 收到 `done` 后拉快照，若 `status` 是 `running` / `queued`，等 1s 再试，最多 3 次
- 3 次都拿不到终态 → 用 `done` 事件本身的 `status` / `error` 构造一个最小 snapshot 交给 UI（成功分支显示"✅ 分析已完成，但报告详情暂时无法加载"占位文案）
- 整个过程对用户透明，最坏多等 3 秒

**代价**：
- 后端修好前，每次任务完成都至少多打 1 次快照请求（race window 存在时最多 3 次）
- 如果 race window > 3s，用户看到的是占位文案，失去详细报告的展示机会（但比"分析失败"红字好得多）

**这个兜底不替代后端修复**。前端只能等最终状态，后端如果一直不同步，占位文案就会长期占主导。

## 6. 联系人 / 后续

- 后端修复后请在此文档末尾追加修复说明（改了哪些文件 / 变更的 commit hash）
- 修复上线后前端会做一次联调验证：
  - [ ] `event: done` 后立即查快照，`status` 能立刻看到 `ok` / `error` / `aborted`
  - [ ] `result.report_markdown` 非空
  - [ ] Network 面板里只有一次 `GET /analyze/tasks/{id}` 请求（没有重试）

---

# Issue 3：聊天消息终态不固化，刷新后前端循环重订阅 SSE

> 报告时间：2026-04-16
> 报告人：前端
> 影响接口：
>   - `GET /api/v1/ptp-agent/sessions/{session_id}`（会话详情 — 聊天消息的 `status` 字段）
>   - `GET /api/v1/ptp-agent/analyze/tasks/{trace_id}/events`（被刷新重复订阅）
> 严重程度：**中高** —— 用户侧表现为"任务已经失败/终结，但刷新后气泡又回到'分析中'"的循环；
> 同时造成 SSE/快照接口被重复打开，资源浪费
> 状态：前端已做兜底（resume 时先查快照，见本 issue §3），但根因在后端

## 1. 现象

1. 用户提交一次异步分析，任务在后端最终走到 `error` / `aborted` / `ok` 终态。
2. 用户刷新或切回该会话。
3. 浏览器 Network 里**仍然看到**：
   - `GET /sessions/{sid}` 返回的消息里，assistant 消息的 `status` 字段是 `"pending"`；
   - 紧接着 `GET /analyze/tasks/{trace_id}/events` 被再次打开一条 SSE 连接；
   - 气泡从"已失败"重新变成"正在分析"。
4. 用户再刷新一次，同样的两条请求再来一轮，无穷循环。

## 2. 问题定位

### 2.1 `chat_messages.status` 停留在 `pending`

按 [docs/chat_history_api_design.md §2](./chat_history_api_design.md) 与 §7 流程："经 `/analyze` 完成后后端固化为 `success` 或 `error`。若是刷新页面后从后端拉的消息，理论上不会再看到 `sending`"。

实际观察：任务已经在 TaskRegistry 视角走到终态（snapshot `GET /analyze/tasks/{trace_id}` 返回 `status=error`/`ok`/`aborted`），但 `chat_messages` 表里对应那条 assistant 消息的 `status` 仍然是 `pending`。

### 2.2 可能的后端原因（按优先级排序）

#### P0 —— 任务终态收尾漏写 `chat_messages.status`

任务完成/失败/中止时，后端的收尾逻辑需要**同时**做两件事：
1. 更新 TaskRegistry 里的快照状态（`GET /analyze/tasks/{id}` 看到的那份）
2. 更新 `chat_messages` 表里那条 assistant 消息：
   - 成功 → `{status: 'success', content: report_markdown, duration_ms, trace_id}`
   - 失败 → `{status: 'error', content: 错误消息或占位, duration_ms, trace_id}`
   - 中止 → `{status: 'error', content: '已中止', duration_ms}`（或按产品约定）

目前怀疑后端**只更新了 1**，漏了 2；或者 2 走在一个可能异常的独立事务里没被兜住。

请检查：
- 任务收尾代码路径里有没有 `UPDATE chat_messages SET status = ... WHERE id = assistant_message_id` 这一条？
- 这条 UPDATE 是不是和快照状态更新在同一个事务 / 同一个原子单元里？
- 异常路径（LLM error / timeout / worker panic）是不是走了同样的收尾？还是短路跳过了？

#### P1 —— worker 崩溃后没有"孤儿任务"扫描

即便 P0 修好，仍有一类极端情况会留下 pending：进程被 kill / OOM / 容器驱逐，收尾代码根本没机会执行。

建议加一个后端定时任务：扫描 `chat_messages.status='pending'` 且 `created_at` 超过某个阈值（比如 30 分钟）的消息，按对应 trace 的最终状态补写 `status` / `content`。如果 trace 也没有终态记录，直接标 `error` + "分析异常中止"。

#### P2 —— 重新生成 (`regenerate_of`) 路径的终态落地

`regenerate_of` 场景下是 PATCH 已有的 assistant 消息，不是插入新消息。请确认 regenerate 失败时同样会把 `status` 改回 `error`，而不是保留上一版的 `success` 或停在 `pending`。

### 2.3 前端为何不能自己把 `status` 改回 `error`

前端 `onError` 当下只在内存里把气泡标为 `status='error'`，**不 PATCH 后端**。理由：

- 前端的"失败"判断来源多样（SSE 断连、15 分钟全局超时、熔断），**并不等价于任务真的失败**。后端任务可能还在跑，前端只是看不到。
- 前端如果强行 PATCH `status='error'`，会和后端真正的终态更新产生写写竞争：前端先写 error → 后端紧接着写 success，最终用户看到哪一版取决于时序，不可控。
- `chat_messages.status` 的真相应由后端任务生命周期维护，这是不变量，不能倒置。

所以前端坚持**只读**这个字段，要求后端保证它在任务终结后必然落成 success / error。

## 3. 前端侧已实施的兜底（方案 B）

**改动**：[src/hooks/useMessageSending.ts](../src/hooks/useMessageSending.ts) 的 resume effect（刷新/切会话后重连 SSE 的那段）。

**策略**：

1. 扫到 `status='sending'` 且有 `traceId` 的消息时，**先**调 `GET /analyze/tasks/{trace_id}` 拿 snapshot。
2. snapshot 的 `status` 是 `ok` / `error` / `aborted` → 直接调用 `applySnapshotToMessage` 把气泡落到终态，**不开 SSE**。
3. snapshot 的 `status` 是 `running` / `queued` → 才真正开 SSE（原路径）。
4. snapshot 请求失败（网络错）→ 退回到原路径开 SSE，交给流自身的 watchdog / 熔断兜底。这一步是刻意保守，避免"一次网络抖动让用户的在途任务永久失联"。

**效果**：

- 后端即使继续留 `chat_messages.status='pending'`，只要快照接口能吐出终态，前端刷新就不会再打开 SSE，循环消失。
- 用户看到的气泡从"正在分析"变成最终的成功/失败形态，数据一致。

**代价**：

- 每次刷新对每条 pending 消息多打一次 `GET /analyze/tasks/{id}` 请求。对一屏几十条聊天来说成本可忽略。
- 如果后端 snapshot 接口也停留在 `running`（即 Issue 2 描述的那种滞后），前端仍然会开 SSE，属于双侧故障的退化场景，不在本 issue 范围。

**这个兜底不替代后端修复**。snapshot 接口和 chat_messages 表是两条独立的信息通路；B 只解决了"前端读 chat_messages 读到假 pending"这一个观感问题，不能让其他依赖 `chat_messages.status` 的后端流程（列表、统计、清理、导出）看到正确的数据。

## 4. 验证方法

### 4.1 手动复现

1. 触发一次必然失败的异步分析（比如构造一个后端会报 LLM_ERROR 的 query）。
2. 等前端气泡变红、显示"分析失败"。
3. 直接查数据库或 `GET /sessions/{sid}`，看对应 assistant 消息的 `status` 字段：
   - 期望：`"error"`
   - 实际（bug 存在时）：`"pending"`

### 4.2 验收标准

- [ ] 任务走到 `error` / `aborted` 终态后，`chat_messages.status` 在 **≤ 1 秒内** 变成 `"error"`
- [ ] 任务成功后，`chat_messages.status` 变成 `"success"` 且 `content = report_markdown`
- [ ] P1 扫描任务上线后，对刻意杀掉 worker 造出的 `pending` 消息，能在扫描周期内补成 `error`
- [ ] 前端 Network 面板：刷新带有已完成/失败消息的会话时，不再看到 `GET /analyze/tasks/{id}/events` 请求（只会看到一次 `GET /analyze/tasks/{id}` 的 snapshot 预检）

## 5. 联系人 / 后续

- 后端修复后请在此文档末尾追加修复说明（改了哪些文件 / 变更的 commit hash）
- 修复上线后前端可考虑把 snapshot 预检改为"仅在诊断开关打开时生效"或完全移除，回归到直接开 SSE 的原始路径（但建议保留 —— 它同时也修掉了"关标签页时任务刚好完成但前端没来得及拿结果"的一类竞态，代价很低）
