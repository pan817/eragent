# 技术债清单

> 本文件统一记录项目所有技术债：架构异味、临时方案、性能隐患、待重构项、已知缺陷。
>
> **维护规则**：
> - 新增条目按发现日期倒序追加在"未修复"区。
> - 每条须包含：问题、影响、涉及文件、建议修复方向、发现日期。
> - **双记录约定**：每条技术债除了在本清单登记，还必须在涉及文件的关键位置写 `# TECH-DEBT(#N): <一句话>` 注释（`#N` 为本清单条目编号），便于在代码里快速定位。代码注释只承担"标记位置"职责，问题详情、影响、修复方案以本清单为准。
> - 修复后两处同步移除（代码注释删掉、清单条目移到"已修复"区，保留记录便于审计），并在 commit message 引用本文件条目编号。

---

## 未修复

### 1. 意图路由命中率优化
- **问题**：当前 IntentRouter 把较多查询兜底到 ReAct 路径（L3 LLM 分类），DAG 命中率不够高。
- **影响**：ReAct 路径延迟更高、token 消耗更多；DAG 模板的并行优势没充分利用。
- **建议修复**：扩充 `config/intent_seeds.yaml` 的语义种子覆盖度；调优 L2 Chroma 相似度阈值；把高频 ReAct 查询沉淀回 DAG 模板。
- **发现日期**：（早于 2026-04）

### 2. core / modules 反向依赖 api.schemas（分层违规）
- **问题**：业务层（`modules/p2p/*`）和编排层（`core/orchestrator/*`、`core/tasks/*`）反向 import `api.schemas.analysis` 里定义的 Pydantic 模型（`AnalysisRequest` / `AnalysisResult` / `Severity` / `ErrorInfo` 等）。违反"下层不依赖上层"的分层原则。
- **影响**：
  - core / modules 无法脱离 api 层独立测试和复用；
  - 未来若要把分析能力以 SDK 形式打包给非 HTTP 场景使用，会被 api 层的 FastAPI 依赖污染；
  - 暂不阻塞功能，FastAPI 项目里这种写法常见。
