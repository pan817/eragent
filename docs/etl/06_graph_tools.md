# 图查询工具设计

## 1. 新增 LangChain Tool（6 个）

放置位置：`modules/p2p/tools/graph.py`，通过 `__init__.py` 导出，工具总数从 19 增至 25。

### 1.1 search_knowledge_graph

```python
@tool
async def search_knowledge_graph(
    query: str,
    entity_types: str = "",
    time_range_days: int = 90,
    max_results: int = 10,
) -> str:
    """在知识图谱中搜索与查询相关的实体和关系。
    支持自然语言查询，返回匹配的节点、关系及其时序信息。

    Args:
        query: 自然语言搜索查询
        entity_types: 限定搜索的实体类型（如 "Supplier,PurchaseOrder"），为空则搜索全部
        time_range_days: 时间范围（天），默认 90 天
        max_results: 最大返回结果数
    """
```

### 1.2 query_entity_timeline

```python
@tool
async def query_entity_timeline(
    entity_type: str,
    entity_id: str,
    include_related: bool = True,
) -> str:
    """查询某个实体的完整时间线，展示其生命周期中的所有事件。

    Args:
        entity_type: 实体类型（如 PurchaseOrder, Supplier, Invoice）
        entity_id: 实体唯一标识
        include_related: 是否包含关联实体的时间线事件
    """
```

### 1.3 query_entity_relationships

```python
@tool
async def query_entity_relationships(
    entity_type: str,
    entity_id: str,
    depth: int = 2,
    relationship_types: str = "",
) -> str:
    """查询实体的关联关系网络，返回指定深度内的所有关联节点和关系。

    Args:
        entity_type: 实体类型
        entity_id: 实体唯一标识
        depth: 遍历深度（1-3），默认 2
        relationship_types: 限定关系类型，逗号分隔，为空则返回全部
    """
```

### 1.4 query_supplier_profile

```python
@tool
async def query_supplier_profile(
    supplier_id: str,
    time_range_days: int = 180,
) -> str:
    """获取供应商的全景画像，聚合采购、交付、发票、付款、合同等多维度信息。

    Args:
        supplier_id: 供应商 ID
        time_range_days: 统计时间范围
    """
```

### 1.5 compare_entities

```python
@tool
async def compare_entities(
    entity_type: str,
    entity_ids: str,
    dimensions: str = "",
) -> str:
    """对比多个同类型实体在各维度的表现。

    Args:
        entity_type: 实体类型（如 Supplier）
        entity_ids: 逗号分隔的实体 ID 列表
        dimensions: 对比维度（如 "delivery,pricing,compliance"），为空则全维度对比
    """
```

### 1.6 detect_graph_anomalies

```python
@tool
async def detect_graph_anomalies(
    scope: str = "all",
    time_range_days: int = 30,
) -> str:
    """基于图结构检测采购流程中的异常模式。

    Args:
        scope: 检测范围 - "all" / "po_without_receipt" / "invoice_without_po" /
               "orphan_payments" / "circular_references"
        time_range_days: 检查时间范围
    """
```

## 2. Orchestrator 上下文增强

在 DAG 执行路径中，编排层自动从图谱注入背景上下文，ReAct 路径不自动注入（Agent 通过 Tool 自主查询更灵活）。

### 注入逻辑

```python
# core/orchestrator/orchestrator.py 新增方法

async def _enrich_with_graph_context(
    self, signal: QuerySignal, params: dict
) -> str:
    """在 DAG 执行前，自动从图谱获取相关上下文"""

    context_parts = []

    # 1. 有供应商实体 → 注入供应商画像摘要
    if supplier_id := params.get("supplier_id"):
        profile = await self._graphiti_client.search(
            f"supplier {supplier_id} profile summary", num_results=5,
        )
        context_parts.append(f"【供应商画像】\n{profile}")

    # 2. 有 PO → 注入 PO 关联链路
    if po_number := params.get("po_number"):
        timeline = await self._graphiti_client.search(
            f"purchase order {po_number} timeline", num_results=5,
        )
        context_parts.append(f"【PO 关联信息】\n{timeline}")

    # 3. 综合分析 → 注入近期图谱概览
    if signal.analysis_type == "comprehensive":
        overview = await self._graphiti_client.search(
            "recent procurement highlights and anomalies", num_results=5,
        )
        context_parts.append(f"【近期采购概览】\n{overview}")

    return "\n\n".join(context_parts)
```

### 注入时机

- **DAG 路径**：注入到 ReportAgent 的 prompt 中，作为补充背景
- **ReAct 路径**：不自动注入，Agent 通过 Tool 自主查询
- 可通过 `context_enrichment_enabled` 配置开关

## 3. 工具注册

### 文件变更

```
modules/p2p/tools/
├── query.py          # 现有 4 个查询工具
├── analysis.py       # 现有 6 个分析工具
├── advanced.py       # 现有 5 个高级工具
├── stub.py           # 现有 4 个桩工具
├── graph.py          # 新增：6 个图查询工具
├── _inject.py        # 新增 set_graphiti_client() / _get_graphiti_client()
└── __init__.py       # 导出更新为 25 个工具
```

### _inject.py 变更

```python
# 新增全局 GraphitiClient 注入

_graphiti_client: GraphitiClient | None = None

def set_graphiti_client(client: GraphitiClient) -> None:
    global _graphiti_client
    _graphiti_client = client

def _get_graphiti_client() -> GraphitiClient:
    if _graphiti_client is None:
        raise RuntimeError("GraphitiClient not initialized")
    return _graphiti_client
```

### P2PModuleProvider.get_tools() 变更

```python
def get_tools(self) -> list[Callable]:
    return [
        # 现有 19 个工具...
        # 新增 6 个图查询工具
        search_knowledge_graph,
        query_entity_timeline,
        query_entity_relationships,
        query_supplier_profile,
        compare_entities,
        detect_graph_anomalies,
    ]
```
