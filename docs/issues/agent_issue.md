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

### 1. 意图路由命中率优化（部分修复）
- **问题**：当前 IntentRouter 把较多查询兜底到 ReAct 路径，DAG 命中率不够高。
- **已修复部分**（2026-04-21）：`l3_dag_min_confidence` 从 0.99 调至 0.75，启用 `generic_template_enabled` 和 `lookup_shortcut_enabled`，DAG + ReAct 共存已激活。
- **遗留问题**：路由架构已从 L0+L1+L2+L3 简化为 L0 bypass + Unified LLM，L2 语义检索（Chroma + intent_seeds.yaml）和 L1 关键词分类已从路由流程中移除。当前 DAG/ReAct 分流完全依赖 LLM 置信度单一维度，缺少低成本的预筛机制。
- **影响**：每个非 bypass 查询都需要一次 LLM 调用才能路由，无法在 LLM 之前通过关键词/语义检索快速命中高置信度场景。
- **建议修复**：评估是否需要恢复 L1/L2 层作为 LLM 前置快筛（降低 token 消耗），或维持当前 Unified LLM 方案并持续观察 `l3_dag_min_confidence` 阈值是否合理。
- **发现日期**：（早于 2026-04）
- **部分修复日期**：2026-04-21

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

### 9. Neo4j 后端不支持金额维度排序（amount_desc / amount_asc）
- **问题**：`Neo4jStructuredBackend` 的 4 个查询方法（`query_purchase_orders` / `query_invoices` / `query_receipts` / `query_payments`）在 `order_by` 参数为 `amount_desc` 或 `amount_asc` 时静默降级为日期排序。PostgreSQL 后端通过 `_apply_order_and_limit` 支持金额排序。
- **影响**：使用 `graphiti` / `hybrid` 查询模式时，工具层传入 `order_by=amount_desc` 的查询无法按金额排序，返回按日期倒序的结果。实际影响较低——当前 15 个 DAG 模板和工具调用中 `order_by` 参数默认为空字符串（不排序），仅 ReAct Agent 自主调用时可能传入金额排序。
- **涉及文件**：
  - [core/etl/query_backend.py](../core/etl/query_backend.py)（`_build_tail` 方法）
- **根因**：各实体类型的金额字段名不统一（PO: `total_amount`、Invoice: `invoice_amount`、Payment: `amount`），且 Cypher RETURN 结构不同（PO 返回 `po_props + line_props`，其余返回 `props`），无法用单一 `order_field` 覆盖。
- **建议修复**：在 `_build_tail` 中增加 `amount_field` 参数，各查询方法传入各自的金额节点属性名（如 `po.total_amount`、`n.invoice_amount`）。需注意 RETURN 子句中属性是否可直接引用（`properties(n)` 是 map，不能直接 `ORDER BY properties(n).amount`，需在 RETURN 中显式暴露金额字段）。
- **发现日期**：2026-04-21

### 16. 默认参数值全面审查（时间区间/角色/输出模式等）
- **问题**：多个默认参数值在实际场景中不合理，已发现 `default_time_range_days=30` 导致刚创建的 PO 因 mock 数据时间戳在 30 天窗口之外而查询不到。此外以下默认值尚未经过业务场景验证：
  - `AnalysisSettings.default_time_range_days = 30`：对时间跨度大的业务数据过于窄小，且当实体编号明确时仍叠加时间窗口可能过滤掉目标数据（当前已有 `days=0` 短路逻辑，但仅覆盖"有明确实体编号"场景，无编号但有供应商名称等模糊查询仍受 30 天限制）。
  - `AnalysisRequest.analyst_role = "general"`：是否应根据用户登录角色自动填充而非硬编码。
  - `AnalysisRequest.output_mode = "auto"`：auto 策略当前 data_lookup→chat、其余→detailed，是否需要按 analyst_role 区分（如 management 偏好 brief）。
  - `MemorySettings.chat_history.search_default_days = 30`：跨会话历史搜索窗口是否过短。
  - `IntentRoutingSettings.l3_dag_min_confidence = 0.75`：阈值合理性需结合线上日志统计评估。
