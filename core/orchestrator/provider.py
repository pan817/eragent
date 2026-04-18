"""ModuleProvider Protocol：业务模块向 Orchestrator 提供的能力接口。

Orchestrator 通过此接口获取业务模块的 Agent、ReportAgent、Repository、
实体类型、工具列表等，不直接 import 具体模块代码。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModuleProvider(Protocol):
    """业务模块能力提供者。

    每个业务模块（如 P2P、O2C）实现此接口，在 api/main.py
    启动时注册到 Orchestrator。
    """

    def get_agent(
        self,
        settings: Any,
        timing_middleware: Any,
        checkpointer: Any | None,
    ) -> Any:
        """创建业务 Agent 实例（ReAct 模式）。"""
        ...

    def get_report_agent(self, settings: Any) -> Any:
        """创建报告生成 Agent 实例。"""
        ...

    def get_repository(self) -> Any:
        """获取数据查询 Repository 实例。"""
        ...

    def get_entity_types(self) -> list[str]:
        """返回模块支持的实体类型列表（如 po_number, supplier_id 等）。"""
        ...

    def get_tools(self) -> list[Any]:
        """返回模块提供的全部 @tool 函数列表。"""
        ...

    def get_dag_templates(self) -> dict[str, list[dict[str, Any]]]:
        """返回 AnalysisType → DAG 模板映射。"""
        ...

    def get_reference_patterns(self) -> list[tuple[str, str, str]]:
        """返回指代消解模式列表：[(regex, entity_key, replace_template), ...]。"""
        ...

    def build_memory_content(
        self,
        query: str,
        response: str,
        summary: dict[str, Any],
    ) -> str:
        """构建长期记忆内容文本。"""
        ...

    def build_memory_metadata(
        self,
        query: str,
        analysis_type: str,
        anomalies: list[dict[str, Any]],
        summary: dict[str, Any],
        time_range_days: int,
    ) -> dict[str, Any]:
        """构建长期记忆元数据。"""
        ...

    def trim_to_token_budget(
        self,
        text: str,
        max_tokens: int,
        label: str,
    ) -> str:
        """按 token 预算裁剪文本。"""
        ...
