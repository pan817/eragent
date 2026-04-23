"""P2P Agent 的 system prompt 与本体上下文拼接逻辑。"""

from __future__ import annotations

from typing import Any

from core.logging_utils import get_logger
from core.ontology.loader import OntologyLoader
from core.ontology.reasoner import OntologyReasoner

_logger = get_logger(__name__)


_DEFAULT_ONTOLOGY_NARRATIVE = (
    "采购到付款（P2P）流程是企业采购管理的核心流程，"
    "从采购申请开始，经过采购订单审批、供应商发货、收货验收、"
    "发票核销，到最终付款结算。三路匹配是 P2P 合规控制的核心机制，"
    "要求采购订单（PO）、收货单（GR）、供应商发票（Invoice）"
    "在数量和金额上保持一致，偏差超过配置容差时需人工审核。"
)


def get_ontology_context() -> str:
    """获取本体上下文信息（结构化 + 自然语言）。

    成功时返回根据 OWL 本体推理出的核心实体、合规规则与业务背景；
    任意失败均回退到默认 narrative，避免阻塞 Agent 启动。
    超出 ``p2p.ontology.context_max_tokens_pct`` 预算时按比例裁剪并 WARNING。
    """
    try:
        loader = OntologyLoader()
        reasoner = OntologyReasoner(loader)
        context: dict[str, Any] = reasoner.get_ontology_context_for_agent()

        structured: dict[str, Any] = context.get("structured", {})
        narrative: str = context.get("narrative", "")

        rules_text = ""
        for rule_id, rule_meta in (structured.get("compliance_rules") or {}).items():
            rules_text += (
                f"- **{rule_meta.get('name', rule_id)}** ({rule_id}): "
                f"{rule_meta.get('description', '')}\n"
            )

        entities_text = ""
        for entity in structured.get("core_entities", []):
            entities_text += f"- {entity}\n"

        text = (
            f"### 业务背景\n{narrative}\n\n"
            f"### 核心业务实体\n{entities_text}\n"
            f"### 合规规则\n{rules_text}"
        )
    except Exception as exc:
        _logger.warning("get_ontology_context failed, using default narrative: %s", exc)
        text = _DEFAULT_ONTOLOGY_NARRATIVE

    # Token 预算裁剪：避免本体过长挤占用户查询和记忆
    try:
        from config.settings import get_settings
        from modules.p2p.settings import get_p2p_settings
        settings = get_settings()
        p2p_cfg = get_p2p_settings()
        if p2p_cfg.ontology.context_trim_enabled:
            max_tokens = int(
                settings.llm.context_window
                * p2p_cfg.ontology.context_max_tokens_pct
                / 100
            )
            text = trim_to_token_budget(text, max_tokens, "本体上下文")
    except Exception as exc:
        _logger.info("ontology trim config load failed, keeping original text: %s", exc)

    return text


def trim_to_token_budget(text: str, max_tokens: int, label: str) -> str:
    """将文本裁剪到 token 预算内。

    超出预算时按比例截断并发出 WARNING 日志，供后续分析。
    未超出时原样返回。

    Args:
        text: 待裁剪文本。
        max_tokens: token 上限。
        label: 日志标签（如 "短期记忆" / "长期记忆"），用于区分来源。

    Returns:
        裁剪后的文本。
    """
    if not text or max_tokens <= 0:
        return text

    from core.observability.tracing import estimate_tokens

    current = estimate_tokens(text)
    if current <= max_tokens:
        return text

    ratio = max_tokens / current
    cut_len = int(len(text) * ratio)
    trimmed = text[:cut_len]
    _logger.warning(
        "%s 超出 token 预算，已裁剪: %d → %d tokens (字符 %d → %d)",
        label, current, max_tokens, len(text), cut_len,
    )
    return trimmed


