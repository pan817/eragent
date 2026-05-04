# ETL 模块架构设计

## 1. 包结构

```
core/etl/                          # ETL 核心模块
├── __init__.py
├── config.py                      # GraphitiETLSettings
├── client.py                      # GraphitiClient + QwenCompatibleClient 集成
├── llm_client.py                  # QwenCompatibleClient（Dashscope LLM 兼容）
│                                  # DashscopeCompatibleEmbedder（embedding 批量分片）
├── scheduler.py                   # ETLScheduler（异步手动触发 + 后台执行）
├── pipeline.py                    # ETLPipeline（全量/增量编排 + ETL Trace 集成）
├── state.py                       # SyncStateManager（水位线管理）
├── tables.py                      # ETLSyncState ORM 表
├── models.py                      # 数据模型（GraphitiNode/Edge/TransformResult 等）
├── metrics.py                     # ETL 指标（内存 Counter/Histogram/Gauge）
├── tracing.py                     # ETL 独立 Trace（span tree + 控制台输出）
├── query_backend.py               # 双后端查询抽象：
│                                  #   PostgreSQLBackend / Neo4jStructuredBackend /
│                                  #   GraphitiBackend / HybridBackend + 工厂函数
├── extractors/                    # 抽取器（按域分组）
│   ├── __init__.py
│   ├── base.py                    # BaseExtractor（分页 + max_rows_per_table 限制）
│   ├── purchasing.py              # PO_HEADERS, PO_LINES, PO_DISTRIBUTIONS, PO_LINE_LOCATIONS
│   ├── payables.py                # AP_INVOICES, AP_INVOICE_LINES, AP_INVOICE_DISTRIBUTIONS,
│   │                              #   AP_CHECKS, AP_INVOICE_PAYMENTS, AP_PAYMENT_SCHEDULES
│   ├── receiving.py               # RCV_SHIPMENT_HEADERS, RCV_SHIPMENT_LINES, RCV_TRANSACTIONS
│   ├── master_data.py             # AP_SUPPLIERS, AP_SUPPLIER_SITES, MTL_SYSTEM_ITEMS_B
│   └── sourcing.py                # PON_AUCTION, PON_BID, OKC_K_HEADERS, OKC_K_LINES
├── transformers/                  # 转换器
│   ├── __init__.py
│   ├── registry.py                # 20 张表声明式 TableMapping 注册表
│   ├── structured.py              # StructuredTransformer（EBS 行 → 节点/边）
│   └── llm_extractor.py           # LLMTextExtractor（自由文本 LLM 抽取，可选）
└── loaders/
    ├── __init__.py
    └── graphiti_loader.py         # GraphitiLoader:
                                   #   - 结构化节点 → Cypher MERGE（不走 LLM）
                                   #   - 边 → UNWIND 批量 Cypher MERGE
                                   #   - 自由文本 → graphiti.add_episode（走 LLM）
```

## 2. 核心数据流（实际实现）

```
PostgreSQL (EBS Tables)
    │
    ▼
Extractor (按域分组，分批 yield, max_rows_per_table 限制)
    │
    ▼
StructuredTransformer
    │ 输入: table_name + list[dict]
    │ 输出: TransformResult
    │       ├── nodes: list[GraphitiNode]        ← 结构化映射
    │       ├── edges: list[GraphitiEdge]         ← 结构化映射
    │       └── free_text_records: list[TextRecord] ← 待 LLM 处理
    │
    ├──────────────────────────────────┐
    ▼                                  ▼
GraphitiLoader.load()          GraphitiLoader.load_free_text()
    │ Cypher MERGE 直写              │ graphiti.add_episode()
    │ 节点属性（不走 LLM）            │ LLM 实体抽取
    │                                │
    ▼                                ▼
Neo4j Entity 节点              Graphiti 语义索引
    │
    ▼
GraphitiLoader.create_edges()
    │ UNWIND 批量 Cypher MERGE
    ▼
Neo4j RELATES_TO 关系
```

**关键设计原则**：结构化数据不走 LLM，只有自由文本（comments, description）走 LLM。

## 3. 查询数据流

