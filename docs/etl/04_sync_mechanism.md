# 同步机制设计

## 1. 同步状态表（etl_sync_state）

```python
class ETLSyncState(Base):
    __tablename__ = "etl_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    table_name: Mapped[str] = mapped_column(String(60), unique=True)
    domain: Mapped[str] = mapped_column(String(30))
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str] = mapped_column(String(20))  # SUCCESS / FAILED / RUNNING
    rows_synced: Mapped[int] = mapped_column(default=0)
    total_rows_synced: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_type: Mapped[str] = mapped_column(String(20))  # FULL / INCREMENTAL
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

## 2. 触发方式

**仅手动触发**，不自动启动：

```
POST /api/v1/ptp-agent/admin/etl/trigger
{"sync_type": "full"}   → 全量同步（清空 Neo4j + 重建）
{"sync_type": "incremental"} → 增量同步（基于水位线）
```

- 异步后台执行（`asyncio.create_task`），立即返回 `sync_id`
- 通过 `GET /admin/etl/sync-status` 轮询进度
- 防并发锁（`asyncio.Lock`）

## 3. 全量同步流程

```python
async def run_full_sync(self) -> SyncResult:
    # 1. 清空 Neo4j（DETACH DELETE）
    await self._loader._client.clear_graph()

    # 2. 按域依赖顺序执行（主数据 → 采购 → 收货 → 应付 → 寻源合同）
    with etl_tracer.sync("full") as sync_id:
        for domain in ["master_data", "purchasing", "receiving", "payables", "sourcing"]:
            for table in extractor.table_names():
                # 标记 RUNNING
                # Extract（分页 + max_rows_per_table 限制）
                # Transform（StructuredTransformer → nodes + edges + free_text）
                # Load 节点（Cypher MERGE，不走 LLM）
                # Load 自由文本（add_episode，走 LLM）
                # Create 边（UNWIND 批量 Cypher）
                # 标记 SUCCESS + 更新水位线
```

## 4. 增量同步流程

```python
async def run_incremental_sync(self) -> SyncResult:
    # 各域并行执行（Semaphore 控制并发度）
    # 域内保持表依赖顺序
    # WHERE last_update_date > watermark ORDER BY last_update_date ASC
    # Cypher MERGE 幂等写入（新增 + 修改）
```

## 5. ETLScheduler 实现

```python
class ETLScheduler:
    def __init__(self, pipeline, state_manager, interval_seconds, full_sync_on_startup):
        self._lock = asyncio.Lock()
        self._active_sync = None  # 追踪当前同步状态

    async def trigger_manual_sync(self, sync_type) -> dict:
        # 防重复：检查 _active_sync.state == "running"
        # asyncio.create_task 后台执行
        # 立即返回 {"sync_id": "xxx", "state": "running"}

    def get_sync_status(self) -> dict:
        # 返回当前/上次同步的 state/result

    async def start(self):
        # 仅做 crash recovery（标记 stale RUNNING → FAILED）
        # 不自动启动同步循环
```

## 6. Loader 数据路径（关键架构决策）

```
结构化字段（所有表的 ORM 列）
    → Cypher MERGE 直接写 Neo4j Entity 节点
    → 不调用 LLM，不做 embedding
    → 写入属性：name, uuid, summary, entity_type, entity_id + 业务字段

自由文本字段（comments, description）
    → graphiti.add_episode()
    → 调用 LLM 实体抽取 + embedding
    → 仅 PO_HEADERS.comments, AP_INVOICES.description,
      RCV_SHIPMENT_HEADERS.comments, OKC_K_HEADERS.description/short_description

边关系（19 种 EdgeMapping）
    → UNWIND 批量 Cypher MERGE
    → MATCH (src:Entity {name: $source_name}) MATCH (tgt:Entity {name: $target_name})
      MERGE (src)-[r:RELATES_TO {name: $edge_type}]->(tgt)
```

## 7. 数据量限制

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `max_rows_per_table` | 20 | 每表同步上限（0=不限制） |
| `batch_size` | 500 | 分页抽取每批行数 |
| `episode_batch_size` | 50 | 自由文本 LLM 合并行数 |
| `max_load_concurrency` | 3 | 并发写入数 |
| `mock_data.record_count` | 20 | 模拟数据生成条数 |
