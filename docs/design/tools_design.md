# Tools 架构重构设计方案

## 背景

当前系统有两套查询 ERP 业务数据的工具：基于 PostgreSQL 的 SQL 查询工具和基于 Graphiti 的图数据查询工具。另一个会话已将 15 个结构化工具统一为通过 `QueryBackend` Protocol 路由到不同后端（PG / Neo4jStructured / Hybrid），6 个图工具直接调用 GraphitiClient。

但这种"等价双实现"的架构存在根本问题：Neo4j 被当作关系库用（只查节点属性不用边），图工具只做语义搜索不用 Cypher 遍历，两套工具能力重叠且各自短板未被弥补。

## 目标

基于 PG 和 Graphiti 分别实现一套**适合自身优势**的查询工具：
- **PG 工具集**：精确过滤 + SQL 聚合 + 规则引擎检测
- **Graph 工具集**：关系遍历 + 模式匹配 + 时间线分析 + 语义搜索

不追求等价双实现，但各自独立满足 ERP Agent 的查询和分析能力。

## 目录

1. [现状分析 — 工具清单](#1-现状分析--工具清单)
2. [现状分析 — 后端实现](#2-现状分析--后端实现)
3. [现状分析 — 图模型](#3-现状分析--图模型)
4. [问题诊断](#4-问题诊断)
5. [PG 工具集设计](#5-pg-工具集设计)
6. [Graph 工具集设计](#6-graph-工具集设计)
7. [注入与切换机制](#7-注入与切换机制)
8. [迁移计划](#8-迁移计划)

---

## 1. 现状分析 — 工具清单

### 1.1 Query 工具（4个）

| 工具名 | 参数 | 调用链路 |
|--------|------|----------|
| `query_purchase_orders` | vendor_id, status, po_number, days, limit, order_by | → `_get_query_backend()` → `backend.query_purchase_orders()` |
| `query_receipts` | po_number, vendor_id, days, limit, order_by | → `_get_query_backend()` → `backend.query_receipts()` |
| `query_invoices` | po_number, vendor_id, status, invoice_num, days, limit, order_by | → `_get_query_backend()` → `backend.query_invoices()` |
| `query_payments` | invoice_num, vendor_id, check_number, days, limit, order_by | → `_get_query_backend()` → `backend.query_payments()` |

### 1.2 Analysis 工具（6个）

| 工具名 | 参数 | 调用链路 |
|--------|------|----------|
| `run_three_way_match` | po_number | → backend.query_purchase_orders + query_receipts + query_invoices → ThreeWayMatchChecker.check() |
| `run_price_variance_analysis` | vendor_id, days | → backend.query_purchase_orders + get_contract_prices → PriceVarianceAnalyzer.analyze() |
| `run_payment_compliance_check` | vendor_id, days | → backend.query_payments + query_invoices → PaymentComplianceChecker.check() |
| `calculate_supplier_kpis` | vendor_id, period | → backend.query_purchase_orders + query_receipts + query_invoices → SupplierPerformanceCalculator.calculate() |
| `query_vendor_master` | vendor_ids | → backend.query_suppliers() |
| `calculate_spend_analysis` | group_by, days | → backend.query_purchase_orders → Python 聚合 |

### 1.3 Advanced 工具（5个）

| 工具名 | 参数 | 调用链路 |
|--------|------|----------|
| `analyze_receipt_anomalies` | vendor_id, po_number, days | → backend.query_receipts + query_purchase_orders → Python 比对 |
| `detect_duplicate_invoices` | vendor_id, days | → backend.query_invoices → Python 分组检测 |
| `analyze_discount_utilization` | vendor_id, days | → backend.query_invoices + query_payments → Python 匹配 |
| `analyze_vendor_concentration` | days, top_n | → backend.query_purchase_orders → Python 聚合 |
| `calculate_po_cycle_time` | days, vendor_id | → backend 多次查询 → Python 计算各阶段耗时 |

### 1.4 Graph 工具（6个）

| 工具名 | 参数 | 调用链路 |
|--------|------|----------|
| `search_knowledge_graph` | query, entity_types, time_range_days, max_results | → `_get_graphiti_client()` → `client.search(自然语言)` |
| `query_entity_timeline` | entity_type, entity_id, include_related | → `client.search("timeline of {type} {id}")` |
| `query_entity_relationships` | entity_type, entity_id, depth, relationship_types | → `client.get_relationships(type, id, depth)` |
| `query_supplier_profile` | vendor_id, time_range_days | → `client.search("supplier {id} ...")` |
| `compare_entities` | entity_type, entity_ids, dimensions | → 循环 `client.search("{type} {id} performance summary")` |
| `detect_graph_anomalies` | scope, time_range_days | → `client.search("anomaly detection: {自然语言}")` |

### 1.5 QueryBackend Protocol（9个方法）

```python
class QueryBackend(Protocol):
    async def query_purchase_orders(self, **kwargs) -> list[dict[str, Any]]
    async def query_invoices(self, **kwargs) -> list[dict[str, Any]]
    async def query_receipts(self, **kwargs) -> list[dict[str, Any]]
    async def query_payments(self, **kwargs) -> list[dict[str, Any]]
    async def query_suppliers(self, **kwargs) -> list[dict[str, Any]]
    async def get_contract_prices(self) -> dict[str, float]
    async def search(self, query: str, **kwargs) -> list[dict[str, Any]]
    async def get_entity_timeline(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]
    async def get_entity_relationships(self, entity_type: str, entity_id: str, depth: int = 2) -> list[dict[str, Any]]
```

---

## 2. 现状分析 — 后端实现

### 2.1 PostgreSQLBackend

| 方法 | 实现 |
|------|------|
| `query_purchase_orders` | `asyncio.to_thread(repo.query_purchase_orders)` — SQL JOIN（PoHeader + PoLine + PoLineLocation） |
| `query_invoices` | `asyncio.to_thread(repo.query_invoices)` — 查 ApInvoice |
| `query_receipts` | `asyncio.to_thread(repo.query_receipts)` — 查 RcvTransaction |
| `query_payments` | `asyncio.to_thread(repo.query_payments)` — 查 ApPayment |
| `query_suppliers` | **返回空列表 `[]`** — Repository 没有此方法 |
| `get_contract_prices` | `asyncio.to_thread(repo.get_contract_prices)` — 查 PoLine.standard_price，带 TTL 缓存 |
| `search` | **返回空列表 `[]`** — PG 不支持语义搜索 |
| `get_entity_timeline` | **返回空列表 `[]`** — 无图遍历能力 |
| `get_entity_relationships` | **返回空列表 `[]`** — 无多跳遍历能力 |

### 2.2 Neo4jStructuredBackend

| 方法 | 实现 |
|------|------|
| `query_purchase_orders` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='PurchaseOrder'` + 属性过滤 → 映射为 PG 相同 dict |
| `query_invoices` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='Invoice'` + 属性过滤 |
| `query_receipts` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='Receipt'` + 属性过滤 |
| `query_payments` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='Payment'` + 属性过滤 |
| `query_suppliers` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='Supplier'` |
| `get_contract_prices` | Cypher: `MATCH (n:Entity) WHERE n.entity_type='POLine'` → 提取 standard_price |
| `search` | 委托 `client.search()` — Graphiti 语义搜索 |
| `get_entity_timeline` | 委托 `client.search("timeline of ...")` — 语义搜索 |
| `get_entity_relationships` | 委托 `client.get_relationships()` |

**关键发现**：前 6 个方法全部是单节点属性查询，**零条 Cypher 用到了边模式**。

### 2.3 HybridBackend

- 所有方法统一逻辑：**Neo4j 优先，失败或空结果时降级到 PG**
- 降级触发条件：抛异常 → WARNING 日志 → 调用 PG；返回空列表 → 静默降级
- `get_contract_prices` 单独处理（返回 dict，空 dict 也触发降级）

### 2.4 GraphitiClient.search() 能力边界

| 能力 | 支持 | 说明 |
|------|------|------|
| 自然语言语义搜索 | 是 | 基于 episode embedding 检索 |
| 精确条件过滤 | 否 | 无法做 `WHERE vendor_id = 'V001'` |
| 聚合统计 | 否 | 无法做 SUM / COUNT / AVG |
| 多跳关系遍历 | 间接 | `get_relationships()` 支持，`search()` 不支持 |
| 时间范围过滤 | 间接 | 靠自然语言表达，不精确 |
| 结构化排序/分页 | 否 | 只有 `num_results` 限制数量 |

---

## 3. 现状分析 — 图模型

### 3.1 节点类型（15种）

| 节点类型 | 来源 EBS 表 | 业务含义 |
|----------|-------------|----------|
| Supplier | AP_SUPPLIERS | 供应商主数据 |
| SupplierSite | AP_SUPPLIER_SITES_ALL | 供应商站点 |
| Material | MTL_SYSTEM_ITEMS_B | 物料主数据 |
| PurchaseOrder | PO_HEADERS_ALL | 采购订单头 |
| POLine | PO_LINES_ALL / PO_LINE_LOCATIONS / PO_DISTRIBUTIONS | 采购订单行 |
| Shipment | RCV_SHIPMENT_HEADERS / RCV_SHIPMENT_LINES | 发货单 |
| Receipt | RCV_TRANSACTIONS | 收货事务 |
| Invoice | AP_INVOICES_ALL | 应付发票 |
| InvoiceLine | AP_INVOICE_LINES_ALL / AP_INVOICE_DISTRIBUTIONS | 发票行 |
| Payment | AP_CHECKS_ALL / AP_INVOICE_PAYMENTS_ALL | 付款支票 |
| PaymentSchedule | AP_PAYMENT_SCHEDULES_ALL | 付款计划 |
| Auction | PON_AUCTION_HEADERS_ALL | 寻源竞价 |
| Bid | PON_BID_HEADERS | 投标记录 |
| Contract | OKC_K_HEADERS_B | 合同头 |
| ContractLine | OKC_K_LINES_B | 合同行 |

### 3.2 边类型（16种）

| 边类型 | 源节点 → 目标节点 | 语义含义 |
|--------|-------------------|----------|
| HAS_SITE | Supplier → SupplierSite | 供应商拥有站点 |
| CREATES_PO | Supplier → PurchaseOrder | 供应商关联采购订单 |
| CONTAINS_LINE | PurchaseOrder → POLine | 订单包含行项目 |
| ORDERS_MATERIAL | POLine → Material | 订单行采购物料 |
| SHIPS_TO | Shipment → SupplierSite | 发货目的地 |
| BELONGS_TO_SHIPMENT | Receipt → Shipment | 收货归属发货单 |
| RECEIVES_LINE | Receipt → POLine | 收货对应 PO 行 |
| SUBMITS_INVOICE | Supplier → Invoice | 供应商提交发票 |
| BELONGS_TO_INVOICE | InvoiceLine → Invoice | 发票行归属发票 |
| INVOICES_LINE | InvoiceLine → POLine | 发票行对应 PO 行 |
| PAYS_INVOICE | Payment → Invoice | 付款对应发票 |
| SCHEDULED_FOR | PaymentSchedule → Invoice | 付款计划对应发票 |
| HAS_BID | Auction → Bid | 竞价包含投标 |
| BIDS_ON | Supplier → Auction | 供应商参与竞价 |
| CONTAINS_CONTRACT_LINE | Contract → ContractLine | 合同包含行 |
| CONTRACT_COVERS | ContractLine → Material | 合同行覆盖物料 |

### 3.3 图模型能表达但当前未利用的查询能力

| 能力类型 | 示例 Cypher | 业务场景 |
|----------|-------------|----------|
| 全链路追踪 | `(s:Supplier)-[:CREATES_PO]->(po)-[:CONTAINS_LINE]->(line)<-[:RECEIVES_LINE]-(rcv)` | PO 完整路径 |
| 断裂链路检测 | `(po)-[:CONTAINS_LINE]->(line) WHERE NOT (line)<-[:RECEIVES_LINE]-()` | 有 PO 行无收货 |
| 供应商关系网络 | `(s:Supplier)-[:CREATES_PO\|SUBMITS_INVOICE\|BIDS_ON]->()` | 供应商全维度关联 |
| 三路匹配路径 | `(line:POLine)<-[:RECEIVES_LINE]-(rcv), (line)<-[:INVOICES_LINE]-(inv_line)` | 图结构精确匹配 |
| 合同覆盖分析 | `(c:Contract)-[:CONTAINS_CONTRACT_LINE]->(cl)-[:CONTRACT_COVERS]->(m)<-[:ORDERS_MATERIAL]-(line)` | 采购行合同覆盖 |
| 供应商竞争分析 | `(s1)-[:BIDS_ON]->(a:Auction)<-[:BIDS_ON]-(s2)` | 同一竞价的竞争者 |
| 付款路径完整性 | `(p:Payment)-[:PAYS_INVOICE]->(inv)<-[:BELONGS_TO_INVOICE]-(il)-[:INVOICES_LINE]->(line)` | 付款反向追溯 |
| 时间线排序 | 沿边遍历 + `ORDER BY node.valid_from` | 实体事件时序 |
| 孤立节点检测 | `(n:Entity) WHERE NOT (n)--()` | 悬挂数据 |
| 环路检测 | `path=(a)-[*2..5]->(a)` | 数据引用环 |

---

## 4. 问题诊断

### 4.1 Neo4jStructuredBackend 只查节点不用边

`core/etl/query_backend.py:92-106` — 所有 6 个结构化查询方法的 Cypher 模式均为：

```cypher
MATCH (n:Entity) WHERE n.entity_type = 'X' [AND n.field = $val]
RETURN properties(n) AS props
```

**零条 Cypher 用到了边模式**（如 `-[:CREATES_PO]->`）。ETL 建立的 16 种边关系在查询侧完全未被使用。本质上是用 Neo4j 做了一个慢版 PostgreSQL。

### 4.2 graph.py 只用语义搜索不用 Cypher

6 个图工具中 5 个完全依赖 `client.search()` 语义搜索：
- `query_entity_timeline` → `client.search("timeline of ...")`
- `query_supplier_profile` → `client.search("supplier ... performance")`
- `detect_graph_anomalies` → `client.search("anomaly detection: ...")`
- `compare_entities` → 循环 `client.search(...)`

仅 `query_entity_relationships` 调用了 `client.get_relationships()`。

问题：语义搜索结果不确定，取决于 embedding 质量；`detect_graph_anomalies` 用自然语言做异常检测，而图的天然优势是用 Cypher 模式匹配（`WHERE NOT EXISTS`）做精确结构异常检测。

### 4.3 Python 聚合替代 SQL 聚合的性能代价

`advanced.py` 改造后，5 个工具从数据库取回全量行再 Python 聚合：
- `analyze_vendor_concentration`：先取全部 PO 行，再 Python 按 vendor_id 分组
- `calculate_spend_analysis`：先取全部 PO 行，再 Python GROUP BY
- `calculate_po_cycle_time`：先取全部 PO + receipts + payments + invoices，再逐个匹配

对比 SQL 版本：数据库内完成聚合只返回结果行。Python 版本网络传输量和内存占用显著更大，在数据量增长后性能退化明显。

### 4.4 工具语义重叠导致 LLM 选择困难

所有模式注入同一个 21 工具集，存在语义重叠：

| 用户问题 | 可选工具（语义重叠） |
|----------|---------------------|
| "供应商 V001 有哪些采购订单？" | `query_purchase_orders` / `search_knowledge_graph` / `query_supplier_profile` |
| "PO-001 的收货情况？" | `query_receipts` / `query_entity_timeline` / `query_entity_relationships` |
| "有没有采购异常？" | `run_three_way_match` / `analyze_receipt_anomalies` / `detect_graph_anomalies` |

PG 模式下 6 个图工具返回 `_PG_DEGRADED` 提示，浪费 Agent 的工具选择空间和 token。

---

## 5. PG 工具集设计

### 5.1 定位

PG 工具集专注于**精确条件过滤、SQL 聚合统计、规则引擎检测**——关系型数据库的天然优势。

### 5.2 保留不动的工具（7个）

| 工具名 | 理由 |
|--------|------|
| `query_purchase_orders` | 精确过滤 + 排序分页，实现已成熟 |
| `query_receipts` | 同上 |
| `query_invoices` | 同上 |
| `query_payments` | 同上 |
| `run_three_way_match` | 规则引擎检测，逻辑不变 |
| `run_price_variance_analysis` | 同上 |
| `run_payment_compliance_check` | 同上 |

### 5.3 改回 SQL 聚合的工具（5个）

| 工具名 | 改动 | 理由 |
|--------|------|------|
| `calculate_spend_analysis` | 改回 SQL `GROUP BY + SUM + AVG` | Python 聚合需传输全量数据 |
| `analyze_vendor_concentration` | 改回 SQL `GROUP BY vendor_id + HAVING` | 需 `COUNT(DISTINCT po_number)` |
| `calculate_po_cycle_time` | 改回 SQL 子查询取 `MIN(transaction_date)` | 避免逐 PO 循环 |
| `detect_duplicate_invoices` | 改回 SQL 自关联 + `ABS(DATEDIFF) <= 3` | SQL 窗口函数更高效 |
| `analyze_discount_utilization` | 改回 SQL JOIN + `WHERE check_date > discount_due_date` | 避免全量发票 Python 匹配 |

### 5.4 需调整的工具

| 工具名 | 调整内容 |
|--------|----------|
| `query_vendor_master` | 改为直接查 `ApSupplier` 表（修复当前返回空列表 bug） |
| `calculate_supplier_kpis` | 改为直接调 Repository 方法 |
| `analyze_receipt_anomalies` | 改回 SQL JOIN（可精确取 PoLineLocation.promised_date） |

不新增工具——当前 15 个已覆盖四大分析场景 + 支出分析 + 高级检测。

### 5.5 参数签名

保持不变——当前 15 个工具的参数签名已面向 PG 优化（精确字段过滤 + days 时间范围 + limit/order_by）。

### 5.6 规则引擎数据来源

| 规则工具 | 数据来源 |
|----------|----------|
| `run_three_way_match` | `repo.query_purchase_orders` + `repo.query_receipts` + `repo.query_invoices` |
| `run_price_variance_analysis` | `repo.query_purchase_orders` + `repo.get_contract_prices()` |
| `run_payment_compliance_check` | `repo.query_payments` + `repo.query_invoices` |
| `calculate_supplier_kpis` | `repo.query_purchase_orders` + `repo.query_receipts` + `repo.query_invoices` |

规则引擎输入契约（`list[dict]` 字段规范）保持不变，不依赖数据来源。

---

## 6. Graph 工具集设计

### 6.1 定位

Graph 工具集专注于**关系遍历、全链路追踪、结构化模式匹配、时间线分析**——图数据库的天然优势。

### 6.2 保留的语义搜索工具（1个）

| 工具名 | 理由 |
|--------|------|
| `search_knowledge_graph` | 自然语言模糊查询是 Graphiti 的独特能力，适用于探索性查询 |

### 6.3 需用 Cypher 重写的工具（4个）

| 工具名 | 当前实现 | 重写为 |
|--------|----------|--------|
| `query_entity_timeline` | `client.search("timeline of ...")` | Cypher 沿边遍历 + 时间戳排序 |
| `query_entity_relationships` | `client.get_relationships()` | Cypher 可变长路径遍历 + 类型过滤 |
| `query_supplier_profile` | `client.search("supplier ...")` | Cypher 多跳遍历聚合各维度 |
| `detect_graph_anomalies` | `client.search("anomaly detection: ...")` | Cypher `WHERE NOT EXISTS` 模式匹配 |

### 6.4 新增的图专属工具（3个）

| 工具名 | 查询场景 | 理由 |
|--------|----------|------|
| `trace_procurement_chain` | 给定任意实体追踪完整采购链路 | PG 需多次 JOIN，图一条路径查询即可 |
| `find_contract_coverage` | 查询采购行是否有合同价格覆盖 | 合同数据已入图但当前完全未被利用 |
| `find_competing_suppliers` | 查询同一竞价中的竞争供应商 | 图天然支持双向遍历 |

### 6.5 参数签名定义

```python
# 保留（语义搜索）
search_knowledge_graph(query: str, entity_types: str = "", max_results: int = 10) -> str

# 重写（Cypher 关系遍历）
query_entity_timeline(entity_type: str, entity_id: str, direction: str = "both") -> str
query_entity_relationships(entity_type: str, entity_id: str, depth: int = 2, relationship_types: str = "") -> str
query_supplier_profile(vendor_id: str) -> str
detect_graph_anomalies(scope: str = "all", time_range_days: int = 30) -> str

# 新增（Cypher 路径查询）
trace_procurement_chain(entity_type: str, entity_id: str) -> str
find_contract_coverage(vendor_id: str = "", material_id: str = "") -> str
find_competing_suppliers(vendor_id: str = "", auction_id: str = "") -> str
```

### 6.6 Cypher 查询模式

#### query_entity_timeline（重写）

```cypher
-- 以 PurchaseOrder 为例
MATCH (po:Entity {entity_type: 'PurchaseOrder', po_number: $id})
OPTIONAL MATCH (po)-[:CONTAINS_LINE]->(line:Entity)
OPTIONAL MATCH (line)<-[:RECEIVES_LINE]-(rcv:Entity)
OPTIONAL MATCH (line)<-[:INVOICES_LINE]-(il:Entity)-[:BELONGS_TO_INVOICE]->(inv:Entity)
OPTIONAL MATCH (inv)<-[:PAYS_INVOICE]-(pmt:Entity)
RETURN po, line, rcv, inv, pmt
ORDER BY COALESCE(rcv.valid_from, inv.valid_from, pmt.valid_from)
```

#### query_entity_relationships（重写）

```cypher
MATCH (start:Entity {entity_type: $type, entity_id: $id})
MATCH path = (start)-[r*1..$depth]->(end)
WHERE ($rel_types IS NULL OR type(r[-1]) IN $rel_types)
RETURN path
LIMIT 50
```

#### query_supplier_profile（重写）

```cypher
MATCH (s:Entity {entity_type: 'Supplier', vendor_id: $vendor_id})
OPTIONAL MATCH (s)-[:CREATES_PO]->(po:Entity)
OPTIONAL MATCH (po)-[:CONTAINS_LINE]->(line:Entity)
OPTIONAL MATCH (line)<-[:RECEIVES_LINE]-(rcv:Entity)
OPTIONAL MATCH (s)-[:SUBMITS_INVOICE]->(inv:Entity)
OPTIONAL MATCH (inv)<-[:PAYS_INVOICE]-(pmt:Entity)
OPTIONAL MATCH (s)-[:BIDS_ON]->(auction:Entity)
OPTIONAL MATCH (s)-[:HAS_SITE]->(site:Entity)
RETURN s, collect(DISTINCT po) AS orders,
       count(DISTINCT rcv) AS receipt_count,
       collect(DISTINCT inv) AS invoices,
       collect(DISTINCT auction) AS auctions
```

#### detect_graph_anomalies（重写）

```cypher
-- missing_receipt: PO 行无收货
MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:CONTAINS_LINE]->(line:Entity)
WHERE NOT (line)<-[:RECEIVES_LINE]-(:Entity)
  AND po.valid_from >= datetime() - duration({days: $days})
RETURN po.po_number AS entity, 'missing_receipt' AS anomaly_type

UNION ALL

-- missing_invoice: 有收货无发票
MATCH (line:Entity)<-[:RECEIVES_LINE]-(:Entity)
WHERE NOT (line)<-[:INVOICES_LINE]-(:Entity)
RETURN line.entity_id AS entity, 'missing_invoice' AS anomaly_type

UNION ALL

-- orphan_payment: 付款无发票关联
MATCH (pmt:Entity {entity_type:'Payment'})
WHERE NOT (pmt)-[:PAYS_INVOICE]->(:Entity)
RETURN pmt.check_number AS entity, 'orphan_payment' AS anomaly_type
```

#### trace_procurement_chain（新增）

```cypher
MATCH (start:Entity {entity_type: $type, entity_id: $id})
MATCH path = (start)-[*1..6]-(end)
RETURN path
LIMIT 100
-- 应用层按 valid_from 排序，组装为时序链路
```

#### find_contract_coverage（新增）

```cypher
MATCH (line:Entity {entity_type:'POLine'})<-[:CONTAINS_LINE]-(po:Entity)
OPTIONAL MATCH (line)-[:ORDERS_MATERIAL]->(m:Entity)<-[:CONTRACT_COVERS]-(cl:Entity)<-[:CONTAINS_CONTRACT_LINE]-(c:Entity)
WHERE ($vendor_id IS NULL OR po.vendor_id = $vendor_id)
RETURN po.po_number, line.item_description, m.segment1 AS material,
       c.contract_number, cl.price_negotiated AS contract_price,
       CASE WHEN c IS NULL THEN 'uncovered' ELSE 'covered' END AS coverage
```

#### find_competing_suppliers（新增）

```cypher
MATCH (s:Entity {entity_type:'Supplier', vendor_id: $vendor_id})-[:BIDS_ON]->(a:Entity {entity_type:'Auction'})
MATCH (competitor:Entity {entity_type:'Supplier'})-[:BIDS_ON]->(a)
WHERE competitor.vendor_id <> $vendor_id
OPTIONAL MATCH (a)<-[:HAS_BID]-(bid:Entity)
RETURN a.auction_title, a.auction_status,
       competitor.vendor_name, bid.bid_total, bid.award_status
```

---

## 7. 注入与切换机制

### 7.1 新 provider.py get_tools() 逻辑

```python
def get_tools(self) -> list[Any]:
    mode = get_settings().graphiti_etl.query_backend

    # PG 工具集（15个）：精确查询 + SQL 聚合 + 规则检测
    pg_tools = [
        query_purchase_orders,        # 直接用 Repository
        query_receipts,
        query_invoices,
        query_payments,
        query_vendor_master,
        run_three_way_match,
        run_price_variance_analysis,
        run_payment_compliance_check,
        calculate_supplier_kpis,
        calculate_spend_analysis,      # SQL GROUP BY
        analyze_receipt_anomalies,     # SQL JOIN
        detect_duplicate_invoices,     # SQL 自关联
        analyze_discount_utilization,  # SQL JOIN
        analyze_vendor_concentration,  # SQL GROUP BY
        calculate_po_cycle_time,       # SQL 子查询
    ]

    # Graph 工具集（8个）：关系遍历 + 模式匹配 + 语义搜索
    graph_tools = [
        search_knowledge_graph,      # 语义搜索（保留）
        query_entity_timeline,       # Cypher 时间线遍历
        query_entity_relationships,  # Cypher 多跳遍历
        query_supplier_profile,      # Cypher 供应商画像
        detect_graph_anomalies,      # Cypher 模式匹配
        trace_procurement_chain,     # Cypher 全链路追踪（新增）
        find_contract_coverage,      # Cypher 合同覆盖（新增）
        find_competing_suppliers,    # Cypher 竞争分析（新增）
    ]

    if mode == "postgresql":
        return pg_tools                 # 15 个
    elif mode == "graphiti":
        return graph_tools              # 8 个
    else:  # hybrid
        return pg_tools + graph_tools   # 23 个
```

### 7.2 _inject.py 改动

```python
# 保留：
set_repository(repo)           # PG 工具直接用
_get_repository()

set_graphiti_client(client)    # Graph 工具直接用
_get_graphiti_client()

# 删除：
# set_query_backend(backend)   ← 不再需要
# _get_query_backend()         ← 不再需要
```

- PG 工具直接调用 `_get_repository()` → P2PRepository 方法（SQL）
- Graph 工具直接调用 `_get_graphiti_client()` → `client.execute_cypher()` / `client.search()`
- `QueryBackend` Protocol 及三种实现不再被工具层使用

### 7.3 DAG 模板兼容性

所有 DAG 模板引用的 `tool_name` 均为 PG 工具名（如 `query_purchase_orders`、`run_three_way_match`）。

**解决方案**：
- `postgresql` / `hybrid` 模式：DAG 正常工作（PG 工具集已注入）
- `graphiti` 模式：DAG 降级为 ReAct（Agent 自主调用图工具）

理由：图查询的优势在于灵活的关系遍历，不适合固定步骤的 DAG 编排。

---

## 8. 迁移计划

### 8.1 要删除的代码

| 文件/位置 | 删除内容 | 理由 |
|-----------|----------|------|
| `core/etl/query_backend.py` | `QueryBackend` Protocol | 工具层不再经过统一抽象 |
| `core/etl/query_backend.py` | `Neo4jStructuredBackend` 类 | 把图当 KV 用的反模式 |
| `core/etl/query_backend.py` | `GraphitiBackend` 类 | 语义搜索包装，图工具直接调 client |
| `core/etl/query_backend.py` | `HybridBackend` 类 | 不再需要后端切换降级 |
| `core/etl/query_backend.py` | `create_query_backend()` | 随 Protocol 删除 |
| `modules/p2p/tools/_inject.py` | `set_query_backend()` / `_get_query_backend()` | 工具不再使用 |

### 8.2 要修改的代码

| 文件 | 修改内容 |
|------|----------|
| `modules/p2p/tools/query.py` | 4 个工具改为直接调用 `_get_repository()` |
| `modules/p2p/tools/analysis.py` | 6 个工具改为直接调用 Repository；`calculate_spend_analysis` 改回 SQL |
| `modules/p2p/tools/advanced.py` | 5 个工具改回 SQL 实现 |
| `modules/p2p/tools/graph.py` | 4 个工具 Cypher 重写 + 新增 3 个工具 |
| `modules/p2p/tools/__init__.py` | 更新导出列表 |
| `modules/p2p/tools/_inject.py` | 删除 QueryBackend 相关代码 |
| `modules/p2p/provider.py` | `get_tools()` 按模式注入不同工具集 |
| `core/orchestrator/orchestrator.py` | graphiti 模式下 DAG 降级为 ReAct |
| `api/main.py` | 删除 `set_query_backend()` 调用 |

### 8.3 要新增的代码

| 文件 | 新增内容 |
|------|----------|
| `modules/p2p/tools/graph.py` | `trace_procurement_chain` 工具 |
| `modules/p2p/tools/graph.py` | `find_contract_coverage` 工具 |
| `modules/p2p/tools/graph.py` | `find_competing_suppliers` 工具 |
| `modules/p2p/repository.py` | `query_suppliers()` 方法（查 ApSupplier 表） |

### 8.4 测试影响范围

| 测试文件 | 影响 |
|----------|------|
| `tests/unit/test_query_backend.py` | 大幅重写或删除 |
| `tests/unit/test_dag.py` | 增加 graphiti 模式 DAG→ReAct 降级测试 |
| 涉及 tools 的单元测试 | mock 从 `_get_query_backend` 改为 `_get_repository` |
| `tests/http/test_graph_query.http` | 新增 3 个图工具测试用例 |
| 新增 `tests/unit/test_graph_tools.py` | 8 个图工具单元测试 |

### 8.5 分步实施顺序

每步可独立验证：

| 步骤 | 内容 | 验证 |
|------|------|------|
| A | `repository.py` 新增 `query_suppliers()` | 单元测试通过 |
| B | `query.py` 改为直接用 Repository | `pytest -k query` 通过 |
| C | `analysis.py` 改为直接用 Repository + SQL | `pytest -k analysis` 通过 |
| D | `advanced.py` 改回 SQL 实现 | `pytest -k advanced` 通过 |
| E | `_inject.py` 删除 QueryBackend 相关 | 全量 pytest 通过 |
| F | `graph.py` 重写 4 个 + 新增 3 个工具 | 新增 test_graph_tools 通过 |
| G | `provider.py` 按模式注入 | 全量测试通过 |
| H | `orchestrator.py` graphiti 模式 DAG→ReAct | `pytest -k dag` 通过 |
| I | `query_backend.py` 删除 Protocol + 类 | 全量 pytest 通过 |
| J | `test_query_backend.py` 重写/删除 | `pytest --cov` ≥ 90% |
| K | `api/main.py` 清理 `set_query_backend` | 集成测试 + 启动验证 |

---

## 9. 方案修正（评审确认）

基于逐项评审讨论，对第 5-8 章方案做以下修正。**本章内容为最终决策，与前文冲突时以本章为准。**

### 9.1 模式定义修正

**删除 `graphiti` 纯图模式，仅保留两种模式：**

| 模式 | PG 存储 | Neo4j 存储 | 工具集 |
|------|---------|------------|--------|
| `postgresql` | ERP 业务数据 + 系统数据 | 未启用 | PG 工具集（15个） |
| `hybrid` | 仅系统数据（session/trace/memory/report） | ERP 业务数据（15种节点 + 16种边） | 结构化工具（15个，Neo4j 查询 + Python 聚合）+ 图工具（12个） |

**关键约束：**
- hybrid 模式下 PG 没有 ERP 业务数据，Neo4j 是 ERP 数据的唯一来源
- 两种模式均无降级逻辑（互补关系，非替代关系）
- 两种模式下 DAG 均正常工作

### 9.2 QueryBackend 保留策略

**保留 `QueryBackend` Protocol（精简版），作为结构化工具的数据源抽象：**

```python
class QueryBackend(Protocol):
    """结构化数据源 — 供 15 个结构化工具获取平铺业务数据。"""
    async def query_purchase_orders(self, **kwargs) -> list[dict[str, Any]]
    async def query_invoices(self, **kwargs) -> list[dict[str, Any]]
    async def query_receipts(self, **kwargs) -> list[dict[str, Any]]
    async def query_payments(self, **kwargs) -> list[dict[str, Any]]
    async def query_suppliers(self, **kwargs) -> list[dict[str, Any]]
    async def get_contract_prices(self) -> dict[str, float]
```

两种实现：
- `PostgreSQLBackend`：调用 P2PRepository SQL 查询（postgresql 模式）
- `Neo4jStructuredBackend`：Cypher 查询节点属性 + **边遍历增强**（hybrid 模式）

**删除：**
- `GraphitiBackend` — 语义搜索不适合结构化数据获取
- `HybridBackend` — 无降级需求
- Protocol 中的 `search` / `get_entity_timeline` / `get_entity_relationships` 方法 — 这些由图工具直接实现

### 9.3 图增强设计（hybrid 模式的架构回报）

hybrid 模式下 `Neo4jStructuredBackend` 不仅查节点属性，还**利用边关系返回增强字段**：

#### 增强 1：查询工具返回关联状态

```cypher
-- query_purchase_orders 的增强 Cypher
MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:CONTAINS_LINE]->(line:Entity)
OPTIONAL MATCH (line)<-[:RECEIVES_LINE]-(rcv:Entity)
OPTIONAL MATCH (line)<-[:INVOICES_LINE]-(il:Entity)-[:BELONGS_TO_INVOICE]->(inv:Entity)
OPTIONAL MATCH (inv)<-[:PAYS_INVOICE]-(pmt:Entity)
WHERE ($vendor_id IS NULL OR po.vendor_id = $vendor_id)
  AND ($po_number IS NULL OR po.po_number = $po_number)
RETURN po, line,
       rcv IS NOT NULL AS has_receipt,
       il IS NOT NULL AS has_invoice,
       pmt IS NOT NULL AS has_payment
```

返回的 dict 比 PG 版本多出：`has_receipt`、`has_invoice`、`has_payment` 字段。
Agent 立刻知道每个 PO 行的履约完成度，无需额外调用。

#### 增强 2：规则引擎利用边做精确匹配

```cypher
-- run_three_way_match 的增强：通过图边精确匹配 PO行↔收货↔发票行
MATCH (po:Entity {entity_type:'PurchaseOrder', po_number:$po})-[:CONTAINS_LINE]->(line)
OPTIONAL MATCH (line)<-[:RECEIVES_LINE]-(rcv)
OPTIONAL MATCH (line)<-[:INVOICES_LINE]-(il)-[:BELONGS_TO_INVOICE]->(inv)
RETURN line.entity_id, line.quantity AS po_qty, line.unit_price,
       rcv.quantity AS rcv_qty,
       il.quantity_invoiced AS inv_qty, il.unit_price AS inv_price
```

优势：不依赖字段值一致性，通过图结构确保匹配精确性。

#### 增强 3：聚合工具附带关系洞察

```cypher
-- analyze_vendor_concentration 增强：额外返回关系密度
MATCH (s:Entity {entity_type:'Supplier'})-[r]->(target)
RETURN s.vendor_id, s.vendor_name,
       count(r) AS total_connections,
       count(CASE WHEN type(r) = 'CREATES_PO' THEN 1 END) AS po_count,
       count(CASE WHEN type(r) = 'SUBMITS_INVOICE' THEN 1 END) AS invoice_count,
       count(CASE WHEN type(r) = 'BIDS_ON' THEN 1 END) AS auction_count
```

PG 版本只能统计金额占比；图版本额外给出"关系密度"——衡量依赖深度更立体。

#### 增强 4：异常检测利用图结构

```cypher
-- analyze_receipt_anomalies 增强：检测重复收货边
MATCH (line:Entity)<-[:RECEIVES_LINE]-(rcv:Entity)
WITH line, count(rcv) AS receipt_count, collect(rcv) AS receipts
WHERE receipt_count > 1
RETURN line.entity_id, receipt_count, receipts
```

### 9.4 修正后的完整架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent (LLM)                               │
├─────────────────────────────────────────────────────────────────┤
│                     provider.get_tools()                         │
│                                                                  │
│  postgresql 模式:                                                │
│  ┌─────────────────────────────────────────────────────┐        │
│  │ 15 个结构化工具 → PostgreSQLBackend → Repository(SQL)│        │
│  └─────────────────────────────────────────────────────┘        │
│                                                                  │
│  hybrid 模式:                                                    │
│  ┌─────────────────────────────────────────────────────┐        │
│  │ 15 个结构化工具 → Neo4jStructuredBackend(增强 Cypher) │        │
│  │   ├─ 查询工具：节点属性 + 边关系状态                    │        │
│  │   ├─ 规则工具：边精确匹配 + 规则引擎                   │        │
│  │   └─ 聚合工具：Cypher 取数据 + Python 聚合 + 关系洞察  │        │
│  ├─────────────────────────────────────────────────────┤        │
│  │ 12 个图工具 → GraphitiClient.execute_cypher()         │        │
│  │   ├─ search_knowledge_graph（语义搜索）               │        │
│  │   ├─ 5 个重写工具（timeline/relationships/profile/    │        │
│  │   │   anomalies+scope扩展/compare_entities）          │        │
│  │   └─ 6 个新增工具（entity_detail/path_between/       │        │
│  │       procurement_chain/contract/competing/risk）      │        │
│  └─────────────────────────────────────────────────────┘        │
├─────────────────────────────────────────────────────────────────┤
│  DAG 模板：两种模式均正常工作                                     │
│  postgresql → 结构化工具(SQL)                                     │
│  hybrid → 结构化工具(Neo4j) + 图工具可在 ReAct 中补充             │
└─────────────────────────────────────────────────────────────────┘
```

### 9.5 修正后的迁移计划

| 步骤 | 内容 | 验证 |
|------|------|------|
| A | `repository.py` 新增 `query_suppliers()` | 单元测试通过 |
| B | `query_backend.py` 精简 Protocol（删 search/timeline/relationships 方法）、删 GraphitiBackend、删 HybridBackend | 编译通过 |
| C | `Neo4jStructuredBackend` 重写为增强版（边遍历 + 额外字段） | 新增单元测试通过 |
| D | `query.py` 保持走 `_get_query_backend()`（两种模式自动切换） | `pytest -k query` 通过 |
| E | `analysis.py` 保持走 `_get_query_backend()`；PG 下 `calculate_spend_analysis` 改回 SQL | `pytest -k analysis` 通过 |
| F | `advanced.py` PG 模式改回 SQL；hybrid 模式保持 Python 聚合 | `pytest -k advanced` 通过 |
| G | `graph.py` 重写 5 个（含 compare_entities + anomalies scope 扩展）+ 新增 4 个工具（含 query_risk_impact） | 新增 test_graph_tools 通过 |
| H | `provider.py` 改为两种模式注入（删除 graphiti 分支） | 全量测试通过 |
| I | `api/main.py` + `orchestrator.py` 适配两种模式 | 集成测试通过 |
| J | `test_query_backend.py` 更新（删除 HybridBackend/GraphitiBackend 测试） | `pytest --cov` ≥ 90% |

### 9.6 hybrid 模式的架构回报总结

| 维度 | postgresql 模式 | hybrid 模式 |
|------|----------------|-------------|
| 查询结果 | 平铺字段 | 平铺字段 + **关系状态** |
| 规则匹配 | 靠字段值匹配 | **靠图边结构精确匹配** |
| 集中度分析 | 金额占比 | 金额占比 + **关系密度** |
| 异常检测 | 数值比对 | 数值比对 + **结构异常** |
| 全链路追踪 | 不支持 | ✅ `trace_procurement_chain` |
| 合同覆盖 | 不支持 | ✅ `find_contract_coverage` |
| 竞争分析 | 不支持 | ✅ `find_competing_suppliers` |
| 实体对比 | 不支持 | ✅ `compare_entities` |
| 语义搜索 | 不支持 | ✅ `search_knowledge_graph` |

### 9.7 补充确认项

#### 9.7.1 `compare_entities` 保留并用 Cypher 重写

保留 `compare_entities` 工具，用 Cypher 重写为并行遍历多个实体的结构化对比：

```cypher
MATCH (s:Entity {entity_type:'Supplier'})
WHERE s.vendor_id IN $vendor_ids
OPTIONAL MATCH (s)-[:CREATES_PO]->(po)
OPTIONAL MATCH (po)-[:CONTAINS_LINE]->(line)<-[:RECEIVES_LINE]-(rcv)
OPTIONAL MATCH (s)-[:SUBMITS_INVOICE]->(inv)
RETURN s.vendor_id, s.vendor_name,
       count(DISTINCT po) AS po_count,
       count(DISTINCT rcv) AS receipt_count,
       count(DISTINCT inv) AS invoice_count
```

图工具集总计 **9 个**（1 保留 + 5 重写 + 3 新增）。

#### 9.7.2 聚合类工具采用方案 B（双模式分支）

5 个聚合类工具（`calculate_spend_analysis`、`analyze_vendor_concentration`、`calculate_po_cycle_time`、`detect_duplicate_invoices`、`analyze_discount_utilization`）内部判断模式：

```python
async def _calculate_spend_analysis_impl(group_by: str, days: int):
    mode = get_settings().graphiti_etl.query_backend
    if mode == "postgresql":
        # 直接用 Repository SQL 聚合（性能最优）
        repo = _get_repository()
        return await asyncio.to_thread(repo.spend_analysis_sql, group_by, days)
    else:  # hybrid
        # Neo4j 取数据 + Python 聚合
        backend = _get_query_backend()
        pos = await backend.query_purchase_orders(days=days)
        # ... Python 聚合逻辑
```

理由：PG 模式下发挥 SQL 聚合性能优势；hybrid 模式下 PG 没有 ERP 数据，必须从 Neo4j 取数据再 Python 聚合。

#### 9.7.3 图模型能力覆盖确认

3.3 节列出的 10 种未利用图查询能力，覆盖情况如下：

| # | 图查询能力 | 覆盖状态 | 覆盖工具 |
|---|-----------|----------|----------|
| 1 | 全链路追踪 | ✅ | `trace_procurement_chain` |
| 2 | 断裂链路检测 | ✅ | `detect_graph_anomalies` |
| 3 | 供应商关系网络 | ✅ | `query_supplier_profile` + `query_entity_relationships` |
| 4 | 三路匹配路径验证 | ✅ | 增强 2（边精确匹配） |
| 5 | 合同覆盖分析 | ✅ | `find_contract_coverage` |
| 6 | 供应商竞争分析 | ✅ | `find_competing_suppliers` |
| 7 | 付款路径完整性 | ✅ | `trace_procurement_chain`（反向） |
| 8 | 时间线排序 | ✅ | `query_entity_timeline` |
| 9 | 孤立节点检测 | ✅ | `detect_graph_anomalies` scope 增加 `orphan_node` |
| 10 | 环路检测 | ❌ 不实现 | 业务价值低 + 性能代价高，列为 P2 迭代 |

`detect_graph_anomalies` 的完整 scope 列表：
- `all` — 执行全部检测
- `missing_receipt` — PO 行无收货
- `missing_invoice` — 有收货无发票
- `orphan_payment` — 付款无发票关联
- `orphan_node` — 无任何关系的孤立节点

对应 Cypher：
```cypher
-- orphan_node
MATCH (n:Entity)
WHERE NOT (n)--()
RETURN n.entity_type, n.entity_id AS entity, 'orphan_node' AS anomaly_type
```

#### 9.7.4 图增强场景（本次实现）

图的边关系可支撑 PG 难以实现的新分析规则。本次在 hybrid 模式下实现以下 9 个新规则，postgresql 模式下记录技术债暂不实现。

##### 第一类：流程合规（利用路径完整性）

**规则 1：流程跳步检测** — PO 直接关联付款但跳过了收货或发票

```cypher
MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:CONTAINS_LINE]->(line)
WHERE (line)<-[:INVOICES_LINE]-()-[:BELONGS_TO_INVOICE]->()<-[:PAYS_INVOICE]-()
  AND NOT (line)<-[:RECEIVES_LINE]-()
  AND po.valid_from >= datetime() - duration({days: $days})
RETURN po.po_number AS entity, 'process_skip_no_receipt' AS anomaly_type
```

**规则 2：无 PO 发票检测** — 供应商提交的发票没有关联任何 PO 行

```cypher
MATCH (s:Entity {entity_type:'Supplier'})-[:SUBMITS_INVOICE]->(inv:Entity)
WHERE NOT (:Entity)-[:BELONGS_TO_INVOICE]->(inv)
   OR NOT (:Entity {entity_type:'InvoiceLine'})-[:BELONGS_TO_INVOICE]->(inv)-[:INVOICES_LINE]->()
RETURN inv.invoice_num AS entity, s.vendor_id AS vendor_id, 'invoice_without_po' AS anomaly_type
```

**规则 3：未授权供应商收款** — 收到付款的供应商没有关联的采购订单

```cypher
MATCH (pmt:Entity {entity_type:'Payment'})-[:PAYS_INVOICE]->(inv:Entity)<-[:SUBMITS_INVOICE]-(s:Entity {entity_type:'Supplier'})
WHERE NOT (s)-[:CREATES_PO]->(:Entity)
RETURN s.vendor_id AS entity, pmt.check_number AS payment, 'unauthorized_supplier_payment' AS anomaly_type
```

##### 第二类：风险传播（利用多跳遍历）

**规则 4：供应商风险影响面** — 指定供应商的全部关联业务实体

```cypher
MATCH (s:Entity {entity_type:'Supplier', vendor_id: $vendor_id})-[*1..4]->(affected)
RETURN labels(affected) AS entity_labels, affected.entity_type AS type,
       affected.entity_id AS entity_id, count(*) AS path_count
ORDER BY path_count DESC
```

**规则 5：物料供应链中断评估** — 某物料断供时受影响的 PO 行

```cypher
MATCH (m:Entity {entity_type:'Material', entity_id: $material_id})<-[:ORDERS_MATERIAL]-(line:Entity)<-[:CONTAINS_LINE]-(po:Entity)
OPTIONAL MATCH (m)<-[:CONTRACT_COVERS]-(cl:Entity)<-[:CONTAINS_CONTRACT_LINE]-(c:Entity)
RETURN po.po_number, line.item_description, line.quantity,
       c.contract_number AS backup_contract,
       CASE WHEN c IS NULL THEN 'no_alternative' ELSE 'has_contract' END AS risk_level
```

**规则 6：付款连锁风险** — 某发票被拒付后，同一供应商其他待付发票

```cypher
MATCH (inv:Entity {entity_type:'Invoice', invoice_num: $invoice_num})<-[:SUBMITS_INVOICE]-(s:Entity)
MATCH (s)-[:SUBMITS_INVOICE]->(other_inv:Entity)
WHERE other_inv.entity_id <> inv.entity_id
OPTIONAL MATCH (other_inv)<-[:SCHEDULED_FOR]-(ps:Entity {payment_status_flag: 'N'})
RETURN other_inv.invoice_num, other_inv.invoice_amount,
       ps.due_date, ps.amount_remaining,
       'chain_risk' AS risk_type
```

##### 第三类：合规审计（利用模式匹配）

**规则 7：分拆订单检测** — 同一供应商短时间内多个小额 PO（规避审批限额）

```cypher
MATCH (s:Entity {entity_type:'Supplier'})-[:CREATES_PO]->(po:Entity)
WHERE po.valid_from >= datetime() - duration({days: $days})
  AND po.total_amount < $threshold
WITH s, collect(po) AS pos, count(po) AS po_count
WHERE po_count >= $min_count
RETURN s.vendor_id, s.vendor_name, po_count,
       [p IN pos | p.po_number] AS po_numbers,
       'split_order_suspect' AS anomaly_type
```

**规则 8：关联交易检测** — 共享站点的供应商互相参与对方竞价

```cypher
MATCH (s1:Entity {entity_type:'Supplier'})-[:HAS_SITE]->(site:Entity)<-[:HAS_SITE]-(s2:Entity {entity_type:'Supplier'})
WHERE s1.entity_id < s2.entity_id
MATCH (s1)-[:BIDS_ON]->(a:Entity {entity_type:'Auction'})<-[:BIDS_ON]-(s2)
RETURN s1.vendor_name, s2.vendor_name, site.vendor_site_code,
       a.auction_title, 'related_party_bidding' AS anomaly_type
```

**规则 9：虚假供应商识别** — 有付款但无站点、无竞价、无合同的供应商

```cypher
MATCH (s:Entity {entity_type:'Supplier'})-[:SUBMITS_INVOICE]->(inv:Entity)<-[:PAYS_INVOICE]-(:Entity)
WHERE NOT (s)-[:HAS_SITE]->(:Entity)
  AND NOT (s)-[:BIDS_ON]->(:Entity)
  AND NOT (s)-[:CREATES_PO]->(:Entity)-[:CONTAINS_LINE]->(:Entity)-[:ORDERS_MATERIAL]->(:Entity)<-[:CONTRACT_COVERS]-(:Entity)
RETURN s.vendor_id, s.vendor_name, count(DISTINCT inv) AS invoice_count,
       'suspicious_supplier' AS anomaly_type
```

##### 实现方式：规则注册表

在 `detect_graph_anomalies` 工具中引入可插拔的规则注册机制：

```python
ANOMALY_RULES: dict[str, str] = {
    # 基础（3.3 覆盖）
    "missing_receipt": "...",
    "missing_invoice": "...",
    "orphan_payment": "...",
    "orphan_node": "...",
    # 流程合规
    "process_skip": "...",
    "invoice_without_po": "...",
    "unauthorized_payment": "...",
    # 风险传播（需参数，独立工具）
    # → query_risk_impact(vendor_id/material_id/invoice_num)
    # 合规审计
    "split_order": "...",
    "related_party": "...",
    "suspicious_supplier": "...",
}
```

其中：
- 规则 1-3、7-9（无需实体参数）纳入 `detect_graph_anomalies` 的 scope 扩展
- 规则 4-6（需要指定实体参数）作为独立工具 `query_risk_impact`

##### 对工具集的影响

图工具集从 9 个增为 **10 个**：

| 工具 | 类型 |
|------|------|
| `search_knowledge_graph` | 保留 |
| `query_entity_timeline` | 重写 |
| `query_entity_relationships` | 重写 |
| `query_supplier_profile` | 重写 |
| `detect_graph_anomalies` | 重写 + scope 扩展（含规则 1-3、7-9） |
| `compare_entities` | 重写 |
| `trace_procurement_chain` | 新增 |
| `find_contract_coverage` | 新增 |
| `find_competing_suppliers` | 新增 |
| `query_risk_impact` | 新增（规则 4-6，风险传播类） |

`query_risk_impact` 参数签名：

```python
query_risk_impact(
    impact_type: str,    # "supplier_risk" / "material_disruption" / "payment_chain"
    entity_id: str,      # 供应商ID / 物料ID / 发票号
) -> str
```

##### postgresql 模式技术债

以上 9 个规则在 postgresql 模式下暂不实现（PG 实现需要多层嵌套 JOIN / 递归 CTE，复杂度高），记录为技术债：
- `# TECH-DEBT: 流程合规规则（process_skip/invoice_without_po/unauthorized_payment）PG 实现`
- `# TECH-DEBT: 合规审计规则（split_order/related_party/suspicious_supplier）PG 实现`
- `# TECH-DEBT: 风险传播查询（supplier_risk/material_disruption/payment_chain）PG 实现`

#### 9.7.5 图原子能力补充（ReAct 自主分析支撑）

hybrid + ReAct 模式下 LLM 无法命中预定义规则时需自主组合工具分析。经分析存在两个原子能力缺口：

1. **实体详情查询** — 查指定实体全部属性 + 直接关联摘要（通用画像）
2. **两点间路径查询** — 判断两个实体之间是否有关联、路径是什么

补充 2 个原子工具：

**`get_entity_detail`** — 通用实体画像

```python
get_entity_detail(entity_type: str, entity_id: str) -> str
```

```cypher
MATCH (n:Entity {entity_type: $type, entity_id: $id})
OPTIONAL MATCH (n)-[r]-(neighbor:Entity)
RETURN properties(n) AS entity,
       collect(DISTINCT {
           relationship: type(r),
           direction: CASE WHEN startNode(r) = n THEN 'outgoing' ELSE 'incoming' END,
           neighbor_type: neighbor.entity_type,
           neighbor_id: neighbor.entity_id
       }) AS connections
```

**`find_path_between`** — 两点间最短路径

```python
find_path_between(from_type: str, from_id: str, to_type: str, to_id: str) -> str
```

```cypher
MATCH (a:Entity {entity_type: $from_type, entity_id: $from_id})
MATCH (b:Entity {entity_type: $to_type, entity_id: $to_id})
MATCH path = shortestPath((a)-[*..6]-(b))
RETURN [n IN nodes(path) | {type: n.entity_type, id: n.entity_id}] AS nodes,
       [r IN relationships(path) | type(r)] AS edges
```

##### 图工具集最终清单（12 个）

| # | 工具名 | 类型 | 原子能力 |
|---|--------|------|----------|
| 1 | `search_knowledge_graph` | 保留 | 模糊搜索 |
| 2 | `get_entity_detail` | **新增** | 实体精确查找 + 通用画像 |
| 3 | `find_path_between` | **新增** | 两点间路径 |
| 4 | `query_entity_timeline` | 重写 | 时间线 |
| 5 | `query_entity_relationships` | 重写 | 多跳遍历 |
| 6 | `query_supplier_profile` | 重写 | 供应商专用画像 |
| 7 | `detect_graph_anomalies` | 重写 + scope 扩展 | 模式匹配（含 9 个规则） |
| 8 | `compare_entities` | 重写 | 实体对比 |
| 9 | `trace_procurement_chain` | 新增 | 全链路追踪 |
| 10 | `find_contract_coverage` | 新增 | 合同覆盖 |
| 11 | `find_competing_suppliers` | 新增 | 竞争分析 |
| 12 | `query_risk_impact` | 新增 | 风险传播 |

hybrid 模式总工具数：15（结构化）+ 12（图）= **27 个**。

LLM ReAct 自主分析路径：
1. `search_knowledge_graph` / `get_entity_detail` — 定位实体
2. `query_entity_relationships` / `find_path_between` — 探索关系
3. `compare_entities` / `query_supplier_profile` — 深度分析
4. `detect_graph_anomalies` / `query_risk_impact` — 检测异常
5. 组合以上步骤完成任意分析

#### 9.7.6 Prompt 动态增强

两种模式下工具数量、能力边界、分析策略不同，需要在 system prompt 中注入模式相关的"工具使用策略指引"。

##### 需要动态增强的部分

| Prompt 部分 | 是否动态 | 说明 |
|-------------|----------|------|
| 系统角色定义 | 否 | 两种模式相同 |
| 工具描述（docstring） | 自动 | LangChain 根据注入工具集自动生成 |
| **工具使用策略指引** | **是** | 告诉 LLM 分析思路和工具选择优先级 |
| **增强字段解读说明** | **是** | hybrid 模式返回的 has_receipt 等字段需要解释 |
| **图能力提示** | **是** | 提示 LLM 可以利用关系遍历、路径查找等能力 |

##### postgresql 模式策略指引

```text
## 分析策略
你拥有 15 个基于 SQL 的查询和分析工具。推荐分析路径：
1. 先用 query_* 工具精确查询相关数据
2. 用规则工具（run_three_way_match 等）检测异常
3. 用聚合工具（calculate_spend_analysis 等）做统计分析
4. 综合以上结果给出结论

数据特点：返回的是精确的结构化记录，可直接用于数值比对和统计。
```

##### hybrid 模式策略指引

```text
## 分析策略
你拥有两类工具：
- 结构化工具（15个）：精确查询、规则检测、聚合统计
- 图工具（12个）：关系遍历、路径查找、模式匹配、异常检测

推荐分析路径：
1. 探索阶段：用 get_entity_detail / search_knowledge_graph 定位实体
2. 关系分析：用 query_entity_relationships / find_path_between 发现关联
3. 精确验证：用结构化工具（query_purchase_orders 等）获取详细数据
4. 异常检测：用 detect_graph_anomalies（支持 scope：missing_receipt /
   missing_invoice / orphan_payment / orphan_node / process_skip /
   invoice_without_po / unauthorized_payment / split_order /
   related_party / suspicious_supplier）
5. 风险评估：用 query_risk_impact 评估影响面

工具选择原则：
- 需要精确数值/聚合统计 → 用结构化工具
- 需要发现关联/追踪路径/检测结构异常 → 用图工具
- 查询结果中 has_receipt/has_invoice/has_payment 字段表示该 PO 行的履约状态

数据特点：结构化工具返回的数据包含关系状态增强字段，图工具返回关系网络和路径信息。
两类工具可以组合使用：先用图工具发现问题，再用结构化工具精确验证。
```

##### 实现方式

在 `modules/p2p/prompts.py` 中根据模式动态拼接：

```python
def get_tool_strategy_prompt() -> str:
    """根据 query_backend 模式返回对应的工具策略指引。"""
    mode = get_settings().graphiti_etl.query_backend
    if mode == "postgresql":
        return _PG_STRATEGY_PROMPT
    else:  # hybrid
        return _HYBRID_STRATEGY_PROMPT
```

在 Agent 构建时注入 system prompt（`modules/p2p/agent.py`）：

```python
system_prompt = BASE_SYSTEM_PROMPT + "\n\n" + get_tool_strategy_prompt()
```

##### 迁移计划影响

在 9.5 迁移步骤中新增：

| 步骤 | 内容 | 验证 |
|------|------|------|
| H2 | `prompts.py` 新增 `get_tool_strategy_prompt()` + 两种模式的策略文本 | agent 构建时 prompt 包含对应策略 |
| H3 | `agent.py` 注入动态策略 prompt | 端到端验证 LLM 遵循策略指引 |

#### 9.7.7 工具可发现性优化（让 LLM 精准选择工具）

##### 问题分析

LLM 选择工具的决策依据：工具名称 → 工具描述（docstring）→ 参数名/描述 → system prompt 策略指引。当前工具描述只说"做什么"，缺少"何时用"和"何时不用"的指引，导致 LLM 在多工具场景下选择困难。

##### 6 层策略

| 层 | 策略 | 作用 | 实现位置 | 优先级 |
|----|------|------|----------|--------|
| 1 | 精准工具集注入 | 不给无用工具 | `provider.py` | 本次必须 |
| 2 | 工具名称语义化 | 名称即用途 | `@tool` 函数命名 | 本次必须 |
| 3 | 工具描述四要素 | 说明何时用/不用 | `@tool` docstring | 本次必须 |
| 4 | 参数描述增强 | 说明取值范围和业务含义 | docstring Args 段 | 本次必须 |
| 5 | 工具分组提示 | 快速定位工具类别 | system prompt 策略指引 | 本次必须 |
| 6 | 返回值引导 | 提示下一步动作 | 工具返回 JSON `_hint` | 后续优化 |

##### 工具命名规范

| 命名原则 | 说明 | 正例 | 反例 |
|----------|------|------|------|
| 动词 + 对象 | 明确动作和目标 | `detect_graph_anomalies` | `anomalies` |
| 区分动词语义 | query=精确查/analyze=分析/detect=检测/find=发现/trace=追踪/compare=对比 | `query_purchase_orders` | `get_po` |
| 图工具有辨识度 | 图专属工具名称体现图能力 | `trace_procurement_chain` | `get_chain` |
| 避免歧义 | 不要一个名称可以理解为多种用途 | `find_path_between` | `relate` |

##### 工具描述四要素规范

每个 `@tool` 的 docstring 必须包含：

```
1. 一句话说明（做什么）
2. 适用场景（何时用这个工具）
3. 不适用场景（何时应该用其他工具，可选但推荐）
4. 返回内容（返回什么数据，可用于什么后续分析）
```

示例：

```python
@tool
async def query_purchase_orders(...) -> str:
    """按条件精确查询采购订单明细数据。

    适用场景：需要获取具体的采购订单记录，支持按供应商、状态、订单号、时间范围精确过滤。
    返回内容：PO 行级明细（单价、数量、金额、状态、交期），可用于规则检测和数值分析。
    不适用：查询实体关联关系请用 query_entity_relationships；模糊搜索请用 search_knowledge_graph。

    Args:
        vendor_id: 供应商 ID（如 "V001"），为空则返回全部供应商的订单。
        status: 订单状态（approved/pending/closed），为空则不过滤。
        po_number: 采购订单号（如 "PO-001"），精确匹配。
        days: 查询最近 N 天内的订单，默认 30 天，0 表示不限时间范围。
        limit: 返回结果数量上限，0 表示不限制（注意数据量）。
        order_by: 排序方式（date_desc/date_asc/amount_desc/amount_asc）。
    """
```

```python
@tool
async def detect_graph_anomalies(...) -> str:
    """基于图结构检测采购流程中的结构性异常模式（非数值偏差）。

    适用场景：需要发现数据缺失、流程违规、可疑交易模式等结构性问题。
    返回内容：异常实体列表（含异常类型、实体 ID、相关上下文）。
    不适用：数值偏差检测（价格差异）用 run_price_variance_analysis；
            数量偏差检测（超量收货）用 analyze_receipt_anomalies。

    Args:
        scope: 检测范围，可选值：
            "all" — 执行全部检测
            "missing_receipt" — PO 行无收货记录
            "missing_invoice" — 有收货但无发票
            "orphan_payment" — 付款无关联发票
            "orphan_node" — 无任何关系的孤立数据
            "process_skip" — 跳过收货直接开票付款
            "invoice_without_po" — 发票无关联采购订单
            "unauthorized_payment" — 未授权供应商收款
            "split_order" — 疑似分拆订单规避审批
            "related_party" — 共享站点供应商互相竞价
            "suspicious_supplier" — 有付款但无站点/竞价/合同的供应商
        time_range_days: 检查时间范围（天），默认 30。
    """
```

##### 参数描述增强规范

| 要素 | 说明 | 示例 |
|------|------|------|
| 业务含义 | 参数在业务中代表什么 | `vendor_id: 供应商 ID（如 "V001"）` |
| 取值范围 | 枚举值或边界 | `scope: 可选值："all" / "missing_receipt" / ...` |
| 默认行为 | 空值/零值时的行为 | `days: 默认 30 天，0 表示不限时间范围` |
| 组合说明 | 参数间的关系 | `vendor_id 和 po_number 可同时指定，取交集` |

##### 工具分组提示（system prompt 中）

```text
## 工具分类速查

### 精确查询（已知条件查具体记录）
query_purchase_orders / query_receipts / query_invoices / query_payments / query_vendor_master

### 规则检测（检查合规性）
run_three_way_match / run_price_variance_analysis / run_payment_compliance_check / calculate_supplier_kpis

### 聚合统计（汇总分析）
calculate_spend_analysis / analyze_vendor_concentration / calculate_po_cycle_time / detect_duplicate_invoices / analyze_discount_utilization

### 实体探索（定位和了解实体）
get_entity_detail / search_knowledge_graph

### 关系发现（探索关联和路径）
query_entity_relationships / find_path_between / trace_procurement_chain / query_entity_timeline

### 异常检测（发现结构性问题）
detect_graph_anomalies / query_risk_impact

### 对比与画像（多维度分析）
compare_entities / query_supplier_profile / find_contract_coverage / find_competing_suppliers
```

##### 迁移计划影响

在 9.5 迁移步骤中新增：

| 步骤 | 内容 | 验证 |
|------|------|------|
| H4 | 全部 27 个工具 docstring 按四要素规范重写 | 人工 review 通过 |
| H5 | system prompt 中加入工具分组速查表 | LLM 测试选择准确率 |

#### 9.7.8 重构后代码目录结构

PG 和 Graph 工具通过目录物理隔离：

```
modules/p2p/tools/
├── __init__.py                 # 统一导出 + 按模式返回工具集
├── _inject.py                  # 依赖注入（set_repository / set_graphiti_client / set_query_backend）
├── _output.py                  # 工具输出裁剪（_clip_and_dump）
│
├── pg/                         # PG 工具集（15个），postgresql 模式使用
│   ├── __init__.py             # 导出全部 15 个 PG 工具
│   ├── query.py                # 精确查询（4个）
│   │                           #   query_purchase_orders / query_receipts /
│   │                           #   query_invoices / query_payments
│   ├── analysis.py             # 规则检测（6个）
│   │                           #   run_three_way_match / run_price_variance_analysis /
│   │                           #   run_payment_compliance_check / calculate_supplier_kpis /
│   │                           #   query_vendor_master / calculate_spend_analysis
│   └── advanced.py             # 聚合分析（5个）
│                               #   analyze_receipt_anomalies / detect_duplicate_invoices /
│                               #   analyze_discount_utilization / analyze_vendor_concentration /
│                               #   calculate_po_cycle_time
│
└── graph/                      # Graph 工具集（12个），hybrid 模式使用
    ├── __init__.py             # 导出全部 12 个图工具
    ├── search.py               # 语义搜索（1个）
    │                           #   search_knowledge_graph
    ├── entity.py               # 实体操作（3个）
    │                           #   get_entity_detail / query_entity_timeline /
    │                           #   query_entity_relationships
    ├── traversal.py            # 路径遍历（3个）
    │                           #   find_path_between / trace_procurement_chain /
    │                           #   query_supplier_profile
    ├── anomaly.py              # 异常检测（2个）
    │                           #   detect_graph_anomalies（含 10 scope）/ query_risk_impact
    └── comparison.py           # 对比分析（3个）
                                #   compare_entities / find_contract_coverage /
                                #   find_competing_suppliers
```

##### 设计说明

| 决策 | 理由 |
|------|------|
| `_inject.py` / `_output.py` 留在顶层 | 两套工具共用依赖注入和输出裁剪 |
| `pg/` 按功能分文件（query/analysis/advanced） | 与当前结构一致，减少迁移量 |
| `graph/` 按能力类型分文件 | 每个文件 2-3 个工具，职责内聚 |
| `stub.py` 删除 | 4 个 stub 工具已移除 |
| 顶层 `__init__.py` 负责按模式组装 | provider 只需调 `get_tools_for_mode(mode)` |

##### 顶层 `__init__.py` 逻辑

```python
"""P2P 工具包：按模式返回对应工具集。"""

from modules.p2p.tools._inject import (
    set_repository,
    _get_repository,
    set_graphiti_client,
    _get_graphiti_client,
    set_query_backend,
    _get_query_backend,
)
from modules.p2p.tools.pg import PG_TOOLS
from modules.p2p.tools.graph import GRAPH_TOOLS


def get_tools_for_mode(mode: str) -> list:
    """根据 query_backend 模式返回对应工具集。"""
    if mode == "postgresql":
        return list(PG_TOOLS)            # 15 个
    else:  # hybrid
        return list(PG_TOOLS) + list(GRAPH_TOOLS)  # 27 个
```

##### 对 provider.py 的影响

```python
def get_tools(self) -> list[Any]:
    from config.settings import get_settings
    from modules.p2p.tools import get_tools_for_mode

    mode = get_settings().graphiti_etl.query_backend
    return get_tools_for_mode(mode)
```

##### 迁移计划影响

在 9.5 迁移步骤中调整：

| 步骤 | 内容 | 验证 |
|------|------|------|
| B2 | 创建 `tools/pg/` 目录，将 query.py / analysis.py / advanced.py 移入 | import 路径更新后 pytest 通过 |
| G2 | 创建 `tools/graph/` 目录，按文件拆分 12 个图工具 | 新增 test_graph_tools 通过 |
| G3 | 重写顶层 `__init__.py` 为 `get_tools_for_mode()` 模式 | provider 调用验证通过 |
| -- | 删除 `stub.py` | 无引用验证 |
