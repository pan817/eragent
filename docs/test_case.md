# Analyze API 正常测试用例汇总

汇总 `tests/http/test_api.http` 和 `tests/http/test_orchestrator.http` 中所有 `/api/v1/ptp-agent/analyze` 的正常测试用例（排除边界/异常输入），按路由路径和业务场景分类。

---

## 一、Level 1 关键词命中 → DAG 执行

query 中包含明确的业务关键词，L1 命中率超过阈值，直接加载静态 DAG 模板并行执行。

### 1.1 三路匹配

| # | query | 参数 | 预期路由 | 预期 DAG |
|---|-------|------|---------|---------|
| 1 | 分析三路匹配发票收货异常 | — | L1, three_way_match | 5 节点（3 并行采集 + 匹配 + 报告） |
| 2 | 请分析最近的三路匹配异常情况，看看哪些订单存在数量或金额偏差 | — | L1, three_way_match | 同上 |
| 3 | 检查采购订单 PO-2024-0035 的三路匹配情况 | analysis_type=three_way_match | L1, three_way_match | po_number 参数替换 |
| 4 | 分析最近60天供应商 SUP-005 海尔智家的三路匹配情况 | time_range_days=60, analysis_type=three_way_match, session_id 指定 | L1, three_way_match | days=60, supplier_id=SUP-005 |

### 1.2 价格差异

| # | query | 参数 | 预期路由 | 预期 DAG |
|---|-------|------|---------|---------|
| 5 | 检查价格差异和合同价偏差 | — | L1, price_variance | 3 节点（采集 + 分析 + 报告） |
| 6 | 分析所有供应商的采购价格差异，找出实际价格与合同价偏差较大的订单 | — | L1, price_variance | 同上 |
| 7 | 分析供应商 SUP-001 华为科技的价格差异情况 | analysis_type=price_variance | L1, price_variance | supplier_id=SUP-001 |
| 8 | 分析价格差异 | 仅 query | L1, price_variance | 默认参数 |

### 1.3 付款合规

| # | query | 参数 | 预期路由 | 预期 DAG |
|---|-------|------|---------|---------|
| 9 | 分析付款逾期和折扣滥用情况 | — | L1, payment_compliance | 4 节点（2 并行采集 + 合规检查 + 报告） |
| 10 | 检查付款合规性，是否存在逾期付款或提前付款的情况 | — | L1, payment_compliance | 同上 |
| 11 | 检查最近90天的付款合规情况 | time_range_days=90 | L1, payment_compliance | days=90 |

### 1.4 供应商绩效

| # | query | 参数 | 预期路由 | 预期 DAG |
|---|-------|------|---------|---------|
| 12 | 评估供应商 SUP-001 的绩效 KPI 和准时交货质量 | — | L1, supplier_performance | 3 节点，supplier_id=SUP-001 |
| 13 | 评估供应商 SUP-001 华为科技的绩效 KPI，包括准时交付率和发票准确率 | — | L1, supplier_performance | supplier_id=SUP-001 |
| 14 | 评估供应商 SUP-002 中兴通讯的绩效表现 | analysis_type=supplier_performance | L1, supplier_performance | supplier_id=SUP-002 |
| 15 | 计算供应商 SUP-003 比亚迪电子的 KPI | — | L1, supplier_performance | supplier_id=SUP-003 |
| 16 | 分析供应商 SUP-002 的绩效评分 | — | L1, supplier_performance | supplier_id=SUP-002 |

---

## 二、Level 2 语义匹配 → DAG 执行

query 中无直接关键词，但与 Chroma 种子库中的种子问题语义相似度 > 0.80，命中对应分析类型。

| # | query | 预期命中种子 | 预期类型 |
|---|-------|------------|---------|
| 17 | 为什么最近采购成本比预算高出那么多 | "采购成本异常偏高" | price_variance |
| 18 | 检查一下开票数量和入库数量对不上的情况 | "发票金额和收货单不匹配" | three_way_match |
| 19 | 哪些供应商送货总是迟到 | "供应商交货总是拖延" | supplier_performance |
| 20 | 有没有该付钱还没付的账单 | "哪些发票过了付款期限还没付" | payment_compliance |

---

## 三、Level 3 LLM 分类 → ReAct 执行

