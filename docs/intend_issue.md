# Orchestrator 编排器改造：当前实现 vs execute.md 全方位 Gap 分析

## 一、当前实现概览

### 1.1 意图解析层（intent.py）
- **方式**：纯关键词 `in` 匹配，无评分，二值判断（命中/未命中）
- **输出**：`AnalysisType` 枚举 + `params` 字典（仅 supplier_id / po_number / days）
- **兜底**：多命中或零命中均返回 `COMPREHENSIVE`
- **无**：模糊度评分、追问机制、领域模块识别、分析深度判断

### 1.2 编排层（orchestrator.py）
- **流程**：`IntentParser.parse()` → `P2PAgent.run()` → 封装 `AnalysisResult`
- **执行**：单 Agent 串行，P2PAgent 内部由 LLM 自主决定工具调用顺序
- **无**：DAG 执行、并行调度、多 Agent 路由、自学习闭环

### 1.3 Agent 层（agent.py）
- **工具**：8 个（4 查询 + 4 分析），全部绑定在 P2PAgent
- **执行**：LangChain `create_agent` + `ainvoke`，LLM 自主 ReAct 调用工具
- **记忆**：短期靠 PostgresSaver checkpointer，长期靠 LongTermMemory
- **结果解析**：尝试从 LLM 输出中 `json.loads`，失败则视为纯 Markdown

### 1.4 API 层（analyze.py）
- **协议**：单轮请求→响应，`AnalysisStatus` 仅有 success / partial_success / failed
- **无**：`need_clarification` 状态、多轮对话支持

---

## 二、execute.md 目标架构概览

execute.md 设计了一套 **三级降级 + DAG 执行 + 自学习** 的完整架构：

1. **信号提取**：LLM 从原始 query 提取 `QuerySignal`（keywords / entities / time_range / ambiguity_score / domain_modules / clarification_needed）
2. **三级路由**：关键词命中率评分 → RAG 历史案例语义匹配 → LLM 动态 DAG 生成
3. **DAG 执行**：asyncio 并行执行器 + DAGValidator 结构校验
4. **多 Agent**：5 种（p2p / vendor / finance / rule / report）
5. **自学习**：成功 DAG 写入 Chroma → 下次 Level 2 命中
6. **追问机制**：ambiguity_score > 0.7 时抛 `ClarificationRequired`

---

## 三、逐层 Gap 详细分析

### 3.1 意图识别层 Gap

| 维度 | 当前实现 | execute.md 目标 | 差距影响 |
|------|---------|----------------|---------|
| 匹配算法 | `keyword in query` 布尔判断 | 命中率评分（hits / total_keywords ≥ threshold） | 当前"付款逾期"会同时命中 PAYMENT 和 SUPPLIER，直接 fallback COMPREHENSIVE |
| 规则库结构 | `dict[AnalysisType, list[str]]` 扁平列表 | `RULE_LIBRARY` 带 threshold / modules / dag_template | 无法按场景配置不同阈值，无法关联 DAG 模板 |
| 信号提取 | 正则提取 supplier_id / po_number / days | LLM 提取 QuerySignal（7 个结构化字段） | 缺少 entities、domain_modules、analysis_depth、ambiguity_score |
| 模糊度评分 | 无 | 0.0~1.0 评分 + 评分规则 | 无法识别"给我看看采购数据"这类过于模糊的请求 |
| 追问机制 | 无 | ClarificationRequired 异常 + clarification_question | 模糊请求只能 fallback，无法主动要求用户补充信息 |
| Level 2 语义匹配 | 无 | Chroma 种子库 + 余弦相似度 > 0.78 | 对"为什么采购成本比预算高"这类症状描述式 query 完全无效 |
| Level 3 LLM 兜底 | 无 | LLM 分类 / 动态 DAG 生成 | 规则未覆盖的新场景无法处理 |
| 多类型联动 | 多命中 → COMPREHENSIVE | 可组合多个 DAG 节点 | 无法为跨类型查询生成精准执行计划 |

**核心问题**：当前 IntentParser 的能力天花板是"关键词恰好出现在 query 中"。用户越用自然语言描述业务问题（而非技术术语），识别准确率越低。

### 3.2 执行层 Gap

