"""
DAG 结构校验器。

校验 DAG 任务列表的合法性：工具白名单、依赖合法性、环检测、
报告节点必须为终点、分析任务必须有数据依赖。
"""

from __future__ import annotations

from typing import Any

from core.orchestrator.dag.registry import ToolRegistry

# 报告工具名（由 ReportAgent 处理，不注册到 ToolRegistry）
_REPORT_TOOLS = {"generate_summary_report", "generate_chart"}

# 最大任务数
_MAX_TASKS = 12


class DAGValidator:
    """DAG 结构校验器。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate(self, tasks: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """全量校验，返回 (是否通过, 错误原因)。"""

        # 1. 基础结构
        if not tasks:
            return False, "任务列表不能为空"
        if len(tasks) > _MAX_TASKS:
            return False, f"任务数量 {len(tasks)} 超过上限 {_MAX_TASKS}"

        task_ids = [t.get("task_id") for t in tasks]
        if len(task_ids) != len(set(task_ids)):
            return False, "存在重复的 task_id"

        # 2. 工具合法性（跳过 generate_summary_report，由 ReportAgent 处理）
        for t in tasks:
            tool_name = t.get("tool_name", "")
            if tool_name in _REPORT_TOOLS:
                continue  # 报告工具由 ReportAgent 处理，不在 registry 中
            if tool_name not in self._registry:
                return False, f"非法工具 '{tool_name}'"

        # 3. 依赖合法性
        id_set = set(task_ids)
        for t in tasks:
            for dep in t.get("depends_on", []):
                if dep not in id_set:
                    return False, f"任务 '{t['task_id']}' 依赖不存在的任务 '{dep}'"

        # 4. 环检测
        has_cycle, cycle_path = self._detect_cycle(tasks)
        if has_cycle:
            return False, f"检测到循环依赖: {' → '.join(cycle_path)}"

        # 5. 报告节点不能被其他任务依赖
        report_task_ids = {
            t["task_id"] for t in tasks if t.get("tool_name", "") in _REPORT_TOOLS
        }
        all_deps = {dep for t in tasks for dep in t.get("depends_on", [])}
        for rt_id in report_task_ids:
            if rt_id in all_deps:
                return False, f"报告任务 '{rt_id}' 不能被其他任务依赖"

        # 6. 分析任务必须有数据依赖（从 Registry 元数据动态获取类别）
        data_tools = self._registry.names_by_category("data")
        analysis_tools = self._registry.names_by_category("analysis")
        data_task_ids = {
            t["task_id"] for t in tasks if t.get("tool_name", "") in data_tools
        }
        for t in tasks:
            if t.get("tool_name", "") in analysis_tools:
                if not set(t.get("depends_on", [])) & data_task_ids:
                    return False, (
                        f"分析任务 '{t['task_id']}' ({t['tool_name']}) "
                        f"没有依赖任何数据采集任务"
                    )

        return True, None

    @staticmethod
    def _detect_cycle(tasks: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        """DFS 环检测。"""
        graph = {t["task_id"]: set(t.get("depends_on", [])) for t in tasks}
        visited: set[str] = set()
        in_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> bool:
            visited.add(node)
            in_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in in_stack:
                    path.append(neighbor)
                    return True
            path.pop()
            in_stack.discard(node)
            return False

        for node in graph:
            if node not in visited:
                if dfs(node):
                    return True, path

        return False, []
