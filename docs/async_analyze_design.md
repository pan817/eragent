# Analyse 接口异步化设计（后端）

## 背景
`POST /analyze` 当前同步阻塞，链路包含多轮 LLM + DAG 执行，最长 900s。前端只能傻等，超时/断网即丢结果。目标：改为"提交即返回 task_id → 轮询/SSE 获取进度 → 完成后拉取结果"的异步模式，尽量复用现有 `trace_runs` / `chat_messages` 基础设施，不引入新中间件。

## 已确认决策
1. **兼容策略**：新增 `/analyze/async`，旧 `/analyze` 保留一段过渡期，不动其行为。
2. **SSE 粒度**：阶段 + 关键节点（queued / running / tool 起止 / DAG 任务起止 / ok / error），不全量推 span。
3. **取消能力**：本期不做。
4. **前端范围**：后端只输出接口 + 对接文档，前端改造由前端团队承担。

## 锁定的配置默认值
- `result_cache_ttl_sec = 600`（任务完成后内存保留 10 分钟；超时后仍可从 trace_runs / reports 表查终态）
- `max_concurrent_tasks = 5`（同进程并发上限，超过则 queued 排队）
- `sse_heartbeat_sec = 15`（SSE 心跳周期，防代理超时）
- 不提供 NDJSON 备选端点；SSE 失败场景前端走 2s 轮询兜底

---

## 目录
1. 后端改造总览
2. TaskRegistry
3. EventBus
4. 新增 API 端点
5. Orchestrator / Observability 埋点适配
6. chat_messages 协作流程
7. 进程重启与异常兜底
8. 测试策略
9. 验证步骤
10. 工作量预估

---

## 1. 后端改造总览

### 新增文件
- `core/tasks/__init__.py`
- `core/tasks/registry.py` — 内存中的 TaskRegistry（trace_id → asyncio.Task + 状态机）
- `core/tasks/events.py` — 轻量 EventBus（trace_id → asyncio.Queue，多订阅者 fanout）
- `core/tasks/schemas.py` — TaskStatus、TaskSnapshot、SSE 事件 Pydantic 模型
- `api/routes/analyze_async.py` — 新增异步提交 / 查询 / SSE 三个端点

### 修改文件
- `api/routes/analyze.py` — 不改旧同步路径；抽取 `_ensure_session` / `_persist_messages` 为可复用函数
- `api/main.py` — lifespan 中初始化/清理 TaskRegistry 与 EventBus；关闭时 cancel 残留任务并把 trace_runs 残留 running 标记为 aborted
- `core/observability/middleware.py` — 在 `start_run` / `finish_run` / `_record_span` / `_span` 的关键点调用 EventBus.publish（非阻塞、失败吞掉不影响主流程）
- `core/orchestrator/orchestrator.py` — `analyze()` 增加可选 `on_stage` 回调参数（仅 DAG/ReAct 分支入口打点，不深入）
- `config/config.yaml` + `config/settings.py` — 新增 `async_analysis` 配置段

### 不新增依赖
SSE 用 FastAPI 原生 `StreamingResponse` + `text/event-stream` 手写。

---

## 2. TaskRegistry（core/tasks/registry.py）

### 职责
进程内管理异步分析任务生命周期，以 `trace_id` 为主键。不做持久化（持久化由 `trace_runs` 承担）。

### 数据结构
- `TaskState` 枚举：queued / running / ok / error / aborted
- `TaskEntry` dataclass：trace_id、user_id、session_id、state、created_at、started_at、finished_at、`asyncio.Task` 句柄、`AnalysisResult` 缓存、`ErrorInfo`

### 关键方法
- `submit(request, runner_coro) -> TaskEntry`：`asyncio.create_task` 启动 runner；登记 entry；同步预写 `trace_runs(status=queued)` 一行。
- `get(trace_id) -> TaskEntry | None`：内存快查；miss 时调用方回落查 `trace_runs` + `reports` 表。
- `on_transition(trace_id, new_state, **payload)`：runner 内部调用；同时 publish 到 EventBus。
- `sweep_finished()`：后台协程每 60s 清理 `finished_at` 超过 `result_cache_ttl_sec` 的 entry。
- `shutdown()`：lifespan 关闭时 cancel 所有 running 任务，把 trace_runs 里仍为 running 的行标记为 aborted。

### 并发控制
`asyncio.Semaphore(max_concurrent_tasks=5)` 在 runner 入口 acquire。超过并发上限的任务保持 queued 状态排队，前端能轮询到 queued。

---

## 3. EventBus（core/tasks/events.py）

### 职责
进程内 pub/sub，把任务进度事件广播给当前订阅了该 trace_id 的 SSE 连接。**不跨进程广播**。

