# Multi-Schema Support Design Document

> 本文档为多 schema 数据源适配方案的完整设计，不涉及代码修改。

---

## 一、现状分析

### 1.1 PG 侧耦合点清单

#### P2PRepository（`modules/p2p/repository.py`）

直接引用的 ORM 模型（来自 `core/database/models.py`）：

| 方法 | 引用的 ORM 模型 | 耦合说明 |
|------|----------------|---------|
| `query_purchase_orders()` | PoHeader, PoLine, PoLineLocation | 三表 JOIN，列名硬编码（po_number, vendor_id, total_amount 等） |
| `query_receipts()` | RcvTransaction | 直接查询，列名硬编码（transaction_id, quantity, rejected_quantity） |
| `query_invoices()` | ApInvoice | 直接查询，列名硬编码（invoice_num, invoice_amount, discount_due_date） |
| `query_payments()` | ApPayment | 直接查询，列名硬编码（check_id, check_number, check_date） |
| `query_suppliers()` | ApSupplier | 直接查询，列名硬编码（vendor_id, vendor_name, vendor_type_lookup_code） |
| `get_contract_prices()` | PoLine | 聚合查询 item_id → standard_price，带缓存 |

#### advanced.py（`modules/p2p/tools/pg/advanced.py`）

5 个 `_xxx_sql()` 函数直接导入 ORM 模型并构建 SQLAlchemy 查询：

| 函数 | 引用的 ORM 模型 | 耦合说明 |
|------|----------------|---------|
| `_receipt_anomalies_sql()` | PoLine, PoLineLocation, RcvTransaction | 直接 `session.get(PoLine, ...)` + 属性访问 |
| `_duplicate_invoices_sql()` | ApInvoice | 直接查询 + 按 vendor_id/amount 分组 |
| `_discount_utilization_sql()` | ApInvoice, ApPayment | JOIN invoice_num 匹配付款 |
| `_vendor_concentration_sql()` | PoHeader, PoLine | GROUP BY 聚合 + 单一来源品类子查询 |
| `_po_cycle_time_sql()` | PoHeader, RcvTransaction, ApInvoice, ApPayment | 4 表关联计算全流程周期 |

> **关键发现**：每个 `_xxx_sql()` 函数都有对应的 `_xxx_python()` 实现，后者通过 QueryBackend 获取数据，**已经是 schema 无关的**。SQL 实现仅在 `postgresql` 模式下使用。

#### analysis.py（`modules/p2p/tools/pg/analysis.py`）

| 函数 | 耦合类型 | 说明 |
|------|---------|------|
| `_three_way_match_graph_enhanced()` | 图侧 Cypher 硬编码 | 含 `PurchaseOrder`、`CONTAINS_LINE`、`RECEIVES_LINE`、`INVOICES_LINE` |
| 其余 5 个工具 | 无 | 全部通过 QueryBackend 调用，schema 无关 |

---

### 1.2 图侧耦合点清单

#### _resolve.py（`modules/p2p/tools/graph/_resolve.py`）

硬编码 `_BUSINESS_ID_FIELDS` 字典，10 个节点类型：

```
PurchaseOrder → ["po_number"]
Supplier      → ["vendor_id", "segment1"]
Invoice       → ["invoice_num"]
Payment       → ["check_number"]
Receipt       → ["receipt_num"]
Material      → ["segment1"]
Contract      → ["contract_number"]
Auction       → ["document_number"]
SupplierSite  → ["vendor_site_code"]
POLine        → ["po_number"]
```

#### 12 个图工具中的硬编码节点/边类型

| 文件 | 工具函数 | 硬编码节点类型 | 硬编码边类型 |
|------|---------|-------------|------------|
| search.py | `search_knowledge_graph` | 无（语义搜索，类型作为过滤参数传入） | 无 |
| entity.py | `get_entity_detail` | 无（类型由参数传入） | 无 |
| entity.py | `query_entity_timeline` | 无 | 无 |
| entity.py | `query_entity_relationships` | 无 | 无 |
| traversal.py | `find_path_between` | 无 | 无 |
| traversal.py | `query_supplier_profile` | **Supplier** | **CREATES_PO, SUBMITS_INVOICE, BIDS_ON, HAS_SITE** |
| traversal.py | `trace_procurement_chain` | **PurchaseOrder** | **CONTAINS_LINE, RECEIVES_LINE, INVOICES_LINE, PAYS_INVOICE** |
| anomaly.py | `detect_graph_anomalies` | **PurchaseOrder, POLine, Supplier, Invoice, Payment, Material, Auction** | **CONTAINS_LINE, RECEIVES_LINE, INVOICES_LINE, PAYS_INVOICE, SUBMITS_INVOICE, CREATES_PO, BIDS_ON, ORDERS_MATERIAL, CONTRACT_COVERS** |
| anomaly.py | `query_risk_impact` | **Supplier** | 通用 `-[r]->(target)` 遍历 |
| comparison.py | `compare_entities` | 无（类型由参数传入） | 无 |
| comparison.py | `find_contract_coverage` | **POLine, Contract, ContractLine** | **ORDERS_MATERIAL, CONTRACT_COVERS** |
| comparison.py | `find_competing_suppliers` | **Supplier, Auction** | **BIDS_ON, HAS_BID** |

#### analysis.py / advanced.py 中的图增强函数

| 函数 | 位置 | 硬编码内容 |
|------|------|-----------|
| `_three_way_match_graph_enhanced()` | analysis.py:56 | 节点：`PurchaseOrder`；边：`CONTAINS_LINE, RECEIVES_LINE, INVOICES_LINE` |
| `_get_vendor_relationship_density()` | advanced.py:585 | 节点：`Supplier`；边：`CREATES_PO, SUBMITS_INVOICE, BIDS_ON` |

#### 汇总：需参数化的节点类型（10 个）

