# Oracle EBS 表结构梳理

## 1. 现有表与 EBS 对应关系

| 现有表 | 对应 EBS 表 | 状态 |
|--------|------------|------|
| ApSupplier | AP_SUPPLIERS | 字段需扩充 |
| PoHeader | PO_HEADERS_ALL | 字段需扩充 |
| PoLine | PO_LINES_ALL | 字段需扩充 |
| PoLineLocation | PO_LINE_LOCATIONS_ALL | 字段需扩充 |
| RcvTransaction | RCV_TRANSACTIONS | 字段需扩充 |
| ApInvoice | AP_INVOICES_ALL | 字段需扩充 |
| ApPayment | AP_CHECKS_ALL | 字段需扩充 |

需新增 13 张表，合计 20 张表覆盖 P2P 完整链路。

## 2. 采购域（4 张表）

### 2.1 PO_HEADERS_ALL（扩充现有 PoHeader）

**现有字段：**
`po_header_id`, `po_number`, `supplier_id`, `supplier_name`, `status`, `creation_date`, `total_amount`, `currency`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| type_lookup_code | VARCHAR(25) | 订单类型：STANDARD / BLANKET / CONTRACT |
| revision_num | INTEGER | 修订版本号 |
| approved_date | DATE | 审批日期 |
| authorization_status | VARCHAR(25) | 审批状态：APPROVED / IN PROCESS / REJECTED / PRE-APPROVED |
| buyer_id | INTEGER | 采购员 ID |
| org_id | INTEGER | 组织 ID |
| comments | TEXT | 备注（自由文本，LLM 抽取候选） |
| blanket_total_amount | DECIMAL(15,2) | 框架协议总金额 |
| rate_type | VARCHAR(30) | 汇率类型 |
| rate | DECIMAL(15,5) | 汇率 |
| agent_id | INTEGER | 代理人 ID |
| closed_code | VARCHAR(25) | 关闭状态：OPEN / CLOSED / FINALLY CLOSED |
| last_update_date | TIMESTAMP | 最后更新时间（增量同步水位线） |
| created_by | INTEGER | 创建人 |
| last_updated_by | INTEGER | 最后更新人 |

### 2.2 PO_LINES_ALL（扩充现有 PoLine）

**现有字段：**
`po_line_id`, `po_header_id`, `po_number`, `line_num`, `item_code`, `item_description`, `quantity`, `unit_price`, `amount`, `category`, `standard_price`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| line_type_id | INTEGER | 行类型 ID |
| unit_meas_lookup_code | VARCHAR(25) | 计量单位 |
| from_header_id | INTEGER | 引用的框架协议头 ID |
| from_line_id | INTEGER | 引用的框架协议行 ID |
| closed_code | VARCHAR(25) | 关闭状态 |
| contract_num | VARCHAR(25) | 关联合同号 |
| vendor_product_num | VARCHAR(25) | 供应商物料编号 |
| last_update_date | TIMESTAMP | 最后更新时间 |
| created_by | INTEGER | 创建人 |

**字段重命名：**
- `item_code` → `item_id`（对齐 EBS `ITEM_ID`）
- `category` → `category_id`（对齐 EBS `CATEGORY_ID`）

### 2.3 PO_LINE_LOCATIONS_ALL（扩充现有 PoLineLocation）

**现有字段：**
`line_location_id`, `po_line_id`, `po_number`, `promised_date`, `need_by_date`, `quantity`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| shipment_num | INTEGER | 发运序号 |
| ship_to_location_id | INTEGER | 收货地点 ID |
| receiving_routing_id | INTEGER | 收货路线 ID |
| qty_rcv_tolerance | DECIMAL(15,2) | 收货数量容差百分比 |
| quantity_received | DECIMAL(15,2) | 已收货数量 |
| quantity_accepted | DECIMAL(15,2) | 已验收数量 |
| quantity_rejected | DECIMAL(15,2) | 已拒收数量 |
| quantity_billed | DECIMAL(15,2) | 已开票数量 |
| quantity_cancelled | DECIMAL(15,2) | 已取消数量 |
| inspection_required_flag | VARCHAR(1) | 是否需要检验：Y/N |
| receipt_required_flag | VARCHAR(1) | 是否需要收货：Y/N |
| closed_code | VARCHAR(25) | 关闭状态 |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 2.4 PO_DISTRIBUTIONS_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| po_distribution_id | INTEGER (PK) | 分配 ID |
| po_header_id | INTEGER (FK) | 采购订单头 ID |
| po_line_id | INTEGER (FK) | 采购订单行 ID |
| line_location_id | INTEGER (FK) | 行地点 ID |
| set_of_books_id | INTEGER | 账套 ID |
| code_combination_id | INTEGER | 会计科目组合 ID |
| quantity_ordered | DECIMAL(15,2) | 订购数量 |
| quantity_delivered | DECIMAL(15,2) | 已交付数量 |
| quantity_billed | DECIMAL(15,2) | 已开票数量 |
| quantity_cancelled | DECIMAL(15,2) | 已取消数量 |
| amount_billed | DECIMAL(15,2) | 已开票金额 |
| gl_encumbered_date | DATE | 总账承诺日期 |
| budget_account_id | INTEGER | 预算科目 ID |
| destination_type_code | VARCHAR(25) | 目标类型：EXPENSE / INVENTORY / SHOP FLOOR |
| org_id | INTEGER | 组织 ID |
| last_update_date | TIMESTAMP | 最后更新时间 |

