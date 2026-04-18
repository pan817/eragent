# 技术债 #8 分析：DATA_LOOKUP 查询缺少 DAG 模板，100% 走 ReAct

> **分析目的**：判定技术债 #8 是否确实是问题，给出明确结论、分析过程和解决方案。
>
> **本文档性质**：纯分析文档，不包含代码改动。

| 项 | 值 |
|---|---|
| 关联技术债 | [agent_issue.md #8](agent_issue.md) |
| 创建日期 | 2026-04-19 |
| 分析范围 | `core/orchestrator/orchestrator.py` 路由决策 + `core/orchestrator/router/__init__.py` 意图识别 + `modules/p2p/tools/query.py` 查询工具 + `modules/p2p/dag_templates.py` DAG 模板 |

---

## 目录

1. [问题定位：DATA_LOOKUP 完整代码链路](#1-问题定位data_lookup-完整代码链路)
2. [现状评估：ReAct 路径下的实际表现](#2-现状评估react-路径下的实际表现)
3. [问题确认结论](#3-问题确认结论)
4. [利弊权衡：DAG 模板方案 vs 维持 ReAct](#4-利弊权衡dag-模板方案-vs-维持-react)
5. [推荐解决方案](#5-推荐解决方案)
6. [实施路线图](#6-实施路线图)

---

## 1. 问题定位：DATA_LOOKUP 完整代码链路

DATA_LOOKUP 查询从用户输入到最终响应的完整执行路径如下：

### 1.1 意图识别阶段（router）

DATA_LOOKUP 在三级路由中有 **两个入口**：

**入口 A — L1 关键词命中**（零延迟）：

- 位置：[router/\_\_init\_\_.py:597-608](../core/orchestrator/router/__init__.py#L597-L608)
- 触发条件：query 未命中任何 `_RULE_LIBRARY` 分析规则，但满足 `_looks_like_data_lookup()` — 即同时包含 lookup 动词（"查/列出/看看"等）和业务实体（"po/发票/付款"等），或包含修饰词（"最新/最近"）+ 实体
- 置信度：固定 0.7（`_LOOKUP_L1_CONFIDENCE`）
- 示例：`"查最新的PO"` `"列出SUP-001的发票"` `"看看最近的付款记录"`

**入口 B — L3 LLM 分类**（0.5~2s 延迟）：

- 位置：[router/\_\_init\_\_.py:1172-1182](../core/orchestrator/router/__init__.py#L1172-L1182)
- 触发条件：L1、L2、L2.5 均未命中，LLM 返回 `intent_kind=data_lookup`
- 置信度：由 LLM 自行判定（通常 0.8~0.95）
- 示例：`"这单多少钱"` `"帮我看看应付明细"` 等口语化/间接表述

> **L2 种子库**也支持 `data_lookup` sentinel 类目（[router/\_\_init\_\_.py:830-847](../core/orchestrator/router/__init__.py#L830-L847)），但需要 `intent_seeds.yaml` 中有对应种子条目。

### 1.2 路由决策阶段（orchestrator）

关键决策点在 [orchestrator.py:538-549](../core/orchestrator/orchestrator.py#L538-L549)：

```python
# TECH-DEBT(#8): DATA_LOOKUP 强制走 ReAct，缺少 Lookup DAG 模板
is_data_lookup = signal.intent_kind == _IntentKindRoute.DATA_LOOKUP
# ...
if is_recall or is_data_lookup or low_confidence:
    use_dag = False
```

`is_data_lookup` 与 `is_recall`、`low_confidence` 并列为三个**强制走 ReAct** 的条件。一旦 `intent_kind=DATA_LOOKUP`，无论置信度多高、是否有实体、L1 还是 L3 命中，都无条件走 ReAct。

### 1.3 output_mode 自适应（orchestrator）

[orchestrator.py:589-591](../core/orchestrator/orchestrator.py#L589-L591)：

```python
if effective_output_mode == "auto":
    if is_data_lookup:
        effective_output_mode = "chat"
```

DATA_LOOKUP 会自动获得 `output_mode=chat`，对应的 prompt 后缀为（[prompts.py:61-65](../core/orchestrator/prompts.py#L61-L65)）：

> "请用自然简洁的语句直接回答；不要加顶级标题、不要写摘要/建议段落；总字数不超过 200 字。"

### 1.4 ReAct 执行阶段

- 构造单节点 agent DAG（[orchestrator.py:721-741](../core/orchestrator/orchestrator.py#L721-L741)），`type=agent`
- DAGExecutor 识别到 `type=agent` 后委托给 P2PAgent
- P2PAgent 拿到带有 `chat` output_mode 的 prompt，自主决定调用哪个 `query_*` 工具
- 工具返回 JSON（经 `_clip_and_dump` 裁剪），Agent 基于 JSON 生成自然语言回复

### 1.5 涉及的 query 工具

| 工具 | 位置 | 用途 |
|------|------|------|
| `query_purchase_orders` | [tools/query.py:14](../modules/p2p/tools/query.py#L14) | 按 supplier_id/status/po_number/days 查询 PO |
| `query_receipts` | [tools/query.py:45](../modules/p2p/tools/query.py#L45) | 按 po_number/supplier_id/days 查询收货记录 |
| `query_invoices` | [tools/query.py:73](../modules/p2p/tools/query.py#L73) | 按 po_number/supplier_id/status/invoice_number/days 查询发票 |
| `query_payments` | [tools/query.py:107](../modules/p2p/tools/query.py#L107) | 按 invoice_number/supplier_id/payment_number/days 查询付款 |

所有 query 工具返回 JSON 字符串，无 LLM 调用，纯数据库查询（200-500ms）。

---

## 2. 现状评估：ReAct 路径下的实际表现

### 2.1 延迟分析

DATA_LOOKUP 走 ReAct 的典型延迟构成：

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 意图路由（L1 命中） | ~0ms | 关键词匹配，零延迟 |
| 意图路由（L3 兜底） | 0.5~2s | 需要一次 LLM 调用 |
| ReAct 第 1 轮推理 | 1~3s | LLM 决定调用哪个 query 工具、填什么参数 |
| query 工具执行 | 0.2~0.5s | 纯数据库查询 |
| ReAct 第 2 轮推理 | 1~3s | LLM 阅读 JSON 结果，生成自然语言回复 |
| **总计** | **2.7~8.5s** | 典型 4~6s |

若走 DAG 直调工具（假设有模板），理论延迟：

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 意图路由 | 0~2s | 同上 |
| query 工具执行 | 0.2~0.5s | 直接调用，无需 LLM 决策 |
| 格式化输出 | ~0ms | 纯模板渲染（无 LLM） |
| **总计** | **0.2~2.5s** | 典型 0.5~1s（L1）或 2~3s（L3） |

**结论**：ReAct 路径比理论最优多 2~5s，主要来自两轮 LLM 推理。

### 2.2 Token 消耗分析

| 路径 | 预估 token | 组成 |
|------|------------|------|
| ReAct | 3000~5000 | system prompt (~1500) + 用户消息 (~200) + 第 1 轮 LLM 输出 (~300) + 工具结果 (~500~2000) + 第 2 轮 LLM 输出 (~200) |
| DAG 直调（无 LLM） | 0 | 工具直调 + 模板渲染，零 LLM 消耗 |

**结论**：每次 DATA_LOOKUP 浪费约 3000~5000 token。按日均 100 次 lookup 估算，月增 ~1000 万 token 消耗。

### 2.3 输出可控性

**现状优点（已有的防护）**：
- `output_mode=chat` 已自动注入，prompt 限制 200 字以内、不加标题/摘要段落
- 这意味着 LLM 输出不会像 ANALYSIS 路径那样生成"4 段报告"

**现状缺点（实际风险）**：
- LLM **可能多调工具**：用户问"查最新PO"，Agent 可能先调 `query_purchase_orders` 再调 `query_vendor_master`，产生不必要的多轮交互
- LLM **可能解读过度**：把纯数据查询变成分析评论（如"该供应商表现良好"），偏离用户"只看数据"的预期
- LLM **参数填充不确定**：Agent 自行决定 `days` 参数，可能与 orchestrator 已解析的 `time_range_days` 不一致
- 输出格式**不稳定**：有时返回自然语言，有时返回 markdown 表格，取决于 LLM 发挥

### 2.4 可缓存性

- **ReAct 路径**：无法走 `DAGCaseStore`（批 4）复用历史 DAG 结果，每次都是全新 LLM 推理
- **DAG 路径（假设有模板）**：工具调用参数确定，可通过 case_store 缓存历史结果

### 2.5 可观测性

- ReAct 路径的 trace 为单节点 `agent` span，内部工具调用嵌套在 LangChain 中间件 trace 中
- DAG 路径的 trace 为多节点拓扑，每个工具有独立 span，可观测性更好

---

## 3. 问题确认结论

### 3.1 判定：确实是问题

**结论：DATA_LOOKUP 100% 走 ReAct 确实是一个需要解决的问题。**

### 3.2 严重等级：中等（P2）

不是阻塞性缺陷，但在生产环境下会持续产生可量化的性能损耗和用户体验下降。

### 3.3 判定依据

| 维度 | 评估 | 权重 |
|------|------|------|
| **延迟浪费** | 每次多 2~5s（两轮 LLM 推理），DATA_LOOKUP 是高频场景 | 高 |
| **token 浪费** | 每次浪费 3000~5000 token，本可零 LLM 消耗完成 | 中 |
| **输出不可预测** | LLM 可能多调工具、解读过度、参数不一致 | 中 |
| **不可缓存** | 无法走 case_store 复用历史结果 | 低 |
| **功能正确性** | ReAct 路径在功能上是正确的，能返回正确数据 | — |

### 3.4 为什么不是 P1

- **功能不受影响**：DATA_LOOKUP 走 ReAct 能正确返回数据，不存在功能缺陷
- **output_mode=chat 已生效**：不会产生"4 段报告"格式的过重输出
- **现有防护已部分缓解**：`_clip_and_dump` 裁剪了工具输出，chat prompt 限制了 200 字

### 3.5 为什么不是 P3

- **延迟感知明显**：用户问"查最新PO"等待 4~6s 是不可接受的，这类查询用户期望亚秒级响应
- **浪费可量化**：token 成本随查询量线性增长，是纯浪费
- **与技术债 #1（路由命中率优化）形成矛盾**：即便优化了路由使更多查询在 L1 命中 DATA_LOOKUP，最终仍然走 ReAct，优化收益被抵消

---

## 4. 利弊权衡：DAG 模板方案 vs 维持 ReAct

### 4.1 方案 A：维持现状（DATA_LOOKUP → ReAct）

| 优点 | 缺点 |
|------|------|
| 零改动成本 | 每次多 2~5s 延迟（两轮 LLM 推理） |
| 天然处理模糊/复杂查询（如"这单怎么回事"） | 每次浪费 3000~5000 token |
| LLM 能灵活组合多个工具（如同时查 PO + 发票） | 输出格式不稳定，LLM 可能多调工具或解读过度 |
| 无需维护额外 DAG 模板 | 无法走 case_store 缓存 |

### 4.2 方案 B：技术债 #8 原方案（5 个 Lookup DAG 模板 + lookup_formatter 工具）

原方案来自 [agent_issue.md #8](agent_issue.md)，建议新增 5 个 2 节点 Lookup DAG 模板（PO/Invoice/Payment/Receipt/Supplier），每个仅 `query_* → lookup_formatter`。

| 优点 | 缺点 |
|------|------|
| 延迟降至 0.2~0.5s（零 LLM） | 需要新增 `lookup_formatter` 工具（不存在） |
| token 消耗归零 | 5 个模板 + 1 个 formatter = 6 个新组件需维护 |
| 输出完全确定性（模板渲染） | **刚性映射问题**：需要在路由阶段确定"查的是 PO 还是发票"，模糊查询（"看看这单"）无法路由 |
| 可走 case_store 缓存 | 无法处理跨实体查询（"SUP-001 最近的 PO 和发票"） |
| | 需要解决 DAG 路径 `output_mode=chat` 被强制升为 `brief` 的矛盾（[orchestrator.py:596-601](../core/orchestrator/orchestrator.py#L596-L601)） |
| | 丧失 ReAct 灵活性：新增查询场景需要新增模板 |

### 4.3 方案 C：轻量级 Lookup 快捷路径（推荐）

在 orchestrator 中为 DATA_LOOKUP 新增一条**直调工具**的快捷路径，不经过 DAGExecutor 也不经过 ReAct Agent：

- 根据路由阶段已提取的实体类型（`po_number` / `invoice_number` / `supplier_id` 等）确定调用哪个 `query_*` 工具
- 直接调用工具，拿到 JSON 结果
- 用简单的 Python 格式化函数渲染为 Markdown 表格
- 无法确定工具时，降级回 ReAct（保留兜底）

| 优点 | 缺点 |
|------|------|
| 延迟降至 0.2~0.5s（明确实体时） | 仍需新增格式化函数（但比 lookup_formatter tool 简单） |
| token 消耗归零（明确实体时） | 模糊查询仍走 ReAct（但这正是 ReAct 擅长的场景） |
| 改动集中在 orchestrator 一个函数 | 不走 DAGExecutor，不享受 DAG trace 的结构化可观测性 |
| 不需要 5 个 DAG 模板 | — |
| 保留 ReAct 兜底灵活性 | — |
| 不触及 `output_mode` chat→brief 矛盾 | — |

### 4.4 三方案对比总结

| 维度 | A（维持现状） | B（5 DAG 模板） | C（快捷路径） |
|------|:---:|:---:|:---:|
| 改动量 | 无 | 大（6 个新组件） | 小（1 个函数 + 1 个 formatter） |
| 明确查询延迟 | 4~6s | 0.2~0.5s | 0.2~0.5s |
| 模糊查询处理 | 好（ReAct） | 差（无法路由） | 好（降级 ReAct） |
| 跨实体查询 | 好（ReAct） | 差（需新模板） | 好（降级 ReAct） |
| token 消耗 | 3000~5000 | 0 | 0（明确时）/ 3000~5000（降级时） |
| 维护成本 | 无 | 高 | 低 |
| 可缓存性 | 无 | 有 | 可扩展 |

---

## 5. 推荐解决方案

**推荐方案 C（轻量级 Lookup 快捷路径）**，分两期实施。

### 5.1 第一期：快赢——直调工具快捷路径（覆盖 80% DATA_LOOKUP）

**核心思路**：对于路由阶段已提取到明确实体的 DATA_LOOKUP 查询，在 orchestrator 中直接调用对应 `query_*` 工具，跳过 ReAct Agent。

#### 5.1.1 实体 → 工具映射规则

路由阶段 `_extract_params()` 已从 query 中正则提取了实体。根据已有实体确定调用的工具：

| 已提取实体 | 调用工具 | 说明 |
|-----------|---------|------|
| `po_number` | `query_purchase_orders(po_number=...)` | 查询特定 PO |
| `invoice_number` | `query_invoices(invoice_number=...)` | 查询特定发票 |
| `payment_number` | `query_payments(payment_number=...)` | 查询特定付款单 |
| `receipt_number` | **降级 ReAct** | 见下方缺口说明 |
| `supplier_id`（无其他实体） | `query_vendor_master(vendor_ids=...)` + `query_purchase_orders(supplier_id=...)` | 先查供应商主数据，再查其 PO 列表 |
| 无任何实体 | **降级 ReAct** | 如"查最新的PO"，需要 LLM 判断参数 |

优先级：`po_number > invoice_number > payment_number > supplier_id`（与现有实体维度 DAG 模板的优先级一致）。

#### 5.1.2 已知缺口与处理策略

| 缺口 | 原因 | 处理策略 |
|------|------|---------|
| `receipt_number` 无法直调 | `query_receipts` 工具只接受 `po_number` 和 `supplier_id` 参数，**没有 `receipt_number` 参数** | 第一期降级 ReAct；后续视需求在 `query_receipts` 中补充 `receipt_number` 参数后再纳入快捷路径 |
| `supplier_id` 查询目标不确定 | 用户可能想查供应商主数据（`query_vendor_master`），也可能想查其 PO 或发票 | 默认同时调 `query_vendor_master` + `query_purchase_orders`，合并输出 |
| 合同（contract）无 query 工具 | `LOOKUP_ENTITIES` 词表包含"合同/contract"，但系统中没有 `query_contracts` 工具，且缺少合同数据模型、Repository 查询方法和模拟数据 | 第一期降级 ReAct；后续需先补齐数据层（ORM 模型 + Repository + mock 数据），再新建 `query_contracts` 工具并纳入快捷路径 |
| 物料（material）仅 stub | `LOOKUP_ENTITIES` 词表包含"物料/material"，`query_material_master` 当前为 stub 实现 | 降级 ReAct（stub 会返回"功能尚未上线"提示） |

#### 5.1.3 lookup_formatter：JSON → Markdown 表格

新增一个纯 Python 格式化函数（非 @tool），将 `_clip_and_dump` 返回的 JSON 字符串转为 Markdown 表格：

- 输入：JSON 字符串（`query_*` 工具的返回值）
- 输出：Markdown 表格字符串 + 一句话摘要（如"共 5 条采购订单，最近一条为 PO-2024-001"）
- 不需要 LLM 调用
- 表格列根据实体类型预定义（PO 展示 po_number/supplier/amount/status/date，发票展示 invoice_number/po_number/amount/status 等）

#### 5.1.4 配置开关

在 `IntentRoutingSettings` 中新增开关，控制有明确实体的 DATA_LOOKUP 走快捷路径（直调工具）还是走 ReAct：

```yaml
# config.yaml
intent_routing:
  lookup_shortcut_enabled: true   # true=直调工具（零 LLM），false=走 ReAct（保持原行为）
```

- **`true`（默认）**：有明确实体时直调 `query_*` 工具 → `lookup_formatter` → 返回结果，无实体时降级 ReAct
- **`false`**：所有 DATA_LOOKUP 一律走 ReAct，与改动前行为完全一致

开关的意义：
1. **灰度上线**：先在测试环境开启，对比延迟和输出质量后再推生产
2. **快速回滚**：如果快捷路径的格式化输出不满足业务需求，改一行配置即可恢复
3. **按场景选择**：某些客户可能偏好 ReAct 的自然语言回复风格，可按部署关闭

#### 5.1.5 orchestrator 改动

在 [orchestrator.py:548](../core/orchestrator/orchestrator.py#L548) 的 `if is_data_lookup` 分支中，增加"开关判断 + 快捷路径优先 + ReAct 兜底"的决策：

```
if is_data_lookup:
    if lookup_shortcut_enabled and 有明确实体:
        → 直调 query_* 工具 → lookup_formatter → 返回结果
    else:
        → use_dag = False（走 ReAct，保持现有行为）
```

#### 5.1.6 output_mode 处理

快捷路径不经过 ReportAgent，不存在 `chat→brief` 升级矛盾。直接使用 `chat` 格式输出 Markdown 表格 + 摘要。

### 5.2 第二期：增强——无实体 DATA_LOOKUP 的关键词推断

覆盖第一期降级到 ReAct 的场景（无明确实体，如"查最新的PO"/"列出最近的发票"）。

**思路**：从 query 中提取实体类型关键词（"PO/采购订单" → PO 类查询、"发票" → 发票类查询），结合 `_LOOKUP_ENTITIES` 词表，推断应调用哪个 `query_*` 工具，参数取默认值（`days=30`）。

| query 关键词 | 推断工具 | 默认参数 |
|-------------|---------|---------|
| po/采购单/采购订单/订单 | `query_purchase_orders` | days=30 |
| 发票/invoice/inv | `query_invoices` | days=30 |
| 付款/付款单/payment/应付 | `query_payments` | days=30 |
| 收货/收货单/receipt/rcv/gr | `query_receipts` | days=30 |
| 供应商/supplier/sup | `query_purchase_orders` | days=30, 按 supplier 聚合 |
| 无法推断 | **降级 ReAct** | — |

### 5.3 与技术债 #8 原方案的差异

| 项 | 原方案（5 DAG 模板） | 推荐方案（快捷路径） |
|---|---|---|
| 新增组件数 | 5 个 DAG 模板 + 1 个 lookup_formatter tool | 1 个 orchestrator 方法 + 1 个 formatter 函数 |
| 是否经过 DAGExecutor | 是 | 否（直调工具） |
| 模糊查询处理 | 无法处理，需全部路由到具体模板 | 降级 ReAct |
| 跨实体查询 | 需新增组合模板 | 降级 ReAct |
| output_mode 矛盾 | 需解决 chat→brief 问题 | 不存在此问题 |
| 维护成本 | 每新增实体类型需新增模板 | 映射表新增一行 |

---

## 6. 实施路线图

### 6.1 第一期：快捷路径（预计改动 3 个文件）

| 步骤 | 内容 | 涉及文件 | 验收标准 |
|------|------|---------|---------|
| 1 | `IntentRoutingSettings` 新增 `lookup_shortcut_enabled: bool = True` 配置项，`config.yaml` 同步新增 | `config/settings.py` + `config/config.yaml` | 配置项可读取，默认 `True` |
| 2 | 新增 `lookup_formatter` 函数：JSON → Markdown 表格 + 摘要 | `modules/p2p/tools/_output.py` 或新建 `core/orchestrator/lookup.py` | 单元测试覆盖 5 种实体类型的格式化输出 |
| 3 | orchestrator 中 DATA_LOOKUP 分支增加"开关 + 有实体 → 直调工具"快捷路径 | `core/orchestrator/orchestrator.py` | 开关开启 + 有实体 → 快捷路径；开关关闭 → ReAct |
| 4 | 快捷路径增加 trace span 记录（`lookup_shortcut`） | `core/orchestrator/orchestrator.py` | trace 中可区分 lookup 快捷路径和 ReAct 路径 |
| 5 | 补充单元测试 | `tests/unit/test_orchestrator.py` 或新建 `tests/unit/test_lookup.py` | 覆盖：开关开 + 有 PO → 快捷路径 / 开关关 → ReAct / 无实体 → ReAct 降级 |

### 6.2 第二期：关键词推断（预计改动 2 个文件）

| 步骤 | 内容 | 涉及文件 | 验收标准 |
|------|------|---------|---------|
| 6 | 新增实体类型关键词 → 工具映射表 | `modules/p2p/intent_rules.py` | 映射表覆盖 PO/发票/付款/收货/供应商 5 类 |
| 7 | orchestrator 中无实体 DATA_LOOKUP 增加关键词推断逻辑（受同一开关控制） | `core/orchestrator/orchestrator.py` | "查最新PO" 不走 ReAct，延迟 < 1s |
| 8 | 补充单元测试 | `tests/unit/test_lookup.py` | 覆盖：关键词命中 → 快捷路径 / 关键词不明 → ReAct 降级 |

### 6.3 完成后清理

| 步骤 | 内容 |
|------|------|
| 9 | 移除 `orchestrator.py:538` 的 `# TECH-DEBT(#8)` 注释 |
| 10 | 将 `agent_issue.md` #8 条目移到"已修复"区，更新修复方案描述 |
| 11 | commit message 引用 `#8`，格式：`fix(#8): DATA_LOOKUP 快捷路径，有实体时跳过 ReAct` |

### 6.4 回滚策略

快捷路径通过步骤 1 新增的 `lookup_shortcut_enabled` 配置开关控制。将 `config.yaml` 中 `intent_routing.lookup_shortcut_enabled` 设为 `false` 即可让所有 DATA_LOOKUP 恢复走 ReAct，与改动前行为完全一致，无需回滚代码。

### 6.5 预期效果

| 指标 | 改动前 | 第一期后 | 第二期后 |
|------|--------|---------|---------|
| 有实体 DATA_LOOKUP 延迟 | 4~6s | **< 1s** | < 1s |
| 无实体 DATA_LOOKUP 延迟 | 4~6s | 4~6s（仍走 ReAct） | **< 1s**（关键词可推断时） |
| DATA_LOOKUP token 消耗 | 3000~5000/次 | **0**（有实体时） | **0**（可推断时） |
| ReAct 降级率 | 100% | ~25%（无实体 + receipt_number + 合同/物料） | ~10%（仅完全模糊 + 不支持的实体类型） |