`PurchaseOrder`, `Supplier`, `Invoice`, `Payment`, `Receipt`, `Material`, `POLine`, `Contract`, `Auction`, `SupplierSite`

#### 汇总：需参数化的边类型（13 个）

`CREATES_PO`, `CONTAINS_LINE`, `RECEIVES_LINE`, `INVOICES_LINE`, `BELONGS_TO_INVOICE`, `PAYS_INVOICE`, `SUBMITS_INVOICE`, `HAS_SITE`, `BIDS_ON`, `HAS_BID`, `ORDERS_MATERIAL`, `CONTRACT_COVERS`, `CONTAINS_CONTRACT_LINE`

---

### 1.3 注入层现状

#### _inject.py（`modules/p2p/tools/_inject.py`）

3 个模块级全局单例 + setter/getter：

| 全局变量 | 类型 | 注入时机 |
|---------|------|---------|
| `_repository` | `P2PRepository` | main.py lifespan 启动时 |
| `_graphiti_client` | `GraphitiClient` | main.py lifespan 启动时（ETL 启用时） |
| `_query_backend` | `QueryBackend` | main.py lifespan 启动时（由 `create_query_backend()` 工厂创建） |

#### main.py 启动注入流程

```
lifespan()
  │
  ├─ 1. P2PRepository(session_factory)
  │     └─ set_repository(repo)
  │
  ├─ 2. [如果 neo4j + graphiti_etl 启用]
  │     ├─ GraphitiClient(settings) → initialize()
  │     │   └─ set_graphiti_client(client)
  │     │
  │     └─ create_query_backend(mode, repo, client)
  │         └─ set_query_backend(backend)
  │
  └─ 3. [关闭时]
        ├─ set_graphiti_client(None)
        └─ set_query_backend(None)
```

#### QueryBackend 工厂逻辑（`core/etl/query_backend.py`）

```
create_query_backend(mode, repository, graphiti_client)
  ├─ mode="postgresql"  → PostgreSQLBackend(repository)
  ├─ mode="hybrid"      → Neo4jStructuredBackend(graphiti_client)
  └─ mode="graphiti"    → Neo4jStructuredBackend(graphiti_client)
      └─ graphiti_client 为 None 时 → 降级为 PostgreSQLBackend(repository)
```

> **关键发现**：当前注入机制天然支持多态——只要注入不同的 Repository 实例和不同配置的 Neo4jStructuredBackend，上层代码完全无感。

---

## 二、核心抽象设计

### 2.1 P2PRepositoryProtocol 接口定义

将当前 `P2PRepository` 的公开方法提取为 `typing.Protocol`，同时将 `advanced.py` 中 5 个直接操作 ORM 的 SQL 函数下沉为 Repository 方法。

```python
# modules/p2p/schemas/protocol.py

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class P2PRepositoryProtocol(Protocol):
    """P2P 数据访问协议。
    
    所有方法返回统一 dict 结构（见 2.4 输出契约），
    上层调用方无需感知底层 schema 差异。
    """

    # ── 基础查询（现有 6 个方法） ──

    def query_purchase_orders(
        self,
        vendor_id: str = "",
        status: str = "",
        days: int = 30,
        po_number: str = "",
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_receipts(
        self,
        po_number: str = "",
        vendor_id: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_invoices(
        self,
        po_number: str = "",
        vendor_id: str = "",
        status: str = "",
        invoice_num: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_payments(
        self,
        invoice_num: str = "",
        vendor_id: str = "",
        check_number: str = "",
        days: int = 30,
        limit: int = 0,
        order_by: str = "",
    ) -> list[dict[str, Any]]: ...

    def query_suppliers(
        self,
        vendor_id: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_contract_prices(self) -> dict[str, float]: ...

    # ── Flattened 方法（规则引擎使用） ──

    def get_flattened_purchase_orders(
        self, vendor_id: str = "", po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_receipts(
        self, vendor_id: str = "", po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_invoices(
        self, vendor_id: str = "", po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_flattened_payments(
        self, vendor_id: str = "", po_number: str = "",
    ) -> list[dict[str, Any]]: ...

    def invalidate_contract_price_cache(self) -> None: ...

    # ── 聚合分析（从 advanced.py 下沉的 5 个方法） ──

    def analyze_receipt_anomalies(
        self, vendor_id: str = "", po_number: str = "", days: int = 30,
    ) -> list[dict[str, Any]]: ...

    def detect_duplicate_invoices(
        self, vendor_id: str = "", days: int = 30,
    ) -> list[dict[str, Any]]: ...

    def analyze_discount_utilization(
        self, vendor_id: str = "", days: int = 30,
    ) -> dict[str, Any]: ...

    def analyze_vendor_concentration(
        self, days: int = 30, top_n: int = 10,
    ) -> dict[str, Any]: ...

    def calculate_po_cycle_time(
        self, days: int = 30, vendor_id: str = "",
    ) -> dict[str, Any]: ...
```

> **设计说明**：
> - 使用 `runtime_checkable Protocol` 而非 ABC，与项目中 `QueryBackend` 保持一致。
> - 下沉 5 个聚合方法的理由：这些函数直接操作 ORM 模型，是 schema 强耦合代码，归属数据访问层而非工具层。
> - 所有方法均为同步（与现有 Repository 一致），异步适配由 `PostgreSQLBackend` 的 `asyncio.to_thread()` 处理。

---

### 2.2 GraphSchema 数据契约定义