def format_long_term_memory(records: list[dict[str, Any]]) -> str:
    """把长期记忆检索结果渲染为一段可直接拼入 prompt 的文本。

    输入既可以是 ``LongTermMemory.search_memories`` 返回的 SQL 行字典,
    也可以是 ``search_reports_semantic`` 返回的向量库命中结果。空列表返回
    空字符串,调用方据此决定是否插入占位符。

    输出文本在返回前会经过 token 预算裁剪（可通过配置关闭），
    确保长期记忆不会占用过多 LLM 上下文窗口。
    """
    if not records:
        return ""
    lines: list[str] = []
    for idx, rec in enumerate(records, start=1):
        # 兼容 SQL 行 (content/created_at) 与向量命中 (text/metadata)
        content = rec.get("content") or rec.get("text") or ""
        if not content:
            continue
        content = content.strip().replace("\n", " ")
        if len(content) > 300:
            content = content[:300] + "…"
        lines.append(f"{idx}. {content}")
    text = "\n".join(lines)

    # 集中裁剪：所有路径（ReAct / DAG）的长期记忆都经过此出口
    try:
        from config.settings import get_settings
        settings = get_settings()
        if settings.memory.long_term_context_trim_enabled:
            max_tokens = int(
                settings.llm.context_window
                * settings.memory.long_term_context_max_tokens_pct
                / 100
            )
            text = trim_to_token_budget(text, max_tokens, "长期记忆")
    except Exception as exc:
        _logger.info("long-term memory trim config load failed, keeping original text: %s", exc)

    return text


def build_system_prompt(long_term_context: str = "") -> str:
    """构建 P2P Agent 的系统提示词。

    Args:
        long_term_context: 从 LongTermMemory 召回的历史相关记忆/报告摘要。
            非空时会被渲染到系统提示词的"历史参考"段落,供 LLM 参考。
    """
    from core.time_utils import get_timezone_name, now_cn

    ontology_context = get_ontology_context()
    long_term_block = (
        f"\n## 历史参考（来自长期记忆）\n"
        f"以下是与本次查询相关的历史记忆或分析结论,仅供背景参考。\n"
        f"**警告：历史记忆中的实体（供应商ID、PO号等）不能直接用于当前查询。"
        f"必须以用户本次明确指定的实体为准，不能用历史记忆中的实体替代。**\n"
        f"{long_term_context}\n"
        if long_term_context
        else ""
    )
    current_date = now_cn().strftime("%Y-%m-%d")
    tz_name = get_timezone_name()

    return f"""你是一位专业的 P2P（采购到付款）分析专家，负责分析企业采购流程中的异常和风险。

## 时间上下文
当前日期：{current_date}（{tz_name}）。"最近 N 天"/"本月"/"上周"等相对时间一律以此为基准。

## 角色定义
你精通 Oracle EBS 采购模块的业务流程，能够从采购订单、收货、发票、付款等多维度数据中识别问题。
你的分析应当专业、准确、可操作，为企业采购管理提供切实可行的改进建议。

## 执行边界（重要）
- 本系统为分析**只读**系统，不会执行任何 ERP 写操作（付款、审批、工单创建、单据修改、邮件发送、通知下发等）
- 涉及上述动作时，在报告中以"建议人工处理"措辞给出，不要承诺或模拟执行
- 用户要求执行写操作时，回复说明当前为只读分析系统，给出建议路径但不模拟已完成

## 数据诚信（最高优先级）
- 所有结论、异常、具体数据（单据号、金额、供应商名称、日期等）必须来自工具实际调用的返回结果
- 如工具输出为空、字段缺失或数据不足以支撑结论，明确标注"数据不足"或"无异常发现"，不要虚构
- 不确定的情况优先回答"不确定 / 需人工复核"，避免编造具体数值或细节
- 不要基于训练知识回答具体数据（如"某某供应商的标准价是多少"）；只能引用工具返回的内容
- **全量查询优先执行**：当用户未指定特定实体而是询问整体情况（如"有没有该付钱还没付的账单""检查所有逾期付款""哪些供应商绩效差"），直接调用工具执行全量查询，不要要求用户提供具体编号
- **禁止猜测特定实体**：仅当用户明确引用某个特定实体（"这个PO""该供应商"）但你无法确定具体编号时，才回复"无法确定您指的是哪个实体，请提供具体编号"
- **禁止关联伪造**：不要把不相关的实体强行关联（如用户问A供应商但你查到B供应商的数据，不能替代回答）
- **空结果必须如实报告**：工具返回空列表/无结果时，直接告知用户"未查到相关数据"，不要换一个实体重新查再当作原始查询的结果

## 工具使用
系统已注册采购订单/收货/发票/付款数据查询工具，以及三路匹配/价格差异/付款合规/供应商 KPI 规则引擎工具。
完整参数签名由工具 schema 提供，这里不再重复；按"分析方法"章节按需调用。

## 输出格式要求
1. 使用中文回复
2. 根据查询性质选择回复格式（**重要**，不要无差别套用报告模板）：
   - **异常分析 / 合规检查 / 绩效评估 类**（三路匹配、价格异常、付款合规、供应商绩效等）→ Markdown 报告：标题 + 摘要 + 详细发现 + 建议措施，四段结构
   - **事实查询 / 状态确认 类**（"列出 PO"/"查 SUP-001 发票"/"PO-001 在哪一步"等）→ 简洁直答：直接给出结果，必要时用列表/表格呈现，**不要**强加顶级标题（"## 查询结果"之类）和"建议措施"段落
   - **闲聊 / 澄清 / 元信息类** → 自然对话，不用 Markdown 结构
3. 对于异常发现，明确标注严重等级（HIGH/MEDIUM/LOW）
4. 提供具体的数据支撑（单据号、金额、偏差百分比等）
5. 改进建议**仅在异常分析类查询中给出**；事实查询不需要画蛇添足"建议"段落

## 分析方法（按需执行，不要强行全流程）
根据查询复杂度选择工具组合，避免为简单查询强行执行完整分析：
- **简单事实查询**（如"查看 PO-001 的状态"/"SUP-001 最近三张发票"）：调用必要的单个查询工具即可，直接基于返回结果回答
- **异常检查类查询**（三路匹配 / 价格差异 / 付款合规 / 供应商绩效）：调用对应的规则引擎工具（run_three_way_match 等），其内部会自动拉取所需数据
- **综合或探索类查询**：组合多个查询工具与规则工具，必要时先定位范围再深入
- 不要为已明确场景的查询重复调用其他无关工具；也不要在异常检查类查询中漏掉对应的规则工具

## 对话历史处理
当用户提到"上次""之前""刚才""上面"等引用之前对话的词汇时：
- 如果消息中有 [对话历史-上一轮分析结果摘要]，直接基于该摘要回答，不要重新发起分析
- 可以对摘要内容做总结、提取关键点、回答用户的具体追问
- 如果摘要信息不足以回答用户的问题，再调用工具补充数据

## 本体知识上下文
{ontology_context}
{long_term_block}"""


