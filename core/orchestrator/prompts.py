"""Orchestrator 层 prompt 模板与渲染函数。

从 orchestrator.py 提取的 output_mode 格式指令和 intent_kind 早退模板，
使 prompt 文本与编排逻辑分离。
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings


# ---------------------------------------------------------------------------
# output_mode → prompt 后缀
# ---------------------------------------------------------------------------


def build_output_mode_prompts(settings: Settings) -> dict[str, str]:
    """根据当前配置渲染 output_mode → prompt 后缀表。

    三种模式都会注入字数上限（取自 ``settings.report.*_max_chars``），
    用于抑制 LLM 过度展开、降低生成延迟；硬上限由 ``report.max_output_tokens``
    在 ReportAgent 侧兜底。
    """
    cfg = settings.report
    # 每个模式都重申"中文输出"：本节优先级高于 _REPORT_PROMPT 的"报告要求"，
    # 小模型可能把上文整块视为低优先级，需要在此处再次锚定语言，避免漂移为英文。
    lang_anchor = "全文使用简体中文输出。"
    # detailed/brief/table 三档都加"格式强制覆盖"声明：用户**显式选择**这些模式时，
    # 必须强行覆盖系统提示词里"按查询性质分档"的判断（即便查询本身是事实查询，
    # 也按所选模式格式输出）。chat 模式与系统提示词的"事实查询简洁直答"语义一致，
    # 不需要覆盖声明。
    override_anchor = (
        "**【格式强制覆盖】** 本节优先级高于系统提示词中"
        "\"根据查询性质选择回复格式\"的判断——"
        "无论查询性质如何，本次输出都必须严格按以下格式："
    )
    return {
        "detailed": (
            f"{lang_anchor}{override_anchor}"
            f"Markdown 报告格式（标题 + 摘要 + 关键发现 + 建议措施，4 段结构）；"
            f"报告总字数控制在 {cfg.detailed_max_chars} 字以内，"
            f"优先保留关键数据、异常清单和建议；"
            f"数据充分时可精简例证，避免长段落复述。"
        ),
        "brief": (
            f"{lang_anchor}{override_anchor}"
            f"简报摘要形式（3-5 个要点列表），"
            f"突出关键数据和结论，总字数不超过 {cfg.brief_max_chars} 字。"
        ),
        "table": (
            f"{lang_anchor}{override_anchor}"
            f"Markdown 表格呈现核心数据，"
            f"辅以不超过 2 句话的结论，总字数不超过 {cfg.table_max_chars} 字。"
        ),
        # chat 模式：用于事实查询（DATA_LOOKUP）。不强加报告结构，
        # 也不附加 4 段模板/要点列表/表格强制项；只保留语言锚定与字数兜底。
        # 与系统提示词的"事实查询简洁直答"语义一致，不需要"格式强制覆盖"声明。
        # 通常由 Orchestrator 按 intent_kind 自动选用，前端无需主动传。
        "chat": (
            f"{lang_anchor}"
            f"请用自然简洁的语句直接回答；不要加顶级标题（如\"## 查询结果\"）、"
            f"不要写\"摘要/建议\"段落；总字数不超过 {cfg.chat_max_chars} 字。"
        ),
    }


# ---------------------------------------------------------------------------
# intent_kind 早退响应模板
# ---------------------------------------------------------------------------

_SUPPORTED_SCENARIO_TEXT = (
    "三路匹配、价格差异、付款合规、供应商绩效、支出分析、收货异常、"
    "重复发票、早付折扣、采购周期、供应商集中度，以及综合跨域分析"
)

# clarification 时按缺失参数生成具体追问句
_MISSING_PARAM_HINTS: dict[str, str] = {
    "time_range": "时间范围（如\"最近 30 天\"/\"本月\"）",
    "supplier_id": "供应商 ID（如 SUP-001）",
    "po_number": "采购订单号（如 PO-2024-0001）",
    "invoice_number": "发票号（如 INV-2024-0001）",
    "analysis_scope": "分析范围（具体单据/品类/部门）",
}


def render_intent_kind_template(signal: Any) -> str:
    """根据 ``signal.intent_kind`` 渲染早退响应文本（Markdown）。

    设计目标：
    - CLARIFICATION：按 ``missing_params`` 给出具体追问，而非泛泛"信息不足"。
    - META：列出系统支持范围，附使用提示。
    - CHITCHAT：简短礼貌回应 + 引导回到业务话题。
    - OUT_OF_SCOPE：明确告知本系统仅覆盖采购，建议另寻渠道。
    """
    from core.orchestrator.signal import IntentKind

    kind = signal.intent_kind

    if kind == IntentKind.CLARIFICATION:
        missing = signal.missing_params or []
        if missing:
            hints = "\n".join(
                f"- {_MISSING_PARAM_HINTS.get(p, p)}" for p in missing
            )
            return (
                "需要您补充以下信息以便启动分析：\n\n"
                f"{hints}\n\n"
                "示例：`分析 SUP-001 最近 30 天的价格差异`。"
            )
        return (
            "您的分析意图已识别，但缺少关键参数。请补充时间范围、供应商或单据号后重试。\n\n"
            "示例：`分析 SUP-001 最近 30 天的价格差异`。"
        )

    if kind == IntentKind.META:
        return (
            "本系统是 ERP 采购分析智能体，支持以下分析场景：\n\n"
            f"{_SUPPORTED_SCENARIO_TEXT}。\n\n"
            "可直接用自然语言提问，例如：\n"
            "- `分析最近 30 天的三路匹配异常`\n"
            "- `查询 SUP-001 最近的发票`\n"
            "- `评估供应商 SUP-001 的绩效`\n"
        )

    if kind == IntentKind.CHITCHAT:
        return (
            "您好，我是 ERP 采购分析助手。如需采购数据分析，请描述具体场景。\n\n"
            f"当前支持：{_SUPPORTED_SCENARIO_TEXT}。"
        )

    if kind == IntentKind.OUT_OF_SCOPE:
        return (
            "您的查询超出本系统覆盖范围——本系统仅处理 ERP 采购（P2P）相关分析，"
            "不支持销售订单、库存周转、HR 数据等其他模块。\n\n"
            f"采购侧支持：{_SUPPORTED_SCENARIO_TEXT}。"
        )

    # 兜底：未识别的 intent_kind（理论上不会进到这里）
    return (
        "您的查询暂时无法识别为某个具体分析场景。\n\n"
        f"本系统支持：{_SUPPORTED_SCENARIO_TEXT}。\n\n"
        "请提供更具体的采购场景或单据信息后重试。"
    )
