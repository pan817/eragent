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
            "vendor_id",
            "invoice_num",
            "check_number",
            "receipt_number",
        ]

    def get_tools(self) -> list[Any]:
        from config.settings import get_settings
        from modules.p2p.tools import get_tools_for_mode

        mode = get_settings().graphiti_etl.query_backend
        return get_tools_for_mode(mode)

    def get_dag_templates(self) -> dict[str, list[dict[str, Any]]]:
        from modules.p2p.dag_templates import get_entity_template_map, get_template_map

        return {
            "type_map": get_template_map(),
            "entity_map": get_entity_template_map(),
        }

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

    def get_planning_prompt_template(self) -> str:
        from modules.p2p.prompts import (
            build_planning_prompt_template,
            get_planning_report_tool_hint,
        )

        template = build_planning_prompt_template()
        return template.replace(
            "{report_tool_hint}", get_planning_report_tool_hint()
        )

    def format_tools_for_planning(self, tools: list[Any]) -> str:
        from modules.p2p.prompts import format_tools_for_planning

        return format_tools_for_planning(tools)
