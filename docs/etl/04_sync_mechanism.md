# 同步机制设计

## 1. 同步状态表（etl_sync_state）

```python
class ETLSyncState(Base):
    __tablename__ = "etl_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    table_name: Mapped[str] = mapped_column(String(60), unique=True)        # EBS 表名，如 "PO_HEADERS_ALL"
    domain: Mapped[str] = mapped_column(String(30))                          # 所属域：purchasing / payables / receiving / master_data / sourcing
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 上次同步完成时间
    last_watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True)) # 上次同步的 LAST_UPDATE_DATE 水位线
    last_sync_status: Mapped[str] = mapped_column(String(20))               # SUCCESS / FAILED / RUNNING
    rows_synced: Mapped[int] = mapped_column(default=0)                     # 上次同步的行数
    total_rows_synced: Mapped[int] = mapped_column(default=0)               # 累计同步行数
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # 失败时的错误信息
    sync_type: Mapped[str] = mapped_column(String(20))                      # FULL / INCREMENTAL
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

需通过 Alembic 迁移创建此表。

## 2. 全量/增量切换逻辑

```
应用启动
    │
    ▼
检查 etl_sync_state 表
    │
    ├── 表不存在或为空
    │   └── 首次启动 → 执行全量同步
    │
    ├── 某个 table_name 无记录
    │   └── 该表执行全量同步，其他表正常增量
    │
    ├── 某个 table 状态为 FAILED
    │   └── 从上次 last_watermark 继续增量重试
    │
    ├── 某个 table 状态为 RUNNING（启动恢复）
    │   └── 标记为 FAILED，下次循环重试
    │
    └── 所有 table 都有 SUCCESS 记录
        └── 正常增量同步
```

## 3. 全量同步流程

```python
async def run_full_sync(self) -> SyncResult:
    """
    全量同步流程：
    1. 按域依赖顺序执行（主数据 → 采购 → 收货 → 应付 → 寻源合同）
       理由：主数据先入图，后续业务数据才能建立关系
    2. 域内按表依赖顺序（头表 → 行表 → 分配表）
    3. 每个表分批抽取（每批 batch_size=500 行），避免内存溢出
    4. 每个表完成后更新 etl_sync_state 水位线
    5. 全量同步期间阻止增量调度触发（通过 asyncio.Lock）
    """

    domain_order = ["master_data", "purchasing", "receiving", "payables", "sourcing"]

    for domain in domain_order:
        extractor = self._extractors[domain]
        for table_name in extractor.table_names():
            # 标记 RUNNING
            await self._state.update_watermark(table_name, domain, ..., status="RUNNING")
            try:
                total_rows = 0
                async for batch in extractor.extract_full():
                    result = self._transformer.transform(table_name, batch)
                    await self._loader.load(result.nodes, result.edges)
                    # LLM 抽取（可选）
                    if self._llm_extractor and result.free_text_records:
                        extra = await self._llm_extractor.extract(result.free_text_records)
                        await self._loader.load_extra_facts(extra)
                    total_rows += len(batch)
                # 标记 SUCCESS
                await self._state.update_watermark(
                    table_name, domain,
                    watermark=max_last_update_date,
                    rows_synced=total_rows,
                    sync_type="FULL",
                    status="SUCCESS",
                )
            except Exception as e:
                # 标记 FAILED，继续下一个表
                await self._state.update_watermark(
                    table_name, domain, ...,
                    status="FAILED",
                    error_message=str(e),
                )
                logger.error("ETL full sync failed for %s", table_name, exc_info=True)
```

## 4. 增量同步流程

```python
async def run_incremental_sync(self) -> SyncResult:
    """
    增量同步流程（每 10 分钟触发）：
    1. 读取各表的 last_watermark
    2. 各域并行执行（增量数据量小，无需严格顺序），域内保持表依赖顺序
    3. 每个表执行：SELECT * FROM {table} WHERE last_update_date > :watermark ORDER BY last_update_date ASC
    4. Graphiti MERGE 语义自动处理新增 + 修改
    5. 更新 last_watermark = max(last_update_date) of this batch
    6. 某表失败记录 FAILED，其他表继续
    """

    domain_tasks = []
    for domain_name, extractor in self._extractors.items():
        task = self._sync_domain_incremental(domain_name, extractor)
        domain_tasks.append(task)

    results = await asyncio.gather(*domain_tasks, return_exceptions=True)
    # 汇总结果...
```

## 5. 调度器实现

```python
class ETLScheduler:
    def __init__(self, pipeline: ETLPipeline, interval_seconds: int = 600):
        self._pipeline = pipeline
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()  # 防止并发执行

    async def start(self) -> None:
        """在 FastAPI lifespan 中调用"""
        self._running = True
        # 检查是否需要全量
        if await self._pipeline.needs_full_sync():
            logger.info("ETL: first run, starting full sync")
            async with self._lock:
                await self._pipeline.run_full_sync()
        # 启动定时增量
        self._task = asyncio.create_task(self._loop())
        logger.info("ETL scheduler started, interval=%ds", self._interval)

    async def _loop(self) -> None:
        """定时增量同步循环"""
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                break
            async with self._lock:
                try:
                    result = await self._pipeline.run_incremental_sync()
                    logger.info("ETL incremental sync completed", extra=result.to_dict())
                except Exception as e:
                    logger.error("ETL incremental sync failed", exc_info=e)

    async def shutdown(self) -> None:
        """优雅停机：等待当前批次完成"""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("ETL scheduler stopped")

    async def trigger_manual_sync(self, sync_type: str = "incremental") -> dict:
        """手动触发（通过 Admin API 调用）"""
        async with self._lock:
            if sync_type == "full":
                result = await self._pipeline.run_full_sync()
            else:
                result = await self._pipeline.run_incremental_sync()
            return result.to_dict()
```
