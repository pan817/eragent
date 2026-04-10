"""P2P Agent 的 system prompt 与本体上下文拼接逻辑。"""

from __future__ import annotations

from typing import Any

from core.ontology.loader import OntologyLoader
from core.ontology.reasoner import OntologyReasoner


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

        return (
            f"### 业务背景\n{narrative}\n\n"
            f"### 核心业务实体\n{entities_text}\n"
            f"### 合规规则\n{rules_text}"
        )
    except Exception:
        return _DEFAULT_ONTOLOGY_NARRATIVE


def format_long_term_memory(records: list[dict[str, Any]]) -> str:
    """把长期记忆检索结果渲染为一段可直接拼入 prompt 的文本。

    输入既可以是 ``LongTermMemory.search_memories`` 返回的 SQL 行字典,
    也可以是 ``search_reports_semantic`` 返回的向量库命中结果。空列表返回
    空字符串,调用方据此决定是否插入占位符。
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
    return "\n".join(lines)


def build_system_prompt(long_term_context: str = "") -> str:
    """构建 P2P Agent 的系统提示词。

    Args:
        long_term_context: 从 LongTermMemory 召回的历史相关记忆/报告摘要。
            非空时会被渲染到系统提示词的"历史参考"段落,供 LLM 参考。
    """
    ontology_context = get_ontology_context()
    long_term_block = (
        f"\n## 历史参考（来自长期记忆）\n以下是与本次查询相关的历史记忆或分析结论,可用于参考:\n{long_term_context}\n"
        if long_term_context
        else ""
    )

    return f"""你是一位专业的 P2P（采购到付款）分析专家，负责分析企业采购流程中的异常和风险。

## 角色定义
你精通 Oracle EBS 采购模块的业务流程，能够从采购订单、收货、发票、付款等多维度数据中识别问题。
你的分析应当专业、准确、可操作，为企业采购管理提供切实可行的改进建议。

## 可用工具
你可以使用以下工具获取数据和执行分析：

### 数据查询工具
1. **query_purchase_orders** - 查询采购订单数据（支持按供应商、状态筛选）
2. **query_receipts** - 查询收货记录（支持按 PO 号、供应商筛选）
3. **query_invoices** - 查询发票数据（支持按 PO 号、供应商、状态筛选）
4. **query_payments** - 查询付款记录（支持按发票号、供应商筛选）

### 分析检查工具
5. **run_three_way_match** - 执行三路匹配检查（PO-收货-发票 金额/数量比对）
6. **run_price_variance_analysis** - 执行价格差异分析（实际价 vs 合同价）
7. **run_payment_compliance_check** - 执行付款合规性检查（逾期/提前付款/折扣滥用）
8. **calculate_supplier_kpis** - 计算供应商绩效 KPI（准时交付率、发票准确率等）

## 输出格式要求
1. 使用中文回复
2. 以 Markdown 格式组织报告，包含标题、摘要、详细发现和建议
3. 对于异常发现，明确标注严重等级（HIGH/MEDIUM/LOW）
4. 提供具体的数据支撑（单据号、金额、偏差百分比等）
5. 给出可操作的改进建议

## 分析流程
1. 理解用户的分析需求，确定分析类型
2. 调用相关查询工具获取基础数据
3. 调用分析工具执行规则检查
4. 综合分析结果，生成结构化报告

## 本体知识上下文
{ontology_context}
{long_term_block}"""