| 维度 | 当前实现 | execute.md 目标 | 差距影响 |
|------|---------|----------------|---------|
| 执行模型 | LLM ReAct 自主决策工具调用顺序 | 外部 DAG Executor 拓扑排序 + asyncio 并行 | LLM 串行调用，无法并行执行无依赖的数据采集任务 |
| 执行计划可见性 | 黑盒（LLM 内部决策，不可预测） | 白盒 DAG（JSON 结构，可审计、可回放） | 无法提前知道 Agent 会调用哪些工具、以什么顺序 |
| 超时控制 | Agent 级别整体超时 | per-node 超时（每个 DAG 节点独立超时） | 单个慢查询拖垮整次分析，无法精准定位瓶颈 |
| 错误处理 | Agent 内部重试（LLM 级别） | DAG 节点级别错误隔离 + 部分成功 | 一个工具失败导致整次分析失败，无法返回已完成部分 |
| 数据流 | LLM 自行记忆上下文传递 | output_key 机制，节点间显式数据传递 | 数据传递依赖 LLM 记忆，可能丢失或曲解中间结果 |
| 执行确认 | 无 | requires_approval 标记高风险操作 | 当前无写操作问题不大，但扩展写操作时缺少安全门控 |

**核心问题**：当前的 P2PAgent 是"LLM 全权代理"模式——给 LLM 8 个工具和一段 prompt，由 LLM 自己决定调什么、调几次、什么顺序。这在简单场景下可行，但：
1. **不可预测**：同一个 query 多次执行可能走不同的工具调用路径
2. **不可并行**：LLM ReAct 天然是串行的（观察→思考→行动→观察…）
3. **不可审计**：事后只能从 trace 中反推执行了什么，无法事前确认

### 3.3 工具层 Gap

**当前 8 个工具**：
| 工具 | 类型 | 对应 execute.md |
|------|------|----------------|
| query_purchase_orders | 查询 | ✅ 一致 |
| query_receipts | 查询 | ⚠️ execute.md 叫 query_goods_receipts |
| query_invoices | 查询 | ⚠️ execute.md 叫 query_vendor_invoices |
| query_payments | 查询 | ✅ execute.md 未列入但合理存在 |
| run_three_way_match | 分析 | ✅ 一致 |
| run_price_variance_analysis | 分析 | ⚠️ execute.md 叫 calculate_ppv |
| run_payment_compliance_check | 合规 | ⚠️ execute.md 叫 validate_compliance |
| calculate_supplier_kpis | 分析 | ⚠️ execute.md 叫 get_vendor_scorecard |

**execute.md 中缺失的 7 个工具**：
| 缺失工具 | 类型 | 实现难度 | 是否阻塞 DAG |
|----------|------|---------|-------------|
| query_vendor_master | 查询 | 低（需 DB schema） | 是，vendor_agent 依赖 |
| query_material_master | 查询 | 低（需 DB schema） | 部分场景依赖 |
| calculate_spend_analysis | 分析 | 中（需聚合计算） | 是，spend_analysis_dag 依赖 |
| calculate_po_cycle_time | 分析 | 中（需时间计算） | 否，可后期补 |
| run_vendor_risk_scoring | 分析 | 高（需风险模型定义） | 否，Phase 4 |
| check_approval_limits | 合规 | 中（需审批规则配置） | 否，Phase 4 |
| check_blacklist | 合规 | 低（需黑名单表） | 否，Phase 4 |

**额外缺失**：
- `generate_summary_report`：execute.md 中由 report_agent 专用，当前报告由 LLM 直接生成 Markdown
- `generate_chart`：图表生成能力完全缺失

**核心问题**：当前工具命名与 execute.md 不一致会导致 DAG 模板和 LLM 生成的 DAG 都无法直接匹配。改造时需决定：统一到 execute.md 命名（破坏现有测试）还是在 Tool Registry 中做别名映射。

### 3.4 多 Agent 层 Gap

| Agent | execute.md 职责 | 当前状态 |
|-------|----------------|---------|
| p2p_agent | 三路匹配 + 价格差异核心 | ✅ P2PAgent 已覆盖（但承担了所有职责） |
| vendor_agent | 供应商绩效、黑名单、风险评分 | ❌ 不存在，相关工具在 P2PAgent 内 |
| finance_agent | 财务合规与报表 | ❌ 不存在 |
| rule_agent | 业务规则合规校验 | ❌ 不存在，合规工具在 P2PAgent 内 |
| report_agent | 报告聚合生成（DAG 终点） | ❌ 不存在，报告由 LLM 在 P2PAgent 内生成 |