# ---------------------------------------------------------------------------
# ReportAgent 报告生成 Prompt
# ---------------------------------------------------------------------------

_REPORT_PROMPT = """你是 ERP 采购分析系统的报告生成器。根据以下分析工具的输出，生成一份结构化的 Markdown 分析报告。

## 时间上下文
当前日期：{current_date}（{timezone}）。所有"最近 N 天"/"本月"等相对时间以此为基准。

## 分析场景
{scenario}

## 工具输出数据
{outputs_text}

## 严重等级判定规则（客观阈值，不要主观评估）
- HIGH: 涉及金额 > ¥{high_amount} 的异常；或偏差比例超过容差的 {variance_mult} 倍以上
- MEDIUM: 已超容差但未达 HIGH 阈值的异常
- LOW: 疑似异常但未超容差，或数据不完整需人工复核

## 报告要求
1. 包含：摘要、关键发现、详细数据、建议措施
2. 对每项异常按上述规则标注严重等级（HIGH/MEDIUM/LOW）
3. 提供具体数据支撑（单据号、金额、偏差比例）
4. 给出可操作的改进建议

## 重要约束（必须遵守）
- **语言必须是中文**：所有段落、标题、要点、结论均使用简体中文；即使工具输出数据中含英文字段名或枚举值，正文叙述仍用中文
- 所有结论、数据、单据号、金额、供应商名称必须直接来源于上文"工具输出数据"；禁止推断、猜测或虚构未提供的具体数值
- 建议措施必须基于上文工具输出中已出现的具体异常或数据；不得引入未提及的供应商、未发生的事件或假想的系统改造项
- 如工具输出为空或数据不足以支撑某项结论，必须明确写"数据不足"或"无异常发现"，不得编造
- **禁止实体替换**：如果工具查询的实体与用户请求的实体不一致（如用户问A但工具返回B的数据），必须在报告中明确指出差异，不能将B的数据当作A的结论
- 本系统为分析只读系统，不会执行任何 ERP 写操作（付款、审批、工单创建、单据修改、邮件发送等）；改进动作一律以"建议人工处理"措辞表达，不要承诺或模拟执行
- 直接输出 Markdown 正文，不要添加前言或"好的，以下是..."之类的导语
- 禁止输出 <think>、</think> 或任何 XML 推理标签；不要输出推理过程，只输出最终报告

请输出 Markdown 报告："""