```
用户查询
    │
    ▼
Orchestrator → IntentRouter → ReAct Agent
    │
    ▼
Provider.get_tools()  ← 根据 query_backend 配置选择工具集
    │
    ├── postgresql 模式: 10 shared + 9 pg_only = 19 工具
    ├── graphiti 模式:   10 shared + 6 graph_only = 16 工具
    └── hybrid 模式:     10 shared + 9 pg_only + 6 graph_only = 25 工具
    │
    ▼
QueryBackend 路由
    │
    ├── PostgreSQLBackend  → SQL 查 PG 表
    ├── Neo4jStructuredBackend → Cypher 查 Entity 节点（同格式 dict）
    └── HybridBackend → Neo4j-first, PG fallback
```

## 4. Qwen/Dashscope 兼容层

graphiti-core 默认使用 OpenAI API，需要适配层：

| 组件 | 问题 | 解决方案 |
|------|------|---------|
| LLM | `responses.parse()` API 不支持 | `QwenCompatibleClient`: JSON mode + 手动 Pydantic 验证 + 字段映射 |
| Embedder | 批量限制 10 条 | `DashscopeCompatibleEmbedder`: 自动分片 ≤10 条/批 |
| CrossEncoder | 需要 OpenAI API key | 复用 Qwen API key + base_url |
| 模型名 | graphiti 默认 gpt-4.1-nano | `small_model` 配置为与主模型相同 |
| Embedding 模型 | text-embedding-3-small 不可用 | 配置 text-embedding-v3 (Dashscope) |

## 5. 全量同步执行顺序

```
POST /admin/etl/trigger {"sync_type": "full"}
    │
    ▼
ETLScheduler._run_manual_sync()  ← asyncio.create_task 后台执行
    │
    ▼
ETLPipeline.run_full_sync()
    │
    ├── 0. clear_graph()  ← DETACH DELETE 清空 Neo4j
    │
    ├── 1. master_data 域
    │   ├── AP_SUPPLIERS        → Cypher MERGE 节点
    │   ├── AP_SUPPLIER_SITES   → Cypher MERGE 节点 + UNWIND 建边
    │   └── MTL_SYSTEM_ITEMS_B  → Cypher MERGE 节点
    │
    ├── 2. purchasing 域
    │   ├── PO_HEADERS_ALL      → 节点 + 边 + free_text(comments)
    │   ├── PO_LINES_ALL        → 节点 + 边(CONTAINS_LINE + ORDERS_MATERIAL)
    │   ├── PO_LINE_LOCATIONS   → 节点
    │   └── PO_DISTRIBUTIONS    → 节点
    │
    ├── 3. receiving 域
    │   ├── RCV_SHIPMENT_HEADERS  → 节点 + free_text(comments)
    │   ├── RCV_SHIPMENT_LINES    → 节点
    │   └── RCV_TRANSACTIONS      → 节点 + 边(BELONGS_TO + RECEIVES_LINE)
    │
    ├── 4. payables 域
    │   ├── AP_INVOICES_ALL       → 节点 + 边 + free_text(description)
    │   ├── AP_INVOICE_LINES      → 节点 + 边
    │   ├── AP_INVOICE_DISTS      → 节点
    │   ├── AP_CHECKS_ALL         → 节点
    │   ├── AP_INVOICE_PAYMENTS   → 节点 + 边(PAYS_INVOICE)
    │   └── AP_PAYMENT_SCHEDULES  → 节点 + 边
    │
    └── 5. sourcing 域
        ├── PON_AUCTION_HEADERS   → 节点
        ├── PON_BID_HEADERS       → 节点 + 边(HAS_BID + BIDS_ON)
        ├── OKC_K_HEADERS_B       → 节点 + free_text(description)
        └── OKC_K_LINES_B         → 节点 + 边(CONTRACT_COVERS)

每张表完成后 → SyncStateManager.update_watermark()
全部完成后 → ETLTracer 打印 span tree 到控制台
```

## 6. 性能指标（20 条/表，本地 Neo4j）

| 指标 | 值 |
|------|-----|
| 总耗时 | ~42 秒 |
| 节点写入 | 301 个 |
| 边创建 | 221 条 |
| LLM 调用 | 1 次（OKC_K_HEADERS 自由文本） |
| 瓶颈 | OKC_K_HEADERS_B 14s（LLM 抽取） |
| 非 LLM 表平均 | 1.5s/表 |
