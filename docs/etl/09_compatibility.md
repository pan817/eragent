# 兼容性设计：双后端查询模式

## 1. 背景

- **Neo4j（Graphiti）为主**：正式的数据查询方式，图谱关联推理 + 时序分析
- **PostgreSQL 为辅**：过渡期保留，确保 ETL 未就绪或 Neo4j 不可用时系统仍可正常运行
- 最终目标：过渡期结束后，PostgreSQL 查询路径退役

## 2. 查询模式开关

通过 `graphiti_etl.query_backend` 配置项控制：

| 值 | 含义 | 适用场景 |
|----|------|---------|
| `graphiti` | 全部走图查询（默认） | 正式生产，ETL 已完成全量同步 |
| `postgresql` | 全部走 SQL 查询（降级） | ETL 未部署、Neo4j 不可用、过渡期 |
| `hybrid` | 图查询优先，失败时降级到 SQL | 灰度切换阶段 |

## 3. 实现方案：QueryBackend 抽象层

在现有 Tool 和 Orchestrator 之间新增一个查询后端抽象层。

### 3.1 接口定义

```python
# core/etl/query_backend.py

class QueryBackend(Protocol):
    """查询后端协议"""

    async def query_purchase_orders(self, **kwargs) -> list[dict]: ...
    async def query_invoices(self, **kwargs) -> list[dict]: ...
    async def query_receipts(self, **kwargs) -> list[dict]: ...
    async def query_payments(self, **kwargs) -> list[dict]: ...
    async def query_suppliers(self, **kwargs) -> list[dict]: ...
    async def search(self, query: str, **kwargs) -> list[dict]: ...
    async def get_entity_timeline(self, entity_type: str, entity_id: str) -> list[dict]: ...
    async def get_entity_relationships(self, entity_type: str, entity_id: str, depth: int) -> list[dict]: ...
```

### 3.2 两个实现

```python
class PostgreSQLBackend(QueryBackend):
    """PostgreSQL 后端 —— 委托给现有 P2PRepository"""

    def __init__(self, repository: P2PRepository):
        self._repo = repository

    async def query_purchase_orders(self, **kwargs) -> list[dict]:
        return self._repo.query_purchase_orders(**kwargs)

    async def search(self, query: str, **kwargs) -> list[dict]:
        # PostgreSQL 不支持语义搜索，返回空或基于关键词的简单匹配
        return []

    async def get_entity_timeline(self, entity_type: str, entity_id: str) -> list[dict]:
        # PostgreSQL 降级：按时间排序返回相关记录，无图遍历能力
        return self._repo.get_entity_history(entity_type, entity_id)

    async def get_entity_relationships(self, entity_type: str, entity_id: str, depth: int) -> list[dict]:
        # PostgreSQL 降级：仅返回一层直接关联（JOIN），不支持多跳遍历
        return self._repo.get_direct_relations(entity_type, entity_id)


class GraphitiBackend(QueryBackend):
    """Graphiti 后端 —— 委托给 GraphitiClient"""

    def __init__(self, client: GraphitiClient):
        self._client = client

    async def query_purchase_orders(self, **kwargs) -> list[dict]:
        return await self._client.search("purchase orders", filters=kwargs)

    async def search(self, query: str, **kwargs) -> list[dict]:
        return await self._client.search(query, **kwargs)

    async def get_entity_timeline(self, entity_type: str, entity_id: str) -> list[dict]:
        return await self._client.get_entity_timeline(entity_type, entity_id)

    async def get_entity_relationships(self, entity_type: str, entity_id: str, depth: int) -> list[dict]:
        return await self._client.get_relationships(entity_type, entity_id, depth)
```

### 3.3 Hybrid 模式（图优先，SQL 降级）