```python
# modules/p2p/schemas/protocol.py（同文件）

from dataclasses import dataclass, field

@dataclass(frozen=True)
class GraphSchema:
    """图数据库 schema 描述。
    
    将业务概念（purchase_order）映射到具体的节点/边类型名（PurchaseOrder / CREATES_PO），
    供图工具 Cypher 查询和实体解析使用。
    """

    # ── 节点类型映射 ──
    # key: 业务概念标识（固定，跨 schema 不变）
    # value: 具体节点类型名（schema 特定）
    node_types: dict[str, str] = field(default_factory=dict)
    # 示例：
    # {
    #     "purchase_order": "PurchaseOrder",
    #     "supplier":       "Supplier",
    #     "invoice":        "Invoice",
    #     "payment":        "Payment",
    #     "receipt":        "Receipt",
    #     "material":       "Material",
    #     "po_line":        "POLine",
    #     "contract":       "Contract",
    #     "auction":        "Auction",
    #     "supplier_site":  "SupplierSite",
    #     "contract_line":  "ContractLine",
    # }

    # ── 边类型映射 ──
    edge_types: dict[str, str] = field(default_factory=dict)
    # 示例：
    # {
    #     "creates_po":              "CREATES_PO",
    #     "contains_line":           "CONTAINS_LINE",
    #     "receives_line":           "RECEIVES_LINE",
    #     "invoices_line":           "INVOICES_LINE",
    #     "belongs_to_invoice":      "BELONGS_TO_INVOICE",
    #     "pays_invoice":            "PAYS_INVOICE",
    #     "submits_invoice":         "SUBMITS_INVOICE",
    #     "has_site":                "HAS_SITE",
    #     "bids_on":                 "BIDS_ON",
    #     "has_bid":                 "HAS_BID",
    #     "orders_material":         "ORDERS_MATERIAL",
    #     "contract_covers":         "CONTRACT_COVERS",
    #     "contains_contract_line":  "CONTAINS_CONTRACT_LINE",
    # }

    # ── 业务 ID 字段映射 ──
    # key: 具体节点类型名（与 node_types 的 value 一致）
    # value: 该类型节点上可作为业务标识的属性名列表
    business_id_fields: dict[str, list[str]] = field(default_factory=dict)
    # 示例：
    # {
    #     "PurchaseOrder": ["po_number"],
    #     "Supplier":      ["vendor_id", "segment1"],
    #     ...
    # }

    # ── 便捷访问方法 ──

    def node(self, concept: str) -> str:
        """根据业务概念获取节点类型名。找不到则抛出 KeyError。"""
        return self.node_types[concept]

    def edge(self, relationship: str) -> str:
        """根据业务关系获取边类型名。找不到则抛出 KeyError。"""
        return self.edge_types[relationship]

    def biz_id_fields(self, node_type: str) -> list[str]:
        """获取指定节点类型的业务 ID 字段列表。"""
        return self.business_id_fields.get(node_type, [])
```

> **业务概念标识（key）命名约定**：全小写 + 下划线，与具体 schema 无关。这组 key 是跨 schema 的固定契约，新 schema 只需提供不同的 value。

---

### 2.3 SchemaRegistry 工厂设计

```python
# modules/p2p/schemas/__init__.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy.orm import sessionmaker, Session

from modules.p2p.schemas.protocol import P2PRepositoryProtocol, GraphSchema

@dataclass
class SchemaRegistration:
    """一套 schema 的完整注册信息。"""
    name: str
    repository_factory: Callable[[sessionmaker[Session]], P2PRepositoryProtocol]
    graph_schema: GraphSchema
    # Neo4jStructuredBackend 属性映射器工厂（可选，仅图模式需要）
    graph_backend_factory: Callable[..., Any] | None = None


class SchemaRegistry:
    """Schema 注册表 — 单例，启动时注册，运行时按配置查找。"""

    _schemas: dict[str, SchemaRegistration] = {}

    @classmethod
    def register(cls, registration: SchemaRegistration) -> None:
        cls._schemas[registration.name] = registration

    @classmethod
    def get(cls, name: str) -> SchemaRegistration:
        if name not in cls._schemas:
            available = ", ".join(cls._schemas.keys()) or "(empty)"
            raise ValueError(
                f"Unknown ERP schema '{name}'. Available: {available}"
            )
        return cls._schemas[name]

    @classmethod
    def available(cls) -> list[str]:
        return list(cls._schemas.keys())
```

**注册时机**：各 schema 包的 `__init__.py` 在 import 时自动注册。

```python
# modules/p2p/schemas/oracle_ebs/__init__.py

from modules.p2p.schemas import SchemaRegistry, SchemaRegistration
from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository
from modules.p2p.schemas.oracle_ebs.graph_schema import ORACLE_EBS_GRAPH_SCHEMA
from modules.p2p.schemas.oracle_ebs.graph_backend import OracleEBSGraphBackendFactory

SchemaRegistry.register(SchemaRegistration(
    name="oracle_ebs",
    repository_factory=OracleEBSRepository,
    graph_schema=ORACLE_EBS_GRAPH_SCHEMA,
    graph_backend_factory=OracleEBSGraphBackendFactory,
))
```

---

### 2.4 输出契约规范

无论底层 schema 如何，所有查询方法必须返回以下固定 key 的 dict。这是 tools 层和规则引擎的唯一契约。

#### Purchase Orders

```python
{
    "po_number": str,           # 采购订单号
    "vendor_id": str,           # 供应商 ID
    "vendor_name": str,         # 供应商名称
    "material_category": str,   # 物料类别
    "po_amount": float,         # 订单行金额
    "po_quantity": float,       # 订单数量
    "unit_price": float,        # 单价
    "contract_price": float,    # 合同价（无合同则 0.0）
    "status": str,              # 状态
    "creation_date": str,       # 创建日期（ISO 格式）
    "required_date": str,       # 需求日期（ISO 格式）
    "material_code": str,       # 物料编码
    "material_name": str,       # 物料名称
    "line_number": str,         # 行号
}
```

#### Receipts

```python
{
    "receipt_id": str,          # 收货单 ID（如 "GR-0001"）
    "gr_number": str,           # 收货单号
    "po_number": str,           # 关联采购订单号
    "vendor_id": str,           # 供应商 ID
    "gr_quantity": float,       # 收货数量
    "receipt_date": str,        # 收货日期（ISO 格式）
    "quality_passed": bool,     # 质检是否通过
}
```

