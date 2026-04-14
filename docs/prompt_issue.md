# Prompt 审视与优化建议

本文档对 eragent 当前 LLM 调用链路上的 prompt 进行逐项审视，指出问题点与优化建议。**本轮仅分析，不含代码改动。**

## 现有 prompt 清单

| # | 调用路径 | prompt 位置 | 触发模型 |
|---|---|---|---|
| 1 | DAG 终点汇总 | `modules/p2p/report_agent.py:35` `_REPORT_PROMPT` | ReportAgent LLM |
| 2 | L3 意图分类 | `core/orchestrator/router.py:571` `_LLM_CLASSIFY_PROMPT` | Router LLM |
| 3 | ReAct 全链路 | `modules/p2p/prompts.py:132` `build_system_prompt` | P2P Agent LLM（含 tool-calling） |
| 4 | 输出模式附加指令 | `core/orchestrator/orchestrator.py:45` `_OUTPUT_MODE_PROMPTS` | 拼接到 ReportAgent / P2P Agent |

---

## 1. ReportAgent `_REPORT_PROMPT`

### 现状
```
你是 ERP 采购分析系统的报告生成器。根据以下分析工具的输出，生成一份结构化的 Markdown 分析报告。

## 分析场景
{scenario}

## 工具输出数据
{outputs_text}

## 报告要求
1. 使用中文
2. 包含：摘要、关键发现、详细数据、建议措施
3. 对异常标注严重等级（HIGH/MEDIUM/LOW）
4. 提供具体数据支撑（单据号、金额、偏差比例）
5. 给出可操作的改进建议
6. 如果数据为空或工具返回空结果，说明无异常发现

请直接输出 Markdown 报告：
```

### 问题点