## 3. 应付域（6 张表）

### 3.1 AP_INVOICES_ALL（扩充现有 ApInvoice）

**现有字段：**
`invoice_id`, `invoice_number`, `po_number`, `supplier_id`, `supplier_name`, `invoice_amount`, `invoice_date`, `due_date`, `discount_due_date`, `status`, `payment_terms`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| invoice_type_lookup_code | VARCHAR(25) | 发票类型：STANDARD / CREDIT / PREPAYMENT |
| invoice_currency_code | VARCHAR(15) | 发票币种 |
| exchange_rate | DECIMAL(15,5) | 汇率 |
| amount_paid | DECIMAL(15,2) | 已付金额 |
| discount_amount_taken | DECIMAL(15,2) | 已使用折扣金额 |
| base_amount | DECIMAL(15,2) | 本位币金额 |
| pay_group_lookup_code | VARCHAR(25) | 付款组 |
| source | VARCHAR(80) | 发票来源 |
| org_id | INTEGER | 组织 ID |
| cancelled_date | DATE | 取消日期 |
| cancelled_amount | DECIMAL(15,2) | 取消金额 |
| gl_date | DATE | 总账日期 |
| goods_received_date | DATE | 收货日期 |
| invoice_received_date | DATE | 发票收到日期 |
| description | TEXT | 描述（自由文本，LLM 抽取候选） |
| last_update_date | TIMESTAMP | 最后更新时间 |
| created_by | INTEGER | 创建人 |

**字段重命名：**
- `invoice_number` → `invoice_num`（对齐 EBS）
- `supplier_id` → `vendor_id`
- `status` → `approval_status`
- `payment_terms` → `terms_id`

### 3.2 AP_INVOICE_LINES_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| invoice_line_id | INTEGER (PK) | 发票行 ID |
| invoice_id | INTEGER (FK) | 发票头 ID |
| line_number | INTEGER | 行号 |
| line_type_lookup_code | VARCHAR(25) | 行类型：ITEM / TAX / FREIGHT / MISCELLANEOUS |
| amount | DECIMAL(15,2) | 金额 |
| quantity_invoiced | DECIMAL(15,2) | 开票数量 |
| unit_price | DECIMAL(15,2) | 单价 |
| po_header_id | INTEGER | 关联 PO 头 ID |
| po_line_id | INTEGER | 关联 PO 行 ID |
| po_line_location_id | INTEGER | 关联 PO 行地点 ID |
| po_distribution_id | INTEGER | 关联 PO 分配 ID |
| inventory_item_id | INTEGER | 物料 ID |
| item_description | VARCHAR(240) | 物料描述 |
| accounting_date | DATE | 会计日期 |
| description | TEXT | 描述 |
| discarded_flag | VARCHAR(1) | 是否废弃：Y/N |
| cancelled_flag | VARCHAR(1) | 是否取消：Y/N |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 3.3 AP_INVOICE_DISTRIBUTIONS_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| invoice_distribution_id | INTEGER (PK) | 发票分配 ID |
| invoice_id | INTEGER (FK) | 发票头 ID |
| invoice_line_number | INTEGER | 发票行号 |
| distribution_line_number | INTEGER | 分配行号 |
| line_type_lookup_code | VARCHAR(25) | 行类型 |
| amount | DECIMAL(15,2) | 金额 |
| base_amount | DECIMAL(15,2) | 本位币金额 |
| quantity_invoiced | DECIMAL(15,2) | 开票数量 |
| po_distribution_id | INTEGER | 关联 PO 分配 ID |
| set_of_books_id | INTEGER | 账套 ID |
| accounting_date | DATE | 会计日期 |
| dist_code_combination_id | INTEGER | 会计科目组合 ID |
| match_status_flag | VARCHAR(1) | 匹配状态 |
| posted_flag | VARCHAR(1) | 是否已过账：Y/N |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 3.4 AP_CHECKS_ALL（扩充现有 ApPayment）