#### Invoices

```python
{
    "invoice_num": str,         # 发票号
    "po_number": str,           # 关联采购订单号
    "vendor_id": str,           # 供应商 ID
    "vendor_name": str,         # 供应商名称
    "invoice_amount": float,    # 发票金额
    "due_date": str,            # 到期日（ISO 格式）
    "discount_due_date": str,   # 折扣截止日（ISO 格式，无则 ""）
    "discount_amount": float,   # 折扣金额
    "approval_status": str,     # 审批状态
    "creation_date": str,       # 创建日期（ISO 格式）
}
```

#### Payments

```python
{
    "check_id": str,            # 付款 ID
    "check_number": str,        # 付款单号
    "invoice_num": str,         # 关联发票号
    "vendor_id": str,           # 供应商 ID
    "amount": float,            # 付款金额
    "check_date": str,          # 付款日期（ISO 格式）
    "payment_method_code": str, # 付款方式
}
```

#### Suppliers

```python
{
    "vendor_id": str,           # 供应商 ID
    "vendor_name": str,         # 供应商名称
    "segment1": str,            # 供应商编号
    "vendor_type": str,         # 供应商类型
    "terms_id": str,            # 付款条款 ID
    "enabled_flag": str,        # 启用标志
}
```

#### Contract Prices

```python
dict[str, float]  # { item_id: standard_price, ... }
```

> **约束**：新 schema 实现时，即使原始字段语义略有差异，也必须映射到上述 key。若某字段在新 schema 中不存在，填默认值（str→""、float→0.0、bool→False）。

---

## 三、目录结构

### 3.1 新增目录布局

```
modules/p2p/schemas/                    # 新增目录
├── __init__.py                         # SchemaRegistry + SchemaRegistration 定义
├── protocol.py                         # P2PRepositoryProtocol + GraphSchema 数据契约
├── oracle_ebs/                         # Oracle EBS schema 实现
│   ├── __init__.py                     # 自动注册到 SchemaRegistry
│   ├── models.py                       # 重新导出 core/database/models.py 中的 EBS ORM 模型
│   ├── repository.py                   # OracleEBSRepository（从 modules/p2p/repository.py 迁入）
│   ├── graph_schema.py                 # ORACLE_EBS_GRAPH_SCHEMA 常量实例
│   └── graph_backend.py               # EBS 专属 Neo4jStructuredBackend 属性映射器
└── new_erp/                            # 新 ERP schema 实现（初始为 EBS 副本，供后续修改）
    ├── __init__.py                     # 自动注册到 SchemaRegistry
    ├── models.py                       # 新 schema 的 ORM 模型定义
    ├── repository.py                   # NewERPRepository
    ├── graph_schema.py                 # NEW_ERP_GRAPH_SCHEMA 常量实例
    └── graph_backend.py               # 新 schema 专属属性映射器
```

**各文件职责**：

| 文件 | 职责 |
|------|------|
| `schemas/__init__.py` | ��义 `SchemaRegistry`、`SchemaRegistration`；导入所有 schema 子包以触发注册 |
| `schemas/protocol.py` | 定义 `P2PRepositoryProtocol`（Protocol）和 `GraphSchema`（dataclass），是跨 schema 的固��契约 |
| `schemas/<name>/models.py` | 该 schema 的 SQLAlchemy ORM 模型（表名、列名、关系） |
| `schemas/<name>/repository.py` | 实现 `P2PRepositoryProtocol`，使用自己的 ORM 模型构建查询 |
| `schemas/<name>/graph_schema.py` | 提供 `GraphSchema` 实例（节点/边类型名、业务 ID 字段映射） |
| `schemas/<name>/graph_backend.py` | 提供属性映射器（Neo4j 节点属性 → 统一输出 dict） |

---

### 3.2 现有代码迁移映射

| 现有位置 | 操作 | 目标位置 |
|---------|------|---------|
| `modules/p2p/repository.py` | **迁移** | `modules/p2p/schemas/oracle_ebs/repository.py`（重命名为 `OracleEBSRepository`） |
| `modules/p2p/repository.py`（原位置） | **保留兼容导出** | 保留文件，内容改为 `from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository as P2PRepository`，避免外部引用断裂 |
| `core/database/models.py` | **原地不动** | ETL、Alembic 等仍引用此文件；`oracle_ebs/models.py` 通过 re-export 引用 |
| `core/etl/query_backend.py` | **��地不动** | `PostgreSQLBackend` 接受任何满足 Protocol 的 repository；`Neo4jStructuredBackend` 的属性映射逻辑提取到 schema 包 |
| `modules/p2p/tools/pg/advanced.py` | **5 个 `_xxx_sql()` 函数迁移** | 各自迁入对应 schema 的 `repository.py` 作为方法 |
| `modules/p2p/tools/graph/_resolve.py` | **`_BUSINESS_ID_FIELDS` 删除** | 改为从注入的 `GraphSchema.business_id_fields` 读取 |
| `modules/p2p/tools/_inject.py` | **新增** `_graph_schema` 全局变量 | 增加 `set_graph_schema()` / `_get_graph_schema()` |
| `config/settings.py` | **新增字段** | `Settings.erp_schema: str = "oracle_ebs"` |
| `api/main.py` | **修改启动逻辑** | 读取 `erp_schema` 配置，从 `SchemaRegistry` 获取注册信息，注入对应实现 |

> **最小侵入原则**：`core/database/models.py`、`core/etl/query_backend.py` 原地不动。工具层（tools/）仅做"将硬编码替换为 schema 查找"的修改，不改变工具签名和业务逻辑。

---

## 四、PG 侧改造方案

### 4.1 Repository 多态方案

**现状**：`P2PRepository` 是一个具体类，直接在 `_inject.py` 中被注入。