- **涉及文件**（共 11 个，6 个 core + 5 个 modules）：
  - [core/orchestrator/orchestrator.py:21](../core/orchestrator/orchestrator.py#L21)
  - [core/orchestrator/router.py](../core/orchestrator/router.py)
  - [core/orchestrator/intent.py](../core/orchestrator/intent.py)
  - [core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py)
  - [core/tasks/registry.py](../core/tasks/registry.py)
  - [core/tasks/schemas.py](../core/tasks/schemas.py)
  - [modules/p2p/agent.py:21](../modules/p2p/agent.py#L21)
  - [modules/p2p/rules/three_way_match.py:14](../modules/p2p/rules/three_way_match.py#L14)
  - [modules/p2p/rules/payment_compliance.py:16](../modules/p2p/rules/payment_compliance.py#L16)
  - [modules/p2p/rules/price_variance.py:13](../modules/p2p/rules/price_variance.py#L13)
  - [modules/p2p/rules/supplier_performance.py:18](../modules/p2p/rules/supplier_performance.py#L18)
- **建议修复**：把 `api/schemas/analysis.py` 下沉到 `core/schemas/analysis.py`，作为业务数据契约的归属。`api/schemas/` 仅保留与 HTTP 协议强相关的 wrapper（如分页 envelope、错误响应格式），并 re-export `core.schemas` 中的核心模型，对外 API 兼容性不变。
- **发现日期**：2026-04-16（pyreverse 扫描 [packages_toplevel.png](architecture/packages_toplevel.png) 时定位）

### 3. EventBus ephemeral 事件缺乏类型区分
- **问题**：`core/tasks/events.py` 的 `publish()` 方法通过 `ephemeral` 布尔标志区分"可丢弃的高频事件"（LLM chunk）和"必须送达的状态事件"（done/error），但在类型层面无区分（都是 `dict`），消费方无法从事件结构上判断是否可安全回放。
- **影响**：依赖 `Last-Event-ID` 回放的 SSE 客户端可能漏掉 ephemeral 事件后误认为数据丢失；未来新增事件类型时，开发者需翻源码确认是否 ephemeral。
- **涉及文件**：
  - [core/tasks/events.py](../core/tasks/events.py)
  - [core/tasks/events_redis.py](../core/tasks/events_redis.py)
  - [api/routes/analyze_async.py](../api/routes/analyze_async.py)
- **建议修复**：引入 `EphemeralEvent` / `PersistentEvent` 类型标记或在事件 payload 中增加 `replay_safe: bool` 字段，让消费方无需了解内部实现即可判断回放安全性。
- **发现日期**：2026-04-18（重构分析时发现）

### 4. TaskRegistry 多级 TTL 清理协调风险
- **问题**：`core/tasks/registry.py` 存在三套独立的清理机制——`result_cache_ttl_sec`（600s，entry 从内存淘汰）、`orphan_pending_chat_max_age_sec`（1800s，pending 状态 chat_messages 标记 error）、启动/关闭时的 `_mark_stale_as_aborted()`——三者的 TTL 窗口和触发时机互不感知。
- **影响**：极端情况下（worker 崩溃 + sweep 间隔过长），chat_messages 可能在 pending 状态停留超过 30 分钟无人回收；entry 过期淘汰后 orphan 清理器找不到对应 entry，跳过清理。
- **涉及文件**：
  - [core/tasks/registry.py](../core/tasks/registry.py)
- **建议修复**：统一 TTL 层级关系（entry TTL ≥ orphan TTL），在 sweep 中增加"entry 已淘汰但 chat_messages 仍 pending"的兜底检查；或将三套清理逻辑收敛到一个 `cleanup()` 方法中统一调度。
- **发现日期**：2026-04-18（重构分析时发现）

### 5. LLM 调用缺少统一超时与重试策略
- **问题**：当前 LLM 调用（主模型 + fast 模型）未统一配置超时和重试策略，部分调用点依赖框架默认值，存在无限等待或无退避重试的风险。
- **影响**：LLM 服务抖动时可能导致请求堆积、线程/协程耗尽，进而引发雪崩。
- **涉及文件**：
  - [core/llm/model_factory.py](../core/llm/model_factory.py)
  - [modules/p2p/model_factory.py](../modules/p2p/model_factory.py)
  - [core/orchestrator/router/__init__.py](../core/orchestrator/router/__init__.py)（L3 LLM 调用）
  - [modules/p2p/report_agent.py](../modules/p2p/report_agent.py)
- **建议修复**：在 `model_factory` 层统一注入 `timeout`、`max_retries`、退避策略配置项（从 `config.yaml` 读取），所有 LLM 调用点继承统一配置。
- **发现日期**：2026-04-18

### 6. 资源上限未全面配置
- **问题**：并发分析任务数、单次查询返回数据量等关键资源缺少硬上限配置，依赖隐式默认值或无限制。
- **影响**：高并发场景下可能导致内存耗尽、数据库连接池打满、响应超时。
- **涉及文件**：
  - [core/tasks/registry.py](../core/tasks/registry.py)（并发任务数）
  - [modules/p2p/repository.py](../modules/p2p/repository.py)（查询返回量）
  - [core/memory/long_term.py](../core/memory/long_term.py)（记忆存储条数）
- **建议修复**：在 `config.yaml` 中增加 `limits` 配置段，统一管理各类资源上限；代码中读取配置并做硬性截断。
- **发现日期**：2026-04-18

### 7. FastAPI 缺少优雅停机处理
- **问题**：FastAPI shutdown 时未等待进行中的分析任务完成或超时取消，直接退出可能丢弃正在处理的请求。
- **影响**：部署更新或重启时，用户正在进行的分析任务会中断，SSE 连接断开无恢复机制。
- **涉及文件**：
  - [api/main.py](../api/main.py)（lifespan shutdown）
  - [core/tasks/registry.py](../core/tasks/registry.py)（任务生命周期）
- **建议修复**：在 lifespan shutdown 阶段调用 `TaskRegistry.shutdown(timeout=30)`，等待所有运行中任务完成或超时后标记为 aborted；同时停止接受新任务。
- **发现日期**：2026-04-18

---

## 已修复

### 8. DATA_LOOKUP 查询缺少 DAG 模板，100% 走 ReAct
- **问题**：所有被识别为 DATA_LOOKUP 的事实查询在 orchestrator 中被强制排除在 DAG 路径之外，只能走 ReAct。
- **修复方案**：采用轻量级 Lookup 快捷路径（非 DAG 模板方案），在 orchestrator 中直调 query_* 工具，跳过 ReAct Agent。通过 `lookup_shortcut_enabled` 配置开关控制。
- **修复文件**：
  - [core/orchestrator/lookup.py](../core/orchestrator/lookup.py)（快捷路径核心逻辑 + 格式化）
  - [core/orchestrator/orchestrator.py](../core/orchestrator/orchestrator.py)（三段决策 + trace span）
  - [modules/p2p/intent_rules.py](../modules/p2p/intent_rules.py)（关键词→工具映射表）
  - [config/settings.py](../config/settings.py) + [config/config.yaml](../config/config.yaml)（开关配置）
- **详细分析**：[docs/data_lookup_react_analysis.md](data_lookup_react_analysis.md)
- **发现日期**：2026-04-18
- **修复日期**：2026-04-19
