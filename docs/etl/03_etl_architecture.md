# ETL 模块架构设计

## 1. 包结构

```
core/etl/                          # ETL 核心模块
├── __init__.py
├── config.py                      # ETL 配置（GraphitiETLSettings）
├── client.py                      # Graphiti 客户端封装
├── scheduler.py                   # 定时调度器（10 分钟轮询）
├── pipeline.py                    # ETL 管道编排（全量/增量）
├── state.py                       # 同步水位线管理（etl_sync_state 表）
├── tables.py                      # ORM 表定义（etl_sync_state）
├── extractors/                    # 抽取器（按域分组）
│   ├── __init__.py
│   ├── base.py                    # BaseExtractor 抽象基类
│   ├── purchasing.py              # 采购域：PO_HEADERS, PO_LINES, PO_DISTRIBUTIONS, PO_LINE_LOCATIONS
│   ├── payables.py                # 应付域：AP_INVOICES, AP_INVOICE_LINES, AP_INVOICE_DISTRIBUTIONS,
│   │                              #         AP_CHECKS, AP_INVOICE_PAYMENTS, AP_PAYMENT_SCHEDULES
│   ├── receiving.py               # 收货域：RCV_SHIPMENT_HEADERS, RCV_SHIPMENT_LINES, RCV_TRANSACTIONS
│   ├── master_data.py             # 主数据：AP_SUPPLIERS, AP_SUPPLIER_SITES, MTL_SYSTEM_ITEMS_B
│   └── sourcing.py                # 寻源合同：PON_AUCTION, PON_BID, OKC_K_HEADERS, OKC_K_LINES
├── transformers/                  # 转换器
│   ├── __init__.py
│   ├── base.py                    # BaseTransformer 抽象基类
│   ├── structured.py              # 结构化字段 → Graphiti 节点/边的预定义映射
│   └── llm_extractor.py           # 自由文本 LLM 抽取（comments, description 等）
└── loaders/                       # 加载器
    ├── __init__.py
    ├── base.py                    # BaseLoader 抽象基类
    └── graphiti_loader.py         # Graphiti API 写入（节点 + 边 + 时序属性）
```

## 2. 核心类与职责

### 2.1 GraphitiClient (`client.py`)

封装 graphiti-core SDK 初始化与生命周期。

```python
class GraphitiClient:
    """Graphiti 客户端封装，管理与 Neo4j 的连接"""

    def __init__(self, settings: Settings): ...

    async def initialize(self) -> None:
        """初始化 Graphiti 实例，连接 Neo4j"""

    async def close(self) -> None:
        """释放连接资源"""

    async def add_episode(self, data: EpisodeData) -> None:
        """写入一条 episode（Graphiti 的基本数据单元）"""

    async def search(self, query: str, num_results: int = 10) -> list[dict]:
        """语义搜索 + 图遍历混合查询"""

    async def get_entity(self, entity_type: str, entity_id: str) -> dict | None:
        """按类型和 ID 获取单个实体"""

    async def get_relationships(self, entity_type: str, entity_id: str, depth: int = 2) -> list[dict]:
        """获取实体的关联关系网络"""
```

- 应用启动时初始化（FastAPI lifespan），关闭时释放
- 复用现有 `Neo4jSettings` 的连接配置

### 2.2 ETLScheduler (`scheduler.py`)

定时调度器，集成到 FastAPI 生命周期。

```python
class ETLScheduler:
    """ETL 定时调度器"""

    def __init__(self, pipeline: ETLPipeline, interval_seconds: int = 600): ...

    async def start(self) -> None:
        """启动调度：检查是否需要全量 → 启动定时增量循环"""

    async def shutdown(self) -> None:
        """优雅停机：等待当前批次完成后退出"""

    async def trigger_manual_sync(self, sync_type: str = "incremental") -> dict:
        """手动触发同步（通过 Admin API 调用），返回同步结果摘要"""
```

- 内部持有 `asyncio.Lock` 防止并发执行
- 首次启动时检查是否需要全量初始化
- 支持通过 Admin API 手动触发

### 2.3 ETLPipeline (`pipeline.py`)

ETL 管道编排器，协调 Extract → Transform → Load 流程。