**核心问题**：当前是"大一统 Agent"模式，P2PAgent 承担了所有 5 个 Agent 的职责。execute.md 的 DAG 设计中 `agent` 字段决定任务路由到哪个 Agent，但当前只有一个 Agent 可用。DAGValidator 的 TOOL_AGENT_MAP 约束（如 `generate_summary_report` 只能由 `report_agent` 执行）也无法生效。

### 3.5 API 协议层 Gap

| 维度 | 当前 | execute.md 目标 |
|------|------|----------------|
| AnalysisStatus 枚举 | success / partial_success / failed | 需新增 `need_clarification` |
| 响应字段 | 固定结构（anomalies / kpis / report_markdown） | 需新增 clarification_question / route_type / reasoning |
| 多轮对话 | 不支持（每次请求独立） | ClarificationRequired → 用户补充信息 → 继续分析 |
| 路由透明度 | 无 | 返回 route_type（STATIC/ADAPTED/DYNAMIC）+ reasoning |

**核心问题**：API 协议变更影响所有调用方。如果前端不做多轮对话适配，追问机制就无法落地。

### 3.6 自学习闭环 Gap

| 维度 | 当前 | execute.md 目标 |
|------|------|----------------|
| 案例沉淀 | 无（长期记忆存分析结论，非执行计划） | DAGCaseStore 存完整 DAG 定义到 Chroma |
| 案例检索 | 无 | Level 2 用 Embedding 语义相似度匹配历史案例 |
| 案例改编 | 无 | _adapt_dag_from_case 替换时间范围/实体参数 |
| 冷启动 | 不适用 | intent_seeds 预置种子库 |
| 去重 | 不适用 | query hash 去重，新案例覆盖旧案例 |

**核心问题**：当前的长期记忆存的是"分析结论"（Q&A 对），不是"执行计划"（DAG）。execute.md 的自学习闭环需要存的是 **可复用的执行计划**，而不是自然语言结论。这是两套完全不同的数据结构。

### 3.7 可观测性层 Gap

| 维度 | 当前 | execute.md 目标 |
|------|------|----------------|
| Trace 粒度 | Agent 级别（model span + tool span） | DAG 节点级别（每个任务独立 span） |
| 路由决策 trace | 无 | 记录命中层级（Level 1/2/3）、置信度、reasoning |
| DAG 执行 trace | 不适用 | 每个节点的状态（pending/running/done/failed）、耗时、输出 |

**核心问题**：当前的 TimingMiddleware 只在 LangChain 中间件层面采集 span，如果切换到 DAG Executor，需要新的 span 注入机制。但现有 middleware 架构可以复用，只需在 Executor 层新增 span 类型。

---

## 四、execute.md 架构设计本身的问题

在对比 Gap 的同时，也需要审视 execute.md 设计本身的合理性：

### 4.1 信号提取的 LLM 调用开销
execute.md 要求**每个请求都先调一次 LLM** 提取 QuerySignal。对于"分析 Q1 三路匹配异常"这类 Level 1 就能命中的简单请求，多一次 LLM 调用（+0.5~2s 延迟 + API 成本）没有必要。建议 Level 1/2 命中时不调 LLM，仅 Level 3 才做完整信号提取。

### 4.2 DAG 模板与 Agent ReAct 模式的冲突
当前 P2PAgent 使用 LangChain `create_agent` 的 ReAct 模式，LLM 自主决定工具调用。execute.md 的 DAG Executor 完全绕过了 LLM 的 ReAct 循环，直接按拓扑顺序调用工具函数。这两种模式是**互斥的**：
- **DAG 模式**：执行计划确定 → 按计划调工具 → 结果汇总
- **ReAct 模式**：LLM 观察 → 思考 → 选工具 → 执行 → 再观察…

如果采用 DAG 模式，现有 P2PAgent 的 `create_agent` + `ainvoke` 架构需要**根本性改造**——Agent 不再自主调工具，而是退化为"工具执行器"。这是最大的架构断裂点。

### 4.3 多 Agent 的必要性
execute.md 定义了 5 种 Agent，但在 DAG 模式下 Agent 本身退化为工具执行容器。`agent` 字段更像是工具的归属分组标记，而非真正独立的 LLM Agent 实例。是否真的需要 5 个独立的 LLM Agent，还是只需要一个 Tool Registry + 分组标签？