| # | 问题 | 严重度 | 说明 |
|---|---|---|---|
| 1.1 | **没有约束"不要输出思考过程 / 前言"** | 中 | 虽然 API 侧已关 thinking，但部分模型仍会先输出"好的，以下是分析报告："之类的导语。进到 `report_markdown` 后影响渲染质量 |
| 1.2 | **"建议措施"缺少边界** | 中 | 没有说明改进建议应基于哪些数据事实。LLM 容易编造出工具输出里没有的建议（常见幻觉：虚构供应商谈判策略、不存在的系统改造项） |
| 1.3 | **严重等级判定依据缺失** | 高 | prompt 只要求"标注 HIGH/MEDIUM/LOW"，没有给出判定规则。CLAUDE.md 里明确定义了"超容差 2 倍或金额 > 50 万为 HIGH"，但这条关键业务规则**没传进 prompt**。结果：等级判定完全靠模型自由发挥，不同查询同类异常可能打出不同等级 |
| 1.4 | **数据引用格式不一致风险** | 中 | 要求"提供具体数据支撑（单据号、金额、偏差比例）"但没给格式示例。不同报告里单据号时而裸写、时而反引号、时而加粗 |
| 1.5 | **空数据分支语义模糊** | 低 | "说明无异常发现"——当工具因参数错误返回空 vs 真正无异常时，LLM 无法区分，容易误报"无异常" |
| 1.6 | **`outputs_text` 里的 JSON 被截断到 3000 字符**（[report_agent.py:91](../modules/p2p/report_agent.py#L91)） | 中 | 没有告诉 LLM "数据可能被截断"，LLM 可能把截断尾巴当成完整数据做出错误结论。截断位置在 JSON 中间时更危险——会破坏结构化读取 |
| 1.7 | **缺少"不确定"的表达指引** | 低 | 没有允许 LLM 在数据不足时输出"数据不足，建议补充 XX"；会被迫硬编出结论 |
| 1.8 | **没有 token 预算控制** | 低 | `outputs_text` 是 N 个节点各 3000 字符拼接，DAG 节点多时 prompt 可轻松超过 20k tokens。无配额保护 |

### 优化建议（按优先级）

1. **把严重等级规则注入 prompt**（修 1.3）：从 `config/settings.py` 读 `three_way_match` 容差 / 金额阈值，渲染成一行"HIGH: 超容差 2 倍或金额 > {threshold}；MEDIUM: ...；LOW: ..."。这是业务正确性问题，不是文字润色。
2. **约束建议来源**（修 1.2）：加一句"建议措施必须基于上文工具输出的具体数据，不得引入未出现的假设；如数据不足以给出建议，明确写'数据不足'"。
3. **明确数据截断语义**（修 1.6）：当 `outputs_text` 中某段被截断时，由代码侧追加明显标记（如 `...[已截断，原始数据更长]`），并在 prompt 里说明"看到截断标记时不要基于末尾做结论"。
4. **固定引用格式**（修 1.4）：给一个最小模板，如 "单据号用反引号包裹，金额用¥前缀并保留 2 位小数"。
5. **允许"不确定"表达**（修 1.7）：替换"说明无异常发现"为"如无异常明确说明；如数据不足以判断，明确标注'数据不足'"。

---

---

## 2. Router L3 `_LLM_CLASSIFY_PROMPT`

### 现状
```
你是 ERP 采购分析系统的意图分类器。根据用户查询，判断最匹配的分析类型。
{role_section}
可选分析类型：
- three_way_match: ...
- price_variance: ...
- ... (共 11 类)
- comprehensive: 以上多类或无法明确归类的综合分析

输出纯 JSON，无其他文字：
{"type": "分析类型", "confidence": 0.0到1.0, "supplier_id": null或字符串, "po_number": null或字符串, "days": null或整数}

用户查询：{query}
```

### 问题点

| # | 问题 | 严重度 | 说明 |
|---|---|---|---|
| 2.1 | **缺少"拒答"语义** | 高 | 用户查询可能是闲聊 / 非采购话题 / 恶意注入。当前 prompt 强制模型必选一个 11 类之一（或 comprehensive），无法返回"非分析查询"信号。现有 `_is_non_analysis_query` 正则仅做粗粒度前置过滤，漏网的会被 L3 强行分类成 comprehensive，触发无意义全量分析 |
| 2.2 | **`comprehensive` 语义混杂** | 高 | 同时承载了"跨多类组合"和"无法归类"两种完全不同的意图。下游 orchestrator 拿到 comprehensive 后一视同仁，丢失了歧义信号 |
| 2.3 | **JSON 格式示例用了单值占位符** | 中 | `"type": "分析类型"` 这种示例既非合法 enum 值也不是明确 placeholder 语法，部分模型会原样输出字符串"分析类型"。应改为 `"type": "three_way_match" // 枚举值之一` 或给出真实示例 |
| 2.4 | **`confidence` 没有锚点** | 高 | "0.0 到 1.0" 没有说明什么叫 0.3 什么叫 0.8。导致 confidence 分布呈现明显的 LLM 偏好模式（常见值集中在 0.8/0.9/0.95），失去区分度。当前 confidence 只用于 observability 展示，但未来若要做"低置信度转人工"就废了 |
| 2.5 | **参数抽取边界不清** | 中 | prompt 要求抽 `supplier_id` / `po_number` / `days`，但未说明"模糊引用（'上次那家供应商'/'昨天的单子'）是否抽取"。LLM 容易把上下文代词抽成具体 ID 造成幻觉 |
| 2.6 | **markdown 代码块风险未闭环** | 中 | 已有 [router.py:643-647](../core/orchestrator/router.py#L643-L647) 在解析侧剥离 ```json ```，但 prompt 没明确要求 raw JSON（"输出纯 JSON，无其他文字"措辞偏弱）。Qwen3 / GLM 经常无视这条，输出带代码块的 JSON。虽然剥离逻辑兜住了，但属于"治标" |
| 2.7 | **角色描述直接插拼接** | 低 | `{role_section}` 是空字符串 / 有角色两种分支，换行格式易错位，不影响功能但影响可读性 |

### 优化建议（按优先级）

1. **引入 `unknown` / `non_analysis` 枚举**（修 2.1 + 2.2）：把 "comprehensive" 拆成 "comprehensive"（明确的跨类组合）和 "unknown"（无法归类）；下游 orchestrator 对 unknown 直接返回友好提示，不触发分析。
2. **给 confidence 语义锚点**（修 2.4）：加一句 "confidence 判定：>0.9=查询直接命中某类型名词；0.6-0.9=语义强相关需要推断；<0.6=多类共存或模糊"。
3. **示例值改为真实枚举**（修 2.3）：`"type": "three_way_match"` 作为示例，而不是占位符"分析类型"。
4. **约束参数抽取**（修 2.5）：加一句 "只抽取用户查询中明确出现的单据号 / 供应商 ID；代词或模糊引用一律填 null"。
5. **强化 JSON-only 要求**（修 2.6）：从"输出纯 JSON，无其他文字"升级到"不要使用 markdown 代码块、不要加前后说明、第一个字符必须是 `{`"。

---

---

## 3. P2P Agent `build_system_prompt`

### 现状结构
- 角色定义 → 可用工具清单（8 个）→ 输出格式要求（5 条）→ 分析流程（4 步）→ 对话历史处理 → 本体知识上下文（动态拼接） → 历史参考（可选）

### 问题点

| # | 问题 | 严重度 | 说明 |
|---|---|---|---|
| 3.1 | **工具清单与实际注册的 tools 重复** | 中 | `_build_tools` 已把工具及其描述注入 LangChain，LLM 本来就能看到。prompt 里再列一次是冗余，既浪费 token 又易漂移（工具新增 / 参数调整时需要改两处，一旦不同步会误导 LLM） |
| 3.2 | **"输出格式要求"与 `_REPORT_PROMPT` 重复** | 中 | 同样的"中文 / Markdown / HIGH-MEDIUM-LOW / 数据支撑 / 改进建议"在两处各写了一次。ReAct 路径最终生成报告是 LLM 内部完成，DAG 路径则经 ReportAgent。两份指令在实际分叉前都会被读到，造成无意义重复 |
| 3.3 | **"分析流程"是固定 4 步剧本** | 高 | "理解需求 → 查数据 → 跑规则 → 生成报告"对简单查询来说是过度规定。对"查一下 SUP-001 最近三笔发票" 这类诉求，LLM 会被迫先 query_purchase_orders / query_receipts / 跑三路匹配再出报告——产生不必要的 tool call。实际只应 `query_invoices` 一次。"流程" 描述应该是可选指南，不是强制顺序 |
| 3.4 | **对话历史处理规则位置突兀** | 中 | 这段其实是"短期记忆注入约定"，和 memory middleware 行为耦合。放在 system prompt 里，但 middleware 实际会在消息历史里注入 `[对话历史-上一轮分析结果摘要]`。如果 middleware 逻辑改了 tag 格式，这里不同步就会失效 |
| 3.5 | **本体上下文无 token 预算** | 中 | `get_ontology_context` 返回的 narrative + entities + rules 拼接后可能很长（取决于 OWL 内容），没做 token budget 裁剪。和长期记忆不对称——长期记忆有 `long_term_context_trim_enabled` 保护，本体上下文没有 |
| 3.6 | **本体上下文降级默认文本粗糙** | 低 | `_DEFAULT_ONTOLOGY_NARRATIVE` 只讲 P2P 基本概念，缺合规规则与实体清单。降级后 LLM 推理基础变弱——但降级仅在 OWL 加载失败时触发，频率低 |
| 3.7 | **历史参考只给原文，不给时间戳 / 置信度** | 中 | `long_term_block` 直接把记忆 content 贴进去。没标注时效（一个月前的结论可能已过期），LLM 无法判断是否仍适用。`format_long_term_memory` 已经知道 `created_at`，但渲染时丢了 |
| 3.8 | **缺少"分析边界"约束** | 高 | 没有明确告知 LLM "本版本只读，不要承诺修改 ERP 系统 / 发送邮件 / 创建工单"。用户问"能不能直接把这笔发票退回"时，LLM 可能回答"已为您发起退回流程"造成误导 |

### 优化建议（按优先级）

1. **删除工具清单重复段**（修 3.1）：LangChain 已注入 tool schema，prompt 里保留一句"使用已注册工具获取数据"即可。
2. **补只读边界声明**（修 3.8）：加一段 "本系统为分析只读系统，不会执行任何 ERP 写操作（付款、审批、单据修改等）。涉及写操作时请在报告中以'建议人工处理'措辞给出，不要承诺执行"。
3. **"分析流程"改为启发式**（修 3.3）：从"必须 4 步"改为"根据查询复杂度自行选择调用工具；简单事实查询只调必要工具即可"。
4. **合并输出格式要求**（修 3.2）：system prompt 里只保留高层风格约束（中文 / Markdown）；细节（严重等级 / 数据支撑等）下沉到 ReportAgent / 最终汇总阶段。
5. **历史记忆加时间戳**（修 3.7）：`format_long_term_memory` 渲染时补 "(YYYY-MM-DD)" 前缀，并在 prompt 里说明"参考时注意时效"。
6. **本体上下文加 token 预算**（修 3.5）：复用 `trim_to_token_budget` 工具，配一个 `ontology_context_max_tokens_pct`。

---

---

## 4. `_OUTPUT_MODE_PROMPTS`

### 现状
```python
_OUTPUT_MODE_PROMPTS = {
    "detailed": "",
    "brief":   "请以简报摘要形式输出，控制在 3-5 个要点，突出关键数据和结论，总字数不超过 500 字。",
    "table":   "请优先使用 Markdown 表格呈现核心数据，辅以不超过 2 句话的结论。",
}
```
拼接位置：[orchestrator.py:862](../core/orchestrator/orchestrator.py#L862) 读取后传给 ReportAgent / P2P Agent。

### 问题点

| # | 问题 | 严重度 | 说明 |
|---|---|---|---|
| 4.1 | **`detailed` 是空字符串** | 低 | 空串意味着 "detailed 模式 = 默认模式"，但 `_REPORT_PROMPT` 本身并没有明确要求"详细"。用户选 detailed 时没有任何额外指令，等价于没选——那这个模式的存在价值何在？ |
| 4.2 | **`brief` 与严重等级 / 数据支撑要求冲突** | 高 | `_REPORT_PROMPT` 强制要求"提供具体数据支撑（单据号、金额、偏差比例）"和"包含摘要、关键发现、详细数据、建议措施"——4 个段落。brief 又要 "3-5 个要点 / 500 字内"。两套指令直接打架，LLM 输出不稳定：时而完整报告时而精简要点 |
| 4.3 | **`table` 模式下其他要求不明** | 中 | "优先使用表格"——那"建议措施 / 严重等级"是否也进表格？没有说。容易产生"表格 + 零散文字段落"的割裂结构 |
| 4.4 | **模式定义与 API 契约耦合不明** | 中 | 这 3 个模式是 API 约定的枚举，但代码里用字符串字典查表，`request.output_mode` 若传了第 4 个值会静默走 "" 分支（见 `.get(request.output_mode, "")`），没有输入校验。用户传 `"summary"` 这类合理词会被静默忽略，无报错 |

### 优化建议

1. **让各模式覆盖而非叠加**（修 4.2）：把 brief / table 的指令从"额外拼接"改为"替换核心输出要求"。如 brief 模式时，不再提"摘要 / 关键发现 / 详细数据 / 建议措施"的 4 段，整体改为"3-5 要点 + 风险等级"。做法：`ReportAgent.generate` 根据 mode 选择不同的 `_REPORT_PROMPT` 变体，而不是字符串追加。
2. **`detailed` 补明确定义**（修 4.1）：即便是默认模式也应有显式描述（"完整四段报告"），避免"空 = 默认"的隐式约定。
3. **加输入校验**（修 4.4）：`output_mode` 非法值应 400，不要静默降级。
4. **`table` 模式明确分界**（修 4.3）：加一句"核心异常数据用表格，严重等级作为表格一列；建议措施单独列出"。

---

---

## 5. 跨 prompt 共性问题

以下问题不属于单个 prompt，而是横跨多个 prompt 的系统性不足。

| # | 问题 | 影响范围 |
|---|---|---|
| 5.1 | **没有统一的 prompt 渲染层** | 4 处 prompt 散落在 3 个模块里，各自用 f-string / `.format()`。结果：占位符命名不一（`{scenario}` / `{query}` / `{role_section}` / `{ontology_context}`），无法静态检查缺失变量；没有版本号，A/B 测试或回滚困难 |
| 5.2 | **prompt 内容未纳入可观测性** | ReportAgent 记录 `prompt_length` 和 `input[:2000]`（[report_agent.py:106,125](../modules/p2p/report_agent.py#L106)），L3 只记 `input[:2000]`。无法区分"prompt 变化导致的输出变化" vs "LLM 自身波动"。无 prompt 版本号 / hash 记录 |
| 5.3 | **中文标点 / 空白一致性问题** | 4 处 prompt 中全半角标点混用（"HIGH/MEDIUM/LOW" 用半角 `/`，"（HIGH/MEDIUM/LOW）" 括号是全角），某些冒号后有空格某些没有。不影响 LLM 理解但体现规范缺失 |
| 5.4 | **没有 token 预算上限机制** | 每个 prompt 独立拼接输入（工具输出 / 本体 / 长期记忆），累加后可能超 `llm.context_window`。只有长期记忆一处做了 trim，其他环节均无保护 |
| 5.5 | **幻觉防御弱** | 所有 prompt 都没有"只基于提供的数据回答，不知道就说不知道"这类通用防幻觉指令。业务场景里 ERP 数据幻觉（编造发票号、虚构金额）是高危问题 |
| 5.6 | **LLM 侧 system / user 角色未显式划分** | ReportAgent 与 L3 Router 都是把整段 prompt 走 `llm.invoke(prompt)`，相当于全部作为单个 user message。`_REPORT_PROMPT` / `_LLM_CLASSIFY_PROMPT` 的"你是 XX"这类角色设定更适合走 system message。分开后模型对角色设定的遵从度通常更高（特别是 thinking 关闭后的 qwen3 / GLM） |
| 5.7 | **prompt 内未显式约束时区 / 日期基准** | "最近 30 天 / 本月"等相对时间在 LLM 端如何计算无定义。CLAUDE.md 已经定义全链路走 Asia/Shanghai，但 prompt 里没把"当前日期 = YYYY-MM-DD（Asia/Shanghai）"注入，LLM 默认假设 UTC 或模型训练时的日期 |

---

---

## 6. 优先级建议

按"业务正确性风险"排序，建议分 3 批落地。

### P0（业务正确性 / 合规风险）—— 建议立刻修

| 编号 | 条目 | 影响 |
|---|---|---|
| 1.3 | ReportAgent 严重等级判定规则未注入 prompt | 合规核心 KPI（HIGH/MEDIUM/LOW）标注不一致 |
| 1.2 | 建议措施缺数据源约束，易幻觉 | 误导采购决策 |
| 3.8 | P2P system prompt 无"只读边界"声明 | LLM 可能承诺执行写操作 |
| 5.5 | 全链路缺通用防幻觉指令 | ERP 数据幻觉高危 |
| 2.1 + 2.2 | L3 分类 `comprehensive` 语义混杂，无拒答 | 非分析查询也触发分析，浪费资源 |

### P1（稳定性 / 输出一致性）

| 编号 | 条目 | 影响 |
|---|---|---|
| 1.6 | `outputs_text` 截断无标记 | LLM 可能基于截断尾巴做错误结论 |
| 4.2 | `brief` 模式与核心要求指令冲突 | 输出不稳定 |
| 2.4 | L3 confidence 无语义锚点 | 置信度失效，未来做路由策略时需重做 |
| 5.7 | 无当前日期 / 时区注入 | 相对时间计算偏差 |
| 3.3 | 分析流程硬编 4 步，过度规定 | 简单查询被迫多调工具，成本增加 |

### P2（工程卫生 / 可维护性）

| 编号 | 条目 |
|---|---|
| 3.1 | P2P prompt 工具清单重复 |
| 3.2 | 输出格式要求 ReportAgent / P2P prompt 两处重复 |
| 5.1 | 无统一 prompt 渲染层与版本号 |
| 5.2 | prompt 内容未纳入 observability（hash / version） |
| 5.6 | system / user 角色未分离 |
| 3.5 | 本体上下文无 token 预算 |
| 4.1 | `detailed` 模式空串 |
| 4.4 | output_mode 非法值静默降级 |

---

## 附：修复的分批建议

- **批次 1（P0）**：集中动 prompt 内容，不改架构。预计 1-2 个 PR，单测覆盖 prompt 渲染后关键词存在性（严重等级规则、只读声明、防幻觉语）。
- **批次 2（P1）**：涉及代码侧辅助（截断标记注入 / 日期注入 / confidence 锚点）+ prompt 文本。预计 2-3 个 PR。
- **批次 3（P2）**：建立 `core/prompts/` 统一渲染层、版本号与 hash 落 trace。涉及架构层面改动，建议独立立项并配迁移计划。

---

## 本次分析范围声明

- 仅审视 prompt 内容与渲染拼接逻辑。
- 不涉及 LLM provider 选择、模型切换、thinking 参数（已在 [disable_thinking 匹配规则](../modules/p2p/model_factory.py) 和 [L3 router](../core/orchestrator/router.py) 两处单独处理）。
- 不涉及 tool 定义 / 本体 OWL 内容修改。

