"""P2P 模块 ModuleProvider 实现。

向 Orchestrator 提供 P2P 模块的 Agent、ReportAgent、Repository、
实体类型、工具集、DAG 模板和记忆构建逻辑。
"""

from __future__ import annotations

from typing import Any


class P2PModuleProvider:
    """P2P 模块能力提供者，实现 core.orchestrator.provider.ModuleProvider。"""

    def get_agent(
        self,
        settings: Any,
        timing_middleware: Any,
        checkpointer: Any | None,
    ) -> Any:
        from modules.p2p.agent import P2PAgent

        return P2PAgent(
            settings=settings,
            timing_middleware=timing_middleware,
            checkpointer=checkpointer,
        )

    def get_report_agent(self, settings: Any) -> Any:
        from modules.p2p.report_agent import ReportAgent

        return ReportAgent(settings=settings)

    def get_repository(self) -> Any:
        from modules.p2p.tools import _get_repository

        return _get_repository()

    def get_entity_types(self) -> list[str]:
        return [
            "po_number",
            "supplier_id",
            "invoice_number",
            "payment_number",
            "receipt_number",
        ]

    def get_tools(self) -> list[Any]:
        from modules.p2p.tools import (
            analyze_discount_utilization,
            analyze_receipt_anomalies,
            analyze_vendor_concentration,
            calculate_po_cycle_time,
            calculate_spend_analysis,
            calculate_supplier_kpis,
            check_approval_limits,
            check_blacklist,
            detect_duplicate_invoices,
            query_invoices,
            query_material_master,
            query_payments,
            query_purchase_orders,
            query_receipts,
            query_vendor_master,
            run_payment_compliance_check,
            run_price_variance_analysis,
            run_three_way_match,
            run_vendor_risk_scoring,
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
            query_vendor_master,
            calculate_spend_analysis,
            analyze_receipt_anomalies,
            detect_duplicate_invoices,
            analyze_discount_utilization,
            analyze_vendor_concentration,
            calculate_po_cycle_time,
            query_material_master,
            run_vendor_risk_scoring,
            check_approval_limits,
            check_blacklist,
        ]

    def get_dag_templates(self) -> dict[str, list[dict[str, Any]]]:
        # Phase 5 时从 templates.py 迁入模板；当前阶段仍由
        # load_dag_template() 直接加载，此方法为 Protocol 占位。
        return {}

    def get_reference_patterns(self) -> list[tuple[str, str, str]]:
        from core.orchestrator.entity import _REF_PATTERNS

        return _REF_PATTERNS

    def build_memory_content(
        self,
        query: str,
        response: str,
        summary: dict[str, Any],
    ) -> str:
        from modules.p2p.agent import _build_memory_content

        return _build_memory_content(query=query, response=response, summary=summary)

    def build_memory_metadata(
        self,
        query: str,
        analysis_type: str,
        anomalies: list[dict[str, Any]],
        summary: dict[str, Any],
        time_range_days: int,
    ) -> dict[str, Any]:
        from modules.p2p.agent import _build_memory_metadata

        return _build_memory_metadata(
            query=query,
            analysis_type=analysis_type,
            anomalies=anomalies,
            summary=summary,
            time_range_days=time_range_days,
        )

    def trim_to_token_budget(
        self,
        text: str,
        max_tokens: int,
        label: str,
    ) -> str:
        from modules.p2p.prompts import trim_to_token_budget

        return trim_to_token_budget(text, max_tokens, label)
