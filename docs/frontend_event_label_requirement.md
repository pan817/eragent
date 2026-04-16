# 前端需求：事件流步骤名称中文化

## 背景

当前 SSE 事件流推送给前端的步骤信息使用的是后端函数名（如 `run_three_way_match`、`intent_resolved`），用户无法理解。后端已完成改造，在所有事件 payload 中新增了 `label` 字段，携带中文展示名称。前端需要适配读取该字段。

## 影响范围

涉及 3 种事件类型，所有事件 payload 结构已变更（向后兼容，原有字段不变）：

### 1. tool 事件

```json
// 改造前
{"type": "tool", "action": "start", "name": "run_three_way_match"}
{"type": "tool", "action": "end", "name": "run_three_way_match", "duration_ms": 1234, "status": "ok"}

// 改造后（新增 label 字段）
{"type": "tool", "action": "start", "name": "run_three_way_match", "label": "三路匹配核对"}
{"type": "tool", "action": "end", "name": "run_three_way_match", "label": "三路匹配核对", "duration_ms": 1234, "status": "ok"}
```

### 2. dag_task 事件

```json
// 改造前
{"type": "dag_task", "action": "start", "task_name": "calculate_supplier_kpis"}
{"type": "dag_task", "action": "end", "task_name": "calculate_supplier_kpis", "duration_ms": 567, "status": "ok"}

// 改造后（新增 label 字段）
{"type": "dag_task", "action": "start", "task_name": "calculate_supplier_kpis", "label": "供应商绩效测算"}
{"type": "dag_task", "action": "end", "task_name": "calculate_supplier_kpis", "label": "供应商绩效测算", "duration_ms": 567, "status": "ok"}
```

### 3. stage 事件

```json
// 改造前
{"type": "stage", "name": "intent_resolved", "attrs": {...}}

// 改造后（新增 label 字段）
{"type": "stage", "name": "intent_resolved", "label": "意图识别完成", "attrs": {...}}
```

## 前端改动要求

**所有展示事件步骤名称的地方，统一改为优先读取 `label` 字段，`name` / `task_name` 作为 fallback：**

```js
// 改前
display = event.name || event.task_name

// 改后
display = event.label || event.name || event.task_name
```

## 中文标签对照表（供前端核对）

| 原始 name / task_name | label |
|---|---|
| query_purchase_orders | 查询采购订单 |
| query_receipts | 查询收货记录 |
| query_invoices | 查询发票记录 |
| query_payments | 查询付款记录 |
| query_vendor_master | 查询供应商主数据 |
| query_material_master | 查询物料主数据 |
| run_three_way_match | 三路匹配核对 |
| run_price_variance_analysis | 价格差异分析 |
| run_payment_compliance_check | 付款合规检查 |
| check_approval_limits | 审批权限校验 |
| check_blacklist | 供应商黑名单核对 |
| calculate_supplier_kpis | 供应商绩效测算 |
| calculate_spend_analysis | 采购支出分析 |
| calculate_po_cycle_time | 采购周期测算 |
| analyze_receipt_anomalies | 收货异常分析 |
| detect_duplicate_invoices | 重复发票检测 |
| analyze_discount_utilization | 折扣利用率分析 |
| analyze_vendor_concentration | 供应商集中度分析 |
| run_vendor_risk_scoring | 供应商风险评分 |
| generate_summary_report | 生成分析报告 |
| generate_chart | 生成图表 |
| intent_resolved | 意图识别完成 |
| dag_planned | 分析计划生成 |
| react_started | 开始智能分析 |

## 注意事项

1. `label` 字段保证存在，但为防后端版本不一致，前端必须保留 fallback 逻辑
2. 原有 `name` / `task_name` 字段不会删除，如果前端有用这些字段做逻辑判断（如事件过滤、去重），不需要改动，只改**展示**部分
3. 后端新增工具时会同步维护 label，前端无需额外适配
