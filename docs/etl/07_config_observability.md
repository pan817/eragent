# 配置与可观测性

## 1. 配置项设计

### 1.1 GraphitiETLSettings（core/etl/config.py）

```python
class GraphitiETLSettings(BaseSettings):
    # --- 总开关 ---
    enabled: bool = True

    # --- 查询模式 ---
    query_backend: str = "graphiti"     # graphiti / postgresql / hybrid

    # --- 同步调度 ---
    sync_interval_seconds: int = 600
    full_sync_on_startup: bool = True
    batch_size: int = 500
    max_concurrent_domains: int = 3

    # --- Loader 批量与并发 ---
    episode_batch_size: int = 50        # 自由文本 LLM episode 合并行数
    max_load_concurrency: int = 3       # 并发写入数

    # --- 每表行数限制 ---
    max_rows_per_table: int = 20        # 0=不限制, >0=每表同步上限

    # --- 超时与重试 ---
    sync_timeout_seconds: int = 3600
    incremental_timeout_seconds: int = 300
    retry_max_attempts: int = 3
    retry_backoff_seconds: float = 5.0

    # --- LLM 抽取（仅自由文本） ---
    llm_extraction_enabled: bool = False
    llm_extraction_batch_size: int = 20
    llm_extraction_model: str = ""

    # --- 图谱查询 ---
    search_default_top_k: int = 10
    search_timeout_seconds: int = 30
    context_enrichment_enabled: bool = True

    # --- 资源上限 ---
    max_nodes_per_sync: int = 50_000
    max_edges_per_sync: int = 100_000

    model_config = {"populate_by_name": True, "env_prefix": "ETL_"}
```

### 1.2 config.yaml 对应段

```yaml
graphiti_etl:
  enabled: true
  query_backend: "graphiti"
  sync_interval_seconds: 600
  full_sync_on_startup: true
  batch_size: 500
  max_concurrent_domains: 3
  episode_batch_size: 50
  max_load_concurrency: 3
  max_rows_per_table: 20
  sync_timeout_seconds: 3600
  incremental_timeout_seconds: 300
  retry_max_attempts: 3
  retry_backoff_seconds: 5.0
  llm_extraction_enabled: false
  llm_extraction_batch_size: 20
  search_default_top_k: 10
  search_timeout_seconds: 30
  context_enrichment_enabled: true
  max_nodes_per_sync: 50000
  max_edges_per_sync: 100000
```

### 1.3 Neo4jSettings

```python
class Neo4jSettings(BaseSettings):
    enabled: bool = True  # 代码默认 True，config.yaml 可覆盖为 false
```

### 1.4 Intent Routing 开关（控制工具路径）

```yaml
intent_routing:
  l3_dag_min_confidence: 0.99       # 强制走 ReAct（让 Agent 自主选工具）
  generic_template_enabled: false    # 关闭通用 DAG 模板
  lookup_shortcut_enabled: false     # 关闭 lookup 快捷路径
```

## 2. 指标埋点（core/etl/metrics.py）

内存 Counter/Histogram/Gauge，通过 `GET /admin/etl/metrics` 暴露。

| 指标 | 类型 | 维度 | 说明 |
|-----|------|------|------|
| etl_sync_total | Counter | sync_type, status | 同步执行总次数 |
| etl_sync_duration | Histogram | sync_type | 同步耗时（最近 100 次） |
| etl_rows_extracted | Counter | domain, table | 抽取行数 |
| etl_nodes_loaded | Counter | node_type | 写入节点数 |
| etl_edges_loaded | Counter | edge_type | 写入边数 |
| etl_errors | Counter | domain, error_type | 错误数 |
| etl_llm_calls | Counter | - | LLM 抽取调用次数 |
| etl_llm_tokens | Counter | - | LLM 抽取 token 消耗 |
| etl_watermark_lag | Gauge | table | 水位线滞后秒数 |

## 3. ETL 独立 Trace 体系（core/etl/tracing.py）

独立于 TimingMiddleware，不耦合 HTTP 请求生命周期。

### 功能
- Sync/Domain/Table 三级 span tree
- 内存保留最近 20 次 trace
- 每次同步完成后打印到控制台（stderr + logger）
- 通过 `GET /admin/etl/traces` 查询

### 控制台输出示例
```
=== ETL sync 0422abe7 (full) ✓ ok  41975ms ===
├─ [domain:master_data] ✓  2102ms
│  ├─ [table:AP_SUPPLIERS] ✓  614ms  rows=5  nodes=5  edges=0
│  ├─ [table:AP_SUPPLIER_SITES_ALL] ✓  839ms  rows=5  nodes=5  edges=5
│  └─ [table:MTL_SYSTEM_ITEMS_B] ✓  635ms  rows=5  nodes=5  edges=0
├─ [domain:purchasing] ✓  6975ms
│  ├─ [table:PO_HEADERS_ALL] ✓  1840ms  rows=20  nodes=20  edges=20
│  └─ ...
--- summary: total=41975ms  rows=301  nodes=301  edges=221  failed=none ---
```

## 4. Admin API（api/routes/etl.py）

| 路由 | 方法 | 说明 |
|------|------|------|
| /admin/etl/status | GET | 各表水位线、上次同步结果、滞后时间 |
| /admin/etl/trigger | POST | 手动触发同步（**异步后台执行**，立即返回 sync_id） |
| /admin/etl/sync-status | GET | 当前/上次手动同步的执行状态 |
| /admin/etl/metrics | GET | ETL 指标快照 |
| /admin/etl/traces | GET | 最近 20 次同步的 span tree |

### trigger 接口行为

```json
// 请求
POST /admin/etl/trigger
{"sync_type": "full"}

// 立即返回
{"sync_id": "04dc10f8", "sync_type": "full", "state": "running"}

// 轮询
GET /admin/etl/sync-status
{"sync_id": "04dc10f8", "sync_type": "full", "state": "completed", "result": {...}}
```

## 5. 零工具调用防护（Agent 数据诚信）

在 `modules/p2p/agent.py` 的 ReAct 循环中：

1. 首次完成时 `tool_call_count == 0` → WARNING + 注入强制指令 + 自动重试
2. 重试后仍然 `tool_call_count == 0` → ERROR + 拒绝返回伪造内容
3. 返回："未能查询到相关数据，请尝试更明确的查询"

此机制仅在 `long_term_enabled=True`（生产模式）且非 `skip_memory_write` 时生效。
