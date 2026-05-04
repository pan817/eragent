# 图查询工具设计

## 1. LangChain Tool（6 个）

位置：`modules/p2p/tools/graph.py`，仅在 `query_backend=graphiti` 或 `hybrid` 时注入。

### 1.1 search_knowledge_graph
自然语言搜索图谱，返回匹配的实体和关系。

### 1.2 query_entity_timeline
查询实体的完整时间线（生命周期事件）。

### 1.3 query_entity_relationships
查询实体的关系网络（指定深度 1-3）。

### 1.4 query_supplier_profile
供应商全景画像（聚合多维度图谱数据）。

### 1.5 compare_entities
对比多个同类型实体的表现。

### 1.6 detect_graph_anomalies
基于图结构检测异常模式（无收货PO、孤立付款等）。

## 2. 工具集选择机制

```python
# modules/p2p/provider.py
def get_tools(self) -> list[Any]:
    mode = get_settings().graphiti_etl.query_backend

    shared = [...]    # 10 个：query(4) + analysis(6)，通过 QueryBackend 自动切换
    pg_only = [...]   # 9 个：advanced(5) + stub(4)
    graph_only = [...] # 6 个：graph tools

    if mode == "postgresql":  return shared + pg_only       # 19
    elif mode == "graphiti":  return shared + graph_only    # 16
    else:                     return shared + pg_only + graph_only  # 25
```

## 3. Orchestrator 上下文增强

在 DAG 执行路径中，编排层可自动从图谱注入背景上下文。

```python
# core/orchestrator/orchestrator.py
async def _enrich_with_graph_context(self, signal, params) -> str:
    # 仅 query_backend != postgresql 且 context_enrichment_enabled 时执行
    # 1. 有 vendor_id → 注入供应商画像摘要
    # 2. 有 po_number → 注入 PO 关联链路
    # 3. comprehensive → 注入近期概览
```

注入时机：DAG 路径的 ReportAgent prompt 中。ReAct 路径不自动注入（Agent 自主选工具）。

## 4. 工具注入管理

```python
# modules/p2p/tools/_inject.py
set_repository(repo)          # P2PRepository（启动时注入）
set_graphiti_client(client)   # GraphitiClient（ETL 启动时注入）
set_query_backend(backend)    # QueryBackend（按 config 创建并注入）

_get_repository()             # analysis/advanced 工具使用
_get_graphiti_client()        # graph 工具使用
_get_query_backend()          # query + analysis 工具使用（自动路由到 PG/Neo4j）
```

## 5. 零工具调用防护

Agent 在 ReAct 路径中可能不调用任何工具就直接回答（用历史上下文编造数据）。

防护机制（`modules/p2p/agent.py`）：
1. ReAct 完成后统计 `tool_call_count`
2. 如果为 0 → WARNING + 注入强制指令 + 自动重试一次
3. 重试后仍为 0 → 拒绝返回，输出"未能查询到相关数据"

## 6. HTTP 测试文件

- `tests/http/test_etl.http` — ETL Admin API 测试
- `tests/http/test_graph_query.http` — 图查询工具测试（12 个用例，每个独立 session）
