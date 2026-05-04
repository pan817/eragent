"""Plan and Solve 在 Orchestrator 中的集成测试。

覆盖范围（均使用 mock LLM，不真实外连）：
- 路由分支：PS enabled/disabled、静态 DAG 命中不走 PS、RECALL/低置信度走 ReAct、
  DATA_LOOKUP 快捷路径 miss 进入 PS。
- 降级：Planning 超时 / plannable=False / 校验失败均降级到 ReAct，结果与直接 ReAct 一致。
- 成功路径：PS 成功后复用 _execute_dag 写入 + 构造 AnalysisResult，summary 标记
  route_type="plan_and_solve"。

真实 LLM 的 PS e2e 用例见 ``tests/integration/test_e2e.py``。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas.analysis import (
    AnalysisRequest,
    AnalysisStatus,
    AnalysisType,
)
from config.settings import Settings
from core.orchestrator.orchestrator import Orchestrator
from core.orchestrator.planner import ExecutionPlan, PlannedTask
from core.orchestrator.signal import IntentKind, QuerySignal


# ============================================================
# Fixtures & helpers
# ============================================================


def _make_mock_provider() -> MagicMock:
    """创建满足 ModuleProvider Protocol 的 mock provider。"""
    from modules.p2p.provider import P2PModuleProvider

    real = P2PModuleProvider()
    provider = MagicMock()
    provider.get_entity_types.return_value = real.get_entity_types()
    provider.get_analysis_keywords.return_value = real.get_analysis_keywords()
    provider.get_analysis_type_descriptions.return_value = real.get_analysis_type_descriptions()
    provider.get_role_descriptions.return_value = real.get_role_descriptions()
    provider.get_lookup_rules.return_value = real.get_lookup_rules()
    provider.get_dag_templates.return_value = real.get_dag_templates()
    provider.get_generic_dag_templates.return_value = real.get_generic_dag_templates()
    provider.get_reference_patterns.return_value = real.get_reference_patterns()
    provider.get_planning_prompt_template.return_value = real.get_planning_prompt_template()
    provider.format_tools_for_planning.side_effect = real.format_tools_for_planning
    provider.trim_to_token_budget.side_effect = real.trim_to_token_budget
    provider.build_memory_content.side_effect = real.build_memory_content
    provider.build_memory_metadata.side_effect = real.build_memory_metadata

    # 构建 mock 工具列表，使 ToolRegistry 可注册（需要 .name 属性）
    _tool_names = [
        "query_purchase_orders", "query_receipts", "query_invoices",
        "query_payments", "query_vendor_master",
        "run_three_way_match", "run_price_variance_analysis",
        "run_payment_compliance_check", "calculate_supplier_kpis",
        "calculate_spend_analysis", "detect_duplicate_invoices",
        "analyze_receipt_anomalies", "analyze_vendor_concentration",
        "analyze_discount_utilization", "calculate_po_cycle_time",
        "generate_summary_report",
    ]
    mock_tools = []
    for tn in _tool_names:
        t = MagicMock()
        t.name = tn
        t.__name__ = tn
        mock_tools.append(t)
    provider.get_tools.return_value = mock_tools

    provider.get_graphiti_client.return_value = None
    provider.is_query_backend_available.return_value = False
    return provider


@pytest.fixture()
def settings() -> Settings:
    """启用 PS 的测试 Settings（默认值即 enabled=True）。"""
    return Settings()


@pytest.fixture()
def settings_ps_disabled() -> Settings:
    """显式关闭 PS 的测试 Settings。"""
    s = Settings()
    s.plan_and_solve.enabled = False
    return s


def _l3_signal(
    intent_kind: IntentKind = IntentKind.ANALYSIS,
    confidence: float = 0.9,
    keywords: list[str] | None = None,
    entities: dict[str, Any] | None = None,
) -> QuerySignal:
    return QuerySignal(
        raw_query="x",
        intent_kind=intent_kind,
        keywords=keywords if keywords is not None else [AnalysisType.COMPREHENSIVE.value],
        entities=entities or {},
        route_level=3,
        confidence=confidence,
        reasoning="L3",
    )


def _mock_agent_result() -> dict[str, Any]:
    return {
        "anomalies": [],
        "supplier_kpis": [],
        "summary": {"total": 0},
        "report_markdown": "# ReAct Report",
        "completed_tasks": ["analysis"],
        "failed_tasks": [],
    }


def _install_mock_agent(orch: Orchestrator) -> MagicMock:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=_mock_agent_result())
    orch._agent = agent
    return agent


def _install_mock_planner(
    orch: Orchestrator,
    plan: ExecutionPlan | Exception | None = None,
) -> MagicMock:
    """向 Orchestrator 注入一个 mock Planner。

    - ``plan`` 为 ExecutionPlan：planner.plan 返回该计划。
    - ``plan`` 为 Exception：planner.plan 抛出该异常（用于测试超时）。
    - ``plan`` 为 None：planner.plan 返回 plannable=False。
    """
    planner = MagicMock()
    planner._tools_section_cache = None

    if isinstance(plan, Exception):
        planner.plan = AsyncMock(side_effect=plan)
    elif plan is None:
        planner.plan = AsyncMock(
            return_value=ExecutionPlan(plannable=False, reasoning="not plannable"),
        )
    else:
        planner.plan = AsyncMock(return_value=plan)

    # 使用真实的 plan_to_tasks / validate_plan 方法（走静态调用）
    from core.orchestrator.planner import Planner

    planner.plan_to_tasks = Planner.plan_to_tasks
    orch._planner = planner
    return planner


def _install_mock_dag_executor_success(
    orch: Orchestrator,
    report_text: str = "# PS Report",
) -> MagicMock:
    """注入一个 mock DAG executor，返回成功结果。"""
    executor = MagicMock()
    executor._registry = orch._lazy_tool_registry
    executor.execute = AsyncMock(
        return_value={
            "status": "ok",
            "outputs": {"report": report_text},
            "completed_tasks": ["t1", "t_report"],
            "failed_tasks": {},
            "report": report_text,
            "duration_sec": 0.5,
            "report_error": None,
        }
    )
    orch._dag_executor = executor
    return executor


def _plannable_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plannable=True,
        reasoning="simple query plan",
        tasks=[
            PlannedTask(
                task_id="t1",
                tool_name="query_purchase_orders",
                inputs={"days": 7, "vendor_id": "", "po_number": ""},
                depends_on=[],
            ),
            PlannedTask(
                task_id="t_report",
                tool_name="generate_summary_report",
                inputs={"scenario": "最新 PO"},
                depends_on=["t1"],
            ),
        ],
        report_scenario="最新 PO",
    )


# ============================================================
# 路由分支测试
# ============================================================


class TestPlanAndSolveRouting:

    @pytest.mark.asyncio
    async def test_ps_disabled_falls_through_to_react(
        self, settings_ps_disabled: Settings
    ) -> None:
        orch = Orchestrator(settings=settings_ps_disabled, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)
        # planner 永远不应被调用
        planner = _install_mock_planner(orch, _plannable_plan())

        with patch.object(
            orch._intent_router, "route", return_value=_l3_signal(confidence=0.9),
        ):
            result = await orch.analyze(AnalysisRequest(query="查询最新 PO"))

        assert result.status == AnalysisStatus.SUCCESS
        agent.run.assert_called_once()
        planner.plan.assert_not_called()
        assert result.summary.get("route_type") == "agent"

    @pytest.mark.asyncio
    async def test_recall_skips_plan_and_solve(self, settings: Settings) -> None:
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)
        planner = _install_mock_planner(orch, _plannable_plan())

        with patch.object(
            orch._intent_router, "route",
            return_value=_l3_signal(intent_kind=IntentKind.RECALL),
        ):
            result = await orch.analyze(AnalysisRequest(query="上次的分析结果呢"))

        assert result.status == AnalysisStatus.SUCCESS
        agent.run.assert_called_once()
        planner.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_skips_plan_and_solve(
        self, settings: Settings
    ) -> None:
        # 默认 l3_dag_min_confidence=0.5；confidence=0.3 属于 low_confidence
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)
        planner = _install_mock_planner(orch, _plannable_plan())

        with patch.object(
            orch._intent_router, "route",
            return_value=_l3_signal(confidence=0.3),
        ):
            result = await orch.analyze(AnalysisRequest(query="模糊的查询"))

        assert result.status == AnalysisStatus.SUCCESS
        agent.run.assert_called_once()
        planner.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_data_lookup_miss_enters_plan_and_solve(
        self, settings: Settings
    ) -> None:
        """DATA_LOOKUP 快捷路径 miss 后应尝试 PS。"""
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        _install_mock_agent(orch)
        planner = _install_mock_planner(orch, _plannable_plan())
        executor = _install_mock_dag_executor_success(orch, "# PS Data Lookup Report")

        # 构造一个不带实体的 DATA_LOOKUP signal 以避开 lookup 快捷路径命中
        sig = _l3_signal(intent_kind=IntentKind.DATA_LOOKUP, entities={})
        with patch.object(orch._intent_router, "route", return_value=sig):
            # 也 mock 掉 lookup 快捷路径让它返回 None（确保 miss）
            with patch.object(
                orch, "_try_lookup_shortcut", new=AsyncMock(return_value=None),
            ):
                result = await orch.analyze(AnalysisRequest(query="查询一下数据"))

        assert result.status == AnalysisStatus.SUCCESS
        planner.plan.assert_called_once()
        executor.execute.assert_called_once()
        assert result.summary.get("route_type") == "plan_and_solve"


# ============================================================
# 降级路径
# ============================================================


class TestPlanAndSolveFallback:

    @pytest.mark.asyncio
    async def test_planning_timeout_falls_back_to_react(
        self, settings: Settings
    ) -> None:
        # 把超时设为 0.01s 触发 timeout
        settings.plan_and_solve.planning_timeout_sec = 0  # 立即超时
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)

        # 让 planner.plan 无限等待触发 wait_for 超时
        async def _never_returns(**_kwargs: Any) -> ExecutionPlan:
            await asyncio.sleep(10)
            return ExecutionPlan(plannable=True)

        planner = MagicMock()
        planner.plan = AsyncMock(side_effect=_never_returns)
        orch._planner = planner

        with patch.object(
            orch._intent_router, "route", return_value=_l3_signal(confidence=0.9),
        ):
            result = await orch.analyze(AnalysisRequest(query="查询最新 PO"))

        assert result.status == AnalysisStatus.SUCCESS
        # 降级到 ReAct：agent 被调用
        agent.run.assert_called_once()
        assert result.summary.get("route_type") == "agent"

    @pytest.mark.asyncio
    async def test_not_plannable_falls_back_to_react(
        self, settings: Settings
    ) -> None:
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)
        _install_mock_planner(orch, None)  # plannable=False

        with patch.object(
            orch._intent_router, "route", return_value=_l3_signal(confidence=0.9),
        ):
            result = await orch.analyze(AnalysisRequest(query="需要动态决策的查询"))

        assert result.status == AnalysisStatus.SUCCESS
        agent.run.assert_called_once()
        assert result.summary.get("route_type") == "agent"

    @pytest.mark.asyncio
    async def test_validation_failure_falls_back_to_react(
        self, settings: Settings
    ) -> None:
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        agent = _install_mock_agent(orch)

        # 构造一个 plannable=True 但缺少报告节点的非法计划
        invalid_plan = ExecutionPlan(
            plannable=True,
            reasoning="missing report",
            tasks=[
                PlannedTask(
                    task_id="t1",
                    tool_name="query_purchase_orders",
                    inputs={},
                ),
            ],
        )
        _install_mock_planner(orch, invalid_plan)

        with patch.object(
            orch._intent_router, "route", return_value=_l3_signal(confidence=0.9),
        ):
            result = await orch.analyze(AnalysisRequest(query="查询最新 PO"))

        assert result.status == AnalysisStatus.SUCCESS
        agent.run.assert_called_once()
        assert result.summary.get("route_type") == "agent"


# ============================================================
# 成功路径（PS → DAG → Report）
# ============================================================


class TestPlanAndSolveSuccess:

    @pytest.mark.asyncio
    async def test_success_uses_dag_executor_and_marks_route_type(
        self, settings: Settings
    ) -> None:
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        _install_mock_agent(orch)
        planner = _install_mock_planner(orch, _plannable_plan())
        executor = _install_mock_dag_executor_success(orch)

        with patch.object(
            orch._intent_router, "route", return_value=_l3_signal(confidence=0.9),
        ):
            result = await orch.analyze(AnalysisRequest(query="查询最新 PO"))

        assert result.status == AnalysisStatus.SUCCESS
        assert result.summary.get("route_type") == "plan_and_solve"
        assert "PS Report" in result.report_markdown
        planner.plan.assert_called_once()
        executor.execute.assert_called_once()

        # 回归保护：PS 路径必须传 skip_case_store=True，避免 LLM 动态规划结果
        # 被作为静态 case 持久化后污染 L2.5 检索
        _, call_kwargs = executor.execute.call_args
        assert call_kwargs.get("skip_case_store") is True

        # planner 元信息回写到 summary
        assert "plan_reasoning" in result.summary
        assert "plan_task_count" in result.summary
        assert result.summary["plan_task_count"] == 2
        assert "plan_planning_duration_ms" in result.summary

    @pytest.mark.asyncio
    async def test_static_dag_still_takes_precedence(
        self, settings: Settings
    ) -> None:
        """明确的 analysis_type（非 COMPREHENSIVE）→ 走静态 DAG，不走 PS。"""
        orch = Orchestrator(settings=settings, provider=_make_mock_provider())
        _install_mock_agent(orch)
        planner = _install_mock_planner(orch, _plannable_plan())
        executor = _install_mock_dag_executor_success(orch, "# Static DAG")

        sig = _l3_signal(
            keywords=[AnalysisType.THREE_WAY_MATCH.value],
        )
        with patch.object(orch._intent_router, "route", return_value=sig):
            request = AnalysisRequest(
                query="做三路匹配",
                analysis_type=AnalysisType.THREE_WAY_MATCH,
            )
            result = await orch.analyze(request)

        assert result.status == AnalysisStatus.SUCCESS
        # 走的是静态 DAG 路径
        assert result.summary.get("route_type") == "DAG"
        planner.plan.assert_not_called()
        executor.execute.assert_called_once()

        # 对照：静态 DAG 路径应正常写入 case_store（skip_case_store=False）
        _, call_kwargs = executor.execute.call_args
        assert call_kwargs.get("skip_case_store") is False