```python
class ETLPipeline:
    """ETL 管道编排"""

    def __init__(
        self,
        extractors: dict[str, BaseExtractor],
        transformer: StructuredTransformer,
        llm_extractor: LLMTextExtractor | None,
        loader: GraphitiLoader,
        state_manager: SyncStateManager,
    ): ...

    async def needs_full_sync(self) -> bool:
        """检查是否需要全量同步（水位线为空）"""

    async def run_full_sync(self) -> SyncResult:
        """全量同步：按域依赖顺序执行"""

    async def run_incremental_sync(self) -> SyncResult:
        """增量同步：各域并行，域内保持表依赖顺序"""
```

- 两种模式：全量（按顺序）/ 增量（可并行）
- 按域顺序执行：主数据 → 采购 → 收货 → 应付 → 寻源合同
- 每个域完成后更新水位线

### 2.4 BaseExtractor (`extractors/base.py`)

抽取器抽象基类，各域继承实现。

```python
class BaseExtractor(ABC):
    """数据抽取器基类"""

    def __init__(self, session_factory: sessionmaker): ...

    @abstractmethod
    async def extract_full(self) -> AsyncIterator[list[dict]]:
        """全量抽取，分批 yield（每批 batch_size 行）"""

    @abstractmethod
    async def extract_incremental(self, since: datetime) -> AsyncIterator[list[dict]]:
        """增量抽取：WHERE last_update_date > since"""

    @abstractmethod
    def table_names(self) -> list[str]:
        """本抽取器负责的表名列表"""

    @abstractmethod
    def domain(self) -> str:
        """所属域名：purchasing / payables / receiving / master_data / sourcing"""
```

- 分批 yield 避免内存溢出（默认每批 500 行）
- 每个域一个具体实现（如 `PurchasingExtractor`）

### 2.5 StructuredTransformer (`transformers/structured.py`)

结构化字段到 Graphiti 节点/边的预定义映射。

```python
class StructuredTransformer:
    """声明式映射：EBS 行 → Graphiti 节点 + 边"""

    def __init__(self, mapping_registry: dict[str, TableMapping]): ...

    def transform(self, table_name: str, rows: list[dict]) -> TransformResult:
        """
        将一批 EBS 行数据转换为节点和边。
        返回 TransformResult(nodes=[], edges=[], free_text_records=[])
        """
```

- 纯函数，无副作用，便于测试
- 通过 `MAPPING_REGISTRY` 声明式配置每张表的映射规则
- 自动收集 `free_text_fields` 标记的字段，传给 LLM 抽取器

### 2.6 LLMTextExtractor (`transformers/llm_extractor.py`)

自由文本字段的 LLM 抽取器。

```python
class LLMTextExtractor:
    """从自由文本中抽取额外的实体和关系"""

    def __init__(self, llm, enabled: bool = True, batch_size: int = 20): ...

    async def extract(self, records: list[TextRecord]) -> list[ExtraFact]:
        """
        批量调用 LLM 抽取文本中的业务事实。
        输入：[{source_node_id, source_node_type, field_name, text}]
        输出：[{fact_text, related_entities, sentiment, urgency_flag}]
        """
```

- 使用 `llm_fast` 模型控制成本
- 可通过配置开关启用/禁用
- 批量处理（累积 20 条一次调用）

### 2.7 GraphitiLoader (`loaders/graphiti_loader.py`)

Graphiti API 写入器。

```python
class GraphitiLoader:
    """通过 Graphiti API 写入节点和边"""

    def __init__(self, client: GraphitiClient, batch_size: int = 100): ...

    async def load(self, nodes: list[GraphitiNode], edges: list[GraphitiEdge]) -> LoadResult:
        """
        批量写入节点和边到 Graphiti。
        幂等（基于唯一标识 MERGE 语义）。
        返回 LoadResult(loaded=N, skipped=N, failed=N)
        """
```

- 批量写入，控制并发
- 幂等：重复写入不产生副作用
- 记录写入统计

### 2.8 SyncStateManager (`state.py`)

同步水位线管理。

