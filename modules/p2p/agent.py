"""
P2P Agent 定义。

基于 LangChain 1.2.0 create_agent API 构建 P2P 采购分析智能体，
使用 ChatOpenAI 兼容接口连接 GLM-4 大模型，集成 8 个结构化工具
实现采购订单查询、三路匹配、价格差异分析、付款合规检查和供应商绩效计算。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from api.schemas.analysis import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.ontology.loader import OntologyLoader
from core.ontology.reasoner import OntologyReasoner


class P2PAgent:
    """P2P 采购分析智能体。

    封装 LangChain Agent，提供自然语言驱动的采购到付款流程分析能力。
    支持三路匹配异常检测、价格差异分析、付款合规检查和供应商绩效评估。

    Agent 和 LLM 均采用延迟加载策略，在首次调用 analyze() 时才初始化，
    避免在导入或实例化阶段产生不必要的网络请求。

    Attributes:
        _settings: 全局配置对象。
        _agent: LangChain Agent 实例（延迟初始化）。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化 P2P Agent。

        Args:
            settings: 全局配置对象，为 None 时自动调用 get_settings() 获取。
        """
        self._settings: Settings = settings if settings is not None else get_settings()
        self._agent: Any | None = None

    # ------------------------------------------------------------------
    # 构建方法
    # ------------------------------------------------------------------

    def _build_model(self) -> ChatOpenAI:
        """构建 ChatOpenAI 兼容模型实例。

        使用 ChatOpenAI 兼容接口连接 GLM-4 大模型，
        所有参数从 settings.llm 配置中读取。

        Returns:
            配置完成的 ChatOpenAI 实例。
        """
        llm_cfg = self._settings.llm
        return ChatOpenAI(
            model=llm_cfg.model,
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.api_base,
            temperature=llm_cfg.temperature,
            max_tokens=llm_cfg.max_tokens,
            timeout=llm_cfg.timeout,
        )

    def _build_tools(self) -> list:
        """导入并返回 P2P 工具集。

        从 modules.p2p.tools 模块导入全部 8 个 @tool 装饰的工具函数。

        Returns:
            包含 8 个 LangChain Tool 对象的列表。
        """
        from modules.p2p.tools import (
            calculate_supplier_kpis,
            query_invoices,
            query_payments,
            query_purchase_orders,
            query_receipts,
            run_payment_compliance_check,
            run_price_variance_analysis,
            run_three_way_match,
        )

        return [
            query_purchase_orders,
            query_receipts,
            query_invoices,
            query_payments,
            run_three_way_match,
            run_price_variance_analysis,
            run_payment_compliance_check,
            calculate_supplier_kpis,
        ]

    def _get_system_prompt(self) -> str:
        """构建 P2P Agent 的系统提示词。

        包含角色定义、可用工具说明、输出格式要求和本体上下文。
        本体上下文通过 OntologyReasoner 获取，获取失败时使用默认文本。

        Returns:
            中文系统提示词字符串。
        """
        # 获取本体上下文
        ontology_context: str = self._get_ontology_context()

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
"""

    def _get_ontology_context(self) -> str:
        """获取本体上下文信息。

        尝试从 OntologyReasoner 获取结构化本体上下文，
        失败时返回默认业务背景文本。

        Returns:
            本体上下文字符串，供注入系统提示词。
        """
        try:
            loader = OntologyLoader()
            reasoner = OntologyReasoner(loader)
            context: dict[str, Any] = reasoner.get_ontology_context_for_agent()

            structured: dict[str, Any] = context.get("structured", {})
            narrative: str = context.get("narrative", "")

            # 格式化规则信息
            rules_text: str = ""
            compliance_rules: dict[str, Any] = structured.get("compliance_rules", {})
            for rule_id, rule_meta in compliance_rules.items():
                rules_text += f"- **{rule_meta.get('name', rule_id)}** ({rule_id}): {rule_meta.get('description', '')}\n"

            # 格式化核心实体
            entities_text: str = ""
            for entity in structured.get("core_entities", []):
                entities_text += f"- {entity}\n"

            return (
                f"### 业务背景\n{narrative}\n\n"
                f"### 核心业务实体\n{entities_text}\n"
                f"### 合规规则\n{rules_text}"
            )
        except Exception:
            return (
                "采购到付款（P2P）流程是企业采购管理的核心流程，"
                "从采购申请开始，经过采购订单审批、供应商发货、收货验收、"
                "发票核销，到最终付款结算。三路匹配是 P2P 合规控制的核心机制，"
                "要求采购订单（PO）、收货单（GR）、供应商发票（Invoice）"
                "在数量和金额上保持一致，偏差超过配置容差时需人工审核。"
            )

    def _get_or_build_agent(self) -> Any:
        """获取或延迟构建 LangChain Agent。

        首次调用时初始化 LLM 模型、工具集和系统提示词，
        使用 create_agent 组装完整的 Agent 实例并缓存。

        Returns:
            LangChain Agent 实例。
        """
        if self._agent is None:
            model = self._build_model()
            tools = self._build_tools()
            system_prompt = self._get_system_prompt()

            self._agent = create_agent(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                name="p2p_agent",
            )

        return self._agent

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    async def run(
        self,
        analysis_type: AnalysisType,
        query: str,
        params: dict[str, Any] | None = None,
        time_range_days: int = 30,
    ) -> dict[str, Any]:
        """供 Orchestrator 调用的异步入口。

        将 Orchestrator 的参数映射到 analyze()，并将 AnalysisResult
        转换为 Orchestrator 期望的 dict 格式。

        Args:
            analysis_type: 分析类型。
            query: 自然语言查询。
            params: 意图解析提取的额外参数（supplier_id, po_number 等）。
            time_range_days: 分析时间范围（天）。

        Returns:
            包含 anomalies, supplier_kpis, summary, report_markdown,
            completed_tasks, failed_tasks 的字典。
        """
        result = self.analyze(
            query=query,
            time_range_days=time_range_days,
        )

        return {
            "anomalies": [a.model_dump(mode="json") for a in result.anomalies],
            "supplier_kpis": [k.model_dump(mode="json") for k in result.supplier_kpis],
            "summary": result.summary,
            "report_markdown": result.report_markdown,
            "completed_tasks": result.completed_tasks,
            "failed_tasks": result.failed_tasks,
        }

    def analyze(
        self,
        query: str,
        user_id: str = "default",
        session_id: str = "",
        time_range_days: int = 30,
    ) -> AnalysisResult:
        """执行 P2P 分析任务。

        接收自然语言查询，驱动 Agent 调用工具链完成分析，
        返回结构化的 AnalysisResult。支持自动重试机制。

        Args:
            query: 自然语言分析查询，如 "检查供应商 SUP-001 的三路匹配情况"。
            user_id: 用户 ID，默认 "default"。
            session_id: 会话 ID，为空则自动生成。
            time_range_days: 分析时间范围（天），默认 30 天。

        Returns:
            AnalysisResult 实例，包含分析状态、异常列表、Markdown 报告等。
        """
        report_id: str = str(uuid.uuid4())
        if not session_id:
            session_id = str(uuid.uuid4())

        start_time: float = time.monotonic()
        max_retries: int = self._settings.llm.max_retries
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                agent = self._get_or_build_agent()

                # 构造带上下文的用户消息
                user_message: str = (
                    f"{query}\n\n"
                    f"[分析参数] 时间范围: 最近 {time_range_days} 天"
                )

                result: dict[str, Any] = agent.invoke({
                    "messages": [{"role": "user", "content": user_message}],
                })

                # 提取最终回复
                messages: list[Any] = result.get("messages", [])
                content: str = ""
                if messages:
                    last_message = messages[-1]
                    content = (
                        last_message.content
                        if hasattr(last_message, "content")
                        else str(last_message)
                    )

                # 尝试从 content 中解析结构化 JSON
                anomalies: list[dict[str, Any]] = []
                summary: dict[str, Any] = {}
                analysis_type: AnalysisType = AnalysisType.COMPREHENSIVE

                try:
                    parsed: Any = json.loads(content)
                    if isinstance(parsed, dict):
                        anomalies = parsed.get("anomalies", [])
                        summary = parsed.get("summary", {})
                        if "analysis_type" in parsed:
                            analysis_type = AnalysisType(parsed["analysis_type"])
                except (json.JSONDecodeError, ValueError):
                    pass  # content 是纯文本 Markdown 报告，无需解析

                elapsed_ms: float = (time.monotonic() - start_time) * 1000

                return AnalysisResult(
                    report_id=report_id,
                    status=AnalysisStatus.SUCCESS,
                    analysis_type=analysis_type,
                    query=query,
                    user_id=user_id,
                    session_id=session_id,
                    time_range=f"最近 {time_range_days} 天",
                    report_markdown=content,
                    summary=summary,
                    duration_ms=round(elapsed_ms, 2),
                    created_at=datetime.utcnow(),
                )

            except Exception as e:
                last_error = e
                # 重置 agent 以便下次重试时重新构建
                self._agent = None
                continue

        # 所有重试均失败
        elapsed_ms = (time.monotonic() - start_time) * 1000
        error_msg: str = str(last_error) if last_error else "未知错误"

        return AnalysisResult(
            report_id=report_id,
            status=AnalysisStatus.FAILED,
            analysis_type=AnalysisType.COMPREHENSIVE,
            query=query,
            user_id=user_id,
            session_id=session_id,
            time_range=f"最近 {time_range_days} 天",
            report_markdown="",
            error=ErrorInfo(
                code="AGENT_INVOKE_FAILED",
                message=f"P2P 分析失败: {error_msg}",
                retry_count=max_retries,
            ),
            duration_ms=round(elapsed_ms, 2),
            created_at=datetime.utcnow(),
        )