**改造**：

1. 在 `schemas/protocol.py` 中定义 `P2PRepositoryProtocol`（见 2.1）。
2. 将现有 `P2PRepository` 代码迁移到 `schemas/oracle_ebs/repository.py`，重命名为 `OracleEBSRepository`。
3. `_inject.py` 中 `_repository` 的类型标注从 `P2PRepository` 改为 `P2PRepositoryProtocol`。
4. `main.py` 启动时根据配置选择 repository 工厂：

```python
# main.py lifespan 中
from modules.p2p.schemas import SchemaRegistry

schema_reg = SchemaRegistry.get(settings.erp_schema)
repo = schema_reg.repository_factory(session_factory)
set_repository(repo)
```

5. 原 `modules/p2p/repository.py` 保留为兼容桥：

```python
# modules/p2p/repository.py（兼容导出）
from modules.p2p.schemas.oracle_ebs.repository import OracleEBSRepository as P2PRepository

__all__ = ["P2PRepository"]
```

---

### 4.2 advanced.py 5 个 SQL 函数下沉方案

**目标**：将直接操作 ORM 的 SQL 逻辑从 tools 层下沉到 Repository，使 tools 层完全 schema 无关。

**逐函数迁移**：

| advanced.py 函数 | → Repository 方法 | 说明 |
|-----------------|-------------------|------|
| `_receipt_anomalies_sql(repo, vendor_id, po_number, days)` | `repo.analyze_receipt_anomalies(vendor_id, po_number, days)` | 消除对 `repo._session_factory()` 的私有属性访问 |
| `_duplicate_invoices_sql(repo, vendor_id, days)` | `repo.detect_duplicate_invoices(vendor_id, days)` | 同上 |
| `_discount_utilization_sql(repo, vendor_id, days)` | `repo.analyze_discount_utilization(vendor_id, days)` | 同上 |
| `_vendor_concentration_sql(repo, days, top_n)` | `repo.analyze_vendor_concentration(days, top_n)` | 同上 |
| `_po_cycle_time_sql(repo, days, vendor_id)` | `repo.calculate_po_cycle_time(days, vendor_id)` | 同上 |

**改造后的 advanced.py 调用方式**：

```python
# 改造前
if _get_mode() == "postgresql":
    repo = _get_repository()
    result = await asyncio.to_thread(
        _receipt_anomalies_sql, repo, vendor_id, po_number, days
    )

# 改造后
if _get_mode() == "postgresql":
    repo = _get_repository()
    result = await asyncio.to_thread(
        repo.analyze_receipt_anomalies, vendor_id, po_number, days
    )
```

> **注意**：`_xxx_python()` 实现不迁移，它们已经通过 QueryBackend 工作，保留在 `advanced.py` 中。

---

### 4.3 ORM 模型隔离方案

**原则**：EBS 模型不动，新 schema 模型独立定义。

#### Oracle EBS 模型

`schemas/oracle_ebs/models.py` 通过 re-export 引用现有模型：

```python
# modules/p2p/schemas/oracle_ebs/models.py
from core.database.models import (
    ApSupplier,
    PoHeader,
    PoLine,
    PoLineLocation,
    RcvTransaction,
    ApInvoice,
    ApPayment,
    # ... 其余 EBS 模型
)
```

`core/database/models.py` 保持不变，ETL 和 Alembic 继续直接引用。

#### 新 ERP 模型

`schemas/new_erp/models.py` 独立定义 ORM 模型：

```python
# modules/p2p/schemas/new_erp/models.py
from core.database.models import Base  # 共用 DeclarativeBase

class NewPurchaseOrder(Base):
    __tablename__ = "new_purchase_orders"
    # 新 schema 特有的列定义 ...

class NewReceipt(Base):
    __tablename__ = "new_receipts"
    # ...
```

> **同一个 PG 实例**：共用 `Base`（DeclarativeBase），SQLAlchemy 可在同一 engine 上查询不同表。新表由外部系统创建，不走 Alembic。

---

## 五、图侧改造方案

### 5.1 _resolve.py 参数化方案

**现状**：`_BUSINESS_ID_FIELDS` 是模块级硬编码字典。

**改造**：

```python
# 改造前（_resolve.py）
_BUSINESS_ID_FIELDS: dict[str, list[str]] = {
    "PurchaseOrder": ["po_number"],
    "Supplier": ["vendor_id", "segment1"],
    ...
}

# 改造后
from modules.p2p.tools._inject import _get_graph_schema

async def resolve_entity(client, entity_type, entity_id):
    schema = _get_graph_schema()
    biz_fields = schema.biz_id_fields(entity_type)  # 替代硬编码字典查找
    ...
```

`_inject.py` 新增 `_graph_schema` 管理：

```python
# _inject.py 新增
from modules.p2p.schemas.protocol import GraphSchema

_graph_schema: GraphSchema | None = None

def set_graph_schema(schema: GraphSchema | None) -> None:
    global _graph_schema
    _graph_schema = schema

def _get_graph_schema() -> GraphSchema:
    if _graph_schema is None:
        raise RuntimeError("GraphSchema not injected")
    return _graph_schema
```

---

### 5.2 12 个图工具 Cypher 参数化方案

**改造模式**：将硬编码的节点/边类型名替换为 `GraphSchema` 查找。以 f-string 插入 Cypher。

> 以下逐工具列出需替换的点。无需改动的工具（search.py、entity.py 三个、find_path_between、compare_entities）已在 1.2 确认，此处不再赘述。

#### traversal.py — `query_supplier_profile`

