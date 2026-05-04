# Graphiti ETL 设计方案总览

## 1. 背景与目标

基于 Graphiti（Zep AI）+ Neo4j 构建时序知识图谱，全面增强 ERP Agent 分析能力：

- **图谱关联推理**：Agent 在三路匹配、价格差异等分析时，通过图谱进行更深层的关联推理
- **业务事实时间线**：利用 Graphiti 时序特性，追踪业务数据变化历史（价格变更、供应商评级变化等）
- **替代/补充关系型查询**：部分 P2PRepository SQL 查询迁移到图查询，利用图的关系遍历优势

## 2. 已确认的技术决策

| 决策项 | 选型 |
|-------|------|
| 数据源 | PostgreSQL（镜像 Oracle EBS 表结构） |
| 图数据库 | Neo4j（代码默认启用，config.yaml 可覆盖） |
| 时序知识图谱 | Graphiti（graphiti-core by Zep AI） |
| 同步策略 | 全量初始化 + 手动触发（异步后台执行） |
| 映射策略 | 混合模式（结构化字段 Cypher 直写 + 自由文本 LLM 抽取） |
| 部署方式 | 集成到现有 FastAPI 应用内 |
| 查询集成 | 双后端模式：`query_backend` 配置切换 postgresql / graphiti / hybrid |
| LLM 兼容 | QwenCompatibleClient 适配 Dashscope（非 OpenAI structured output） |
| ERP 数据库 | PostgreSQL（表名/字段名照搬 Oracle EBS） |
| 增量触发 | 手动触发（Admin API），不自动定时同步 |
| 变更识别 | `LAST_UPDATE_DATE > 上次水位线` |

## 3. 前置任务

**表结构改造**：原 7 张简化表已改造为 Oracle EBS 完整表结构（20 张表），详见 [01_ebs_tables.md](01_ebs_tables.md)。

## 4. 子方案文档索引

| 文档 | 内容 |
|------|------|
| [01_ebs_tables.md](01_ebs_tables.md) | Oracle EBS 表结构梳理（20 张表、核心字段） |
| [02_graph_model.md](02_graph_model.md) | Graphiti 节点/边模型设计（15 种节点、19 种关系、时序属性） |
| [03_etl_architecture.md](03_etl_architecture.md) | ETL 模块架构设计（包结构、核心类、数据流） |
| [04_sync_mechanism.md](04_sync_mechanism.md) | 同步机制设计（状态表、全量/增量流程、调度器） |
| [05_mapping_rules.md](05_mapping_rules.md) | 混合映射规则设计（声明式映射、LLM 抽取策略） |
| [06_graph_tools.md](06_graph_tools.md) | 图查询工具设计（6 个 Tool + 双后端工具选择） |
| [07_config_observability.md](07_config_observability.md) | 配置与可观测性（Settings、指标、日志、Admin API、ETL Trace） |
| [08_test_strategy.md](08_test_strategy.md) | 测试策略（单元测试、集成测试、Fixture、覆盖率） |
| [09_compatibility.md](09_compatibility.md) | 兼容性设计（双后端查询模式、工具集选择、过渡路径） |

## 5. 实施顺序与状态

```
Phase 0: 表结构改造（前置）                    ✅ 已完成
    ↓
Phase 1: core/etl 基础设施                     ✅ 已完成
         - GraphitiClient + GraphitiETLSettings + SyncStateManager
         - QwenCompatibleClient（Dashscope LLM 兼容）
         - DashscopeCompatibleEmbedder（embedding 批量分片）
         - Alembic 迁移（etl_sync_state 表）
    ↓
Phase 2: Extractor + StructuredTransformer + GraphitiLoader   ✅ 已完成
         - 5 域 Extractor（master_data/purchasing/receiving/payables/sourcing）
         - 20 张表声明式映射注册表
         - Loader: 结构化节点 Cypher 直写 + 自由文本 add_episode
    ↓
Phase 3: ETLScheduler + Pipeline 编排           ✅ 已完成
         - 异步手动触发（不自动启动）
         - 全量同步前清理 Neo4j
         - 批量建边（UNWIND Cypher）
         - FastAPI lifespan 集成
    ↓
Phase 4: 图查询工具                            ✅ 已完成
         - 6 个 LangChain Tool
         - 双后端工具集选择（Provider 按 query_backend 注入）
    ↓
Phase 5: LLM 抽取（可选增强）                   ✅ 已完成
         - LLMTextExtractor
         - 仅自由文本字段走 LLM（comments, description）
    ↓
Phase 6: 可观测性 + Admin API                   ✅ 已完成
         - ETL 指标埋点（9 类 Counter/Histogram/Gauge）
         - ETL 独立 Trace 体系（span tree + 控制台打印）
         - Admin API（status/trigger/sync-status/metrics/traces）
         - 零工具调用防护（防止 Agent 伪造数据）

补充改造:
  - Neo4jStructuredBackend（Cypher 查 Entity 节点返回结构化 dict）
  - Analysis 工具改为 async + QueryBackend
  - Provider 按 query_backend 选择工具集（postgresql/graphiti/hybrid）
  - mock_data 20 条 + max_rows_per_table 20
  - migrations/env.py 事务回滚 bug 修复
```

## 6. 关键架构决策（实施中调整）

| 原设计 | 实际实现 | 原因 |
|--------|---------|------|
| 所有数据走 `add_episode` (LLM) | 结构化数据 Cypher 直写，仅自由文本走 LLM | 性能：362s → 42s |
| 边嵌入 episode body | 边通过 Cypher MERGE 独立创建 | 跨 episode 实体引用无法解析 |
| 定时自动同步 | 手动触发（Admin API） | 生产环境需控制同步时机 |
| 逐条建边 | UNWIND 批量建边 | 40 边从 3s 降到 0.2s |
| OpenAI structured output | QwenCompatibleClient (JSON mode + 手动验证) | Dashscope 不支持 responses.parse API |
| TimingMiddleware 复用 | ETL 独立 Trace 体系 | 生命周期和消费方不同 |
