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
| 图数据库 | Neo4j（升级为必选组件，`enabled` 默认 `true`） |
| 时序知识图谱 | Graphiti（graphiti-core by Zep AI） |
| 同步策略 | 全量初始化 + 每 10 分钟增量（`LAST_UPDATE_DATE`） |
| 映射策略 | 混合模式（结构化字段预定义映射 + 自由文本 LLM 抽取） |
| 部署方式 | 集成到现有 FastAPI 应用内（复用 `core/tasks/` 基础设施） |
| 查询集成 | 主 LangChain Tool（Agent 自主调用）+ 辅 Orchestrator 上下文增强 |
| ERP 数据库 | PostgreSQL（表名/字段名照搬 Oracle EBS） |
| 增量触发 | 定时轮询（每 10 分钟） |
| 变更识别 | `LAST_UPDATE_DATE > 上次水位线` |

## 3. 前置任务

**表结构改造**：当前 7 张简化表需改造为 Oracle EBS 完整表结构（20 张表），详见 [01_ebs_tables.md](01_ebs_tables.md)。

## 4. 子方案文档索引

| 文档 | 内容 |
|------|------|
| [01_ebs_tables.md](01_ebs_tables.md) | Oracle EBS 表结构梳理（20 张表、核心字段） |
| [02_graph_model.md](02_graph_model.md) | Graphiti 节点/边模型设计（15 种节点、19 种关系、时序属性） |
| [03_etl_architecture.md](03_etl_architecture.md) | ETL 模块架构设计（包结构、核心类、数据流） |
| [04_sync_mechanism.md](04_sync_mechanism.md) | 同步机制设计（状态表、全量/增量流程、调度器） |
| [05_mapping_rules.md](05_mapping_rules.md) | 混合映射规则设计（声明式映射、LLM 抽取策略） |
| [06_graph_tools.md](06_graph_tools.md) | 图查询工具设计（6 个 Tool + Orchestrator 上下文增强） |
| [07_config_observability.md](07_config_observability.md) | 配置与可观测性（Settings、指标、日志、Admin API） |
| [08_test_strategy.md](08_test_strategy.md) | 测试策略（单元测试、集成测试、Fixture、覆盖率） |
| [09_compatibility.md](09_compatibility.md) | 兼容性设计（双后端查询模式、过渡路径） |

## 5. 实施顺序

```
Phase 0: 表结构改造（前置）
    ↓
Phase 1: core/etl 基础设施
         - GraphitiClient + GraphitiETLSettings + SyncStateManager
         - Alembic 迁移（etl_sync_state 表）
    ↓
Phase 2: Extractor + StructuredTransformer + GraphitiLoader
         - 先实现主数据域（Supplier, Material）
         - 逐步扩展到采购、收货、应付、寻源合同域
    ↓
Phase 3: ETLScheduler + Pipeline 编排
         - FastAPI lifespan 集成
         - 全量/增量同步
    ↓
Phase 4: 图查询工具
         - 6 个 LangChain Tool
         - Orchestrator 上下文增强
    ↓
Phase 5: LLM 抽取（可选增强）
         - 自由文本字段 LLM 抽取
    ↓
Phase 6: 可观测性 + Admin API
         - 指标埋点、日志完善
         - ETL 管理 API
```