```python
# 改造前
cypher = """
MATCH (s:Entity {entity_type:'Supplier', entity_id: $id})
OPTIONAL MATCH (s)-[:RELATES_TO {name:'CREATES_PO'}]->(po:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {name:'SUBMITS_INVOICE'}]->(inv:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {name:'BIDS_ON'}]->(auc:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {name:'HAS_SITE'}]->(site:Entity)
...
"""

# 改造后
gs = _get_graph_schema()
cypher = f"""
MATCH (s:Entity {{entity_type:'{gs.node("supplier")}', entity_id: $id}})
OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("creates_po")}'}}]->(po:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("submits_invoice")}'}}]->(inv:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("bids_on")}'}}]->(auc:Entity)
OPTIONAL MATCH (s)-[:RELATES_TO {{name:'{gs.edge("has_site")}'}}]->(site:Entity)
...
"""
```

#### traversal.py — `trace_procurement_chain`

| 硬编码值 | 替换为 |
|---------|--------|
| `'PurchaseOrder'` | `gs.node("purchase_order")` |
| `'CONTAINS_LINE'` | `gs.edge("contains_line")` |
| `'RECEIVES_LINE'` | `gs.edge("receives_line")` |
| `'INVOICES_LINE'` | `gs.edge("invoices_line")` |
| `'PAYS_INVOICE'` | `gs.edge("pays_invoice")` |

#### anomaly.py — `detect_graph_anomalies`

此函数是硬编码最密集的，按 scope 分支逐一替换：

| scope | 硬编码节点 | 硬编码边 |
|-------|----------|---------|
| `missing_receipt` | PurchaseOrder, POLine | CONTAINS_LINE, RECEIVES_LINE |
| `missing_invoice` | PurchaseOrder, POLine | CONTAINS_LINE, INVOICES_LINE |
| `unpaid_invoice` | Invoice | PAYS_INVOICE |
| `supplier_risk` | Supplier | CREATES_PO, SUBMITS_INVOICE |
| `price_anomaly` | POLine | ORDERS_MATERIAL, CONTRACT_COVERS |
| `bidding_anomaly` | Auction, Supplier | BIDS_ON, HAS_BID |
| `payment_delay` | Payment, Invoice | PAYS_INVOICE |
| `overview`（默认） | PurchaseOrder, Supplier, Invoice, Payment, Material, Auction | CONTAINS_LINE, RECEIVES_LINE, INVOICES_LINE, PAYS_INVOICE, SUBMITS_INVOICE, CREATES_PO, BIDS_ON, ORDERS_MATERIAL, CONTRACT_COVERS |

**改造策略**：在函数入口获取 `gs = _get_graph_schema()`，所有 Cypher 模板中的字面量替换为 `gs.node(...)` / `gs.edge(...)` 调用。

#### anomaly.py — `query_risk_impact`

| 硬编码值 | 替换为 |
|---------|--------|
| `'Supplier'` | `gs.node("supplier")` |

#### comparison.py — `find_contract_coverage`

| 硬编码值 | 替换为 |
|---------|--------|
| `'POLine'` | `gs.node("po_line")` |
| `'Contract'` / `'ContractLine'` | `gs.node("contract")` / `gs.node("contract_line")` |
| `'ORDERS_MATERIAL'` | `gs.edge("orders_material")` |
| `'CONTRACT_COVERS'` | `gs.edge("contract_covers")` |

#### comparison.py — `find_competing_suppliers`

| 硬编码值 | 替换为 |
|---------|--------|
| `'Supplier'` | `gs.node("supplier")` |
| `'Auction'` | `gs.node("auction")` |
| `'BIDS_ON'` | `gs.edge("bids_on")` |
| `'HAS_BID'` | `gs.edge("has_bid")` |

---

### 5.3 analysis.py / advanced.py 图增强函数参数化方案

#### `_three_way_match_graph_enhanced()`（analysis.py:56）

```python
# 改造前
cypher = """
MATCH (po:Entity {entity_type:'PurchaseOrder'})-[:RELATES_TO {name:'CONTAINS_LINE'}]->(line:Entity)
WHERE ($po_number IS NULL OR po.po_number = $po_number)
OPTIONAL MATCH (line)<-[:RELATES_TO {name:'RECEIVES_LINE'}]-(rcv:Entity)
OPTIONAL MATCH (line)<-[:RELATES_TO {name:'INVOICES_LINE'}]-(il:Entity)
...
"""

# 改造后
gs = _get_graph_schema()
cypher = f"""
MATCH (po:Entity {{entity_type:'{gs.node("purchase_order")}'}})
  -[:RELATES_TO {{name:'{gs.edge("contains_line")}'}}]->(line:Entity)
WHERE ($po_number IS NULL OR po.po_number = $po_number)
OPTIONAL MATCH (line)<-[:RELATES_TO {{name:'{gs.edge("receives_line")}'}}]-(rcv:Entity)
OPTIONAL MATCH (line)<-[:RELATES_TO {{name:'{gs.edge("invoices_line")}'}}]-(il:Entity)
...
"""
```

#### `_get_vendor_relationship_density()`（advanced.py:585）

```python
# 改造前
cypher = """
MATCH (s:Entity {entity_type:'Supplier'})-[r]->(target:Entity)
WHERE s.vendor_id IN $ids
RETURN s.vendor_id AS vendor_id,
       count(r) AS total_connections,
       count(CASE WHEN r.name = 'CREATES_PO' THEN 1 END) AS po_connections,
       count(CASE WHEN r.name = 'SUBMITS_INVOICE' THEN 1 END) AS invoice_connections,
       count(CASE WHEN r.name = 'BIDS_ON' THEN 1 END) AS auction_connections
...
"""

# 改造后
gs = _get_graph_schema()
cypher = f"""
MATCH (s:Entity {{entity_type:'{gs.node("supplier")}'}})
  -[r]->(target:Entity)
WHERE s.vendor_id IN $ids
RETURN s.vendor_id AS vendor_id,
       count(r) AS total_connections,
       count(CASE WHEN r.name = '{gs.edge("creates_po")}' THEN 1 END) AS po_connections,
       count(CASE WHEN r.name = '{gs.edge("submits_invoice")}' THEN 1 END) AS invoice_connections,
       count(CASE WHEN r.name = '{gs.edge("bids_on")}' THEN 1 END) AS auction_connections
...
"""
```