### 4.4 RAG 历史案例的质量控制
execute.md 的 Level 2 依赖历史案例的 Embedding 相似度（阈值 0.78）。但：
- 早期案例质量不稳定（可能是降级兜底的最小 DAG）
- 相似度 0.78 阈值是否合理需要真实数据调参
- 用户反馈（`user_feedback` 字段）当前无采集机制

---

## 五、P2PAgent 内部的深层架构问题

### 5.1 结果解析的脆弱性（agent.py:451-462）
当前从 LLM 输出中 `json.loads` 尝试解析结构化结果。这是极其脆弱的：
- LLM 输出格式不可控，可能夹杂 Markdown 代码块标记
- 解析失败时 anomalies 和 summary 都为空，AnalysisResult 退化为纯文本报告
- execute.md 的 DAG 模式天然避免了这个问题：工具函数直接返回结构化数据，无需从 LLM 文本中逆向解析

### 5.2 analysis_type 的双重来源冲突（orchestrator.py:155-158）
```python
parsed_type, parsed_params = self._intent_parser.parse(request.query)
analysis_type = request.analysis_type or parsed_type
```
`analysis_type` 在 Orchestrator 中解析，但 P2PAgent.analyze() 完全不使用它——Agent 内部由 LLM 自主决定执行什么工具。IntentParser 的输出实际上被 P2PAgent 忽略了。execute.md 的 DAG 模式中，路由结果直接决定执行计划，不存在这种"解析了但不用"的尴尬。

### 5.3 Orchestrator 与 Agent 职责边界模糊
- Orchestrator 做意图解析 + 参数提取，但结果只用于填充 AnalysisResult 元数据
- P2PAgent 接收 query 后自己重新理解意图、自己决定调什么工具
- 两者之间有大量冗余的"理解用户意图"工作

execute.md 的架构清晰划分：Orchestrator 负责"做什么"（DAG），Executor 负责"怎么做"（执行），Agent 退化为工具容器。

---

## 六、改造可行性评估

### 6.1 Phase 1：三级意图路由（不改执行层）
- **改动范围**：新增 `signal.py` + `router.py`，修改 `orchestrator.py` 调用入口
- **风险**：低。执行层零改动，P2PAgent 不受影响
- **工作量**：2~3 天
- **前置依赖**：无
- **关键决策**：信号提取是否 Level 3 才调 LLM（建议是）

### 6.2 Phase 2：DAG 执行层
- **改动范围**：新增 `dag/` 包（executor / validator / templates / generator），重构 `orchestrator.py`
- **风险**：中高。这是**最大的架构断裂点**——从 ReAct 模式切换到 DAG 模式
- **工作量**：5~7 天
- **前置依赖**：Phase 1 完成 + 缺失工具至少补存根
- **关键决策**：
  - 工具命名是否对齐 execute.md（影响 DAG 模板和测试）
  - DAG 模式和 ReAct 模式是否共存（建议过渡期共存：静态 DAG 命中走 Executor，Level 3 兜底仍走 P2PAgent ReAct）

### 6.3 Phase 3：自学习闭环
- **改动范围**：新增 `case_store.py`，Level 2 路由对接 Chroma
- **风险**：低，纯增量
- **工作量**：1~2 天
- **前置依赖**：Phase 2 完成

### 6.4 Phase 4：多 Agent + 追问机制
- **改动范围**：API schema 变更、新增 Agent 类、ClarificationRequired 流程
- **风险**：高。API 协议变更影响所有调用方
- **工作量**：5~8 天
- **前置依赖**：Phase 1/2/3 稳定

### 6.5 实施排序建议

```
Phase 1（意图路由）──┐
                     ├──→ Phase 2（DAG 执行层）──→ Phase 3（自学习）──→ Phase 4（多 Agent）
缺失工具补存根 ──────┘
```

- Phase 1 和"缺失工具补存根"**并行启动**
- Phase 2 是核心，也是风险最高的阶段，建议先做一个 PoC（仅 three_way_match_dag 走 DAG 模式，其余仍走 ReAct）验证可行性
- Phase 4 建议单独立项，涉及 API 协议变更需要和前端协调

---

## 七、关键决策（已确认）

