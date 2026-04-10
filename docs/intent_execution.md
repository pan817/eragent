# Orchestrator 意图识别优化方案分析

## 背景

当前 `IntentParser`（`core/orchestrator/intent.py`）采用纯关键词匹配，对"三路匹配"、"付款"等强信号词有效，但对描述性、症状式或语义相近但用词不同的查询完全失效。

## 当前问题根因

```
用户问："为什么最近采购成本比预算高出那么多？"
       → 命中关键词：0
       → 结果：COMPREHENSIVE（兜底）→ Agent 乱猜分析类型
```

核心矛盾：**关键词匹配是词汇级别的，业务理解是语义级别的**。

失效场景举例：
- 症状描述式：「最近采购成本偏高是什么原因？」→ 应为 PRICE_VARIANCE
- 动作描述式：「检查一下发票和收货单对不上的情况」→ 应为 THREE_WAY_MATCH
- 结果导向式：「哪些供应商交货总是拖」→ 应为 SUPPLIER_PERFORMANCE
- 跨类型联动：「SUP-001 质量投诉变多，是否也影响了付款？」→ 应为 COMPREHENSIVE / 多工具

---

## 方案 A：LLM 直接分类（Zero-shot / Few-shot）

**原理**：将意图识别本身交给 LLM，用 prompt 描述每个分析类型的定义和典型场景，让 LLM 直接输出结构化 JSON。

```
System: 你是 P2P 分析系统的意图解析器，分析类型有：
  - THREE_WAY_MATCH: 发票/订单/收货三单数量或金额不一致
  - PRICE_VARIANCE: 实际采购价格与合同/预算价格偏差
  - PAYMENT_COMPLIANCE: 付款逾期、提前付款、未授权付款
  - SUPPLIER_PERFORMANCE: 供应商交期、质量、KPI 综合评估
  - COMPREHENSIVE: 以上多类或无法归类

User: "为什么最近采购成本比预算高出那么多？"

Output: {"type": "PRICE_VARIANCE", "confidence": 0.92, "params": {"days": null}}
```

**优点**
- 理解能力极强，对语义相近表达、反问句、症状描述均有效
- 可同时提取结构化参数（合并现有 `_extract_params` 逻辑）
- 当前项目 LLM 已就绪，接入成本低

**缺点**
- 每次意图解析多一次 LLM 调用（延迟 +0.5~2s）
- 增加 API 成本

**适用场景**：查询复杂度高、关键词模糊；作为 Level 3 兜底或独立方案

---

## 方案 B：向量语义相似度分类（Embedding-based）

**原理**：为每个分析类型准备一组"种子问题"，将种子问题和用户 query 都 embed 成向量，用余弦相似度找最近邻，取最相似的类别。

```
种子问题（离线 embed，存入 Chroma）：
  THREE_WAY_MATCH:
    - "发票金额和收货单不匹配"
    - "PO、GR、Invoice 三单差异"
    - "验货数量和付款金额对不上"
  PRICE_VARIANCE:
    - "实际价格超出合同价"
    - "采购成本异常偏高"
    - "报价和实际结算价差太多"
  ...

用户 query → embed → 余弦相似度 → 最近邻类别
```

**优点**
- 无需 LLM 调用，延迟极低（毫秒级）
- 可增量添加种子问题，持续改善
- 项目已有 Chroma + `core/knowledge/embeddings.py`，可直接复用

**缺点**
- 依赖 Embedding 模型质量
- 需要维护种子库（冷启动需手工设计）
- 置信度阈值需要调参

**适用场景**：对低延迟要求高、query 模式相对固定；作为 Level 2 中间层

---

## 方案 C：三级混合路由（Keyword → Embedding → LLM）⭐ 推荐

**原理**：按置信度逐级升级，简单 query 走快路径，复杂 query 才升级。

```
用户 query
  ↓
[Level 1] 关键词匹配（现有逻辑，0 延迟）
  命中且唯一 → 直接返回
  ↓ 未命中或多命中
[Level 2] Embedding 相似度（vs 种子问题库）
  相似度 > 阈值（如 0.80）→ 返回
  ↓ 低于阈值
[Level 3] LLM 分类（带置信度 + 参数提取）
  返回最终结果
```