### 实现要点
- 内部结构：`dict[trace_id, list[asyncio.Queue[Event]]]`，每个 SSE 订阅一个 Queue。
- `publish(trace_id, event)`：非阻塞 `put_nowait`，Queue 满则丢弃最旧事件并打 WARNING。
- `subscribe(trace_id) -> async iterator[Event]`：SSE 端点调用；创建 Queue 加入列表，连接关闭时在 finally 里移除。
- `close(trace_id)`：任务结束后广播 `done` 并关闭所有订阅 Queue，让 SSE 生成器自然退出。
- 每 trace_id 维护环形缓冲（最近 200 条），支持 `Last-Event-ID` 重放。
- Queue 容量：每订阅 200（满足阶段 + 关键节点粒度下任意任务）。

### 事件类型（阶段 + 关键节点）
| type | 触发时机 | 专属字段 |
|------|---------|---------|
| status | queued / running / ok / error / aborted 状态跃迁 | state |
| stage | 关键阶段：intent_resolved / dag_planned / react_started | name、attrs |
| tool | 工具调用起止 | action (start/end)、name、duration_ms?、status? |
| dag_task | DAG 任务起止 | action、task_name、duration_ms?、status? |
| report | 报告生成完成 | anomaly_count、duration_ms |
| heartbeat | 每 15s 一次 | — |
| done | 终态 | status、duration_ms、anomaly_count?、error? |

### 通用字段
每条事件必含：`type`、`trace_id`、`ts`（ISO8601）、`seq`（单调递增序列号）。

---

## 4. 新增 API 端点

详细接口契约见前端对接文档 `docs/async_analyze_frontend.md`，此处仅列后端侧要点：

### 4.1 POST /analyze/async
- 请求体：复用 `AnalysisRequest`
- 处理流程：
  1. 生成 trace_id（uuid4）
  2. `auto_persist=True` 时 `_ensure_session` + `append_messages`（user status=success、assistant status=pending）
  3. `TaskRegistry.submit` 启动 runner
  4. 立即返回 202
- 响应：`AnalysisTaskAck`（trace_id / status=queued / session_id / user_message_id / assistant_message_id / poll_url / stream_url）

### 4.2 GET /analyze/tasks/{trace_id}
- 响应：`AnalysisTaskSnapshot`（trace_id / status / created_at / started_at / finished_at / duration_ms / stage? / result? / error?）
- 查找顺序：TaskRegistry 内存 → miss 回落查 `trace_runs` + `reports` 表 → 都没有返回 404

### 4.3 GET /analyze/tasks/{trace_id}/events
- `Content-Type: text/event-stream`
- 支持 `Last-Event-ID`：从环形缓冲重放
- 心跳：每 15s 一条 `event: heartbeat`
- 终态：发送 `event: done` 后服务端关闭连接

---

## 5. Orchestrator / Observability 埋点适配

### 原则
**不侵入业务逻辑**，只在 observability middleware 已有事件发射点旁挂回调。

### Observability 侧（core/observability/middleware.py）
- 模块级维护可选 `_event_publisher: Callable[[str, dict], None] | None`，由 TaskRegistry 启动时注入（指向 EventBus.publish）
- `start_run` 结尾：publish `status=running` + `stage=agent_started`
- `finish_run` 结尾：publish `status=ok/error` + `done`
- `_span` / `_record_span` 结束处：span_type=tool 或 name 以 `dag_task.` 开头时 publish 对应事件；其他类型（model 细粒度）不发
- publish 外包 try/except，失败吞掉 + WARNING，不影响 observability 主流程

### Orchestrator 侧（core/orchestrator/orchestrator.py）
- `analyze(request, *, on_stage: Callable | None = None)` 新增关键字参数；默认 None 保持原行为
- 3 个关键节点调用 on_stage：
  - `on_stage("intent_resolved", {"analysis_type": ..., "confidence": ...})`
  - `on_stage("dag_planned", {"tasks": [...]})`
  - `on_stage("react_started", {})`
- 不改返回值、异常、超时

### 不动
DAG executor、P2P agent、tools 源码一律不改。DAG 任务起止和 tool 调用起止通过 observability span 事件获得。

---

## 6. chat_messages 协作流程

### 为什么要改
同步接口里 user/assistant 消息都是在分析完成后一次性写入。异步模式下需要"提交即见气泡"。

### 流程
1. **提交时**（`/analyze/async` 端点内）：
   - `_ensure_session(user_id, session_id)`
   - `append_messages([user_msg(status=success), assistant_msg(status=pending, content="", trace_id=...)])`
   - 拿到 user_message_id / assistant_message_id 随 202 响应返回
2. **运行中**：runner 只通过 EventBus 推事件，不写 chat 表
3. **完成时**（runner 协程 finally 块）：
   - `update_message(assistant_message_id, {content, status=success/error, duration_ms, trace_id})`
   - 失败场景 content 填 error.message，status=error