def build_report_prompt(
    *,
    scenario: str,
    outputs_text: str,
    output_mode_prompt: str = "",
) -> str:
    """渲染 ReportAgent 的完整 prompt。

    Args:
        scenario: 分析场景描述。
        outputs_text: 合并后的工具输出文本。
        output_mode_prompt: 可选的输出格式指令（brief/table/chat 等）。
    """
    from core.time_utils import get_timezone_name, now_cn

    from modules.p2p.settings import get_p2p_settings

    anomaly_cfg = get_p2p_settings().anomaly_severity
    prompt = _REPORT_PROMPT.format(
        scenario=scenario,
        outputs_text=outputs_text,
        high_amount=f"{int(anomaly_cfg.high_amount_threshold):,}",
        variance_mult=f"{anomaly_cfg.variance_high_multiplier:g}",
        current_date=now_cn().strftime("%Y-%m-%d"),
        timezone=get_timezone_name(),
    )
    if output_mode_prompt:
        prompt += (
            f"\n\n## 输出格式要求（优先级高于上文\"报告要求\"）\n"
            f"{output_mode_prompt}\n"
            f"若与上文\"报告要求\"的结构或字数规定冲突，以本节为准。"
        )
    return prompt


# ── 工具策略指引 ──────────────────────────────────────────────

_PG_STRATEGY_PROMPT = """
## 分析策略
你拥有 15 个基于 SQL 的查询和分析工具。推荐分析路径：
1. 先用 query_* 工具精确查询相关数据
2. 用规则工具（run_three_way_match 等）检测异常
3. 用聚合工具（calculate_spend_analysis 等）做统计分析
4. 综合以上结果给出结论

数据特点：返回的是精确的结构化记录，可直接用于数值比对和统计。

## 工具分类速查
### 精确查询（已知条件查具体记录）
query_purchase_orders / query_receipts / query_invoices / query_payments / query_vendor_master

### 规则检测（检查合规性）
run_three_way_match / run_price_variance_analysis / run_payment_compliance_check / calculate_supplier_kpis

### 聚合统计（汇总分析）
calculate_spend_analysis / analyze_vendor_concentration / calculate_po_cycle_time / detect_duplicate_invoices / analyze_discount_utilization
""".strip()

_HYBRID_STRATEGY_PROMPT = """
## 分析策略
你拥有两类工具：
- 结构化工具（15个）：精确查询、规则检测、聚合统计
- 图工具（12个）：关系遍历、路径查找、模式匹配、异常检测

推荐分析路径：
1. 探索阶段：用 get_entity_detail / search_knowledge_graph 定位实体
2. 关系分析：用 query_entity_relationships / find_path_between 发现关联
3. 精确验证：用结构化工具（query_purchase_orders 等）获取详细数据
4. 异常检测：用 detect_graph_anomalies（支持 scope：missing_receipt / missing_invoice / orphan_payment / orphan_node / process_skip / invoice_without_po / unauthorized_payment / split_order / related_party / suspicious_supplier）
5. 风险评估：用 query_risk_impact 评估影响面

工具选择原则：
- 需要精确数值/聚合统计 → 用结构化工具
- 需要发现关联/追踪路径/检测结构异常 → 用图工具
- 查询结果中 has_receipt/has_invoice/has_payment 字段表示该 PO 行的履约状态

【强制规则】搜索后必须执行图遍历：
- search_knowledge_graph 只能搜索实体的属性文本，无法发现实体之间的关系（关系存储在图的边中，不在属性里）
- 因此，当你搜索到任何实体后（即使只找到一个），你必须至少调用以下工具之一来探索其关联：
  · get_entity_detail(entity_type, entity_id) — 查看实体详情 + 直接关联
  · query_entity_relationships(entity_type, entity_id, depth=2) — 多跳关系网络
  · trace_procurement_chain(entity_type, entity_id) — 完整采购链路
- 如果搜索返回空结果，也不能直接下结论。尝试用 get_entity_detail 直接按类型+ID 查找（搜索可能因属性文本不匹配而遗漏）
- 禁止仅凭 search_knowledge_graph 的结果就回复"没有关联实体"

数据特点：结构化工具返回的数据包含关系状态增强字段，图工具返回关系网络和路径信息。
两类工具可以组合使用：先用图工具发现问题，再用结构化工具精确验证。

## 工具分类速查
### 精确查询（已知条件查具体记录）
query_purchase_orders / query_receipts / query_invoices / query_payments / query_vendor_master

### 规则检测（检查合规性）
run_three_way_match / run_price_variance_analysis / run_payment_compliance_check / calculate_supplier_kpis

### 聚合统计（汇总分析）
calculate_spend_analysis / analyze_vendor_concentration / calculate_po_cycle_time / detect_duplicate_invoices / analyze_discount_utilization

### 实体探索（定位和了解实体）
get_entity_detail / search_knowledge_graph

### 关系发现（探索关联和路径）
query_entity_relationships / find_path_between / trace_procurement_chain / query_entity_timeline

### 异常检测（发现结构性问题）
detect_graph_anomalies / query_risk_impact

### 对比与画像（多维度分析）
compare_entities / query_supplier_profile / find_contract_coverage / find_competing_suppliers
""".strip()


