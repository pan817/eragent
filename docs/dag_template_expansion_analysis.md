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