**现有字段：**
`payment_id`, `payment_number`, `invoice_number`, `supplier_id`, `payment_amount`, `payment_date`, `payment_method`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| bank_account_id | INTEGER | 银行账户 ID |
| currency_code | VARCHAR(15) | 币种 |
| exchange_rate | DECIMAL(15,5) | 汇率 |
| status_lookup_code | VARCHAR(25) | 状态：NEGOTIABLE / VOIDED / CLEARED / RECONCILED |
| cleared_date | DATE | 清算日期 |
| cleared_amount | DECIMAL(15,2) | 清算金额 |
| void_date | DATE | 作废日期 |
| org_id | INTEGER | 组织 ID |
| last_update_date | TIMESTAMP | 最后更新时间 |
| created_by | INTEGER | 创建人 |

**字段重命名：**
- `payment_id` → `check_id`
- `payment_number` → `check_number`
- `supplier_id` → `vendor_id`
- `payment_amount` → `amount`
- `payment_date` → `check_date`
- `payment_method` → `payment_method_code`

### 3.5 AP_INVOICE_PAYMENTS_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| invoice_payment_id | INTEGER (PK) | 发票付款 ID |
| invoice_id | INTEGER (FK) | 发票 ID |
| check_id | INTEGER (FK) | 付款 ID |
| payment_num | INTEGER | 付款序号 |
| amount | DECIMAL(15,2) | 付款金额 |
| discount_taken | DECIMAL(15,2) | 已使用折扣 |
| discount_lost | DECIMAL(15,2) | 已失去折扣 |
| invoice_base_amount | DECIMAL(15,2) | 发票本位币金额 |
| payment_base_amount | DECIMAL(15,2) | 付款本位币金额 |
| accounting_date | DATE | 会计日期 |
| reversal_flag | VARCHAR(1) | 是否冲销：Y/N |
| reversal_inv_pmt_id | INTEGER | 冲销关联 ID |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 3.6 AP_PAYMENT_SCHEDULES_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| payment_schedule_id | INTEGER (PK) | 付款计划 ID |
| invoice_id | INTEGER (FK) | 发票 ID |
| payment_num | INTEGER | 付款期数 |
| due_date | DATE | 到期日 |
| discount_date | DATE | 折扣截止日 |
| gross_amount | DECIMAL(15,2) | 总金额 |
| amount_remaining | DECIMAL(15,2) | 剩余金额 |
| discount_amount_available | DECIMAL(15,2) | 可用折扣金额 |
| payment_priority | INTEGER | 付款优先级 |
| hold_flag | VARCHAR(1) | 是否暂挂：Y/N |
| payment_status_flag | VARCHAR(1) | 付款状态：Y(已付) / N(未付) / P(部分付) |
| last_update_date | TIMESTAMP | 最后更新时间 |

## 4. 收货域（3 张表）

### 4.1 RCV_SHIPMENT_HEADERS（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| shipment_header_id | INTEGER (PK) | 收货单头 ID |
| receipt_num | VARCHAR(30) | 收货单号 |
| vendor_id | INTEGER (FK) | 供应商 ID |
| vendor_site_id | INTEGER | 供应商地点 ID |
| ship_to_org_id | INTEGER | 收货组织 ID |
| shipped_date | DATE | 发货日期 |
| expected_receipt_date | DATE | 预计到货日期 |
| receipt_source_code | VARCHAR(25) | 来源类型：VENDOR / INVENTORY |
| shipment_num | VARCHAR(30) | 发运单号 |
| waybill_airbill_num | VARCHAR(20) | 运单号 |
| freight_carrier_code | VARCHAR(25) | 承运商代码 |
| packing_slip | VARCHAR(25) | 装箱单号 |
| comments | TEXT | 备注（自由文本，LLM 抽取候选） |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 4.2 RCV_SHIPMENT_LINES（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| shipment_line_id | INTEGER (PK) | 收货单行 ID |
| shipment_header_id | INTEGER (FK) | 收货单头 ID |
| line_num | INTEGER | 行号 |
| po_header_id | INTEGER | 关联 PO 头 ID |
| po_line_id | INTEGER | 关联 PO 行 ID |
| po_line_location_id | INTEGER | 关联 PO 行地点 ID |
| item_id | INTEGER | 物料 ID |
| item_description | VARCHAR(240) | 物料描述 |
| quantity_shipped | DECIMAL(15,2) | 发货数量 |
| quantity_received | DECIMAL(15,2) | 收货数量 |
| unit_of_measure | VARCHAR(25) | 计量单位 |
| category_id | INTEGER | 类别 ID |
| vendor_item_num | VARCHAR(25) | 供应商物料编号 |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 4.3 RCV_TRANSACTIONS（扩充现有 RcvTransaction）