> **安全说明**：`gs.node()` / `gs.edge()` 返回的是启动时从配置加载的静态字符串，不含用户输入，不存在 Cypher 注入风险。

---

## 六、注入与启动流程改造

### 6.1 config.yaml 新增配置项

```yaml
# config.yaml 顶层新增
erp_schema: oracle_ebs    # 可选值: oracle_ebs | new_erp
```

`config/settings.py` 对应新增：

```python
class Settings(BaseSettings):
    # ... 现有字段 ...
    erp_schema: str = "oracle_ebs"
```

支持环境变量覆盖：`ERP_SCHEMA=new_erp`。

---

### 6.2 main.py 启动流程改造

**改造前**（简化）：

```python
async def lifespan(app):
    settings = get_settings()
    session_factory = get_session_factory()

    # 硬编码 P2PRepository
    repo = P2PRepository(session_factory)
    set_repository(repo)

    # 硬编码 create_query_backend
    if etl_enabled:
        graphiti_client = GraphitiClient(settings)
        set_graphiti_client(graphiti_client)
        qb = create_query_backend(mode, repo, graphiti_client)
        set_query_backend(qb)
```

**改造���**：

```python
async def lifespan(app):
    settings = get_settings()
    session_factory = get_session_factory()

    # ── 1. 按配置获取 schema 注册信息 ──
    import modules.p2p.schemas  # 触发所有 schema 子包注册
    from modules.p2p.schemas import SchemaRegistry
    schema_reg = SchemaRegistry.get(settings.erp_schema)

    # ── 2. 注入 Repository（多态） ──
    repo = schema_reg.repository_factory(session_factory)
    set_repository(repo)

    # ── 3. 注入 GraphSchema ──
    set_graph_schema(schema_reg.graph_schema)

    # ── 4. 注入 QueryBackend（已有逻辑不变） ──
    if etl_enabled:
        graphiti_client = GraphitiClient(settings)
        set_graphiti_client(graphiti_client)
        qb = create_query_backend(mode, repo, graphiti_client)
        set_query_backend(qb)

    # ── 5. 如有 schema 专属的图后端映射器，覆盖默认行为 ──
    # （可选：当 Neo4jStructuredBackend 的属性映射需要 schema 感知时）

    yield

    set_graph_schema(None)
    set_graphiti_client(None)
    set_query_backend(None)
```

**`_inject.py` 变更汇总**：

| 变更 | 说明 |
|------|------|
| 新增 `_graph_schema: GraphSchema \| None` | 图 schema 全局变量 |
| 新增 `set_graph_schema()` | setter |
| 新增 `_get_graph_schema()` | getter，未注入时抛 RuntimeError |
| `_repository` 类型标注 | 从 `P2PRepository` 改为 `P2PRepositoryProtocol` |

---

## 七、实施计划（42 个任务）

### Phase 1：建立抽象层（无行为变更）

| # | 任务 | 涉及文件 |
|---|------|---------|
| 1 | 创建 `modules/p2p/schemas/__init__.py` — SchemaRegistry + SchemaRegistration | 新建 |
| 2 | 创建 `modules/p2p/schemas/protocol.py` — P2PRepositoryProtocol + GraphSchema | 新建 |
| 3 | 创建 `modules/p2p/schemas/oracle_ebs/__init__.py` — 空注册（暂不填实现） | 新建 |
| 4 | 创建 `modules/p2p/schemas/oracle_ebs/models.py` — re-export EBS ORM 模型 | 新建 |
| 5 | 创建 `modules/p2p/schemas/oracle_ebs/graph_schema.py` — ORACLE_EBS_GRAPH_SCHEMA 实例 | 新建 |
| 6 | `config/settings.py` 新增 `erp_schema: str = "oracle_ebs"` | 修改 |
| 7 | `config/config.yaml` 新增 `erp_schema: oracle_ebs` | 修改 |
| 8 | 运行全量测试，确认零行为变化 | 验证 |

**验收标准**：所有现有测试通过，无行为变化。

---

### Phase 2：Repository 迁移 + SQL 下沉

| # | 任务 | 涉及文件 |
|---|------|---------|
| 9 | 将 `repository.py` 内容复制到 `schemas/oracle_ebs/repository.py`，类名改为 `OracleEBSRepository` | 新建 |
| 10 | `_receipt_anomalies_sql()` 迁移为 `OracleEBSRepository.analyze_receipt_anomalies()` | oracle_ebs/repository.py |
| 11 | `_duplicate_invoices_sql()` 迁移为 `OracleEBSRepository.detect_duplicate_invoices()` | oracle_ebs/repository.py |
| 12 | `_discount_utilization_sql()` 迁移为 `OracleEBSRepository.analyze_discount_utilization()` | oracle_ebs/repository.py |
| 13 | `_vendor_concentration_sql()` 迁移为 `OracleEBSRepository.analyze_vendor_concentration()` | oracle_ebs/repository.py |
| 14 | `_po_cycle_time_sql()` 迁移为 `OracleEBSRepository.calculate_po_cycle_time()` | oracle_ebs/repository.py |
| 15 | `advanced.py` 改为调用 `repo.analyze_xxx()`，删除 5 个 `_xxx_sql()` 和 ORM import | advanced.py |
| 16 | 原 `modules/p2p/repository.py` 改为兼容 re-export | repository.py |
| 17 | `_inject.py` 的 `_repository` 类型标注改为 `P2PRepositoryProtocol` | _inject.py |
| 18 | `schemas/oracle_ebs/__init__.py` 补全 repository_factory 注册 | oracle_ebs/__init__.py |
| 19 | 运行全量测试，确认行为一致 | 验证 |

