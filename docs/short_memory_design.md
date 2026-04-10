# 短期记忆实现方案

## 1. 概述

短期记忆负责在**单次会话（session）内**保持多轮对话的上下文连贯性。用户在同一 `session_id` 下的每一轮提问，Agent 都能看到本次会话的完整历史消息，无需客户端重传。

与长期记忆的分工：
- **短期记忆**：会话级，跨轮次，消息历史，由 LangGraph 自动管理
- **长期记忆**：跨会话，结构化知识沉淀，按 `user_id` 隔离，需要主动写入和检索

---

## 2. 技术选型

| 方案 | 选型 | 原因 |
|------|------|------|
| 存储后端 | LangGraph `PostgresSaver` | 与项目已有 PostgreSQL 共用，持久化跨进程重启，无需额外中间件 |
| 隔离单元 | `session_id` → `thread_id` | LangGraph 原生概念，天然支持多会话并发隔离 |
| 可观测性 | 实例级方法包装 | 零侵入，不修改 LangGraph 源码，通过 `attach_tracing()` 打补丁 |

依赖包：`langgraph-checkpoint-postgres >= 2.0.0`

---

## 3. 核心架构

```
Client Request (session_id)
        │
        ▼
  P2PAgent.analyze()
        │  thread_id = session_id
        ▼
  LangGraph agent.ainvoke(messages, config={thread_id})
        │
        ├─── checkpointer.get_tuple(config) ──→ PostgreSQL
        │         加载该 thread_id 的历史 checkpoint（含历史 messages）
        │
        ├─── LLM 推理（含历史上下文）
        │
        └─── checkpointer.put(config, checkpoint) ──→ PostgreSQL
                  写回更新后的 checkpoint（含本轮新消息）
```

PostgresSaver 管理的数据库表（由 `saver.setup()` 自动建表）：
- `checkpoints` — checkpoint 元数据
- `checkpoint_blobs` — 序列化的 channel 状态（含 messages 列表）
- `checkpoint_writes` — 中间写操作记录

---

## 4. 初始化流程

`P2PAgent._get_checkpointer()` 采用**懒加载 + 双重检查锁**：

```python
def _get_checkpointer(self) -> Any:
    if self._checkpointer is not None:
        return self._checkpointer
    with self._lock:  # RLock，防止并发重复初始化
        if self._checkpointer is not None:
            return self._checkpointer
        cm = PostgresSaver.from_conn_string(conninfo)
        saver = cm.__enter__()
        saver.setup()                          # 幂等建表
        attach_tracing(saver, middleware)      # 注入 span 监控
        self._checkpointer = saver
        atexit.register(self._close_checkpointer)  # 进程退出时清理连接
        return saver
```

**降级策略**：若 PostgreSQL 不可用，`_get_or_build_agent()` 捕获异常，以 `checkpointer=None` 继续构建 Agent，短期记忆禁用，主路径不受阻，同时输出 WARNING 日志。

---

## 5. 消息历史的加载与写回

在每次 `analyze()` 调用中：

```python
invoke_config = {"configurable": {"thread_id": session_id}}
result = await agent.ainvoke(
    {"messages": invoke_messages},  # 仅本轮新消息
    config=invoke_config,
)
```

LangGraph 内部自动完成：
1. `get_tuple(config)` — 按 `thread_id` 加载历史 checkpoint，将 `channel_values.messages`（历史消息列表）注入本轮上下文
2. Agent 推理（LLM 可见完整历史 + 本轮新消息）
3. `put(config, checkpoint)` — 将包含新消息的更新后 checkpoint 写回 PostgreSQL

**代码层面无需手动管理消息列表**，checkpointer 全程自动处理。

---

## 6. 可观测性集成

`core/observability/checkpointer.py` 通过**实例级方法包装**给 PostgresSaver 打补丁，将 6 个核心方法的 I/O 纳入 trace 监控体系。

### 包装的方法

| 方法 | 类型 | 采集信息 |
|------|------|---------|
| `get_tuple` | 同步 | `thread_id`, `checkpoint_id_in`, `hit`, `n_messages`, `checkpoint_id`, `step`, `source` |
| `put` | 同步 | `thread_id`, `checkpoint_id`, `n_messages`, `step`, `source` |
| `put_writes` | 同步 | `thread_id`, `task_id`, `n_writes` |
| `aget_tuple` | 异步 | 委托同步方法（复用 span） |
| `aput` | 异步 | 委托同步方法（复用 span） |
| `aput_writes` | 异步 | 委托同步方法（复用 span） |

### 异步桥接

LangGraph `AsyncPregelLoop` 需要异步 checkpointer 方法，但 `PostgresSaver` 是同步实现。解决方案：

```python
async def aget_tuple(config):
    return await asyncio.to_thread(get_tuple, config)
```

Python 3.11+ 会自动将 `contextvar`（含 `_current_trace`）复制到子线程，span 归属正确无需额外处理。

