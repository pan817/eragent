# 兼容性设计：双后端查询模式

## 1. 背景

- **Neo4j（Graphiti）为主**：通过 Cypher 查询结构化 Entity 节点 + Graphiti 语义搜索
- **PostgreSQL 为辅**：保留完整 SQL 查询能力，确保 ETL 未就绪或 Neo4j 不可用时系统正常运行
- 通过配置开关切换，两种模式共存

## 2. 查询模式开关

通过 `graphiti_etl.query_backend` 配置项控制：

| 值 | 含义 | 工具数 | 适用场景 |
|----|------|--------|---------|
| `graphiti` | 全部走 Neo4j Cypher 查询 | 16 | ETL 已完成全量同步 |
| `postgresql` | 全部走 SQL 查询 | 19 | ETL 未部署、Neo4j 不可用 |
| `hybrid` | Neo4j 优先，失败降级 SQL | 25 | 灰度切换阶段 |

## 3. 实现方案：QueryBackend + 工具集选择

### 3.1 QueryBackend 层（core/etl/query_backend.py）

```
QueryBackend (Protocol)
    ├── PostgreSQLBackend      → 委托 P2PRepository（SQL）
    ├── Neo4jStructuredBackend → Cypher 查 Entity 节点（结构化 dict）
    ├── GraphitiBackend        → Graphiti 语义搜索（facts）
    └── HybridBackend          → Neo4j-first + PG fallback
```

**Neo4jStructuredBackend** 的关键设计：
- 查询 ETL 写入的 `Entity` 节点（Cypher MERGE 写入，包含完整结构化属性）
- 返回与 PostgreSQLBackend **完全相同的 dict 格式**
- 规则引擎（ThreeWayMatch、PriceVariance 等）无需任何修改

### 3.2 Protocol 接口

```python
class QueryBackend(Protocol):
    async def query_purchase_orders(self, **kwargs) -> list[dict]: ...
    async def query_invoices(self, **kwargs) -> list[dict]: ...
    async def query_receipts(self, **kwargs) -> list[dict]: ...
    async def query_payments(self, **kwargs) -> list[dict]: ...
    async def query_suppliers(self, **kwargs) -> list[dict]: ...
    async def get_contract_prices(self) -> dict[str, float]: ...
    async def search(self, query: str, **kwargs) -> list[dict]: ...
    async def get_entity_timeline(self, entity_type, entity_id) -> list[dict]: ...
    async def get_entity_relationships(self, entity_type, entity_id, depth) -> list[dict]: ...
```

### 3.3 工具集选择（Provider.get_tools()）

```python
def get_tools(self) -> list[Any]:
    mode = get_settings().graphiti_etl.query_backend

    # 共享工具：query (4) + analysis (6) — 通过 QueryBackend 自动切换后端
    shared = [query_purchase_orders, ..., calculate_spend_analysis]

    # PostgreSQL 专属：advanced (5) + stub (4)
    pg_only = [analyze_receipt_anomalies, ..., check_blacklist]

    # Graph 专属 (6)
    graph_only = [search_knowledge_graph, ..., detect_graph_anomalies]

    if mode == "postgresql":
        return shared + pg_only       # 19 工具
    elif mode == "graphiti":
        return shared + graph_only    # 16 工具
    else:  # hybrid
        return shared + pg_only + graph_only  # 25 工具
```

### 3.4 工具注入点（api/main.py lifespan）

```python
from core.etl.query_backend import create_query_backend
qb = create_query_backend(
    query_backend_mode=etl_cfg.query_backend,
    repository=P2PRepository(session_factory),
    graphiti_client=graphiti_client,
)
set_query_backend(qb)
```

## 4. 工具层适配

### 4.1 query 工具（4 个）— 通过 QueryBackend

```python
@tool
async def query_purchase_orders(...) -> str:
    backend = _get_query_backend()  # 自动选择 PG/Neo4j
    pos = await backend.query_purchase_orders(...)
    return _clip_and_dump(pos)
```

### 4.2 analysis 工具（6 个）— 通过 QueryBackend

```python
async def _run_three_way_match_impl(po_number: str) -> str:
    backend = _get_query_backend()
    po_lines = await backend.query_purchase_orders(po_number=po_number, days=0)
    gr_lines = await backend.query_receipts(po_number=po_number, days=0)
    invoice_lines = await backend.query_invoices(po_number=po_number, days=0)
    # 规则引擎不变 — 接收相同格式的 dict
    checker = ThreeWayMatchChecker(...)
    anomalies = checker.check(po_lines, gr_lines, invoice_lines)
```

### 4.3 graph 工具（6 个）— 仅 graphiti/hybrid 模式注入

```python
@tool
async def search_knowledge_graph(query, ...) -> str:
    client = _get_graphiti_client()
    results = await client.search(query, ...)
```

### 4.4 advanced 工具（5 个）— 仅 postgresql/hybrid 模式注入

保持直接使用 `P2PRepository._session_factory()` 做复杂 SQL 聚合。

## 5. ETL 与查询模式的关系

| ETL enabled | query_backend | 行为 |
|-------------|---------------|------|
| true | graphiti | ETL 同步 + 纯 Neo4j 查询（正式模式） |
| true | hybrid | ETL 同步 + Neo4j 优先 SQL 降级（灰度） |
| true | postgresql | ETL 同步但查询仍走 SQL（仅做数据预热） |
| false | postgresql | 无 ETL，纯 SQL 查询（过渡期 / 降级） |
| false | graphiti | Neo4j 未连接时自动降级到 postgresql |
| false | hybrid | 无 ETL，所有查询降级到 SQL |

## 6. 过渡路径

```
Phase 1: query_backend=postgresql, etl.enabled=false
         → 现有行为不变

Phase 2: query_backend=hybrid, etl.enabled=true
         → ETL 同步，Neo4j 优先 SQL 降级
         → 验证 Neo4j 查询结果与 SQL 一致

Phase 3: query_backend=graphiti, etl.enabled=true
         → 纯 Neo4j 查询
         → PostgreSQL 查询路径保留但不使用

Phase 4: 退役 PostgreSQL 查询路径（可选）
         → 移除 advanced 工具或改写为 Cypher
```

## 7. 已知限制

| 限制 | 说明 | 影响 |
|------|------|------|
| advanced 工具无 Neo4j 版 | 5 个聚合查询工具仅 PG 可用 | graphiti 模式下不可用 |
| `calculate_spend_analysis` | SQL GROUP BY 聚合，未迁移 | 两种模式下都走 PG |
| Neo4j Entity 属性名 | 可能与 Cypher 查询的 WHERE 条件不匹配 | 需要 ETL 写入时保证属性名一致 |