- **影响**：用户查询最新数据时可能得到"无结果"的误导性空响应；不同角色用户得到同质化输出；记忆检索遗漏早期重要上下文。
- **涉及文件**：
  - [config/settings.py:263](../config/settings.py#L263)（`default_time_range_days`）
  - [config/settings.py:532](../config/settings.py#L532)（`search_default_days`）
  - [api/schemas/analysis.py:55-70](../api/schemas/analysis.py#L55)（`analyst_role` / `output_mode`）
  - [core/orchestrator/orchestrator.py:617-628](../core/orchestrator/orchestrator.py#L617)（时间窗口解析逻辑）
- **建议修复**：
  1. 将 `default_time_range_days` 从 30 调为 90（或按数据源配置化），确保常规查询覆盖更宽时间范围。
  2. 对"无实体编号但有供应商名称/模糊描述"的场景也考虑放宽时间窗口（当前仅 entity_keys 命中时 days=0）。
  3. 逐项评估上述默认值，结合线上路由命中率日志和用户反馈调整。
  4. 新增集成测试覆盖"当前日期数据可查"场景，防止默认值回归。
- **发现日期**：2026-05-02

### 17. 测试用例体系重构 — 全量通过但实际查询失败的系统性覆盖缺陷（部分修复）
- **问题**：所有 1521 个测试用例通过，但用户通过 API 查询"最新 PO"仍返回空结果。根因是测试体系系统性绕开了真实查询路径中的默认参数：
  - **Fixture 日期脱节**：`tests/fixtures/p2p.py` 硬编码 PO 日期为 2026-03-15 ~ 2026-04-01（距今 31-48 天），全部落在 30 天窗口之外。
  - **Mock 生成器 60 天缓冲**：`mock_data/generator.py` 的 `_rand_creation_date` 方法刻意将"近期数据"上限设为 `today - 60 days`，确保测试数据不与当天碰撞，但副作用是 30 天窗口永远查不到这批数据。
  - **Repository 测试全部 `days=0`**：`test_repository.py` 中所有查询调用显式传 `days=0`（无时间过滤），绕过了默认值。
  - **工具层测试不验证时间过滤效果**：`test_tools.py` 传 `days=30` 但不断言"应有结果"，因为 fixture 数据本就在窗口外，0 结果也算"通过"。
  - **编排层测试 mock 掉 orchestrator**：API 测试（`test_api_routes.py`、`test_memory_via_analyze.py`）mock 了 `orchestrator.analyze`，不走真实的时间窗口解析路径。
  - **Orchestrator 的 `days=0` 短路**：`has_specific_entity` 或 `has_ranking_constraint` 时设 `days=0`，使得带 PO 编号的查询绕过时间过滤——但用户输入"查看最新采购订单"（无编号、无排序约束）时走默认 30 天路径，恰好是测试未覆盖的场景。
- **影响**：测试套件给出 100% 通过的虚假安全感，无法捕获"默认参数 + 真实数据时间分布"组合下的查询空结果 bug。新功能上线后回归测试无法发现此类时间窗口导致的静默降级。
- **涉及文件**：
  - [tests/fixtures/p2p.py](../tests/fixtures/p2p.py)（hardcoded PO 日期）
  - [tests/unit/test_repository.py](../tests/unit/test_repository.py)（`days=0` 绕过）
  - [tests/unit/test_tools.py](../tests/unit/test_tools.py)（不验证结果完整性）
  - [tests/unit/test_api_routes.py](../tests/unit/test_api_routes.py)（mock orchestrator）
  - [modules/p2p/mock_data/generator.py](../modules/p2p/mock_data/generator.py)（60 天缓冲逻辑）
  - [modules/p2p/tools/pg/query.py](../modules/p2p/tools/pg/query.py)（`days=30` 默认值）
  - [modules/p2p/schemas/oracle_ebs/repository.py](../modules/p2p/schemas/oracle_ebs/repository.py)（时间过滤 WHERE 子句）
- **建议修复方向（测试体系重构）**：
  1. **引入"端到端默认路径"测试层**：不 mock orchestrator，用真实 Settings 默认值从 API 层打穿到 Repository 层，验证用户自然语言查询（如"最新采购订单"、"近期价格差异"）能返回非空结果。
  2. **Fixture 日期动态化**：将 `tests/fixtures/p2p.py` 的 PO 日期改为相对当天计算（如 `today - 5 days`、`today - 15 days`），确保始终在默认窗口内。
  3. **Mock 生成器缓冲收窄**：`_rand_creation_date` 的 60 天缓冲改为 10 天，或将"近期"桶的下限提到 `today - 25 days`，确保有数据落在 30 天窗口内。
  4. **Repository 测试增加"默认参数"用例**：新增不传 `days` 参数的测试，断言结果非空（需配合 fixture 日期动态化）。
  5. **工具测试断言非空**：`test_tools.py` 中 `days=30` 的测试应断言 `len(results) > 0`，而非仅验证"不报错"。
  6. **测试分类标签**：引入 `@pytest.mark.integration_default_path` 标记，CI 中单独聚合这类测试的通过率，作为"默认路径健康度"指标。
- **发现日期**：2026-05-02
- **已修复部分**（2026-05-03）：
  - Mock generator 60 天缓冲收窄至 10 天，近期 PO 落入默认 30 天查询窗口。
  - 4 个事件日期字段（creation_date/invoice_date/transaction_date/check_date）从 DATE 升级为 TIMESTAMP，排序确定性（500 条唯一时间戳）。
  - Fixture 硬编码日期改为 `date.today()` 动态计算。
  - Alembic 迁移 0015。
- **遗留**：Repository 测试增加默认参数用例、工具测试断言非空、端到端默认路径测试层尚未实施。

### 19. admin_metrics.py 静默吞异常（chat_index_status indexer 分支）
- **问题**：`api/routes/admin_metrics.py:90` 的 `except Exception: pass` 在获取 chat indexer queue depth 时静默吞异常，违反 CLAUDE.md "不接受 `except: pass` 静默吞异常" 规约。与已修复的 #14 属同类问题。
- **影响**：chat indexer 初始化异常（如模块导入失败）会被静默忽略，运维无法从日志发现问题。实际风险较低（仅影响诊断端点的一个字段），但违反工程规约。
- **涉及文件**：
  - [api/routes/admin_metrics.py:90](../api/routes/admin_metrics.py#L90)
- **建议修复**：将 `except Exception: pass` 改为 `except Exception as exc: _logger.debug("chat indexer queue check failed: %s", exc)`（使用 DEBUG 级别因为是非关键诊断分支）。
- **优先级**：P3
- **发现日期**：2026-05-03

---

## 已修复

### 18. core → modules 直接依赖（已修复）
- **问题**：`core/` 层存在 4 处直接 import `modules.p2p.*`，绕过 `ModuleProvider` Protocol 抽象。
- **修复方案**：
  - `core/orchestrator/orchestrator.py`：移除 `P2PModuleProvider` 硬编码 fallback，`provider` 参数改为必传（API 层和测试层注入）。同时为 `load_dag_template` / `load_generic_template` 调用传入 `provider`。
  - `core/orchestrator/dag/registry.py`：删除 `build_default_registry()`（直接创建 P2PModuleProvider 的兼容入口），统一使用 `build_registry_from_provider(provider)`。
  - `core/orchestrator/dag/templates.py`：`load_generic_template()` 新增 `provider` 参数，通过 `provider.get_generic_dag_templates()` 获取模板。
  - `core/database/init_db.py`：`reset_and_seed()` / `init_database()` 新增 `data_generator_factory` 参数，移除顶层 `MockDataGenerator` import。`api/main.py` 在调用时传入。
  - 测试层（17 个文件）更新：所有 `Orchestrator` 实例化传入 mock provider；`reset_and_seed` / `init_database` 调用传入 `MockDataGenerator`；`resolve_lookup_tool` 调用传入 provider。
- **验证**：`grep -r 'from modules\.\|import modules\.' core/` 输出为空；1637 测试全部通过。
- **发现日期**：2026-05-03
- **修复日期**：2026-05-03

### 15. ParamExtractor 死代码遗留（已修复）
- **问题**：v1 memory 设计 I4 章明确要求"删除 `core/orchestrator/param_extractor.py` — 合并进统一 LLM 调用"。Unified LLM Router 已实施（`core/orchestrator/unified_router.py`），`intent_seeds.yaml` 已删除，但 `param_extractor.py`（含 `ParamExtractor` 类，约 200 行）+ `tests/unit/test_param_extractor.py`（约 20 个测试用例）仍存在。
- **影响**：约 220 行死代码 + 20 个无效测试用例占用维护精力；新开发者读到 `ParamExtractor` 类可能误以为仍在使用；不影响功能（orchestrator 已无调用路径）。
- **涉及文件**：
  - [core/orchestrator/param_extractor.py](../core/orchestrator/param_extractor.py)（已删除）
  - [tests/unit/test_param_extractor.py](../tests/unit/test_param_extractor.py)（已删除）
- **修复方案**：直接删除两个文件；移除 `core/orchestrator/orchestrator.py:160` 的过时注释；将 `core/orchestrator/unified_router.py:4` 和 `core/orchestrator/router/__init__.py:342` 文档字符串/注释中"ParamExtractor"的引用改写为更通用的表述（"独立参数提取器"）。修复后跑全量测试确认无引用残留。
- **发现日期**：2026-05-01
- **修复日期**：2026-05-01

### 10. L1/L2 路由配置残留为死代码（已修复）
- **问题**：`IntentRoutingSettings` 中仍保留 `l1_threshold_default`、`l1_threshold_strict`、`l2_similarity_threshold`、`l2_length_ratio_floor`、`l2_length_ratio_penalty`、`l2_topk` 共 6 个配置项，`config.yaml` 中也有对应条目。但路由架构已简化为 L0 bypass + Unified LLM，这些配置没有任何代码路径引用。
- **影响**：配置文件膨胀，新开发者可能误以为 L1/L2 仍在生效而调参无效果；`IntentRoutingSettings` 模型加载了无用字段。不影响功能。
- **涉及文件**：
  - [config/settings.py](../config/settings.py)（`IntentRoutingSettings` 类定义）
  - [config/config.yaml](../config/config.yaml)（`intent_routing` 段 L1/L2 条目）
- **修复方案**：从 `config/settings.py` 的 `IntentRoutingSettings` 删除 6 个 L1/L2 字段定义；从 `config/config.yaml` 的 `intent_routing` 段删除对应 6 行；同步移除 CLAUDE.md 项目结构中已不存在的 `intent_seeds.yaml` 引用。`Settings` 顶层 `extra="ignore"` 确保用户旧 `config.yaml` 仍能加载。历史设计文档（`docs/intent_routing_hit_rate_optimization.md` 等）中的引用作为存档保留，不清理。若未来决定恢复 L1/L2，从 git 历史恢复即可。
- **发现日期**：2026-04-21
- **修复日期**：2026-05-01

### 14. 图查询工具静默吞异常缺少日志（已修复）
- **问题**：`modules/p2p/tools/pg/advanced.py`（`get_vendor_connection_overview`）使用 `try / except Exception: pass` 捕获 Neo4j Cypher 调用失败后直接返回空列表 `[]`，没有任何 WARNING 以上级别日志。CLAUDE.md "工程要求" 节明确要求"失败必须有日志/metric，不接受 `except: pass` 静默吞异常。任何降级必须有 WARNING 以上日志"。
- **影响**：图后端故障（连接断开、Cypher 语法异常、超时）会被静默降级为"该供应商无连接"，调用方拿到空结果但无法区分"真无连接"还是"查询失败"；运维侧没有可观测信号，无法及时发现 Neo4j 异常。
- **涉及文件**：
  - [modules/p2p/tools/pg/advanced.py](../modules/p2p/tools/pg/advanced.py)
- **修复方案**：模块顶部新增 `from core.logging_utils import get_logger` 与 `_logger = get_logger(__name__)`；将 `except Exception: pass` 改为 `except Exception as exc: _logger.warning("get_vendor_connection_overview cypher failed: %s", exc)`；保留 `return []` 降级行为。同文件其它 `except` 分支已确认无相同模式。
- **发现日期**：2026-05-01
- **修复日期**：2026-05-01

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

### 11. lifespan shutdown 未重置全局工具注入状态（已修复）
- **问题**：`api/main.py` lifespan shutdown 关闭 `GraphitiClient` 后，未调用 `set_graphiti_client(None)` 和 `set_query_backend(None)` 重置 `modules/p2p/tools/_inject.py` 中的全局变量，导致残留已关闭的客户端引用。
- **影响**：
  - 测试环境：e2e 测试创建的 TestClient 退出后，全局 `_graphiti_client` 和 `_query_backend` 残留，后续单元测试调用 PG 查询工具时触发 `RuntimeError: GraphitiClient is not initialised`，造成 22 个测试用例失败。
  - 生产环境：热重启场景下可能引用已关闭的连接。
- **涉及文件**：
  - [api/main.py](../api/main.py)（shutdown 逻辑）
  - [modules/p2p/tools/_inject.py](../modules/p2p/tools/_inject.py)（全局状态管理）
- **修复方案**：在 shutdown 中 `graphiti_client.close()` 后追加 `set_graphiti_client(None)` 和 `set_query_backend(None)`。
- **发现日期**：2026-04-22
- **修复日期**：2026-04-22

### 13. ETL Pipeline 使用 `datetime.utcnow()` 违反时区约定（已修复）
- **问题**：`core/etl/pipeline.py` 在 `update_watermark` 写入 RUNNING 状态时直接调用 `datetime.utcnow()` 生成时间戳，绕过了 `core.time_utils.now_cn()` 统一入口。CLAUDE.md "时区约定" 节明确禁止 `datetime.utcnow()` / `datetime.now(timezone.utc)`。
- **影响**：写入 PostgreSQL 的 RUNNING 水位线为 naive UTC 时间，与系统其它路径产生的业务时区时间戳不一致，可能在 UI 展示或对比逻辑中出现 8 小时偏差；也是后续审计时容易被忽略的隐性退化。功能未直接破坏（同步链路仍可工作），但属于生产标准违规。
- **涉及文件**：
  - [core/etl/pipeline.py](../core/etl/pipeline.py)
- **修复方案**：在文件顶部追加 `from core.time_utils import now_cn`，将 `datetime.utcnow()` 替换为 `now_cn()`；保留 `datetime` 类型注解所需的 import；grep 确认全文件无 `datetime.utcnow|datetime.now(timezone.utc)` 残留。
- **发现日期**：2026-05-01
- **修复日期**：2026-05-01

### 12. DocumentRef 字段未防御 None 输入（已修复）
- **问题**：`api/schemas/domain.py` 的 `DocumentRef` 模型所有字段定义为 `str`（`default=""`），但上游规则引擎（如 `modules/p2p/rules/three_way_match.py`）传入 `vendor_name=None` 时，Pydantic 严格校验抛出 `ValidationError: Input should be a valid string`。
- **影响**：DAG 执行中任务 `t4`（供应商绩效）和 `t5`（依赖 t4 的后续任务）失败，分析结果降级为 `partial_success`，e2e 测试断言 `status == "success"` 失败。
- **涉及文件**：
  - [api/schemas/domain.py](../api/schemas/domain.py)（DocumentRef 模型定义）
  - [modules/p2p/rules/three_way_match.py](../modules/p2p/rules/three_way_match.py)（传入 vendor_name 的调用方）
- **修复方案**：为 DocumentRef 添加 `field_validator`，将所有 `str` 字段的 `None` 输入转为空字符串。
- **发现日期**：2026-04-22
- **修复日期**：2026-04-22