L1/L2 均未命中，由 LLM 判断意图后走 P2PAgent ReAct 自主执行。

| # | query | 场景说明 | 预期类型 |
|---|-------|---------|---------|
| 21 | 哪些采购员的价格谈判能力比较弱 | 探索性问题，无法归类到单一分析类型 | comprehensive / LLM 判断 |
| 22 | SUP-001 质量投诉变多了，是否也影响了付款节奏 | 跨类型联动（绩效 + 付款） | comprehensive |
| 23 | 给我看看采购数据 | 完全模糊 | comprehensive |
| 24 | 分析一下销售部门的业绩趋势 | 非 P2P 领域 | comprehensive |
| 25 | 最近采购有什么异常吗？ | 模糊查询，系统自动判断 | comprehensive / LLM 判断 |

---

## 四、综合分析

显式或隐式走 COMPREHENSIVE 类型，通常由 ReAct Agent 自主选择多个工具。

| # | query | 参数 | 预期路径 |
|---|-------|------|---------|
| 26 | 帮我全面分析一下最近的采购数据，包括三路匹配、价格差异、付款合规和供应商绩效 | — | L3 → ReAct，Agent 自主调用多个工具 |
| 27 | 分析最近30天的三路匹配异常，并评估供应商绩效 | analysis_type=comprehensive | 显式 COMPREHENSIVE → ReAct |

---

## 五、显式 analysis_type 覆盖路由

请求中显式指定 analysis_type，覆盖路由识别结果。

| # | query | analysis_type | 预期行为 |
|---|-------|--------------|---------|
| 28 | 分析价格差异情况 | three_way_match | query 命中 price_variance，但显式指定 three_way_match，走 three_way_match DAG |
| 29 | 分析三路匹配发票收货异常 | comprehensive | L1 命中 three_way_match，但显式 comprehensive 跳过 DAG，走 ReAct |

---

## 六、参数提取与传递

验证从 query 文本中正则提取参数 + request 级别参数覆盖。

| # | query | request 参数 | 预期提取结果 |
|---|-------|-------------|-------------|
| 30 | 分析 SUP-001 最近60天的价格差异 | — | supplier_id=SUP-001, days=60 |
| 31 | 检查 PO-2024-0035 的三路匹配情况 | — | po_number=PO-2024-0035 |
| 32 | 分析最近60天的付款逾期 | time_range_days=90 | time_range=最近 90 天（request 覆盖 query 中的 60） |

---

## 七、会话连续性

验证同一 session_id 下短期记忆的上下文延续。

| # | query | session_id | 预期行为 |
|---|-------|-----------|---------|
| 33 | 分析三路匹配发票异常 | session-continuity-001 | 首次分析，建立上下文 |
| 34 | 上次分析中最严重的异常是哪个 | session-continuity-001 | 利用短期记忆回答，引用上一次结果 |
| 35 | 上次分析的结果呢 | session-isolated-002 | 不同 session，无上下文，应表示无历史 |

---

## 八、多用户隔离

验证不同 user_id 的数据和记忆互相隔离。

| # | query | user_id | 预期行为 |
|---|-------|--------|---------|
| 36 | 分析三路匹配发票收货异常 | user-A | 用户 A 的分析结果存入 user-A 的报告 |
| 37 | 分析供应商 SUP-002 的绩效评分 | user-B | 用户 B 的分析结果存入 user-B 的报告 |

---

## 九、空数据场景

DAG 正常执行但业务数据为空的场景。

| # | query | 参数 | 预期行为 |
|---|-------|------|---------|
| 38 | 评估供应商 SUP-999 的绩效和交货质量 | — | DAG 执行成功，数据为空，报告应说明无异常 |
| 39 | 分析三路匹配发票收货异常 | time_range_days=1 | 极短时间范围，可能无数据 |

---

## 统计

| 分类 | 用例数 |
|------|--------|
| L1 关键词 → DAG | 16 |
| L2 语义 → DAG | 4 |
| L3 LLM → ReAct | 5 |
| 综合分析 | 2 |
| 显式类型覆盖 | 2 |
| 参数提取 | 3 |
| 会话连续性 | 3 |
| 多用户隔离 | 2 |
| 空数据场景 | 2 |
| **合计** | **39** |