4. **重新生成** (`regenerate_of`)：沿用旧同步路径语义——update 既有 assistant 消息；仅用 pending→success/error 过渡

### 复用点
- `core/chat/repository.py` 的 `append_messages` / `update_message`（无需改）
- 抽取 `api/routes/analyze.py` 的 `_ensure_session` / `_persist_messages` 为 helper

### 注意
- assistant 预落 pending 时 content 用空字符串；显示文案由前端决定
- `status="pending"` 是新字面量；chat_messages.status 本就是自由字符串无约束

---

## 7. 进程重启与异常兜底

### 启动时清理
`api/main.py` lifespan startup 调用 `TaskRegistry.recover_on_startup()`：
- 扫描 `trace_runs` 表中 status ∈ (queued, running) 的记录，update 成 status=aborted，error="process restarted"
- 同时把对应 `chat_messages` 里 assistant status=pending 的消息 update 成 status=error，content="任务因服务重启中断，请重新发起"

### 运行中异常
runner 协程用 try/except/finally 包全过程：
- `except asyncio.TimeoutError` → `ErrorInfo(code=TIMEOUT)`
- `except Exception as e` → `ErrorInfo(code=API_ERROR, message=str(e))`，记 ERROR 日志
- finally 里保证：TaskRegistry 状态机推进 + trace_runs 终态写入 + chat_messages update + EventBus 发 done 并 close 订阅

### SSE 连接异常
- 客户端断开（`except asyncio.CancelledError`）：从 EventBus 订阅列表移除自己，不影响任务
- 服务端异常：FastAPI 捕获关闭连接，后台任务继续跑，前端可重连

### 事件丢失容忍
- 环形缓冲（每 trace_id 最近 200 条）保证断线重连用 `Last-Event-ID` 能拿回中间事件
- 兜底：前端在 `done` 之前也可随时 GET `/analyze/tasks/{trace_id}` 拿最终结果，SSE 只是优化体验

---

## 8. 测试策略

### 单元测试（tests/unit/）
- `test_task_registry.py`：submit / get / 状态跃迁 / semaphore 限流 / sweep / shutdown 批量 cancel
- `test_event_bus.py`：publish / subscribe / 多订阅 fanout / Queue 满丢弃 / Last-Event-ID 重放
- `test_analyze_async_routes.py`：mock Orchestrator.analyze，验证 3 个端点契约和 chat_messages pending→success 流程

### 集成测试（tests/integration/）
- `test_async_analyze_e2e.py`：真实 orchestrator + 真实 LLM，覆盖成功 / 失败 / 超时 / 并发超限入队

### 覆盖率
保持项目 ≥ 90% 门槛（`pytest --cov-fail-under=90`）。

### 手工验证
`tests/http/async_analyze.http` REST Client 脚本：提交 → 轮询 → `curl -N` 订阅 SSE。

---

## 9. 验证步骤

1. `pytest tests/unit/test_task_registry.py tests/unit/test_event_bus.py tests/unit/test_analyze_async_routes.py -v`
2. `pytest --cov=. --cov-report=term-missing --cov-fail-under=90` 覆盖率不跌
3. `pytest tests/integration/test_async_analyze_e2e.py -v`
4. **手工烟囱**：`uvicorn api.main:app --reload`
   - 提交 → 断言 202 + trace_id
   - 立刻 GET snapshot → 断言 status=queued/running
   - `curl -N http://localhost:8000/analyze/tasks/{trace_id}/events` 观察事件流
   - 完成后 GET snapshot → 断言 status=ok + result 完整
5. **异常场景**：
   - 并发提交 > 5 个任务，验证排队
   - 运行中重启服务，snapshot 应看到 status=aborted，assistant 消息 status=error
   - 故意调低 LLM timeout，SSE 应收到 done 且 status=error
6. 前端对接文档 review 签字

---

## 10. 工作量预估

| 阶段 | 产出 | 预估 |
|------|------|------|
| P1 | TaskRegistry + EventBus + schemas + 单元测试 | 0.5 天 |
| P2 | 三个新 API 端点 + chat_messages pending 流程 + 单元测试 | 0.5 天 |
| P3 | Observability middleware 事件接入 + Orchestrator on_stage 钩子 | 0.3 天 |
| P4 | lifespan 启动兜底 + sweep + 配置项 | 0.2 天 |
| P5 | 集成测试 + 手工烟囱 | 0.5 天 |
| P6 | 前端对接文档 docs/async_analyze_frontend.md | 0.2 天 |
| **合计** | | **约 2.2 天** |

建议按 P1→P2→P3→P4→P5→P6 顺序提交 PR，P1/P2 可合。