**验收标准**：全部测试通过。`advanced.py` 不再直接 import 任何 ORM 模型。

---

### Phase 3：图侧参数化（可与 Phase 2 并行）

| # | 任务 | 涉及文件 |
|---|------|---------|
| 20 | `_inject.py` 新增 `_graph_schema` 全局变量 + setter/getter | _inject.py |
| 21 | `_resolve.py` 删除 `_BUSINESS_ID_FIELDS`，改从 `_get_graph_schema()` 读取 | _resolve.py |
| 22 | `traversal.py` 参数化 — `query_supplier_profile` 中 4 个硬编码替换 | traversal.py |
| 23 | `traversal.py` 参数化 — `trace_procurement_chain` 中 5 个硬编码替换 | traversal.py |
| 24 | `anomaly.py` 参数化 — `detect_graph_anomalies` 全部 scope 硬编码替换 | anomaly.py |
| 25 | `anomaly.py` 参数化 — `query_risk_impact` 中 1 个硬编码替换 | anomaly.py |
| 26 | `comparison.py` 参数化 — `find_contract_coverage` 中 5 个硬编码替换 | comparison.py |
| 27 | `comparison.py` 参数化 — `find_competing_suppliers` 中 4 个硬编码替换 | comparison.py |
| 28 | `analysis.py` 参数化 — `_three_way_match_graph_enhanced()` 中 4 个硬编码替换 | analysis.py |
| 29 | `advanced.py` 参数化 — `_get_vendor_relationship_density()` 中 4 个硬编码替换 | advanced.py |
| 30 | grep 验证 `tools/graph/` 和 `tools/pg/` 中不再有节点/边类型字面量 | 验证 |
| 31 | 运行全量测试，确认行为一致 | 验证 |

**验收标准**：全部测试通过。grep 确认无硬编码节点/边类型残留。

---

### Phase 4：启动流程改造 + 端到端验证

| # | 任务 | 涉及文件 |
|---|------|---------|
| 32 | `main.py` lifespan 改为通过 SchemaRegistry 创建 repository | main.py |
| 33 | `main.py` lifespan 新增 `set_graph_schema()` 注入 | main.py |
| 34 | 补充 SchemaRegistry 单元测试（注册/查找/未注册报错/available） | 新建测试文件 |
| 35 | 补充 GraphSchema 单元测试（node/edge 查找、biz_id_fields 缺省值） | 新建测试文件 |
| 36 | 以 `erp_schema=oracle_ebs` 端到端测试，确认全流程正常 | 验证 |

**验收标准**：端到端测试通过。行为与改造前完全一致。

---

### Phase 5：new_erp 占位实现

| # | 任务 | 涉及文件 |
|---|------|---------|
| 37 | 创建 `schemas/new_erp/models.py` — 初始复制 EBS 模型，改表名 | 新建 |
| 38 | 创建 `schemas/new_erp/repository.py` — 初始复制 EBS 逻辑，引用新模型 | 新建 |
| 39 | 创建 `schemas/new_erp/graph_schema.py` — 初始复制 EBS 映射，改类型名 | 新建 |
| 40 | 创建 `schemas/new_erp/__init__.py` — 注册到 SchemaRegistry | 新建 |
| 41 | `schemas/__init__.py` import 列表添加 new_erp 子包 | 修改 |
| 42 | 以 `erp_schema=new_erp` 启动，端到端测试通过 | 验证 |

**验收标准**：两套 schema 均可独立启动并通过测试。

---

### Phase 间依赖关系

```
Phase 1（抽象层）  #1-8
  │
  ├── Phase 2（Repository 迁移）  #9-19  ←─ 依赖 Phase 1 的 Protocol
  │
  └── Phase 3（图侧参数化）  #20-31  ←─ 依赖 Phase 1 的 GraphSchema
       │
       └── Phase 4（启动流程改造）  #32-36  ←─ 依赖 Phase 2 + Phase 3
            │
            └── Phase 5（new_erp 占位）  #37-42  ←─ 依赖 Phase 4
```

> Phase 2 和 Phase 3 可并行开发（无代码依赖），Phase 4 需等两者完成后合并。

---

## 八、新 Schema 扩展 Checklist

当需要接入一套全新的 ERP 数据源时，按以下步骤操作：

### 8.1 PG 侧

- [ ] 在 `modules/p2p/schemas/` 下创建新目录（如 `sap_s4/`）
- [ ] `models.py`：定义新表的 SQLAlchemy ORM 模型（表名、列名、关系）
- [ ] `repository.py`：实现 `P2PRepositoryProtocol` 的全部方法（11 个基础 + 5 个聚合）
  - [ ] 所有查询方法返回值必须符合 2.4 输出契约
  - [ ] 字段不存在时填默认值
- [ ] 单元测试：对每个 repository 方法编写测试

### 8.2 图侧

- [ ] `graph_schema.py`：创建 `GraphSchema` 实例
  - [ ] `node_types`：11 个业务概念 → 新 schema 的具体节点类型名
  - [ ] `edge_types`：13 个业务关系 → 新 schema 的具体边类型名
  - [ ] `business_id_fields`：每个节点类型的业务 ID 属性列表
- [ ] `graph_backend.py`（可选）：如果 `Neo4jStructuredBackend` 的属性映射不适用，提供自定义映射器

### 8.3 注册

- [ ] `__init__.py`：注册到 `SchemaRegistry`
- [ ] `schemas/__init__.py`：在 import 列表中添加新子包

### 8.4 配置

- [ ] `config/config.yaml` 中 `erp_schema` 改为新 schema 名称
- [ ] 或通过环境变量 `ERP_SCHEMA=sap_s4` 覆盖

### 8.5 验证

- [ ] 所有单元测试通过
- [ ] 以新 schema 配置启动，端到端测试通过
- [ ] 验证 tools 层和规则引擎无需任何修改即可工作
