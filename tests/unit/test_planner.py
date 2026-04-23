"""Plan and Solve Planner 单元测试。

覆盖：
- ExecutionPlan / PlannedTask Pydantic 模型
- Planner.plan_to_tasks 转换（含 report 默认字段补齐）
- Planner.validate_plan 校验（空 tasks / 报告节点唯一 / 工具合法 / 依赖）
- Planner 工具指纹缓存命中与模式切换后失效
- Planner.plan 的 mock LLM 交互（成功 / plannable=false / 异常 / 非法输出类型）
- Planning Prompt 渲染（Provider 注入 vs 默认兜底）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import PlanAndSolveSettings, Settings
from core.orchestrator.dag.registry import ToolRegistry
from core.orchestrator.planner import (
    ExecutionPlan,
    PlannedTask,
    Planner,
)


# ============================================================
# 辅助工具
# ============================================================


def _make_tool(name: str, desc: str = "", args: dict[str, Any] | None = None) -> MagicMock:
    """构造一个最简 LangChain tool 替身。"""
    tool = MagicMock()
    tool.name = name
    tool.description = desc
    tool.args = args or {}
    return tool


def _make_registry(tool_names: list[str]) -> ToolRegistry:
    from core.orchestrator.dag.registry import _infer_category

    reg = ToolRegistry()
    for n in tool_names:
        reg.register(n, AsyncMock(), category=_infer_category(n))
    return reg


def _make_settings(use_fast_model: bool = True) -> Settings:
    s = Settings(app_name="test")
    s.plan_and_solve = PlanAndSolveSettings(
        enabled=True,
        use_fast_model=use_fast_model,
        max_planning_tokens=1000,
        planning_timeout_sec=10,
        validate_plan=True,
    )
    return s


def _simple_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plannable=True,
        reasoning="simple lookup",
        tasks=[
            PlannedTask(
                task_id="t1",
                tool_name="query_purchase_orders",
                inputs={"days": 7},
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
# ExecutionPlan / PlannedTask
# ============================================================


class TestExecutionPlan:

    def test_defaults(self) -> None:
        plan = ExecutionPlan(plannable=False)
        assert plan.plannable is False
        assert plan.reasoning == ""
        assert plan.tasks == []
        assert plan.report_scenario == ""

    def test_planned_task_defaults(self) -> None:
        task = PlannedTask(task_id="t1", tool_name="query_purchase_orders")
        assert task.inputs == {}
        assert task.depends_on == []
        assert task.output_key == ""
        assert task.timeout_sec == 0


# ============================================================
# Planner.plan_to_tasks
# ============================================================


class TestPlanToTasks:

    def test_basic_conversion(self) -> None:
        plan = _simple_plan()
        tasks = Planner.plan_to_tasks(plan)

        assert len(tasks) == 2
        assert tasks[0]["task_id"] == "t1"
        assert tasks[0]["tool_name"] == "query_purchase_orders"
        assert tasks[0]["output_key"] == "t1"
        assert tasks[0]["timeout_sec"] == 60

        assert tasks[1]["task_id"] == "t_report"
        assert tasks[1]["output_key"] == "report"
        assert tasks[1]["timeout_sec"] == 180

    def test_report_scenario_fallback(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(task_id="t1", tool_name="query_purchase_orders"),
                PlannedTask(
                    task_id="t_report",
                    tool_name="generate_summary_report",
                    inputs={},
                    depends_on=["t1"],
                ),
            ],
            report_scenario="采购健康度",
        )
        tasks = Planner.plan_to_tasks(plan)
        assert tasks[1]["inputs"]["scenario"] == "采购健康度"

    def test_report_scenario_default_when_no_report_scenario(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(task_id="t1", tool_name="query_purchase_orders"),
                PlannedTask(
                    task_id="t_report",
                    tool_name="generate_summary_report",
                    depends_on=["t1"],
                ),
            ],
        )
        tasks = Planner.plan_to_tasks(plan)
        assert tasks[1]["inputs"]["scenario"] == "采购分析"

    def test_explicit_timeout_and_output_key_preserved(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(
                    task_id="t1",
                    tool_name="query_purchase_orders",
                    timeout_sec=120,
                    output_key="po_rows",
                ),
            ],
        )
        tasks = Planner.plan_to_tasks(plan)
        assert tasks[0]["timeout_sec"] == 120
        assert tasks[0]["output_key"] == "po_rows"


# ============================================================
# Planner.validate_plan
# ============================================================


class TestValidatePlan:

    def test_not_plannable_skips_validation(self) -> None:
        plan = ExecutionPlan(plannable=False)
        registry = _make_registry(["query_purchase_orders"])
        assert Planner.validate_plan(plan, registry) == []

    def test_plannable_empty_tasks_errors(self) -> None:
        plan = ExecutionPlan(plannable=True)
        registry = _make_registry(["query_purchase_orders"])
        errors = Planner.validate_plan(plan, registry)
        assert errors and "empty" in errors[0]

    def test_missing_report_task_errors(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(task_id="t1", tool_name="query_purchase_orders"),
            ],
        )
        registry = _make_registry(["query_purchase_orders"])
        errors = Planner.validate_plan(plan, registry)
        assert errors and "generate_summary_report" in errors[0]

    def test_duplicate_report_task_errors(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(task_id="t1", tool_name="query_purchase_orders"),
                PlannedTask(
                    task_id="t_r1",
                    tool_name="generate_summary_report",
                    depends_on=["t1"],
                ),
                PlannedTask(
                    task_id="t_r2",
                    tool_name="generate_summary_report",
                    depends_on=["t1"],
                ),
            ],
        )
        registry = _make_registry(["query_purchase_orders"])
        errors = Planner.validate_plan(plan, registry)
        assert errors and "2 report tasks" in errors[0]

    def test_valid_plan_passes(self) -> None:
        plan = _simple_plan()
        registry = _make_registry(["query_purchase_orders"])
        assert Planner.validate_plan(plan, registry) == []

    def test_unknown_tool_errors(self) -> None:
        plan = ExecutionPlan(
            plannable=True,
            tasks=[
                PlannedTask(task_id="t1", tool_name="nonexistent_tool"),
                PlannedTask(
                    task_id="t_report",
                    tool_name="generate_summary_report",
                    depends_on=["t1"],
                ),
            ],
        )
        registry = _make_registry(["query_purchase_orders"])
        errors = Planner.validate_plan(plan, registry)
        assert errors and "nonexistent_tool" in errors[0]


# ============================================================
# Planner 工具格式化与缓存（Q7）
# ============================================================


class TestToolsCache:

    def test_fingerprint_stable_across_order(self) -> None:
        planner = Planner(settings=_make_settings())
        tools_a = [_make_tool("query_purchase_orders"), _make_tool("run_three_way_match")]
        tools_b = [_make_tool("run_three_way_match"), _make_tool("query_purchase_orders")]
        assert planner._tools_fingerprint(tools_a) == planner._tools_fingerprint(tools_b)

    def test_fingerprint_changes_when_tool_set_changes(self) -> None:
        planner = Planner(settings=_make_settings())
        tools_a = [_make_tool("query_purchase_orders")]
        tools_b = [_make_tool("query_purchase_orders"), _make_tool("run_three_way_match")]
        assert planner._tools_fingerprint(tools_a) != planner._tools_fingerprint(tools_b)

    def test_cache_hit_reuses_text(self) -> None:
        planner = Planner(settings=_make_settings())
        tools = [_make_tool("query_purchase_orders", "查询 PO", {"days": {"type": "integer"}})]
        first = planner._format_tools_section(tools)
        second = planner._format_tools_section(tools)
        # 同一实例多次调用结果一致且 cache 命中
        assert first == second
        assert planner._tools_section_cache is not None

    def test_cache_invalidates_on_new_toolset(self) -> None:
        planner = Planner(settings=_make_settings())
        tools_a = [_make_tool("query_purchase_orders")]
        tools_b = [_make_tool("run_three_way_match")]
        first = planner._format_tools_section(tools_a)
        second = planner._format_tools_section(tools_b)
        # 不同工具集 → 指纹不同 → 输出文本应不同
        assert first != second

    def test_default_format_fallback_when_no_provider(self) -> None:
        planner = Planner(settings=_make_settings(), provider=None)
        tools = [_make_tool("query_purchase_orders", "查询 PO")]
        text = planner._format_tools_section(tools)
        assert "query_purchase_orders" in text

    def test_provider_format_used_when_present(self) -> None:
        provider = MagicMock()
        provider.format_tools_for_planning.return_value = "## PROVIDER_FORMAT"
        planner = Planner(settings=_make_settings(), provider=provider)
        tools = [_make_tool("query_purchase_orders")]
        text = planner._format_tools_section(tools)
        assert text == "## PROVIDER_FORMAT"
        provider.format_tools_for_planning.assert_called_once()


# ============================================================
# Planner.plan（mock LLM）
# ============================================================


class TestPlan:

    def _make_planner_with_mock_llm(self, llm_return: Any) -> Planner:
        planner = Planner(settings=_make_settings(), provider=None)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=llm_return)
        planner._llm = mock_llm
        return planner

    @pytest.mark.asyncio
    async def test_successful_plan_returned_as_is(self) -> None:
        expected = _simple_plan()
        planner = self._make_planner_with_mock_llm(expected)
        result = await planner.plan(
            query="查询最新的 PO",
            tools=[_make_tool("query_purchase_orders")],
            params={"days": 7},
            time_range_days=7,
        )
        assert result is expected

    @pytest.mark.asyncio
    async def test_dict_output_coerced_to_execution_plan(self) -> None:
        raw = {
            "plannable": True,
            "reasoning": "dict",
            "tasks": [],
            "report_scenario": "x",
        }
        planner = self._make_planner_with_mock_llm(raw)
        result = await planner.plan(
            query="q",
            tools=[_make_tool("query_purchase_orders")],
            params={},
            time_range_days=0,
        )
        assert isinstance(result, ExecutionPlan)
        assert result.plannable is True

    @pytest.mark.asyncio
    async def test_llm_exception_returns_not_plannable(self) -> None:
        planner = Planner(settings=_make_settings(), provider=None)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        planner._llm = mock_llm
        result = await planner.plan(
            query="q",
            tools=[_make_tool("query_purchase_orders")],
            params={},
            time_range_days=0,
        )
        assert result.plannable is False
        assert "llm_error" in result.reasoning

    @pytest.mark.asyncio
    async def test_invalid_output_type_returns_not_plannable(self) -> None:
        planner = self._make_planner_with_mock_llm("this is not a plan")
        result = await planner.plan(
            query="q",
            tools=[_make_tool("query_purchase_orders")],
            params={},
            time_range_days=0,
        )
        assert result.plannable is False
        assert "invalid_output_type" in result.reasoning


# ============================================================
# _build_prompt 占位符替换
# ============================================================


class TestBuildPrompt:

    def test_placeholders_filled(self) -> None:
        provider = MagicMock()
        provider.get_planning_prompt_template.return_value = (
            "Q:{query}\nP:{params_json}\nT:{tools_section}\nD:{time_range_days}"
        )
        provider.format_tools_for_planning.return_value = "TOOLSECTION"
        planner = Planner(settings=_make_settings(), provider=provider)

        prompt = planner._build_prompt(
            query="查询 PO",
            tools_section="TOOLSECTION",
            params={"days": 7, "vendor_id": "SUP-001"},
            time_range_days=7,
        )
        assert "Q:查询 PO" in prompt
        assert "T:TOOLSECTION" in prompt
        assert "D:7" in prompt
        assert '"days": 7' in prompt
        assert '"vendor_id": "SUP-001"' in prompt
