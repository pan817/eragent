# Graphiti 节点/边模型设计

## 1. 节点类型（Entity Nodes）

共 15 种节点类型，按域分组：

### 主数据域

| 节点类型 | 来源表 | 唯一标识 | 说明 |
|---------|-------|---------|------|
| Supplier | AP_SUPPLIERS | vendor_id | 供应商主数据 |
| SupplierSite | AP_SUPPLIER_SITES_ALL | vendor_site_id | 供应商地点（采购/付款地点） |
| Material | MTL_SYSTEM_ITEMS_B | inventory_item_id | 物料主数据 |

### 采购域

| 节点类型 | 来源表 | 唯一标识 | 说明 |
|---------|-------|---------|------|
| PurchaseOrder | PO_HEADERS_ALL | po_header_id | 采购订单（含 STANDARD/BLANKET/CONTRACT 类型） |
| POLine | PO_LINES_ALL | po_line_id | 采购订单行项 |

### 收货域

| 节点类型 | 来源表 | 唯一标识 | 说明 |
|---------|-------|---------|------|
| Shipment | RCV_SHIPMENT_HEADERS | shipment_header_id | 收货单 |
| Receipt | RCV_TRANSACTIONS | transaction_id | 收货事务（实际收货/退货/更正） |

### 应付域

| 节点类型 | 来源表 | 唯一标识 | 说明 |
|---------|-------|---------|------|
| Invoice | AP_INVOICES_ALL | invoice_id | 发票 |
| InvoiceLine | AP_INVOICE_LINES_ALL | invoice_line_id | 发票行 |
| Payment | AP_CHECKS_ALL | check_id | 付款 |
| PaymentSchedule | AP_PAYMENT_SCHEDULES_ALL | payment_schedule_id | 付款计划 |

### 寻源/合同域

| 节点类型 | 来源表 | 唯一标识 | 说明 |
|---------|-------|---------|------|
| Auction | PON_AUCTION_HEADERS_ALL | auction_header_id | 寻源事件（询价/招标） |
| Bid | PON_BID_HEADERS | bid_number | 投标 |
| Contract | OKC_K_HEADERS_B | id | 合同 |
| ContractLine | OKC_K_LINES_B | id | 合同行 |

## 2. 关系类型（Edges）

共 19 种关系，按业务语义分组：

### 供应商关系

| 关系类型 | 起始节点 | 目标节点 | 说明 |
|---------|---------|---------|------|
| HAS_SITE | Supplier | SupplierSite | 供应商拥有地点 |
| CREATES_PO | Supplier | PurchaseOrder | 供应商关联的采购订单 |
| SUBMITS_INVOICE | Supplier | Invoice | 供应商提交发票 |
| BIDS_ON | Supplier | Auction | 供应商参与寻源 |
| BOUND_BY_CONTRACT | Supplier | Contract | 供应商关联合同 |

### 采购订单关系

| 关系类型 | 起始节点 | 目标节点 | 说明 |
|---------|---------|---------|------|
| CONTAINS_LINE | PurchaseOrder | POLine | PO 包含行项 |
| ORDERS_MATERIAL | POLine | Material | 订单行采购物料 |
| REFERENCES_BLANKET | PurchaseOrder | PurchaseOrder | 标准 PO 引用框架协议 |

### 收货关系

| 关系类型 | 起始节点 | 目标节点 | 说明 |
|---------|---------|---------|------|
| SHIPS_TO | Shipment | SupplierSite | 从供应商地点发货 |
| BELONGS_TO_SHIPMENT | Receipt | Shipment | 收货事务属于收货单 |
| RECEIVES_LINE | Receipt | POLine | 收货对应 PO 行 |

### 应付关系

| 关系类型 | 起始节点 | 目标节点 | 说明 |
|---------|---------|---------|------|
| BELONGS_TO_INVOICE | InvoiceLine | Invoice | 发票行属于发票 |
| INVOICES_LINE | InvoiceLine | POLine | 发票行匹配 PO 行 |
| PAYS_INVOICE | Payment | Invoice | 付款支付发票（通过 AP_INVOICE_PAYMENTS_ALL） |
| SCHEDULED_FOR | PaymentSchedule | Invoice | 付款计划关联发票 |

### 寻源/合同关系

| 关系类型 | 起始节点 | 目标节点 | 说明 |
|---------|---------|---------|------|
| HAS_BID | Auction | Bid | 寻源事件包含投标 |
| AWARDS_TO | Auction | Supplier | 寻源授标给供应商 |
| CONTAINS_CONTRACT_LINE | Contract | ContractLine | 合同包含行 |
| CONTRACT_COVERS | ContractLine | Material | 合同行覆盖物料 |

## 3. 时序属性设计

Graphiti 的核心优势是时序感知。每个节点和边都携带时序属性。

### 节点时序属性

所有节点统一携带以下时序字段：

| 属性 | 类型 | 说明 |
|-----|------|------|
| valid_from | datetime | 业务生效时间（如 PO 的 creation_date，合同的 start_date） |
| valid_to | datetime \| None | 业务失效时间（如合同 end_date，供应商 end_date_active；无失效则 None） |
| last_updated | datetime | 最后修改时间（对应 EBS 的 LAST_UPDATE_DATE） |
| status | str | 当前业务状态（随时间变化的关键属性） |

**各节点类型的 valid_from / valid_to 映射：**

| 节点类型 | valid_from 来源 | valid_to 来源 |
|---------|---------------|--------------|
| Supplier | start_date_active | end_date_active |
| SupplierSite | (创建时间) | inactive_date |
| Material | (创建时间) | None |
| PurchaseOrder | creation_date | None |
| POLine | (继承 PO 的 creation_date) | None |
| Shipment | shipped_date | None |
| Receipt | transaction_date | None |
| Invoice | invoice_date | cancelled_date |
| InvoiceLine | accounting_date | None |
| Payment | check_date | void_date |
| PaymentSchedule | (创建时间) | due_date |
| Auction | open_bidding_date | close_bidding_date |
| Bid | publish_date | None |
| Contract | start_date | end_date |
| ContractLine | start_date | end_date |

### 边时序属性

| 属性 | 类型 | 说明 |
|-----|------|------|
| created_at | datetime | 关系建立时间 |
| valid_from | datetime \| None | 关系生效时间（如合同有效期内的供应关系） |
| valid_to | datetime \| None | 关系失效时间 |

### 时序查询场景

1. **供应商历史表现**："供应商 A 在 2025 年 Q3 的交付表现" → 按时间窗口过滤 Receipt 节点
2. **价格变化追踪**："这个物料的合同价格变化历史" → ContractLine 节点的 price_unit 随时间变化
3. **端到端时间线**："PO-2024-0001 从创建到付款的完整时间线" → PO → Receipt → Invoice → Payment 时间链路
4. **合同有效性**："当前有效的框架协议有哪些" → 按 valid_from/valid_to 过滤 Contract 节点
5. **供应商关系演变**："供应商 A 近两年参与了哪些寻源事件" → Auction + Bid 节点按时间排序
