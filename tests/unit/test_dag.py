"""DAG 执行层单元测试（Registry / Validator / Templates / Executor / CaseStore）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

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


class TestStubTools:
    """存根工具测试。"""

    @pytest.mark.asyncio
    async def test_stub_tools_return_empty(self) -> None:
        """所有存根工具应返回空列表 JSON。"""
        from modules.p2p.tools import (
            query_material_master,
            run_vendor_risk_scoring,
            check_approval_limits,
            check_blacklist,
        )
        for tool in [query_material_master,
                     run_vendor_risk_scoring, check_approval_limits, check_blacklist]:
            result = await tool.ainvoke({})
            assert result == "[]"


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
        # 19 canonical + 5 aliases = 24
        assert len(reg.tool_names) == 24
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

    def test_load_comprehensive_no_entity_returns_none(self) -> None:
        """COMPREHENSIVE 无实体时返回 None（走 ReAct）。"""
        dag = load_dag_template(AnalysisType.COMPREHENSIVE, {})
        assert dag is None

    def test_load_po_risk_dag(self) -> None:
        """COMPREHENSIVE + po_number → PO 综合风险 DAG。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"po_number": "PO-001", "days": 30},
        )
        assert dag is not None
        assert len(dag) == 7  # 3 采集 + 3 分析 + 1 报告
        tools = [t["tool_name"] for t in dag]
        assert "run_three_way_match" in tools
        assert "run_price_variance_analysis" in tools
        assert "run_payment_compliance_check" in tools

    def test_load_supplier_risk_dag(self) -> None:
        """COMPREHENSIVE + supplier_id → 供应商综合分析 DAG。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"supplier_id": "SUP-001", "days": 30},
        )
        assert dag is not None
        assert len(dag) == 5
        tools = [t["tool_name"] for t in dag]
        assert "calculate_supplier_kpis" in tools
        assert "run_price_variance_analysis" in tools

    def test_load_payment_single_dag(self) -> None:
        """COMPREHENSIVE + payment_number → 单笔付款单合规 DAG。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"payment_number": "PAY-001", "days": 30},
        )
        assert dag is not None
        assert len(dag) == 4
        tools = [t["tool_name"] for t in dag]
        assert "query_payments" in tools
        assert "run_payment_compliance_check" in tools

    def test_load_invoice_single_dag(self) -> None:
        """COMPREHENSIVE + invoice_number → 单笔发票分析 DAG。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"invoice_number": "INV-001", "days": 30},
        )
        assert dag is not None
        assert len(dag) == 5
        tools = [t["tool_name"] for t in dag]
        assert "query_invoices" in tools
        assert "run_three_way_match" in tools

    def test_payment_takes_priority_over_po(self) -> None:
        """同时有 payment_number 和 po_number 时，付款单优先。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"payment_number": "PAY-001", "po_number": "PO-001"},
        )
        assert dag is not None
        assert len(dag) == 4  # payment_single DAG

    def test_po_risk_takes_priority_over_supplier(self) -> None:
        """同时有 po_number 和 supplier_id 时，PO 维度优先。"""
        dag = load_dag_template(
            AnalysisType.COMPREHENSIVE,
            {"po_number": "PO-001", "supplier_id": "SUP-001"},
        )
        assert dag is not None
        assert len(dag) == 7  # PO 风险 DAG

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

        assert result["status"] == "ok"
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

        assert result["status"] == "ok"
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

        assert result["status"] == "error"
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

        assert result["status"] == "ok"
        assert result["report"] == "# Report"
        mock_report.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_generation_failure_recorded(self) -> None:
        """ReportGenerationError 抛出时 DAG 应把 code/message 记录到 report_error。"""
        from modules.p2p.errors import ReportGenerationError

        reg = ToolRegistry()
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value='{"data": 1}')
        reg.register("query_purchase_orders", mock_tool)

        mock_report = MagicMock()
        mock_report.generate = AsyncMock(side_effect=ReportGenerationError(
            "LLM_CONNECTION_ERROR", "dashscope 无法访问",
        ))
        executor = DAGExecutor(registry=reg, report_agent=mock_report)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": [],
             "inputs": {}, "output_key": "po_data", "timeout_sec": 10},
            {"task_id": "t2", "tool_name": "generate_summary_report", "depends_on": ["t1"],
             "inputs": {"scenario": "测试"}, "output_key": "report", "timeout_sec": 10},
        ])
        # 工具成功 + 报告失败 → 整体 warning（有 completed 也有 failed）
        assert result["status"] == "warning"
        assert "t2" in result["failed_tasks"]
        assert result["report_error"] == {
            "code": "LLM_CONNECTION_ERROR",
            "message": "dashscope 无法访问",
        }

    @pytest.mark.asyncio
    async def test_report_timeout_recorded(self) -> None:
        """report 超时应记录 REPORT_TIMEOUT code。"""
        reg = ToolRegistry()
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value='{}')
        reg.register("query_purchase_orders", mock_tool)

        async def slow_report(*args, **kwargs):
            import asyncio
            await asyncio.sleep(5)
            return "# report"

        mock_report = MagicMock()
        mock_report.generate = slow_report
        executor = DAGExecutor(registry=reg, report_agent=mock_report)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": [],
             "inputs": {}, "output_key": "po", "timeout_sec": 5},
            {"task_id": "t2", "tool_name": "generate_summary_report", "depends_on": ["t1"],
             "inputs": {"scenario": "x"}, "output_key": "report", "timeout_sec": 0.1},
        ])
        assert result["report_error"]["code"] == "REPORT_TIMEOUT"

    @pytest.mark.asyncio
    async def test_report_generic_exception_recorded(self) -> None:
        """ReportAgent 抛非 ReportGenerationError 时应记录 REPORT_GEN_FAILED code。"""
        reg = ToolRegistry()
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value='{}')
        reg.register("query_purchase_orders", mock_tool)

        mock_report = MagicMock()
        mock_report.generate = AsyncMock(side_effect=RuntimeError("boom"))
        executor = DAGExecutor(registry=reg, report_agent=mock_report)
        result = await executor.execute([
            {"task_id": "t1", "tool_name": "query_purchase_orders", "depends_on": [],
             "inputs": {}, "output_key": "po", "timeout_sec": 10},
            {"task_id": "t2", "tool_name": "generate_summary_report", "depends_on": ["t1"],
             "inputs": {"scenario": "x"}, "output_key": "report", "timeout_sec": 10},
        ])
        assert result["report_error"]["code"] == "REPORT_GEN_FAILED"
        assert "boom" in result["report_error"]["message"]

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

    def _make_case_store(self) -> DAGCaseStore:
        """创建带 mock PG + Chroma 的 CaseStore。"""
        store = DAGCaseStore(settings=MagicMock())
        store._chroma_store = MagicMock()
        store._session_factory = MagicMock()
        return store

    @pytest.mark.asyncio
    async def test_store_successful_case(self) -> None:
        """成功案例应写入 PG 和 Chroma。"""
        store = self._make_case_store()

        with patch.object(store, "_write_to_pg") as mock_pg, \
             patch.object(store, "_write_to_chroma") as mock_chroma:
            await store.store_successful_case(
                query="分析三路匹配",
                analysis_type="three_way_match",
                dag=[{"task_id": "t1"}],
                route_type="DAG",
                exec_result={"status": "ok", "failed_tasks": {}, "duration_sec": 1.0},
            )

            mock_pg.assert_called_once()
            mock_chroma.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_failed_case(self) -> None:
        """失败案例不应存储。"""
        store = self._make_case_store()

        with patch.object(store, "_write_to_pg") as mock_pg:
            await store.store_successful_case(
                query="失败查询",
                analysis_type="three_way_match",
                dag=[],
                route_type="DAG",
                exec_result={"status": "error", "failed_tasks": {"t1": "err"}},
            )

            mock_pg.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_partial_case(self) -> None:
        """有失败任务的案例不应存储。"""
        store = self._make_case_store()

        with patch.object(store, "_write_to_pg") as mock_pg:
            await store.store_successful_case(
                query="部分失败",
                analysis_type="three_way_match",
                dag=[],
                route_type="DAG",
                exec_result={"status": "ok", "failed_tasks": {"t2": "err"}},
            )

            mock_pg.assert_not_called()

    def test_write_to_chroma(self) -> None:
        """_write_to_chroma 应调用 add_documents。"""
        store = self._make_case_store()
        store._write_to_chroma(
            query_hash="abc123",
            query="test query",
            analysis_type="three_way_match",
            dag=[{"task_id": "t1"}],
            route_type="DAG",
            duration_sec=1.5,
        )
        store._chroma_store.add_documents.assert_called_once()

    def test_load_pg_init_failure_returns_zero(self) -> None:
        """PG session 初始化失败应返回 0（不阻塞启动）。"""
        store = DAGCaseStore(settings=MagicMock())
        store._session_factory = None

        with patch.object(store, "_ensure_session_factory", side_effect=RuntimeError("pg down")):
            result = store.load_cases_from_pg()

        assert result == 0

    def test_chroma_failure_not_blocking(self) -> None:
        """Chroma 写入失败不应抛异常，返回 False。"""
        store = DAGCaseStore(settings=MagicMock())
        store._chroma_store = MagicMock()
        store._chroma_store.add_documents.side_effect = RuntimeError("chroma down")

        result = store._write_to_chroma(
            query_hash="x", query="q", analysis_type="t",
            dag=[], route_type="DAG", duration_sec=0,
        )
        assert result is False

    def test_write_to_chroma_success(self) -> None:
        """Chroma 写入成功应返回 True。"""
        store = self._make_case_store()
        result = store._write_to_chroma(
            query_hash="abc123", query="test", analysis_type="three_way_match",
            dag=[{"task_id": "t1"}], route_type="DAG", duration_sec=1.0,
        )
        assert result is True
        store._chroma_store.add_documents.assert_called_once()

    def test_write_to_pg_init_failure(self) -> None:
        """PG session 初始化失败应返回 False。"""
        store = DAGCaseStore(settings=MagicMock())
        with patch.object(store, "_ensure_session_factory", side_effect=RuntimeError("pg")):
            result = store._write_to_pg(
                query_hash="x", query="q", analysis_type="t",
                dag=[], route_type="DAG", duration_sec=0,
            )
        assert result is False

    def test_chroma_init_failure(self) -> None:
        """Chroma 初始化失败应返回 False。"""
        store = DAGCaseStore(settings=MagicMock())
        with patch.object(store, "_ensure_chroma", side_effect=RuntimeError("chroma")):
            result = store._write_to_chroma(
                query_hash="x", query="q", analysis_type="t",
                dag=[], route_type="DAG", duration_sec=0,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_store_records_pg_and_chroma_status(self) -> None:
        """store_successful_case 应同时调用 PG 和 Chroma 写入。"""
        store = self._make_case_store()

        with patch.object(store, "_write_to_pg", return_value=True) as mock_pg, \
             patch.object(store, "_write_to_chroma", return_value=True) as mock_chroma:
            await store.store_successful_case(
                query="测试查询",
                analysis_type="price_variance",
                dag=[{"task_id": "t1"}, {"task_id": "t2"}],
                route_type="DAG",
                exec_result={"status": "ok", "failed_tasks": {}, "duration_sec": 2.0},
            )
            mock_pg.assert_called_once()
            mock_chroma.assert_called_once()
            # 确认 query_hash 一致
            assert mock_pg.call_args.kwargs["query_hash"] == mock_chroma.call_args.kwargs["query_hash"]

    def test_pg_failure_not_blocking(self) -> None:
        """PG 写入失败不应抛异常。"""
        store = DAGCaseStore(settings=MagicMock())
        store._chroma_store = MagicMock()
        # session_factory 初始化失败
        store._session_factory = None

        with patch.object(store, "_ensure_session_factory", side_effect=RuntimeError("pg down")):
            # 不应抛异常
            store._write_to_pg(
                query_hash="x",
                query="q",
                analysis_type="t",
                dag=[],
                route_type="DAG",
                duration_sec=0,
            )
