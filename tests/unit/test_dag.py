"""DAG 执行层单元测试（Registry / Validator / Templates / Executor / CaseStore）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas.analysis import AnalysisType
from core.orchestrator.dag.registry import ToolRegistry, build_default_registry
from core.orchestrator.dag.validator import DAGValidator
from core.orchestrator.dag.templates import load_dag_template
from core.orchestrator.dag.executor import DAGExecutor
from core.orchestrator.dag.case_store import DAGCaseStore


# ============================================================
# ToolRegistry
# ============================================================


class TestToolRegistry:

    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        fn = AsyncMock()
        reg.register("my_tool", fn)
        assert reg.get("my_tool") is fn
        assert "my_tool" in reg

    def test_get_missing(self) -> None:
        reg = ToolRegistry()
        assert reg.get("nope") is None
        assert "nope" not in reg

    def test_alias(self) -> None:
        reg = ToolRegistry()
        fn = AsyncMock()
        reg.register("canonical", fn)
        reg.register_alias("alias", "canonical")
        assert reg.get("alias") is fn

    def test_alias_missing_canonical(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.register_alias("alias", "nonexistent")

    def test_build_default_registry(self) -> None:
        reg = build_default_registry()
        # 15 canonical + 5 aliases = 20
        assert len(reg.tool_names) == 20
        # aliases resolve correctly
        assert reg.get("query_goods_receipts") is reg.get("query_receipts")
        assert reg.get("calculate_ppv") is reg.get("run_price_variance_analysis")
        assert reg.get("get_vendor_scorecard") is reg.get("calculate_supplier_kpis")
        assert reg.get("validate_compliance") is reg.get("run_payment_compliance_check")
        assert reg.get("query_vendor_invoices") is reg.get("query_invoices")


# ============================================================
# DAGValidator
# ============================================================


class TestDAGValidator:

    def _make_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        for name in [
            "query_purchase_orders", "query_receipts", "query_invoices",
            "query_payments", "query_vendor_master",
            "run_three_way_match", "run_price_variance_analysis",
            "run_payment_compliance_check", "calculate_supplier_kpis",
        ]:
            reg.register(name, AsyncMock())
        return reg

    def test_valid_dag(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": []},
            {"task_id": "t2", "tool_name": "run_three_way_match", "depends_on": ["t1"]},
            {"task_id": "t3", "tool_name": "generate_summary_report", "depends_on": ["t2"]},
        ])
        assert ok, err

    def test_empty_tasks(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([])
        assert not ok
        assert "不能为空" in err

    def test_too_many_tasks(self) -> None:
        v = DAGValidator(self._make_registry())
        tasks = [
            {"task_id": f"t{i}", "tool_name": "query_purchase_orders", "depends_on": []}
            for i in range(13)
        ]
        ok, err = v.validate(tasks)
        assert not ok
        assert "上限" in err

    def test_duplicate_task_ids(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": []},
            {"task_id": "t1", "tool_name": "query_receipts", "depends_on": []},
        ])
        assert not ok
        assert "重复" in err

    def test_invalid_tool(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "nonexistent_tool", "depends_on": []},
        ])
        assert not ok
        assert "非法工具" in err

    def test_missing_dependency(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": ["t99"]},
        ])
        assert not ok
        assert "不存在" in err

    def test_cycle_detection(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": ["t2"]},
            {"task_id": "t2", "tool_name": "query_receipts", "depends_on": ["t1"]},
        ])
        assert not ok
        assert "循环" in err

    def test_analysis_without_data_dependency(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "run_three_way_match", "depends_on": []},
        ])
        assert not ok
        assert "数据采集" in err

    def test_report_node_as_dependency(self) -> None:
        v = DAGValidator(self._make_registry())
        ok, err = v.validate([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": []},
            {"task_id": "t2", "tool_name": "generate_summary_report", "depends_on": ["t1"]},
            {"task_id": "t3", "tool_name": "query_receipts", "depends_on": ["t2"]},
        ])
        assert not ok
        assert "不能被其他任务依赖" in err


# ============================================================
# DAG Templates
# ============================================================


class TestDAGTemplates:

    def test_load_three_way_match(self) -> None:
        dag = load_dag_template(AnalysisType.THREE_WAY_MATCH, {"days": 60, "supplier_id": "SUP-001"})
        assert dag is not None
        assert len(dag) == 5
        # 参数应被替换
        assert dag[0]["inputs"]["days"] == 60
        assert dag[0]["inputs"]["supplier_id"] == "SUP-001"

    def test_load_price_variance(self) -> None:
        dag = load_dag_template(AnalysisType.PRICE_VARIANCE, {})
        assert dag is not None
        assert len(dag) == 3

    def test_load_payment_compliance(self) -> None:
        dag = load_dag_template(AnalysisType.PAYMENT_COMPLIANCE, {})
        assert dag is not None
        assert len(dag) == 4

    def test_load_supplier_performance(self) -> None:
        dag = load_dag_template(AnalysisType.SUPPLIER_PERFORMANCE, {"supplier_id": "SUP-001"})
        assert dag is not None
        assert len(dag) == 3

    def test_load_comprehensive_returns_none(self) -> None:
        dag = load_dag_template(AnalysisType.COMPREHENSIVE, {})
        assert dag is None

    def test_default_params(self) -> None:
        dag = load_dag_template(AnalysisType.THREE_WAY_MATCH, {})
        assert dag[0]["inputs"]["days"] == 30
        assert dag[0]["inputs"]["supplier_id"] == ""


# ============================================================
# DAG Executor
# ============================================================


class TestDAGExecutor:

    @pytest.mark.asyncio
    async def test_execute_simple_dag(self) -> None:
        """简单 DAG 应成功执行。"""
        reg = ToolRegistry()

        mock_query = AsyncMock()
        mock_query.ainvoke = AsyncMock(return_value='[{"po": "PO-001"}]')
        reg.register("query_purchase_orders", mock_query)

        executor = DAGExecutor(registry=reg)
        result = await executor.execute([
            {
                "task_id": "t1",
                "tool_name": "query_purchase_orders",
                "depends_on": [],
                "inputs": {"days": 30},
                "output_key": "po_data",
                "timeout_sec": 10,
            },
        ])

        assert result["status"] == "completed"
        assert "po_data" in result["outputs"]
        assert "t1" in result["completed_tasks"]

    @pytest.mark.asyncio
    async def test_execute_with_dependencies(self) -> None:
        """有依赖的任务应在前置完成后执行。"""
        reg = ToolRegistry()

        mock_t1 = AsyncMock()
        mock_t1.ainvoke = AsyncMock(return_value="data1")
        mock_t2 = AsyncMock()
        mock_t2.ainvoke = AsyncMock(return_value="data2")

        reg.register("tool_a", mock_t1)
        reg.register("tool_b", mock_t2)

        executor = DAGExecutor(registry=reg)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "tool_a", "depends_on": [],
             "inputs": {}, "output_key": "out1", "timeout_sec": 10},
            {"task_id": "t2", "tool_name": "tool_b", "depends_on": ["t1"],
             "inputs": {}, "output_key": "out2", "timeout_sec": 10},
        ])

        assert result["status"] == "completed"
        assert len(result["completed_tasks"]) == 2

    @pytest.mark.asyncio
    async def test_execute_failed_task(self) -> None:
        """工具执行失败应记录到 failed_tasks。"""
        reg = ToolRegistry()

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(side_effect=RuntimeError("db error"))
        reg.register("broken_tool", mock_tool)

        executor = DAGExecutor(registry=reg)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "broken_tool", "depends_on": [],
             "inputs": {}, "output_key": "out", "timeout_sec": 10},
        ])

        assert result["status"] == "failed"
        assert "t1" in result["failed_tasks"]

    @pytest.mark.asyncio
    async def test_execute_cascade_failure(self) -> None:
        """前置任务失败应导致依赖任务也失败。"""
        reg = ToolRegistry()

        mock_t1 = AsyncMock()
        mock_t1.ainvoke = AsyncMock(side_effect=RuntimeError("fail"))
        mock_t2 = AsyncMock()
        mock_t2.ainvoke = AsyncMock(return_value="ok")

        reg.register("tool_a", mock_t1)
        reg.register("tool_b", mock_t2)

        executor = DAGExecutor(registry=reg)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "tool_a", "depends_on": [],
             "inputs": {}, "output_key": "out1", "timeout_sec": 10},
            {"task_id": "t2", "tool_name": "tool_b", "depends_on": ["t1"],
             "inputs": {}, "output_key": "out2", "timeout_sec": 10},
        ])

        assert "t1" in result["failed_tasks"]
        assert "t2" in result["failed_tasks"]

    @pytest.mark.asyncio
    async def test_execute_with_report_agent(self) -> None:
        """报告节点应调用 ReportAgent。"""
        reg = ToolRegistry()

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value='{"data": 1}')
        reg.register("query_purchase_orders", mock_tool)

        mock_report = MagicMock()
        mock_report.generate = AsyncMock(return_value="# Report")

        executor = DAGExecutor(registry=reg, report_agent=mock_report)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": [],
             "inputs": {}, "output_key": "po_data", "timeout_sec": 10},
            {"task_id": "t2", "tool_name": "generate_summary_report", "depends_on": ["t1"],
             "inputs": {"scenario": "测试"}, "output_key": "report", "timeout_sec": 10},
        ])

        assert result["status"] == "completed"
        assert result["report"] == "# Report"
        mock_report.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_tool(self) -> None:
        """未注册的工具应记录到 failed_tasks。"""
        reg = ToolRegistry()
        executor = DAGExecutor(registry=reg)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "nonexistent", "depends_on": [],
             "inputs": {}, "output_key": "out", "timeout_sec": 10},
        ])
        assert "t1" in result["failed_tasks"]


# ============================================================
# DAGCaseStore
# ============================================================


class TestDAGCaseStore:

    @pytest.mark.asyncio
    async def test_store_successful_case(self) -> None:
        """成功案例应写入 Chroma。"""
        mock_store = MagicMock()
        case_store = DAGCaseStore(settings=MagicMock())
        case_store._store = mock_store

        await case_store.store_successful_case(
            query="分析三路匹配",
            analysis_type="three_way_match",
            dag=[{"task_id": "t1"}],
            route_type="DAG",
            exec_result={"status": "completed", "failed_tasks": {}, "duration_sec": 1.0},
        )

        mock_store.add_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_failed_case(self) -> None:
        """失败案例不应存储。"""
        mock_store = MagicMock()
        case_store = DAGCaseStore(settings=MagicMock())
        case_store._store = mock_store

        await case_store.store_successful_case(
            query="失败查询",
            analysis_type="three_way_match",
            dag=[],
            route_type="DAG",
            exec_result={"status": "failed", "failed_tasks": {"t1": "err"}},
        )

        mock_store.add_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_partial_case(self) -> None:
        """有失败任务的案例不应存储。"""
        mock_store = MagicMock()
        case_store = DAGCaseStore(settings=MagicMock())
        case_store._store = mock_store

        await case_store.store_successful_case(
            query="部分失败",
            analysis_type="three_way_match",
            dag=[],
            route_type="DAG",
            exec_result={"status": "completed", "failed_tasks": {"t2": "err"}},
        )

        mock_store.add_documents.assert_not_called()