**现有字段：**
`transaction_id`, `shipment_header_id`, `po_number`, `po_line_id`, `transaction_type`, `quantity`, `accepted_quantity`, `rejected_quantity`, `transaction_date`, `supplier_id`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| po_header_id | INTEGER | 关联 PO 头 ID |
| po_line_location_id | INTEGER | 关联 PO 行地点 ID |
| shipment_line_id | INTEGER (FK) | 收货单行 ID |
| source_document_code | VARCHAR(25) | 来源单据类型：PO / RMA |
| destination_type_code | VARCHAR(25) | 目标类型：RECEIVING / INVENTORY / EXPENSE |
| unit_of_measure | VARCHAR(25) | 计量单位 |
| primary_quantity | DECIMAL(15,2) | 主单位数量 |
| inspection_status_code | VARCHAR(25) | 检验状态：NOT INSPECTED / ACCEPTED / REJECTED |
| parent_transaction_id | INTEGER | 父事务 ID（退货/更正场景） |
| organization_id | INTEGER | 组织 ID |
| last_update_date | TIMESTAMP | 最后更新时间 |

## 5. 主数据域（3 张表）

### 5.1 AP_SUPPLIERS（扩充现有 ApSupplier）

**现有字段：**
`supplier_id`, `supplier_name`, `supplier_site_id`, `payment_terms`, `status`

**需新增的 EBS 字段：**

| 字段名 | 类型 | 说明 |
|-------|------|------|
| segment1 | VARCHAR(30) | 供应商编号 |
| vendor_type_lookup_code | VARCHAR(25) | 供应商类型 |
| start_date_active | DATE | 生效日期 |
| end_date_active | DATE | 失效日期 |
| num_1099 | VARCHAR(30) | 税务编号 |
| tax_reporting_name | VARCHAR(80) | 税务报告名称 |
| standard_industry_class | VARCHAR(25) | 行业分类 |
| minority_group_lookup_code | VARCHAR(25) | 少数群体分类 |
| women_owned_flag | VARCHAR(1) | 女性持有企业：Y/N |
| small_business_flag | VARCHAR(1) | 小型企业：Y/N |
| created_by | INTEGER | 创建人 |
| last_update_date | TIMESTAMP | 最后更新时间 |

**字段重命名：**
- `supplier_id` → `vendor_id`（对齐 EBS）
- `supplier_name` → `vendor_name`
- `status` → `enabled_flag`
- `payment_terms` → `terms_id`

