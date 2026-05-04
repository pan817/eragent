# DAG 模板扩展分析

> **专题目标**：评估"扩充更多常见 ERP P2P 分析模板"的合理性、候选清单与实施梯度。
>
> **本文档性质**：分析与设计建议，**不包含代码**。
> **关联文档**：[intent_routing_hit_rate_optimization.md](intent_routing_hit_rate_optimization.md)（路由命中率主方案，本方案为其方向一的延伸分析）

---

## 元信息

| 项 | 值 |
|---|---|
| 创建日期 | 2026-04-16 |
| 当前版本 | v1.0 (初稿) |
| 范围 | 仅 [core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py) 的模板扩充 |
| 不在范围 | 模板编排引擎本身、tools 实现、报告 prompt、L2 种子库 |
| 前置依赖 | 主方案批 1（监控就位）建议先上线，便于评估各模板的实际流量 |

---

## 目录

1. [当前 DAG 模板覆盖现状](#1-当前-dag-模板覆盖现状)
2. [当前 P2P tools 原子能力清单](#2-当前-p2p-tools-原子能力清单)
3. [业界 P2P 标准模块对比](#3-业界-p2p-标准模块对比)
4. [候选新增模板清单](#4-候选新增模板清单)
5. [候选模板三维评分](#5-候选模板三维评分)
6. [模板膨胀风险分析](#6-模板膨胀风险分析)
7. [推荐结论与执行梯度](#7-推荐结论与执行梯度)

---

## 1. 当前 DAG 模板覆盖现状

### 1.1 模板总览

[core/orchestrator/dag/templates.py](../core/orchestrator/dag/templates.py) 当前定义 **14 个 DAG 模板**：

| # | 模板名 | 类别 | 触发条件 | 任务节点数 | 核心 tools |
|---|---|---|---|---|---|
| 1 | `_THREE_WAY_MATCH_DAG` | AnalysisType | THREE_WAY_MATCH | 5 | query_purchase_orders, query_receipts, query_invoices, run_three_way_match, report_agent |
| 2 | `_PRICE_VARIANCE_DAG` | AnalysisType | PRICE_VARIANCE | 3 | query_purchase_orders, run_price_variance_analysis, report_agent |
| 3 | `_PAYMENT_COMPLIANCE_DAG` | AnalysisType | PAYMENT_COMPLIANCE | 4 | query_invoices, query_payments, run_payment_compliance_check, report_agent |
| 4 | `_SUPPLIER_PERFORMANCE_DAG` | AnalysisType | SUPPLIER_PERFORMANCE | 4 | query_vendor_master, query_purchase_orders, calculate_supplier_kpis, report_agent |
| 5 | `_SPEND_ANALYSIS_DAG` | AnalysisType | SPEND_ANALYSIS | 3 | query_purchase_orders, calculate_spend_analysis, report_agent |
| 6 | `_RECEIPT_ANOMALY_DAG` | AnalysisType | RECEIPT_ANOMALY | 4 | query_purchase_orders, query_receipts, analyze_receipt_anomalies, report_agent |
| 7 | `_INVOICE_DUPLICATE_DAG` | AnalysisType | INVOICE_DUPLICATE | 3 | query_invoices, detect_duplicate_invoices, report_agent |
| 8 | `_DISCOUNT_UTILIZATION_DAG` | AnalysisType | DISCOUNT_UTILIZATION | 4 | query_invoices, query_payments, analyze_discount_utilization, report_agent |
| 9 | `_PO_CYCLE_TIME_DAG` | AnalysisType | PO_CYCLE_TIME | 3 | query_purchase_orders, calculate_po_cycle_time, report_agent |
| 10 | `_VENDOR_CONCENTRATION_DAG` | AnalysisType | VENDOR_CONCENTRATION | 3 | query_purchase_orders, analyze_vendor_concentration, report_agent |
| 11 | `_PO_RISK_DAG` | 实体维度 | COMPREHENSIVE + po_number | ~6 | 多 rule 综合 + report_agent |
| 12 | `_SUPPLIER_RISK_DAG` | 实体维度 | COMPREHENSIVE + supplier_id | ~6 | KPI + 黑名单 + 集中度 + report_agent |
| 13 | `_PAYMENT_SINGLE_DAG` | 实体维度 | COMPREHENSIVE + payment_number | ~5 | 单笔付款合规 |
| 14 | `_INVOICE_SINGLE_DAG` | 实体维度 | COMPREHENSIVE + invoice_number | ~5 | 单笔发票分析 |

派发逻辑见 [load_dag_template](../core/orchestrator/dag/templates.py)（739-790 行），优先级：**实体维度模板 > 分析类型模板 > None（降级 ReAct）**。

### 1.2 模板结构通用形态

每个 DAG 任务节点结构（5 字段：`task_id` / `name` / `agent` / `tool_name` / `depends_on` / `inputs` / `output_key` / `timeout_sec`）。模板内任务编排基本是固定三段式：

```
[采集层]   并行调几个 query_* tools 拿原始数据
   ↓
[分析层]   调一个 rule_* / calculate_* / analyze_* tool 跑分析逻辑
   ↓
[报告层]   调 report_agent 渲染 Markdown 报告
```

参数化用占位符 `{days}` / `{supplier_id}` / `{po_number}` / `{invoice_number}` / `{payment_number}` / `{receipt_number}`，由 `_replace_params()` 在 `load_dag_template` 时填充。

### 1.3 覆盖盲区一览

| 流量来源 | 当前是否有模板 | 落点 |
|---|---|---|
| 10 类 AnalysisType（直接命中）| ✅ 全覆盖 | 对应 `_*_DAG` |
| COMPREHENSIVE + 单笔付款单 | ✅ | `_PAYMENT_SINGLE_DAG` |
| COMPREHENSIVE + 单张发票 | ✅ | `_INVOICE_SINGLE_DAG` |
| COMPREHENSIVE + 单 PO | ✅ | `_PO_RISK_DAG` |
| COMPREHENSIVE + 单供应商 | ✅ | `_SUPPLIER_RISK_DAG` |
| COMPREHENSIVE + 单收货单（receipt_number）| ❌ | ReAct 兜底 |
| **COMPREHENSIVE 无实体（"看看最近采购"）** | ❌ | ReAct 兜底（主方案批 5 拟新增 `RECENT_PROCUREMENT_HEALTH`）|
| **DATA_LOOKUP（"查最新 PO"/"列出发票"）**| ❌ | ReAct 兜底（本文档方向二讨论） |
| 跨 AnalysisType 组合（如三路匹配 + 价差联动）| ❌ | ReAct 或 COMPREHENSIVE 兜底 |
| P2P 流程外的常见诉求（GR/IR 暂记差异、采购申请、退货、寄售对账等）| ❌ | ReAct 兜底（本文档方向一讨论） |

### 1.4 关键观察

- **模板偏"分析"重，缺"事实查询"轻**——所有现有模板都以 `report_agent` 收尾，结构假设是"分析 → 报告"，没有"快速 lookup"形态
- **实体维度模板已覆盖 4/5 主单据**（缺 receipt_number 单据）
- **跨场景组合靠 LLM 兜底**——没有"复合模板"概念
- **业务流程上游缺失**——采购申请（PR）、合同（Contract）、寄售（Consignment）等模块完全不在覆盖内

---

## 2. 当前 P2P tools 原子能力清单

[modules/p2p/tools.py](../modules/p2p/tools.py) 当前 **19 个 `@tool`**，按能力性质分四大类：

### 2.1 查询类（query_*）— 5 个

| Tool | 入参 | 功能 | 是否 stub |
|---|---|---|---|
| `query_purchase_orders` | supplier_id, status, po_number, days | 查 PO（按时间窗口/供应商/状态筛选）| 否 |
| `query_receipts` | po_number, supplier_id, days | 查收货单 | 否 |
| `query_invoices` | po_number, supplier_id, status, invoice_number, days | 查发票 | 否 |
| `query_payments` | invoice_number, supplier_id, payment_number, days | 查付款单 | 否 |
| `query_vendor_master` | vendor_ids | 查供应商主数据 | 否 |
| `query_material_master` | material_ids | 查物料主数据 | **是（Phase 4 实现）** |

### 2.2 规则/合规类（run_*）— 3 个

| Tool | 入参 | 功能 |
|---|---|---|
| `run_three_way_match` | po_number | 三路匹配（PO/GR/INV）|
| `run_price_variance_analysis` | supplier_id, days | 价差分析（实际 vs 合同/标准）|
| `run_payment_compliance_check` | supplier_id, days | 付款合规（逾期/早付/折扣滥用）|
| `run_vendor_risk_scoring` | vendor_ids | 供应商风险评分 | **是（Phase 4 stub）** |

### 2.3 计算/聚合类（calculate_* / analyze_*）— 7 个

| Tool | 入参 | 功能 |
|---|---|---|
| `calculate_supplier_kpis` | supplier_id, period | 供应商 KPI（OTIF/质量/价格合规）|
| `calculate_spend_analysis` | group_by, days | 支出分析（按品类/供应商）|
| `calculate_po_cycle_time` | days, supplier_id | PO 周期分析 |
| `analyze_receipt_anomalies` | supplier_id, po_number, days | 收货异常 |
| `detect_duplicate_invoices` | supplier_id, days | 重复发票检测 |
| `analyze_discount_utilization` | supplier_id, days | 折扣利用率 |
| `analyze_vendor_concentration` | days, top_n | 供应商集中度 |

### 2.4 校验/守卫类（check_*）— 2 个 stub

| Tool | 入参 | 功能 | 状态 |
|---|---|---|---|
| `check_approval_limits` | po_ids | 审批限额合规 | **stub** |
| `check_blacklist` | vendor_ids | 黑名单校验 | **stub** |

### 2.5 关键观察

- **查询类原子能力齐全**——5 个 query_* 已经覆盖 PO/GR/INV/PAY/Vendor 五大主单据；这是方向二（DATA_LOOKUP 模板化）的天然基础
- **统一返回 JSON 字符串**——所有 tools 返回 `str`（JSON-serialized），便于 LangChain 串联
- **入参以筛选维度为主**（supplier_id / po_number / days），无排序/分页参数——批 4 的查询模板若需"最新 N 条"形态，需要 tool 层面增强
- **3 个 stub 是已知技术债**：`query_material_master` / `run_vendor_risk_scoring` / `check_approval_limits` / `check_blacklist`——任何依赖它们的新模板都要先把 stub 实现
- **缺失的能力**（影响新模板可行性）：
  - **采购申请（PR）查询**——P2R 流程上游
  - **合同/价格主数据查询**——价差分析的"合同价"目前从哪来？看 `run_price_variance_analysis` 内部
  - **GR/IR 暂记账查询**——FI 侧应付暂记
  - **退货单查询**——已有的 `query_receipts` 是否覆盖？需确认
  - **采购变更单查询**——PO 修订追踪
  - **AP 应付明细**（合并 invoice + payment 视角）

---

## 3. 业界 P2P 标准模块对比

参考 SAP MM/FI、Oracle EBS（iProcurement + Payables）、Coupa、Workday Procurement 等主流 ERP/SaaS 在 P2P 领域的常见模块覆盖。下表只列**与"分析"或"事实查询"相关**的模块（流程发起类如审批工作流不在比较范围）。

### 3.1 单据/主数据维度对比

| 业务对象 | SAP MM/FI | Oracle EBS | Coupa | **eragent 当前** |
|---|---|---|---|---|
| 采购申请 PR | EBAN/EBKN | po_requisitions | Requests | ❌ 无 query_pr |
| 询报价 RFQ | EKKO type A | RFQ | Sourcing Events | ❌ 无 |
| 合同 Contract | EKKO type K | po_headers type='BLANKET' | Contracts | ❌ 无 query_contract |
| 采购订单 PO | EKKO/EKPO | po_headers/po_lines | Purchase Orders | ✅ `query_purchase_orders` |
| 收货单 GR | MSEG/MKPF | rcv_transactions | Receipts | ✅ `query_receipts` |
| 发票 INV | RBKP/RSEG | ap_invoices | Invoices | ✅ `query_invoices` |
| 付款 PAY | BSEG payment | ap_checks/ap_payments | Payments | ✅ `query_payments` |
| 退货 RTV | MSEG type 122/161 | rcv_transactions type RETURN | Returns | ⚠️ 可能在 query_receipts 内（需确认）|
| 寄售库存 | MKPF type WE consignment | po_distributions consigned | Consignment | ❌ 无 |
| GR/IR 暂记 | BSIS/BSEG GR/IR clearing | ap_invoice_distributions accrual | GR/IR Accruals | ❌ 无 |
| 采购变更单 | EKKO change docs | po_change_history | PO Changes | ❌ 无 query_po_changes |
| 供应商主数据 | LFA1/LFB1 | po_vendors | Suppliers | ✅ `query_vendor_master` |
| 物料主数据 | MARA/MBEW | mtl_system_items | Items | ⚠️ stub `query_material_master` |
| 价格条件 / 合同价 | A018/KONP | mtl_categories pricing | Catalogs | ❌ 无独立 query_price_master（隐含在 PV 分析内）|

### 3.2 分析场景对比

| 分析场景 | SAP/Oracle 标准报表 | eragent 当前 |
|---|---|---|
| 三路匹配异常 | MR8M / MIRO 异常报表 | ✅ THREE_WAY_MATCH |
| 价格差异（PPV）| ME1P / S_ALR_87013344 | ✅ PRICE_VARIANCE |
| 付款合规/账期 | F.41 / FBL1N aging | ✅ PAYMENT_COMPLIANCE |
| 供应商绩效 | ME6H / Vendor Evaluation | ✅ SUPPLIER_PERFORMANCE |
| 支出分析 | Spend Analytics dashboard | ✅ SPEND_ANALYSIS |
| 收货异常 | MB51 quality + over-receipt | ✅ RECEIPT_ANOMALY |
| 重复发票 | F.13 / SAP duplicate check | ✅ INVOICE_DUPLICATE |
| 早付折扣利用 | F-58 cash discount report | ✅ DISCOUNT_UTILIZATION |
| 采购周期 | ME80FN cycle | ✅ PO_CYCLE_TIME |
| 供应商集中度 | Spend by vendor | ✅ VENDOR_CONCENTRATION |
| **GR/IR 暂记差异** | MB5S / GR/IR clearing | ❌ |
| **采购申请审批分析** | PR approval cycle | ❌ |
| **合同执行率（vs PO）**| Contract leakage | ❌ |
| **退货 / RTV 分析** | MR11 returns | ❌（流程上属 RECEIPT_ANOMALY 但语义独立）|
| **PO 变更分析（修订频率/金额漂移）**| PO change history report | ❌ |
| **供应商响应时效（询报价）**| RFQ response time | ❌ |
| **应付账款老化（AP Aging）**| FBL1N classic aging buckets | ⚠️ PAYMENT_COMPLIANCE 内含但形态不同 |
| **现金流预测**（按 due date）| F.78 cash forecast | ❌ |
| **采购品类合规率**（合同覆盖率）| Maverick spend | ❌ |
| **税务/抵扣分析** | Tax in Invoice | ❌ |

### 3.3 关键观察

- **当前 10 个分析模板已覆盖 P2P 核心流程的主分析场景**——三路匹配、价差、付款、绩效、支出、收货、发票、折扣、周期、集中度，这是教科书级的覆盖
- **缺口主要在 4 个方向**：
  1. **AP 财务侧深化**——GR/IR 暂记、AP aging、现金流预测
  2. **流程上游**——采购申请、合同、询报价
  3. **变更/退货**——PO 变更、退货专项
  4. **战略级合规**——合同执行率、品类合规、税务
- **业务直觉**：缺口里 **GR/IR 差异、AP aging、合同执行率、退货分析** 这 4 项在中大型企业是高频需求；其余多为偶发或战略层面，频次低
- **当前 stub 工具**（material_master、vendor_risk_scoring、approval_limits、blacklist）需要先实现才能支撑相关新模板

---

## 4. 候选新增模板清单

基于第 3 章的对比，列出 **12 个候选模板**，覆盖 4 个缺口方向。每个候选给出：业务问题、典型 query 句式、所需 tools（含哪些是 stub 需要先实现）、模板任务编排骨架。

### 4.1 AP 财务侧深化（4 个）

#### C-AP-01 · `GR_IR_RECONCILIATION` — GR/IR 暂记差异分析
- **业务问题**：已收货未开票、已开票未收货的差异（财务暂记账平衡）
- **典型 query**：`查 GR/IR 暂记差异` / `已收货但还没开票的有哪些`
- **所需 tools**：`query_receipts` ✅ + `query_invoices` ✅ + **`run_gr_ir_reconciliation`**（新建）+ `report_agent`
- **节点编排**：3 + 1（采集 GR/INV → 对账 → 报告）
- **依赖**：需新增 1 个 rule tool

#### C-AP-02 · `AP_AGING` — 应付账款老化分析
- **业务问题**：未付发票按账龄分桶（0-30/31-60/61-90/90+ 天）
- **典型 query**：`应付账款老化` / `账期超 60 天的发票有哪些`
- **所需 tools**：`query_invoices` ✅ + **`calculate_ap_aging`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **与 PAYMENT_COMPLIANCE 区别**：PC 关注合规违规，AP_AGING 关注存量结构

#### C-AP-03 · `CASH_FLOW_FORECAST` — 现金流预测
- **业务问题**：基于发票 due_date 预测未来 N 天的应付现金流出
- **典型 query**：`未来 30 天要付多少` / `下周付款预测`
- **所需 tools**：`query_invoices` ✅ + **`calculate_cash_forecast`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：发票表需有 due_date 字段

#### C-AP-04 · `TAX_DEDUCTION` — 税务/抵扣分析
- **业务问题**：进项税抵扣金额、抵扣率、跨期抵扣异常
- **典型 query**：`进项抵扣分析` / `这季度可抵扣多少`
- **所需 tools**：`query_invoices` ✅ + **`calculate_tax_deduction`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：发票表需有 tax_amount / tax_rate 字段（中国增值税场景）

### 4.2 流程上游（3 个）

#### C-UP-01 · `PR_APPROVAL_CYCLE` — 采购申请审批分析
- **业务问题**：PR 创建到审批通过的时长、瓶颈环节
- **典型 query**：`采购申请审批要多久` / `谁审批最慢`
- **所需 tools**：**`query_purchase_requisitions`**（新建）+ **`calculate_pr_approval_cycle`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：需新增 PR 主单据 query；ERP 数据源需支持

#### C-UP-02 · `CONTRACT_LEAKAGE` — 合同执行率（合同覆盖率）
- **业务问题**：实际采购金额中走合同的比例（maverick spend 分析）
- **典型 query**：`合同覆盖率` / `没走合同的采购有多少`
- **所需 tools**：**`query_contracts`**（新建）+ `query_purchase_orders` ✅ + **`calculate_contract_coverage`**（新建）+ `report_agent`
- **节点编排**：2 并行 + 1 + 1
- **依赖**：合同主数据 + 合同价主数据

#### C-UP-03 · `RFQ_RESPONSE_TIME` — 询报价响应时效
- **业务问题**：询报价发出后供应商响应的时长
- **典型 query**：`供应商报价多久` / `询价响应慢的供应商`
- **所需 tools**：**`query_rfqs`**（新建）+ **`calculate_rfq_response_time`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：RFQ 主数据 + 报价子表

### 4.3 变更/退货（3 个）

#### C-CH-01 · `PO_CHANGE_HISTORY` — PO 变更分析
- **业务问题**：PO 修订频率、金额漂移、变更原因
- **典型 query**：`PO 改了几次` / `哪些 PO 金额涨了`
- **所需 tools**：**`query_po_changes`**（新建）+ **`analyze_po_drift`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：PO 变更日志表

#### C-CH-02 · `RTV_ANALYSIS` — 退货专项分析
- **业务问题**：退货率、退货原因、退货金额（语义独立于 RECEIPT_ANOMALY）
- **典型 query**：`退货分析` / `哪些供应商退货多`
- **所需 tools**：`query_receipts` ✅（含退货类型）+ **`analyze_returns`**（新建，可能 receipt_anomaly 内可扩展）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：query_receipts 已含退货类型，但需 analyze tool 拆出来

#### C-CH-03 · `CONSIGNMENT_RECONCILIATION` — 寄售库存对账
- **业务问题**：寄售库存量与实际消耗、结算差异
- **典型 query**：`寄售对账` / `寄售库存差异`
- **所需 tools**：**`query_consignment_inventory`**（新建）+ **`reconcile_consignment`**（新建）+ `report_agent`
- **节点编排**：1 + 1 + 1
- **依赖**：寄售库存表（很多企业用 SAP MM 寄售流程）

### 4.4 战略级合规（2 个）

#### C-ST-01 · `CATEGORY_COMPLIANCE` — 采购品类合规率
- **业务问题**：每个品类下的合同覆盖率、议价空间利用、首选供应商占比
- **典型 query**：`品类合规率` / `哪个品类最不规范`
- **所需 tools**：`query_purchase_orders` ✅ + **`query_contracts`**（依赖 C-UP-02）+ **`calculate_category_compliance`**（新建）+ `report_agent`
- **节点编排**：2 + 1 + 1
- **依赖**：合同主数据 + 品类主数据

#### C-ST-02 · `MAVERICK_SPEND` — 计划外采购（绕开流程的采购）
- **业务问题**：未经 PR 审批直接下 PO、紧急采购占比
- **典型 query**：`计划外采购` / `紧急采购有多少`
- **所需 tools**：**`query_purchase_requisitions`**（依赖 C-UP-01）+ `query_purchase_orders` ✅ + **`detect_maverick_spend`**（新建）+ `report_agent`
- **节点编排**：2 + 1 + 1
- **依赖**：PR 主数据

### 4.5 候选清单汇总

| ID | 模板名 | 方向 | 新建 tools 数 | 依赖 stub | 节点数 |
|---|---|---|---|---|---|
| C-AP-01 | GR_IR_RECONCILIATION | AP 财务 | 1 | 否 | 4 |
| C-AP-02 | AP_AGING | AP 财务 | 1 | 否 | 3 |
| C-AP-03 | CASH_FLOW_FORECAST | AP 财务 | 1 | 否 | 3 |
| C-AP-04 | TAX_DEDUCTION | AP 财务 | 1 | 否（需 schema 字段）| 3 |
| C-UP-01 | PR_APPROVAL_CYCLE | 上游流程 | 2 | 是（PR 数据源）| 3 |
| C-UP-02 | CONTRACT_LEAKAGE | 上游流程 | 2 | 是（合同数据源）| 4 |
| C-UP-03 | RFQ_RESPONSE_TIME | 上游流程 | 2 | 是（RFQ 数据源）| 3 |
| C-CH-01 | PO_CHANGE_HISTORY | 变更/退货 | 2 | 是（变更日志）| 3 |
| C-CH-02 | RTV_ANALYSIS | 变更/退货 | 1 | 否 | 3 |
| C-CH-03 | CONSIGNMENT_RECONCILIATION | 变更/退货 | 2 | 是（寄售表）| 3 |
| C-ST-01 | CATEGORY_COMPLIANCE | 战略合规 | 1 | 是（依赖 C-UP-02）| 4 |
| C-ST-02 | MAVERICK_SPEND | 战略合规 | 1 | 是（依赖 C-UP-01）| 4 |

**总计**：12 个候选模板，需新增 ~17 个 tools，其中 7 个需要新数据源接入。

---

## 5. 候选模板三维评分

### 5.1 评分维度定义

每个候选模板按 3 个维度打分，每维 1-5 分（5 最优）。

| 维度 | 含义 | 高分（5）特征 | 低分（1）特征 |
|---|---|---|---|
| **业务价值 V** | 该分析对采购/财务决策的实际影响力 | 直接影响合规/资金/审计；缺失会导致显著业务风险 | 偶发使用；战略层面但不紧急 |
| **流量预估 F** | 该模板上线后能吃下的查询占比预估 | 高频概念（"逾期 / 老化 / 合规率"等日常问题）| 季度性 / 年度性需求 |
| **实施成本 C** | 工程投入（数据接入 + tools + 模板 + 测试）| 1（极低，只新建 1 个 tool）| 5（极高，需新数据源 + 多 tools + ETL）|

**ROI 综合分** = `V × F / C`（数值越高越优先）。

### 5.2 评分明细表

| ID | 模板 | V | F | C | ROI | 备注 |
|---|---|---|---|---|---|---|
| **C-AP-02** | AP_AGING | 5 | 5 | 1 | **25.0** | 高频；只需 1 个 calc tool；现有 query_invoices 直接可用 |
| **C-AP-01** | GR_IR_RECONCILIATION | 5 | 4 | 2 | **10.0** | 财务日常对账；需 1 个 reconcile tool |
| **C-CH-02** | RTV_ANALYSIS | 4 | 4 | 2 | **8.0** | query_receipts 已含退货数据，需独立 analyze tool |
| **C-AP-03** | CASH_FLOW_FORECAST | 5 | 4 | 2 | **10.0** | 财务高价值；依赖 due_date 字段 |
| **C-CH-01** | PO_CHANGE_HISTORY | 4 | 3 | 3 | 4.0 | 需 PO 变更日志数据源 |
| **C-UP-01** | PR_APPROVAL_CYCLE | 4 | 3 | 4 | 3.0 | 需新接 PR 数据源 |
| **C-AP-04** | TAX_DEDUCTION | 4 | 3 | 3 | 4.0 | 需 schema 加税务字段；中国企业刚需 |
| **C-UP-02** | CONTRACT_LEAKAGE | 5 | 3 | 5 | 3.0 | 战略价值高但合同数据源接入难 |
| **C-ST-01** | CATEGORY_COMPLIANCE | 4 | 2 | 5 | 1.6 | 依赖 C-UP-02；季度性需求 |
| **C-ST-02** | MAVERICK_SPEND | 4 | 2 | 4 | 2.0 | 依赖 C-UP-01 |
| **C-CH-03** | CONSIGNMENT_RECONCILIATION | 3 | 2 | 4 | 1.5 | 行业相关（制造/零售）；非通用需求 |
| **C-UP-03** | RFQ_RESPONSE_TIME | 3 | 2 | 4 | 1.5 | 需 RFQ 数据源；非所有企业必用 |

### 5.3 ROI 排序梯度

按 ROI 分档：

#### **P0 高 ROI**（推荐立即做，ROI ≥ 8）
- **C-AP-02 · AP_AGING**（ROI 25）— 极高性价比
- **C-AP-01 · GR_IR_RECONCILIATION**（ROI 10）
- **C-AP-03 · CASH_FLOW_FORECAST**（ROI 10）
- **C-CH-02 · RTV_ANALYSIS**（ROI 8）

#### **P1 中 ROI**（值得做，需小立项，4 ≤ ROI < 8）
- **C-CH-01 · PO_CHANGE_HISTORY**（ROI 4）
- **C-AP-04 · TAX_DEDUCTION**（ROI 4）

#### **P2 低 ROI**（需大立项 / 数据源依赖，ROI < 4）
- **C-UP-01 · PR_APPROVAL_CYCLE**（ROI 3）
- **C-UP-02 · CONTRACT_LEAKAGE**（ROI 3）
- **C-ST-02 · MAVERICK_SPEND**（ROI 2）
- **C-ST-01 · CATEGORY_COMPLIANCE**（ROI 1.6）
- **C-CH-03 · CONSIGNMENT_RECONCILIATION**（ROI 1.5）
- **C-UP-03 · RFQ_RESPONSE_TIME**（ROI 1.5）

### 5.4 评分背后的关键判断

- **AP 财务侧 ROI 普遍最高**：因为 `query_invoices` / `query_payments` 已就绪，只需补 1 个 calc/rule tool 即可上线 → 工程成本极低
- **流程上游的 ROI 普遍偏低**：不是因为业务价值低，而是因为**数据源接入成本高**（PR/合同/RFQ 通常在 ERP 不同模块/系统中，eragent 当前 ERP 适配器只接了 PO/GR/INV/PAY/Vendor）
- **战略级合规模板**几乎都依赖上游数据源 → ROI 被传导降低
- **流量预估 F 是最大不确定项**：当前无监控数据，依赖业务直觉；批 1 上线后应根据真实流量重新校准 ROI

### 5.5 评分敏感性分析

如果数据源接入成本变化（例如未来接入了 PR/合同数据源后），ROI 会显著重排：

| 假设 | 影响 |
|---|---|
| C-UP-01 PR 数据源接入完成 → C 从 4 降到 2 | C-UP-01 ROI 升到 6（进入 P1）；C-ST-02 ROI 也间接升到 4 |
| C-UP-02 合同数据源接入完成 → C 从 5 降到 2 | C-UP-02 ROI 升到 7.5（进入 P1）；C-ST-01 ROI 升到 4 |
| 监控显示 RTV 实际流量很低（F 从 4 降到 2）| C-CH-02 ROI 降到 4（仍在 P1，但优先级降）|

---

## 6. 模板膨胀风险分析

模板数从 14 扩到 20+ 不是免费的——需要正面评估"模板太多会带来什么问题"，避免落入"为加而加"的陷阱。

### 6.1 风险一：维护成本指数上升

**现象**：
- 每个模板都是一份 list[dict] 编排定义，平均 30-80 行（含注释）
- tools 升级时（如 `query_purchase_orders` 加新参数），所有引用它的模板都要同步检查
- 测试矩阵线性增长（每模板至少 3-5 个集成测试）

**当前**：14 模板 × 5 测试 ≈ 70 集成测试场景
**P0+P1 全做后**：20 模板 × 5 ≈ 100 测试场景，维护成本 +40%

**缓解**：
- 模板按"骨架函数 + 参数"重构，避免 list[dict] 重复（例如 `_build_collect_analyze_report_dag(collect_tools, analyze_tool, scenario_name)`）—— 但这是另一个重构专题
- 严格的模板覆盖率门槛（每模板必须有集成测试）

### 6.2 风险二：L2 种子库语义稀释

**现象**：
- 每新增模板需要在 [intent_seeds.yaml](../config/intent_seeds.yaml) 加 10-15 条种子
- 种子数从 145 → 250+ 后，相邻 analysis_type 的语义边界可能模糊
- 某些边界查询（如"付款合规" vs "AP 老化"）可能两侧都命中，L2 投票（批 3）需要更精细的权重

**风险量化**：
- 当前 L2 阈值 0.80，假设新增 6 个模板各 15 条种子 → 新增 90 条
- 如果新种子与现有种子语义重叠 > 0.85，会导致原有类型的命中分散

**缓解**：
- 新增种子前做"种子去重 / 边界审计"——检查每条新种子是否与现有 3 个最相似种子的 cosine sim < 0.85
- 主方案批 3 的 top-k 投票天然对此有抗噪能力

### 6.3 风险三：模板选择歧义

**现象**：
- 当前 `load_dag_template` 派发逻辑是简单的 if-else（实体维度 → analysis_type 映射）
- 模板增多后，**多个模板可能同时合理**（如"应付逾期" 既可触发 PAYMENT_COMPLIANCE 也可触发 AP_AGING）
- LLM 在 L3 也会面临选择困难，confidence 下降

**缓解**：
- 模板间设计**互斥触发条件**，避免同时合理（如 PAYMENT_COMPLIANCE 限定"违规检查"语义，AP_AGING 限定"账龄分桶"语义）
- 实在无法互斥的就**合并模板**（如把 PAYMENT_COMPLIANCE 内部加 aging 模式分支，而非独立 AP_AGING 模板）

### 6.4 风险四：DAG 模板与 tools 耦合度上升

**现象**：
- 新模板带来新 tools，新 tools 又被多个模板引用
- 如果某 tool 改签名或废弃，影响面扩散到所有模板
- 当前 tools 已经 19 个，新增 17 个变 36 个，"工具索引"在 LLM prompt / tool selection 上也会变重

**缓解**：
- tools 严格遵循"只增字段不删字段"的演进原则
- 模板内部不直接拼 tool params，统一走 `inputs` 占位符 + `_replace_params`

### 6.5 风险五：报告 prompt 多样化

**现象**：
- 每个新模板的 `report_agent` 调用都需要专属 scenario prompt（如 "AP 账龄分析报告" vs "PR 审批分析报告"）
- 当前 `report_agent` 用统一 `_REPORT_PROMPT` + scenario 占位符；新模板增多后 prompt 适配性下降

**缓解**：
- 按模板分类组织 scenario prompt 库（已在 [docs/prompt_issue.md](prompt_issue.md) 提及）
- report_agent 输出格式按"4 段标准 + 模板特色字段"区分

### 6.6 风险六：与"通用模板"语义冲突

**现象**：
- 主方案批 5 拟新增 `RECENT_PROCUREMENT_HEALTH`（通用概览模板）
- 如果同时上线 6 个新具体模板，"看看最近采购"可能既走通用模板也部分匹配某个具体模板
- 路由层需要明确优先级

**缓解**：
- **明确优先级**：具体模板 > 实体维度模板 > 通用概览模板（即"概览类"是最弱的兜底）
- 通用模板触发条件加强（必须含"概览/总体/全面"等明确触发词）

### 6.7 综合风险评级

| 风险 | 严重度 | 可缓解程度 | 影响时点 |
|---|---|---|---|
| 维护成本上升 | 中 | 高（模板骨架重构）| 长期 |
| 种子库稀释 | 中 | 高（去重审计）| 上线即影响 |
| 模板选择歧义 | **高** | 中（设计互斥条件）| 上线即影响 |
| Tools 耦合 | 低 | 高（演进规范）| 长期 |
| 报告 prompt 多样化 | 低 | 高（prompt 库化）| 上线即影响 |
| 与通用模板冲突 | 中 | 高（优先级规则）| 上线即影响 |

**核心结论**：模板膨胀**不是"能不能加"，而是"加多少 + 怎么管"**。建议每批新增 ≤ 3 个，每加完后观察 4 周再评估下一批。

---

## 7. 推荐结论与执行梯度

### 7.1 总体判断

**"扩充更多 P2P 模板"在原则上是合理且必要的**——理由：
1. 当前 14 模板只覆盖 P2P 核心 10 个分析场景 + 4 个实体维度，**业界标准 P2P 至少有 20+ 个常见分析场景**
2. AP 财务侧（GR/IR、AP aging、现金流、税务）是高频但完全未覆盖的盲区
3. 数据基础已就绪——`query_invoices` / `query_payments` 等核心 query tools 完整

**但要严格控制节奏与边界**——理由：
1. 模板膨胀的维护成本和路由歧义风险是真实的
2. 上游流程（PR/合同/RFQ）需要先解决数据源接入问题，工程成本远高于模板本身
3. 应该让批 1 监控数据指导优先级，避免凭直觉加模板

### 7.2 推荐执行梯度

#### 阶段 A · "AP 财务深化批"（推荐立即立项）

新增 3 个高 ROI 模板，全部基于现有 query tools，工程成本最低：

| 模板 | ROI | 工作量 |
|---|---|---|
| **C-AP-02 · AP_AGING** | 25 | ~150 行（1 calc tool + 1 模板 + 测试）|
| **C-AP-01 · GR_IR_RECONCILIATION** | 10 | ~250 行（1 reconcile tool + 1 模板 + 测试）|
| **C-AP-03 · CASH_FLOW_FORECAST** | 10 | ~200 行（1 forecast tool + 1 模板 + 测试）|

**前置条件**：
- 主方案批 1 上线（监控就位）
- 验证 `query_invoices` 含 due_date 字段（CASH_FLOW_FORECAST 需要）

**预估流量收益**：吃下当前 PAYMENT_COMPLIANCE 流量的 ~30% 边界查询 + 新增 ~5pp 的 DAG 命中率

**预估时间**：1.5-2 周

#### 阶段 B · "退货 + 变更"（推荐第二阶段）

| 模板 | ROI | 工作量 |
|---|---|---|
| **C-CH-02 · RTV_ANALYSIS** | 8 | ~150 行（1 analyze tool + 1 模板 + 测试）|
| **C-CH-01 · PO_CHANGE_HISTORY** | 4 | ~300 行（含数据源接入）|

**前置条件**：阶段 A 完成 + 4 周观察期通过

**预估时间**：2-3 周

#### 阶段 C · "上游流程 + 战略合规"（需要专题立项）

| 模板组 | 前置数据接入 |
|---|---|
| C-UP-01 / C-UP-02 / C-ST-01 / C-ST-02 / C-AP-04 | PR 数据源 + 合同数据源 + 税务字段 |

**特点**：
- 工程成本主要在数据源接入（1-2 个月级别）
- 必须有明确的业务方需求驱动（而非"业界有就要加"）
- 建议拆分为独立专题，按客户/业务需求逐个立项

#### 阶段 D · 行业相关 / 偶发需求（不建议主动做）

| 模板 | 不做理由 |
|---|---|
| C-CH-03 · CONSIGNMENT_RECONCILIATION | 行业局限（主要制造/零售）|
| C-UP-03 · RFQ_RESPONSE_TIME | 频次低，按需再说 |

### 7.3 关于"通用模板" vs "具体模板"的关系

主方案批 5 拟新增 `RECENT_PROCUREMENT_HEALTH` 通用模板。本文档建议的具体模板与通用模板**互补不冲突**：

```
触发优先级（高 → 低）：
1. 实体维度模板（COMPREHENSIVE + 单据号）       ← 现有
2. AnalysisType 模板（具体分析场景）             ← 现有 10 + 新增
3. 通用 DAG 模板（COMPREHENSIVE 无实体 + 概览词）← 主方案批 5
4. ReAct 兜底
```

**新增模板要点**：每个新模板都有自己明确的 `AnalysisType` 枚举值（需要在 [api/schemas/analysis.py `AnalysisType`](../api/schemas/analysis.py) 注册），不会和通用模板抢路由。

### 7.4 与主方案批次的关系

| 本文档阶段 | 主方案批次 | 关系 |
|---|---|---|
| 阶段 A | 主方案批 1 | 依赖批 1 监控数据；建议批 1 上线后 2-3 周启动 |
| 阶段 B | 主方案批 5 | 阶段 B 与批 5 并行开发；触发条件互斥 |
| 阶段 C | 独立专题 | 与主方案解耦，按数据接入节奏推进 |

### 7.5 不在本专题范围内的事项

明确排除：
1. **DAG 引擎改造**（如支持条件分支 / 循环 / 子 DAG）—— 当前 list[dict] 编排已够用，未来需要再立项
2. **模板可视化编辑器**—— 配置工程化的方向，与本"扩充模板"专题独立
3. **基于 case_store 的"模板自学习"**（让系统自动从案例库提取新模板形态）—— 主方案批 4 后期可考虑
4. **多租户模板差异化**（不同客户用不同模板）—— 配置层问题，独立专题

### 7.6 决策清单（给业务方确认）

**立即可决定**：
- [ ] 是否同意阶段 A 立项（3 个 AP 财务模板）？
- [ ] 谁是业务侧 owner（财务团队？采购团队？）

**等批 1 监控数据后决定**：
- [ ] 阶段 A 上线后哪些场景的实际流量值得继续投入？
- [ ] 阶段 B 是否启动？

**需要专项立项**：
- [ ] 阶段 C 涉及的数据源接入（PR/合同）何时排期？
- [ ] 优先 C-UP-01 还是 C-UP-02？

---

## 文档版本历史

| 版本 | 日期 | 改动 |
|---|---|---|
| v1.0 | 2026-04-16 | 初稿：现状调研 + 12 候选模板 + 三维评分 + 风险 + 执行梯度 |