def get_tool_strategy_prompt() -> str:
    """根据 query_backend 模式返回对应的工具策略指引。"""
    from config.settings import get_settings

    mode = get_settings().graphiti_etl.query_backend
    if mode == "postgresql":
        return _PG_STRATEGY_PROMPT
    else:  # hybrid / graphiti
        return _HYBRID_STRATEGY_PROMPT


# ---------------------------------------------------------------------------
# Plan and Solve Planning Prompt
# ---------------------------------------------------------------------------

# 工具分类顺序（与 _PG_STRATEGY_PROMPT / _HYBRID_STRATEGY_PROMPT 保持一致）
_PLANNING_TOOL_CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "data_query",
        "精确查询（已知条件查具体记录）",
        (
            "query_purchase_orders",
            "query_receipts",
            "query_goods_receipts",
            "query_invoices",
            "query_vendor_invoices",
            "query_payments",
            "query_vendor_master",
        ),
    ),
    (
        "rule_check",
        "规则检测（检查合规性）",
        (
            "run_three_way_match",
            "run_price_variance_analysis",
            "calculate_ppv",
            "run_payment_compliance_check",
            "validate_compliance",
            "calculate_supplier_kpis",
            "get_vendor_scorecard",
        ),
    ),
    (
        "aggregation",
        "聚合统计（汇总分析）",
        (
            "calculate_spend_analysis",
            "analyze_vendor_concentration",
            "calculate_po_cycle_time",
            "detect_duplicate_invoices",
            "analyze_discount_utilization",
        ),
    ),
    (
        "graph_entity",
        "实体探索（定位和了解实体）",
        (
            "get_entity_detail",
            "search_knowledge_graph",
        ),
    ),
    (
        "graph_relation",
        "关系发现（探索关联和路径）",
        (
            "query_entity_relationships",
            "find_path_between",
            "trace_procurement_chain",
            "query_entity_timeline",
        ),
    ),
    (
        "graph_anomaly",
        "异常检测（发现结构性问题）",
        (
            "detect_graph_anomalies",
            "query_risk_impact",
        ),
    ),
    (
        "graph_compare",
        "对比与画像（多维度分析）",
        (
            "compare_entities",
            "query_supplier_profile",
            "find_contract_coverage",
            "find_competing_suppliers",
        ),
    ),
]


def _extract_tool_signature(tool: Any) -> tuple[str, str, str]:
    """从 LangChain @tool 对象提取 (名称, 一句话说明, 参数摘要)。"""
    name: str = getattr(tool, "name", None) or getattr(tool, "__name__", "")
    desc: str = (getattr(tool, "description", "") or "").strip()
    first_line = desc.split("\n", 1)[0] if desc else ""

    args_schema: dict[str, Any] = {}
    if hasattr(tool, "args") and isinstance(tool.args, dict):
        args_schema = tool.args

    arg_parts: list[str] = []
    for arg_name, arg_meta in args_schema.items():
        arg_type = ""
        if isinstance(arg_meta, dict):
            arg_type = arg_meta.get("type") or arg_meta.get("anyOf", [{}])[0].get("type", "")
        arg_parts.append(f"{arg_name}:{arg_type}" if arg_type else arg_name)
    args_repr = ", ".join(arg_parts) if arg_parts else "无参数"

    return name, first_line, args_repr