### 幂等性保障

```python
if getattr(saver, "_tracing_attached", False):
    return saver  # 防止重复打补丁
```

### Span 数据结构

```python
SpanEvent(
    trace_id=ctx.trace_id,      # 与当前请求 trace 关联
    span_type="checkpoint",
    name="get_tuple" | "put" | "put_writes",
    status="ok" | "error",
    duration_ms=...,
    attributes={...},           # 上表中的字段
    error=...,                  # 异常时含 traceback
)
```

异常时：先落 `status="error"` 的 span，再重新抛出，不吞异常。

---

## 7. 会话生命周期

```
会话创建
  └─ analyze() 调用时若 session_id 为空，自动生成 UUID
  └─ 首次 put() 为该 thread_id 创建 checkpoint

会话持续
  └─ 同一 session_id 多轮调用，每轮 get_tuple → 推理 → put

会话清理
  └─ DELETE /memory/short-term?session_id=<id>
  └─ → orchestrator.clear_short_term_memory(session_id)
  └─ → agent.clear_short_term_memory(session_id)
  └─ → checkpointer.delete_thread(session_id)
```

**批量清理限制**：`PostgresSaver` 未暴露"删除所有 thread"的 API。需要批量清空时，直接对数据库执行：
```sql
TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;
```

---

## 8. 配置项

```yaml
# config/config.yaml
memory:
  short_term:
    max_messages: 20          # 单会话最大消息数（见注意事项）
    summary_threshold: 15     # 触发摘要的消息数阈值（见注意事项）
```

```python
# config/settings.py
class MemorySettings(BaseSettings):
    short_term_max_messages: int = 20
    short_term_summary_threshold: int = 15
```

### ⚠️ 重要：配置与实现的缺口

`short_term_max_messages` 和 `short_term_summary_threshold` **当前仅作为配置预留，代码中没有任何执行层实现**。经过全代码库 grep 确认，这两个配置项没有被任何业务代码引用。

**当前实际行为**：每个 session 的消息历史**无上限地累积**，PostgresSaver 会存储该 thread_id 的全部历史消息。

---

## 9. 设计缺口与演进方案

### 缺口：消息历史无限增长

**问题**：随着同一 session 的轮次增多，LLM 需要处理的 token 数量线性增长，最终会超出模型上下文窗口，导致推理失败或成本急剧上升。

**推荐演进方案（按优先级）：**

#### 方案 A：消息裁剪（实现简单，推荐优先）

在 `analyze()` 的 `invoke_messages` 构建阶段，使用 LangChain `trim_messages` 对历史消息进行尾部保留：

```python
from langchain_core.messages import trim_messages

trimmed = trim_messages(
    history_messages,
    max_tokens=self._settings.memory.short_term_max_messages,
    strategy="last",
    token_counter=len,  # 按消息条数裁剪，简单可控
)
```

需要从 checkpointer 手动加载历史、裁剪后重新注入。

#### 方案 B：摘要压缩（效果好，实现复杂）

当消息数超过 `short_term_summary_threshold` 时，触发 LLM 对早期消息进行摘要，将摘要作为 SystemMessage 替换旧消息：

```
[SystemMessage: 摘要（前 N-5 轮）] + [最近 5 轮消息]
```

可实现为 LangGraph 中的条件节点：在 agent 节点前检查消息数，超阈值则调用摘要子链。

#### 方案 C：Session 过期清理（运维层）

为 checkpoint 表增加 `expires_at` 字段，定期清理超期会话，控制存储总量。可与方案 A/B 并用。

---

## 10. 测试策略

### 单元测试（`tests/unit/test_checkpointer.py`）

覆盖点：
- 6 个包装方法（同步 + 异步）的 span 正确发出
- 异常时 `status="error"` + 重新抛出（不吞异常）
- 幂等性：重复调用 `attach_tracing()` 不重复打补丁
- 辅助函数：`_truncate`, `_thread_id`, `_checkpoint_id`, `_summarize_tuple`, `_summarize_checkpoint`

### 集成测试

- `P2PAgent.clear_short_term_memory(session_id)` 正确调用 `delete_thread`
- 多轮对话下历史消息正确累积（需真实 PostgreSQL）
- checkpointer 初始化失败时 Agent 正常降级（mock DB 连接失败）

---

## 11. 关键文件索引

| 文件 | 职责 |
|------|------|
| `core/observability/checkpointer.py` | PostgresSaver 可观测性补丁，attach_tracing() 实现 |
| `modules/p2p/agent.py` | P2PAgent，checkpointer 初始化、analyze() 调用、清理逻辑 |
| `config/settings.py` | MemorySettings，short_term_max_messages/summary_threshold 配置定义 |
| `config/config.yaml` | 短期记忆运行时配置值 |
| `api/routes/analyze.py` | DELETE /memory/short-term 端点 |
| `tests/unit/test_checkpointer.py` | checkpointer 单元测试 |