```python
class HybridBackend(QueryBackend):
    """混合后端：Graphiti 优先，失败时降级到 PostgreSQL"""

    def __init__(self, graphiti: GraphitiBackend, postgresql: PostgreSQLBackend):
        self._graphiti = graphiti
        self._postgresql = postgresql

    async def query_purchase_orders(self, **kwargs) -> list[dict]:
        try:
            result = await self._graphiti.query_purchase_orders(**kwargs)
            if result:
                return result
        except Exception:
            logger.warning("Graphiti query failed, falling back to PostgreSQL")
        return await self._postgresql.query_purchase_orders(**kwargs)

    # 其他方法同理...
```

### 3.4 工厂函数

```python
def create_query_backend(
    settings: Settings,
    repository: P2PRepository,
    graphiti_client: GraphitiClient | None,
) -> QueryBackend:
    """根据配置创建查询后端"""

    mode = settings.graphiti_etl.query_backend

    if mode == "postgresql" or graphiti_client is None:
        return PostgreSQLBackend(repository)
    elif mode == "graphiti":
        return GraphitiBackend(graphiti_client)
    elif mode == "hybrid":
        return HybridBackend(
            graphiti=GraphitiBackend(graphiti_client),
            postgresql=PostgreSQLBackend(repository),
        )
    else:
        raise ValueError(f"Unknown query_backend: {mode}")
```

## 4. Tool 层适配

### 4.1 现有 19 个 Tool

现有 Tool 内部调用 `_get_repository()` 获取 P2PRepository。改造方式：

- **postgresql 模式**：不变，继续走 P2PRepository
- **graphiti / hybrid 模式**：注入 QueryBackend，Tool 内部改为调用 QueryBackend

```python
# modules/p2p/tools/_inject.py 新增

_query_backend: QueryBackend | None = None

def set_query_backend(backend: QueryBackend) -> None:
    global _query_backend
    _query_backend = backend

def _get_query_backend() -> QueryBackend:
    if _query_backend is None:
        # 降级：未初始化时走 PostgreSQL
        return PostgreSQLBackend(_get_repository())
    return _query_backend
```

### 4.2 新增 6 个图查询 Tool

- **graphiti / hybrid 模式**：正常工作
- **postgresql 模式**：返回降级提示或简化结果

```python
@tool
async def search_knowledge_graph(query: str, ...) -> str:
    backend = _get_query_backend()
    if isinstance(backend, PostgreSQLBackend):
        return json.dumps({"warning": "图谱查询未启用，当前为 PostgreSQL 降级模式", "results": []})
    return json.dumps(await backend.search(query, ...))
```

## 5. Orchestrator 上下文增强适配

```python
async def _enrich_with_graph_context(self, signal, params) -> str:
    # 仅在 graphiti/hybrid 模式且开关开启时执行
    if (
        self._settings.graphiti_etl.query_backend == "postgresql"
        or not self._settings.graphiti_etl.context_enrichment_enabled
    ):
        return ""
    # ... 正常图谱查询注入逻辑
```

## 6. ETL 与查询模式的关系

| ETL enabled | query_backend | 行为 |
|-------------|---------------|------|
| true | graphiti | ETL 同步运行 + 全部走图查询（正式模式） |
| true | hybrid | ETL 同步运行 + 图优先 SQL 降级（灰度切换） |
| true | postgresql | ETL 同步运行但查询仍走 SQL（仅做数据预热） |
| false | postgresql | 无 ETL，纯 SQL 查询（过渡期 / 降级） |
| false | graphiti | 启动报错：无 ETL 同步，图谱数据为空 |
| false | hybrid | 无 ETL，所有查询降级到 SQL |

## 7. 过渡路径

```
Phase 1: query_backend=postgresql, etl.enabled=false
         → 现有行为不变

Phase 2: query_backend=hybrid, etl.enabled=true
         → ETL 开始同步，图查询优先但可降级
         → 验证图查询结果与 SQL 查询一致

Phase 3: query_backend=graphiti, etl.enabled=true
         → 全面切换到图查询
         → PostgreSQL 查询路径保留但不再使用

Phase 4: 退役 PostgreSQL 查询路径
         → 移除 PostgreSQLBackend 和相关降级代码
```
