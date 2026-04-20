# 配置与可观测性

## 1. 配置项设计

### 1.1 GraphitiETLSettings（config/settings.py 新增）

```python
class GraphitiETLSettings(BaseSettings):
    # --- ETL 总开关 ---
    enabled: bool = True

    # --- 查询模式 ---
    query_backend: str = "graphiti"               # "graphiti"(主) / "postgresql"(降级) / "hybrid"(双查)

    # --- 同步调度 ---
    sync_interval_seconds: int = 600              # 增量轮询间隔（10 分钟）
    full_sync_on_startup: bool = True             # 首次启动时是否自动全量
    batch_size: int = 500                         # 每批抽取行数
    max_concurrent_domains: int = 3               # 增量同步时域级并发数

    # --- 超时与重试 ---
    sync_timeout_seconds: int = 3600              # 单次全量同步超时（1 小时）
    incremental_timeout_seconds: int = 300        # 单次增量同步超时（5 分钟）
    retry_max_attempts: int = 3                   # 失败重试次数
    retry_backoff_seconds: float = 5.0            # 重试退避基数

    # --- LLM 抽取 ---
    llm_extraction_enabled: bool = False          # 自由文本 LLM 抽取开关（默认关闭）
    llm_extraction_batch_size: int = 20           # LLM 批量抽取条数
    llm_extraction_model: str = ""                # 为空则自动取 llm_fast 配置

    # --- 图谱查询 ---
    search_default_top_k: int = 10                # 图谱搜索默认返回数
    search_timeout_seconds: int = 30              # 单次查询超时
    context_enrichment_enabled: bool = True       # Orchestrator 上下文增强开关

    # --- 资源限制 ---
    max_nodes_per_sync: int = 50000               # 单次同步最大节点数
    max_edges_per_sync: int = 100000              # 单次同步最大边数

    model_config = {"populate_by_name": True, "env_prefix": "ETL_"}
```

### 1.2 config.yaml 新增段

```yaml
graphiti_etl:
  enabled: true
  query_backend: "graphiti"             # graphiti / postgresql / hybrid
  sync_interval_seconds: 600
  full_sync_on_startup: true
  batch_size: 500
  max_concurrent_domains: 3
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

### 1.3 Neo4jSettings 变更

```python
# enabled 默认值从 false 改为 true
class Neo4jSettings(BaseSettings):
    enabled: bool = True  # 原为 False
```

## 2. 指标埋点

| 指标 | 类型 | 维度 | 说明 |
|-----|------|------|------|
| etl_sync_total | Counter | sync_type, status | 同步执行总次数 |
| etl_sync_duration_seconds | Histogram | sync_type | 同步耗时 |
| etl_rows_extracted | Counter | domain, table | 抽取行数 |
| etl_nodes_loaded | Counter | node_type | 写入节点数 |
| etl_edges_loaded | Counter | edge_type | 写入边数 |
| etl_errors | Counter | domain, error_type | 错误数 |
| etl_llm_calls | Counter | - | LLM 抽取调用次数 |
| etl_llm_tokens | Counter | - | LLM 抽取 token 消耗 |
| etl_watermark_lag_seconds | Gauge | table | 水位线滞后（now - last_watermark） |

## 3. 日志规范

```python
# 同步开始
logger.info("ETL sync started", extra={
    "sync_type": "incremental",
    "tables": ["PO_HEADERS_ALL", ...],
})

# 单表完成
logger.info("ETL table synced", extra={
    "table": "PO_HEADERS_ALL",
    "rows_extracted": 42,
    "nodes_loaded": 42,
    "edges_loaded": 85,
    "duration_ms": 1234,
})

# 错误（不静默吞异常）
logger.error("ETL extraction failed", extra={
    "table": "AP_INVOICES_ALL",
    "error_type": "DatabaseError",
    "watermark": "2026-04-20T10:00:00",
}, exc_info=True)

# 同步完成汇总
logger.info("ETL sync completed", extra={
    "sync_type": "incremental",
    "total_rows": 156,
    "total_nodes": 156,
    "total_edges": 312,
    "total_duration_ms": 5678,
    "failed_tables": [],
})
```

## 4. Admin API

| 路由 | 方法 | 说明 |
|------|------|------|
| /api/v1/ptp-agent/admin/etl/status | GET | 各表水位线、上次同步结果、滞后时间 |
| /api/v1/ptp-agent/admin/etl/trigger | POST | 手动触发同步（body: `{"sync_type": "full" \| "incremental"}`） |
| /api/v1/ptp-agent/admin/etl/metrics | GET | ETL 指标汇总 |

### 路由文件

新增 `api/routes/etl.py`，挂载到 FastAPI app。

```python
router = APIRouter(prefix="/admin/etl", tags=["ETL Admin"])

@router.get("/status")
async def get_etl_status() -> list[dict]:
    """返回所有表的同步状态"""
    state_manager = get_sync_state_manager()
    return await state_manager.get_all_states()

@router.post("/trigger")
async def trigger_etl_sync(body: TriggerRequest) -> dict:
    """手动触发 ETL 同步"""
    scheduler = get_etl_scheduler()
    return await scheduler.trigger_manual_sync(body.sync_type)

@router.get("/metrics")
async def get_etl_metrics() -> dict:
    """返回 ETL 指标汇总"""
    ...
```
