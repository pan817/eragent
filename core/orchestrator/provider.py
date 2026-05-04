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
        """返回模块支持的实体类型列表（如 po_number, vendor_id 等）。"""
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

    def get_planning_prompt_template(self) -> str:
        """返回 Plan and Solve Planning Prompt 模板字符串。

        模板必须包含以下占位符（``str.format`` 语法）：
          - ``{query}`` / ``{params_json}`` / ``{time_range_days}``
          - ``{tools_section}`` / ``{report_tool_hint}``

        由 ``Planner`` 在调用前填充。业务模块可在模板内注入领域角色说明、
        工具分组描述、报告工具命名约定等模块特有内容。
        """
        ...

    def format_tools_for_planning(self, tools: list[Any]) -> str:
        """将工具列表格式化为 Planning Prompt 中的 ``{tools_section}`` 文本。"""
        ...

    # ── 意图路由规则 ──

    def get_analysis_keywords(self) -> set[str]:
        """返回 L1 分析关键词集合（触发分析意图的关键词）。"""
        ...

    def get_analysis_type_descriptions(self) -> list[tuple[str, str]]:
        """返回 analysis_type 枚举描述列表，用于 L3 LLM 分类 prompt。"""
        ...

    def get_role_descriptions(self) -> dict[str, str]:
        """返回分析师角色描述映射。"""
        ...

    def get_lookup_rules(self) -> dict[str, Any]:
        """返回 lookup 快捷路径所需的规则集。

        Returns:
            包含 high_confidence_keywords, exclusion_words,
            graph_intent_words 三个键的字典。
        """
        ...

    # ── DAG 模板（通用概览） ──

    def get_generic_dag_templates(self) -> dict[str, list[dict[str, Any]]]:
        """返回不按 AnalysisType 索引的通用概览 DAG 模板映射。"""
        ...

    # ── 基础设施访问 ──

    def get_graphiti_client(self) -> Any | None:
        """获取 Graphiti 客户端实例，未初始化返回 None。"""
        ...

    def is_query_backend_available(self) -> bool:
        """查询后端是否已注入（非 PG 模式需要 QueryBackend 注入）。"""
        ...