| # | 决策项 | 结论 | 理由 |
|---|--------|------|------|
| 1 | DAG vs ReAct 模式 | **共存**：Level 1/2 命中走 DAG Executor，Level 3 兜底走 P2PAgent ReAct | 渐进迁移，降低风险；确定性场景用确定性执行，开放性场景保留 LLM 灵活性 |
| 2 | LLM 信号提取时机 | **方案 B（渐进式）**：Level 1/2 用正则+规则填充轻量 QuerySignal，仅 Level 3 调 LLM | 节省 60%+ 请求的 LLM 调用；Level 1/2 已命中确定性路由，ambiguity_score 无实际影响 |
| 3 | Agent 数量 | **2 个 Agent + 1 个 Tool Registry** | P2PAgent（ReAct 兜底）+ ReportAgent（DAG 终点汇总报告）+ Tool Registry（DAG 节点直接调函数）；execute.md 的 5 个 agent 标签保留为元数据 |
| 4 | 工具命名 | **保持现有命名 + Tool Registry 别名映射** | 不破坏现有 19 个测试；Registry 同时注册两个 key 指向同一 callable |
| 5 | 缺失工具策略 | **真实实现 2 + 存根 5 + ReportAgent 1 + 延后 1** | 见下方详表 |
| 6 | 种子库管理 | **方案 B（config/intent_seeds.yaml）** | 种子是业务数据非代码逻辑，YAML 语义清晰，符合项目 config.yaml 风格 |

### 缺失工具详表

| 工具 | 策略 | 说明 |
|------|------|------|
| `query_vendor_master` | ✅ 真实实现 | `ap_suppliers` 表已存在，只缺 Repository 方法 |
| `calculate_spend_analysis` | ✅ 真实实现 | `po_headers` + `po_lines` 已存在，需聚合 SQL |
| `query_material_master` | 🔲 存根 | 缺物料主数据表（MTL_SYSTEM_ITEMS），Phase 4 新建表 |
| `calculate_po_cycle_time` | 🔲 存根 | 表已存在可做，但非四大核心场景，延后 |
| `run_vendor_risk_scoring` | 🔲 存根 | 缺风险评分模型定义 |
| `check_approval_limits` | 🔲 存根 | 缺审批限额配置表 |
| `check_blacklist` | 🔲 存根 | 缺黑名单表 |
| `generate_summary_report` | 📎 ReportAgent | 由 ReportAgent 承担，调 LLM 汇总 DAG 各节点结果生成 Markdown |
| `generate_chart` | ⏸️ 延后 | 缺图表库依赖 + 图片存储方案，非核心能力 |

---

## 八、最终架构蓝图

### 请求处理流程

```
用户 query
  │
  ▼
[Level 1] 关键词命中率评分（正则填充轻量 QuerySignal）
  命中 → 加载静态 DAG 模板 → DAG Executor 并行执行 → ReportAgent 汇总 → 返回
  │
  ▼ 未命中
[Level 2] Chroma intent_seeds 语义匹配（config/intent_seeds.yaml 种子库）
  相似度 > 0.80 → 加载对应 DAG 模板 → DAG Executor → ReportAgent → 返回
  │
  ▼ 未命中
[Level 3] LLM 完整信号提取 → P2PAgent ReAct 自主执行 → 返回
```

### 组件架构

```
core/orchestrator/
├── signal.py              # QuerySignal dataclass
├── router.py              # IntentRouter（Level 1/2/3 三级路由）
├── orchestrator.py        # 主编排器（改造）
└── dag/
    ├── executor.py        # asyncio DAGExecutor
    ├── validator.py       # DAGValidator
    ├── templates.py       # 4 个静态 DAG 模板
    ├── generator.py       # Level 3 DynamicDAGGenerator（Phase 2 后期）
    ├── case_store.py      # DAGCaseStore（Phase 3）
    └── registry.py        # Tool Registry（含别名映射）

config/
└── intent_seeds.yaml      # Level 2 种子问题库

modules/p2p/
├── agent.py               # P2PAgent（保留，Level 3 兜底）
├── report_agent.py        # ReportAgent（新增，DAG 终点）
└── tools.py               # 现有 8 工具 + 新增 2 真实 + 5 存根
```

### 实施路径

```
Phase 1（意图路由）───┐
                      ├──→ Phase 2（DAG 执行层）──→ Phase 3（自学习）──→ Phase 4（追问 + 补全工具）
缺失工具（2真实+5存根）┘
```