### 5.2 AP_SUPPLIER_SITES_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| vendor_site_id | INTEGER (PK) | 供应商地点 ID |
| vendor_id | INTEGER (FK) | 供应商 ID |
| vendor_site_code | VARCHAR(15) | 地点编码 |
| address_line1 | VARCHAR(240) | 地址行 1 |
| address_line2 | VARCHAR(240) | 地址行 2 |
| address_line3 | VARCHAR(240) | 地址行 3 |
| address_line4 | VARCHAR(240) | 地址行 4 |
| city | VARCHAR(60) | 城市 |
| state | VARCHAR(60) | 省/州 |
| zip | VARCHAR(20) | 邮编 |
| country | VARCHAR(60) | 国家 |
| phone | VARCHAR(15) | 电话 |
| fax | VARCHAR(15) | 传真 |
| email_address | VARCHAR(2000) | 邮箱 |
| terms_id | INTEGER | 付款条款 ID |
| pay_group_lookup_code | VARCHAR(25) | 付款组 |
| payment_method_lookup_code | VARCHAR(25) | 付款方式 |
| org_id | INTEGER | 组织 ID |
| purchasing_site_flag | VARCHAR(1) | 是否采购地点：Y/N |
| pay_site_flag | VARCHAR(1) | 是否付款地点：Y/N |
| inactive_date | DATE | 停用日期 |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 5.3 MTL_SYSTEM_ITEMS_B（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| inventory_item_id | INTEGER (PK) | 物料 ID |
| organization_id | INTEGER (PK) | 组织 ID（联合主键） |
| segment1 | VARCHAR(40) | 物料编号 |
| description | VARCHAR(240) | 物料描述 |
| primary_uom_code | VARCHAR(3) | 主计量单位 |
| item_type | VARCHAR(30) | 物料类型 |
| buyer_id | INTEGER | 默认采购员 ID |
| list_price_per_unit | DECIMAL(15,2) | 目录单价 |
| purchasing_item_flag | VARCHAR(1) | 是否采购物料：Y/N |
| purchasing_enabled_flag | VARCHAR(1) | 是否启用采购：Y/N |
| inventory_item_status_code | VARCHAR(10) | 物料状态 |
| item_catalog_group_id | INTEGER | 目录分组 ID |
| last_update_date | TIMESTAMP | 最后更新时间 |

## 6. 寻源/合同域（4 张表）

### 6.1 PON_AUCTION_HEADERS_ALL（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| auction_header_id | INTEGER (PK) | 寻源事件 ID |
| document_number | VARCHAR(25) | 文档编号 |
| auction_title | VARCHAR(80) | 事件标题 |
| auction_type | VARCHAR(25) | 类型：SEALED_BID / REVERSE_AUCTION / RFQ / RFI |
| auction_status | VARCHAR(25) | 状态 |
| open_bidding_date | TIMESTAMP | 开标时间 |
| close_bidding_date | TIMESTAMP | 截标时间 |
| outcome | VARCHAR(25) | 结果 |
| contract_type | VARCHAR(25) | 合同类型 |
| org_id | INTEGER | 组织 ID |
| created_by | INTEGER | 创建人 |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 6.2 PON_BID_HEADERS（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| bid_number | INTEGER (PK) | 投标编号 |
| auction_header_id | INTEGER (FK) | 寻源事件 ID |
| bid_status | VARCHAR(25) | 投标状态 |
| vendor_id | INTEGER (FK) | 供应商 ID |
| vendor_site_id | INTEGER | 供应商地点 ID |
| bid_total | DECIMAL(15,2) | 投标总金额 |
| bid_currency_code | VARCHAR(15) | 投标币种 |
| publish_date | TIMESTAMP | 发布日期 |
| award_status | VARCHAR(25) | 授标状态 |
| award_date | DATE | 授标日期 |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 6.3 OKC_K_HEADERS_B（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER (PK) | 合同头 ID |
| contract_number | VARCHAR(120) | 合同编号 |
| contract_number_modifier | VARCHAR(120) | 合同编号修饰符 |
| sts_code | VARCHAR(30) | 状态代码 |
| start_date | DATE | 生效日期 |
| end_date | DATE | 失效日期 |
| estimated_amount | DECIMAL(15,2) | 预估金额 |
| currency_code | VARCHAR(15) | 币种 |
| authoring_org_id | INTEGER | 编制组织 ID |
| buy_or_sell | VARCHAR(3) | 采购/销售：B / S |
| scs_code | VARCHAR(30) | 子类别代码 |
| description | TEXT | 描述（自由文本，LLM 抽取候选） |
| short_description | VARCHAR(600) | 简述（自由文本，LLM 抽取候选） |
| last_update_date | TIMESTAMP | 最后更新时间 |

### 6.4 OKC_K_LINES_B（新增）

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER (PK) | 合同行 ID |
| chr_id | INTEGER (FK) | 合同头 ID |
| line_number | VARCHAR(150) | 行号 |
| sts_code | VARCHAR(30) | 状态代码 |
| start_date | DATE | 生效日期 |
| end_date | DATE | 失效日期 |
| item_id | INTEGER | 物料 ID |
| item_description | VARCHAR(240) | 物料描述 |
| price_unit | DECIMAL(15,2) | 合同单价 |
| price_negotiated | DECIMAL(15,2) | 协商价格 |
| quantity | DECIMAL(15,2) | 数量 |
| uom_code | VARCHAR(25) | 计量单位 |
| last_update_date | TIMESTAMP | 最后更新时间 |
