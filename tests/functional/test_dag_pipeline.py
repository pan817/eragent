"""DAG 真实执行测试。

真实工具执行 4 种 DAG 模板，Report Agent 的 LLM 调用仍 mock。
"""

from __future__ import annotations

import json

import pytest

from api.schemas.domain import AnalysisType
from core.orchestrator.dag.executor import DAGExecutor
from core.orchestrator.dag.templates import load_dag_template
from modules.p2p.provider import P2PModuleProvider

_provider = P2PModuleProvider()
_REPORT_TOOLS = {"generate_report", "generate_summary_report"}


def _load(analysis_type: AnalysisType, **extra_params) -> list[dict]:
    params = {"days": 0, **extra_params}
    dag = load_dag_template(analysis_type, params, provider=_provider)
    assert dag is not None, f"no template for {analysis_type}"
    return dag


def _non_report_tasks(dag: list[dict]) -> list[dict]:
    return [t for t in dag if t.get("tool_name", "") not in _REPORT_TOOLS]


class TestDAGRealExecution:
    """DAG 真实工具执行（skip report 节点）。"""

    async def test_three_way_match_all_tasks_succeed(self, tool_registry):
        dag = _load(AnalysisType.THREE_WAY_MATCH)
        executor = DAGExecutor(registry=tool_registry)
        results = await executor.execute(dag)

        assert results["status"] == "ok"
        for task in _non_report_tasks(dag):
            tid = task["task_id"]
            assert tid in results["completed_tasks"], f"{tid} not completed"

    async def test_three_way_match_output_has_data(self, tool_registry):
        dag = _load(AnalysisType.THREE_WAY_MATCH)
        executor = DAGExecutor(registry=tool_registry)
        results = await executor.execute(dag)
        outputs = results["outputs"]
        assert len(outputs) >= 1
        for key, output in outputs.items():
            assert output, f"{key} output is empty"

    async def test_price_variance_succeeds(self, tool_registry):
        dag = _load(AnalysisType.PRICE_VARIANCE)
        executor = DAGExecutor(registry=tool_registry)
        results = await executor.execute(dag)
        assert not results["failed_tasks"]

    async def test_supplier_performance_with_entity(
        self, tool_registry, seed_data_summary,
    ):
        vid = seed_data_summary["supplier_ids"][0]
        dag = _load(AnalysisType.SUPPLIER_PERFORMANCE, vendor_id=vid)
        executor = DAGExecutor(registry=tool_registry)
        results = await executor.execute(dag)
        combined = " ".join(str(v) for v in results["outputs"].values())
        assert vid in combined

    async def test_dag_with_missing_data_graceful(self, tool_registry):
        dag = _load(AnalysisType.SUPPLIER_PERFORMANCE, vendor_id="NONEXISTENT-999")
        executor = DAGExecutor(registry=tool_registry)
        results = await executor.execute(dag)
        assert not results["failed_tasks"]


class TestDAGTemplateLoading:

    @pytest.mark.parametrize(
        "atype",
        [
            AnalysisType.THREE_WAY_MATCH,
            AnalysisType.PRICE_VARIANCE,
            AnalysisType.PAYMENT_COMPLIANCE,
            AnalysisType.SUPPLIER_PERFORMANCE,
        ],
    )
    def test_template_loads(self, atype):
        dag = load_dag_template(atype, {"days": 30}, provider=_provider)
        assert dag is not None
        assert len(dag) >= 2

    def test_params_substituted(self):
        dag = _load(AnalysisType.THREE_WAY_MATCH, days=60)
        for task in dag:
            inputs = task.get("inputs", {})
            for val in inputs.values():
                if isinstance(val, str):
                    assert "{days}" not in val

    def test_task_dependency_structure(self):
        dag = _load(AnalysisType.THREE_WAY_MATCH)
        task_ids = {t["task_id"] for t in dag}
        for task in dag:
            for dep in task.get("depends_on", []):
                assert dep in task_ids, f"{task['task_id']} depends on unknown {dep}"
