"""
Orchestrator 编排器模块。

负责接收分析请求、解析意图、路由到对应的 Agent 执行分析，
并将结果封装为统一的 AnalysisResult 返回。

MVP 阶段采用粗粒度编排，后续可扩展为多 Agent 协作模式。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    ErrorInfo,
)
from config.settings import Settings, get_settings
from core.orchestrator.intent import IntentParser


class Orchestrator:
    """P2P 分析编排器。

    协调意图解析、Agent 调度和结果封装的核心组件。
    采用延迟初始化策略，避免启动时加载重量级依赖。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化编排器。

        Args:
            settings: 全局配置对象，为 None 时自动加载默认配置。
        """
        if settings is None:
            settings = get_settings()
        self._settings: Settings = settings
        self._agent: Any = None
        self._init_components()

    def _init_components(self) -> None:
        """初始化轻量级组件。

        仅初始化不涉及外部资源的组件（如 IntentParser），
        重量级组件（如 P2PAgent）通过延迟属性按需创建。
        """
        self._intent_parser: IntentParser = IntentParser()

    @property
    def _lazy_agent(self) -> Any:
        """延迟初始化 P2PAgent 实例。

        首次访问时创建 Agent，后续访问直接复用，
        避免应用启动时就加载 LLM 模型等重量级资源。

        Returns:
            P2PAgent 实例。
        """
        if self._agent is None:
            from modules.p2p.agent import P2PAgent

            self._agent = P2PAgent(settings=self._settings)
        return self._agent

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """执行分析请求的完整编排流程。

        流程步骤：
        1. 调用 IntentParser 解析用户查询的意图和参数。
        2. 如果请求中显式指定了 analysis_type，则优先使用。
        3. 根据分析类型路由到 P2PAgent 执行具体分析。
        4. 生成 report_id 并封装为 AnalysisResult 返回。

        Args:
            request: 分析请求对象，包含查询文本、用户信息等。

        Returns:
            封装好的分析结果，包含异常记录、KPI 报告等。
        """
        start_time = time.monotonic()
        report_id = str(uuid.uuid4())
        session_id = request.session_id or str(uuid.uuid4())

        try:
            # 1. 意图解析
            parsed_type, parsed_params = self._intent_parser.parse(request.query)

            # 2. 显式指定的类型优先级更高
            analysis_type: AnalysisType = request.analysis_type or parsed_type

            # 3. 合并参数
            time_range_days: int = (
                request.time_range_days
                or parsed_params.get("days")
                or self._settings.analysis.default_time_range_days
            )

            # 4. 路由到 Agent 执行分析
            agent_result: dict[str, Any] = await self._lazy_agent.run(
                analysis_type=analysis_type,
                query=request.query,
                params=parsed_params,
                time_range_days=time_range_days,
            )

            # 5. 封装结果
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return AnalysisResult(
                report_id=report_id,
                status=AnalysisStatus.SUCCESS,
                analysis_type=analysis_type,
                query=request.query,
                user_id=request.user_id,
                session_id=session_id,
                time_range=f"最近 {time_range_days} 天",
                anomalies=agent_result.get("anomalies", []),
                supplier_kpis=agent_result.get("supplier_kpis", []),
                summary=agent_result.get("summary", {}),
                report_markdown=agent_result.get("report_markdown", ""),
                completed_tasks=agent_result.get("completed_tasks", []),
                failed_tasks=agent_result.get("failed_tasks", []),
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return AnalysisResult(
                report_id=report_id,
                status=AnalysisStatus.FAILED,
                analysis_type=request.analysis_type or AnalysisType.COMPREHENSIVE,
                query=request.query,
                user_id=request.user_id,
                session_id=session_id,
                time_range="",
                error=ErrorInfo(
                    code="ORCHESTRATOR_ERROR",
                    message=str(exc),
                ),
                duration_ms=duration_ms,
            )