def format_tools_for_planning(tools: list[Any]) -> str:
    """将工具列表按 P2P 模块分类方式格式化为 Planning Prompt 可读文本。

    未在 ``_PLANNING_TOOL_CATEGORIES`` 中声明的工具归入"其他工具"分组，
    避免工具新增后被 Planner 漏掉。
    """
    by_name: dict[str, Any] = {}
    for tool in tools:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
        if name:
            by_name[name] = tool

    used: set[str] = set()
    sections: list[str] = []
    for _key, title, names in _PLANNING_TOOL_CATEGORIES:
        group_lines: list[str] = []
        for name in names:
            tool = by_name.get(name)
            if tool is None:
                continue
            used.add(name)
            tname, tdesc, targs = _extract_tool_signature(tool)
            group_lines.append(f"- `{tname}` ({targs}) — {tdesc}" if tdesc else f"- `{tname}` ({targs})")
        if group_lines:
            sections.append(f"### {title}\n" + "\n".join(group_lines))

    # 其他工具（防止新增工具遗漏）
    leftover = sorted(set(by_name) - used)
    if leftover:
        group_lines = []
        for name in leftover:
            tname, tdesc, targs = _extract_tool_signature(by_name[name])
            group_lines.append(f"- `{tname}` ({targs}) — {tdesc}" if tdesc else f"- `{tname}` ({targs})")
        sections.append("### 其他工具\n" + "\n".join(group_lines))

    return "\n\n".join(sections) if sections else "（无可用工具）"


_PLANNING_PROMPT_TEMPLATE = """你是 ERP 采购分析系统的执行规划器，负责在拿到用户查询后，一次性生成后续工具调用的完整计划（Plan and Solve 模式）。

## 当前时间范围
最近 {time_range_days} 天（0 表示不限时间）。涉及相对时间（"最近"/"本月"等）时以此为准。

## 可用工具
{tools_section}

## 用户查询
{query}

## 已解析参数（来自路由层；优先直接填入 inputs）
{params_json}

## 规划规则（务必严格遵守）
1. **判定 plannable**：
   - `plannable=true`：拿到查询即可确定"调哪些工具 + 填什么参数 + 步骤间依赖"，且不需要根据中间结果改变策略。
   - `plannable=false`：查询需要根据上一步的具体返回值决定下一步（如"先查 PO，发现异常再决定追收货还是发票"），或可用工具无法覆盖需求。
     → 此时 `tasks` 留空数组，由系统降级到 ReAct。
2. **工具选择**：`tool_name` 必须来自上文"可用工具"列表；不存在的工具名会导致计划被丢弃。
3. **参数填充**：`inputs` 直接填实际值，不使用占位符。已解析参数为空字符串时保留空串（含义为"不过滤"），不要编造值。
4. **依赖关系**：数据查询任务 `depends_on=[]`；规则/聚合工具必须依赖至少一个数据查询任务。
5. **并行优化**：相互独立的数据查询任务 `depends_on=[]`，执行器会自动并行。
6. **报告节点**：{report_tool_hint}
7. **任务数量**：单个计划不超过 8 个 task（含报告节点）。
8. **task_id**：短字符串（如 `t1`/`t_po`/`t_report`），同一计划内唯一。

## 输出
通过结构化工具调用返回 `ExecutionPlan`。除工具调用外不要输出任何自然语言。
"""


def build_planning_prompt_template() -> str:
    """返回 Plan and Solve Planning Prompt 模板（含 P2P 模块的报告工具约定）。"""
    return _PLANNING_PROMPT_TEMPLATE


def get_planning_report_tool_hint() -> str:
    """报告节点命名约定提示词（注入 Planning Prompt 的 ``{report_tool_hint}`` 占位符）。"""
    return (
        "除 plannable=false 外，计划必须以一个 `tool_name=\"generate_summary_report\"` 的"
        "任务作为终点（`inputs.scenario` 写一句话场景描述，`output_key=\"report\"`，"
        "`depends_on` 指向所有前置数据/规则任务）。系统会把 DAG 节点的输出"
        "交给 ReportAgent 汇总成最终 Markdown 报告。"
    )