**实现要点**
- Level 1 完全复用现有 `IntentParser._match_type()`
- Level 2 种子库存入 Chroma 独立 collection（`intent_seeds`），与业务向量库隔离
- Level 3 LLM prompt 同时提取 `supplier_id / po_number / days`，合并现有 `_extract_params`
- 阈值可配置化（`config.yaml` 中 `intent.embedding_threshold: 0.80`）

**优点**
- 简单 query 不引入延迟（Level 1 命中率约 60%）
- 语义理解兜底（Level 3），召回率最高
- 可观测：每次解析记录命中层级，便于调参

**缺点**
- 三级逻辑有维护成本
- 置信度阈值需要基于真实 query 分布调参

**适用场景**：生产级系统，兼顾性能和准确率 ✅

---

## 方案 D：LLM Tool-calling 路由（AgentPlanner 模式）

**原理**：不做显式意图分类，直接把所有分析工具暴露给 LLM，由 LLM 根据 query 决定调用哪些工具、以什么顺序调用（ReAct / Tool-use 模式）。Orchestrator 退化为工具调用的执行器。

```
LLM 拿到工具列表：
  - run_three_way_match_analysis(supplier_id, days)
  - run_price_variance_analysis(days, threshold)
  - run_payment_compliance_check(days)
  - run_supplier_kpi_report(supplier_id)

用户问："最近供应商 SUP-001 质量投诉变多，是否也影响了付款？"
→ LLM 规划：先 supplier_kpi(SUP-001)，再 payment_compliance(days=30)，并联或串行
→ Orchestrator 按规划执行，聚合结果
```

**优点**
- 最灵活，天然支持多步骤、跨类型组合分析
- Orchestrator 逻辑极简，`IntentParser` 可完全废弃
- 与当前 P2PAgent（`create_agent` + tools）架构方向一致

**缺点**
- 成本最高（每次都要 LLM 规划）
- 工具粒度设计要求高，粒度过细导致规划链过长
- 可观测性需要额外设计（规划链 trace）

**适用场景**：复杂的探索性分析、多类型联动；中期演进目标

---

## 方案 E：Fine-tuning 意图分类小模型

**原理**：收集历史 query 标注数据，fine-tune 一个小分类模型（如 BERT-base-Chinese），本地推理。

**优点**：精度高、延迟极低、无 API 成本  
**缺点**：需要大量标注数据（冷启动问题）；维护模型版本成本高  
**适用场景**：数据量充足、query 模式稳定、对成本极度敏感的大规模部署

---

## 对比总结

| 方案 | 理解能力 | 延迟 | API 成本 | 实现复杂度 | 近期可行性 |
|------|---------|------|---------|-----------|-----------|
| A LLM 直接分类 | ⭐⭐⭐⭐⭐ | 中（+1~2s） | 中 | 低 | ✅ |
| B Embedding 相似度 | ⭐⭐⭐⭐ | 极低（ms） | 极低 | 中 | ✅ |
| C 三级混合路由 | ⭐⭐⭐⭐⭐ | 低→中 | 低→中 | 中 | ✅✅ 推荐 |
| D LLM Tool-calling | ⭐⭐⭐⭐⭐ | 高 | 高 | 高 | 中期目标 |
| E Fine-tuning | ⭐⭐⭐⭐ | 极低 | 无 | 极高 | 数据成熟后 |

---

## 建议路径

### 近期（当前迭代）
实施 **方案 C（三级混合路由）**：

1. Level 1 保留现有关键词匹配（零改动）
2. Level 2 为 4 个分析类型各设计 10~20 条种子问题，存入 Chroma `intent_seeds` collection
3. Level 3 添加 LLM 分类 prompt，输出 `{type, confidence, params}`，合并 `_extract_params` 逻辑
4. 所有层级命中情况记录到 trace（`span_type="intent"`），便于后续调参

### 中期
演进为 **方案 D（Tool-calling Orchestration）**，Orchestrator 不再做意图分类，让 Agent 直接规划工具调用链。P2PAgent 现有的 `create_agent` + 8 个工具的架构天然支持这个方向，主要工作在于：
- 将粗粒度工具（`run_three_way_match`）拆分为细粒度（`query_invoice_discrepancies` / `check_quantity_mismatch` 等）
- 设计规划链的 trace 格式

### 长期
数据积累后考虑 **方案 E（Fine-tuning）**，将分类能力下沉到本地小模型，彻底消除 LLM 调用开销。