```python
class SyncStateManager:
    """管理 ETL 同步状态（水位线）"""

    def __init__(self, session_factory: sessionmaker): ...

    async def get_watermark(self, table_name: str) -> datetime | None:
        """获取指定表的最后同步水位线"""

    async def update_watermark(
        self, table_name: str, domain: str, watermark: datetime,
        rows_synced: int, sync_type: str, status: str, error_message: str | None = None,
    ) -> None:
        """更新水位线和同步状态"""

    async def needs_full_sync(self, table_names: list[str]) -> bool:
        """检查是否有表缺少水位线记录（需要全量同步）"""

    async def get_all_states(self) -> list[dict]:
        """获取所有表的同步状态（Admin API 用）"""
```

## 3. 数据流

### 3.1 ETL 管道数据流

```
PostgreSQL (EBS Tables)
    │
    ▼
Extractor (按域分组，分批 yield list[dict])
    │
    ▼
StructuredTransformer
    │ 输入: table_name + list[dict]
    │ 输出: TransformResult
    │       ├── nodes: list[GraphitiNode]        ← 结构化映射
    │       ├── edges: list[GraphitiEdge]         ← 结构化映射
    │       └── free_text_records: list[TextRecord] ← 待 LLM 处理
    │
    ├─────────────────────────────────┐
    ▼                                 ▼
GraphitiLoader                  LLMTextExtractor (可选)
    │                                 │
    │ 写入 nodes + edges              │ 抽取额外 facts
    │                                 │
    ▼                                 ▼
Graphiti (Neo4j)              GraphitiLoader (追加写入)
                                      │
                                      ▼
                              Graphiti (Neo4j)
```

### 3.2 全量同步执行顺序

```
ETLPipeline.run_full_sync()
    │
    ├── 1. master_data 域（顺序执行）
    │   ├── AP_SUPPLIERS
    │   ├── AP_SUPPLIER_SITES_ALL
    │   └── MTL_SYSTEM_ITEMS_B
    │
    ├── 2. purchasing 域（顺序执行）
    │   ├── PO_HEADERS_ALL
    │   ├── PO_LINES_ALL
    │   ├── PO_LINE_LOCATIONS_ALL
    │   └── PO_DISTRIBUTIONS_ALL
    │
    ├── 3. receiving 域（顺序执行）
    │   ├── RCV_SHIPMENT_HEADERS
    │   ├── RCV_SHIPMENT_LINES
    │   └── RCV_TRANSACTIONS
    │
    ├── 4. payables 域（顺序执行）
    │   ├── AP_INVOICES_ALL
    │   ├── AP_INVOICE_LINES_ALL
    │   ├── AP_INVOICE_DISTRIBUTIONS_ALL
    │   ├── AP_CHECKS_ALL
    │   ├── AP_INVOICE_PAYMENTS_ALL
    │   └── AP_PAYMENT_SCHEDULES_ALL
    │
    └── 5. sourcing 域（顺序执行）
        ├── PON_AUCTION_HEADERS_ALL
        ├── PON_BID_HEADERS
        ├── OKC_K_HEADERS_B
        └── OKC_K_LINES_B

每个表完成后 → SyncStateManager.update_watermark()
```

### 3.3 增量同步执行顺序

```
ETLPipeline.run_incremental_sync()
    │
    ├── 并行执行各域（asyncio.gather）
    │   ├── master_data 域（域内顺序）
    │   ├── purchasing 域（域内顺序）
    │   ├── receiving 域（域内顺序）
    │   ├── payables 域（域内顺序）
    │   └── sourcing 域（域内顺序）
    │
    └── 各域独立成功/失败，互不影响

增量 SQL:
  SELECT * FROM {table}
  WHERE last_update_date > :watermark
  ORDER BY last_update_date ASC
  LIMIT :batch_size
```

### 3.4 FastAPI 生命周期集成

```
FastAPI startup (lifespan)
├── [现有] DB → Repositories → TraceStore → EventBus → TaskRegistry
│
├── [新增] GraphitiClient.initialize()
│   └── 连接 Neo4j，初始化 Graphiti
│
└── [新增] ETLScheduler.start()
    ├── 检查 needs_full_sync()
    │   ├── True → run_full_sync()
    │   └── False → 跳过
    └── 启动定时增量循环（每 10 分钟）

FastAPI shutdown
├── [新增] ETLScheduler.shutdown()
│   └── 等待当前批次完成
├── [新增] GraphitiClient.close()
├── [现有] TaskRegistry.shutdown()
└── [现有] engine.dispose()
```
