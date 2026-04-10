"""
DAG 执行器。

基于 asyncio 的拓扑排序并行执行引擎。
无依赖的节点并行执行，有依赖的节点等待前置完成后执行。
报告节点（generate_summary_report）交由 ReportAgent 处理。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from core.logging_utils import get_logger
from core.orchestrator.dag.registry import ToolRegistry

_logger = get_logger(__name__)

# 报告工具名，由 ReportAgent 而非 ToolRegistry 处理
_REPORT_TOOLS = {"generate_summary_report", "generate_chart"}


class DAGExecutionError(Exception):
    """DAG 执行异常。"""


class DAGExecutor:
    """DAG 并行执行器。

    拓扑排序所有任务，按依赖关系分层并行执行。
    每个节点有独立超时控制，失败节点不阻塞无关节点。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        report_agent: Any = None,
    ) -> None:
        self._registry = registry
        self._report_agent = report_agent

    async def execute(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """执行 DAG 任务列表。

        Args:
            tasks: DAG 任务列表（已通过 DAGValidator 校验）。

        Returns:
            执行结果字典，包含：
            - status: "completed" / "partial" / "failed"
            - outputs: {output_key: result_str}
            - completed_tasks: 成功的任务 ID 列表
            - failed_tasks: 失败的 {task_id: error_msg} 字典
            - report: Markdown 报告（若有 report_agent）
            - duration_sec: 总耗时
        """
        start = time.monotonic()
        outputs: dict[str, str] = {}
        completed: list[str] = []
        failed: dict[str, str] = {}
        task_map = {t["task_id"]: t for t in tasks}

        # 用 Event 跟踪每个任务完成状态
        events: dict[str, asyncio.Event] = {
            t["task_id"]: asyncio.Event() for t in tasks
        }

        async def run_task(task: dict[str, Any]) -> None:
            task_id = task["task_id"]
            tool_name = task.get("tool_name", "")
            timeout_sec = task.get("timeout_sec", 60)

            # 等待所有依赖完成
            for dep_id in task.get("depends_on", []):
                await events[dep_id].wait()
                # 如果依赖失败，当前任务也标记失败
                if dep_id in failed:
                    failed[task_id] = f"前置任务 '{dep_id}' 失败"
                    events[task_id].set()
                    return

            # 报告节点交给 ReportAgent
            if tool_name in _REPORT_TOOLS:
                if self._report_agent is not None:
                    try:
                        report_text = await asyncio.wait_for(
                            self._report_agent.generate(
                                scenario=task.get("inputs", {}).get("scenario", "分析"),
                                outputs=outputs,
                            ),
                            timeout=timeout_sec,
                        )
                        output_key = task.get("output_key", task_id)
                        outputs[output_key] = report_text
                        completed.append(task_id)
                    except Exception as exc:
                        failed[task_id] = str(exc)
                else:
                    # 无 ReportAgent 时跳过报告节点
                    completed.append(task_id)
                events[task_id].set()
                return

            # 常规工具执行
            tool_fn = self._registry.get(tool_name)
            if tool_fn is None:
                failed[task_id] = f"工具 '{tool_name}' 未注册"
                events[task_id].set()
                return

            try:
                inputs = task.get("inputs", {})
                # 过滤掉未替换的占位符和空值
                clean_inputs = {
                    k: v for k, v in inputs.items()
                    if v and not (isinstance(v, str) and v.startswith("{"))
                }
                result = await asyncio.wait_for(
                    tool_fn.ainvoke(clean_inputs),
                    timeout=timeout_sec,
                )
                output_key = task.get("output_key", task_id)
                outputs[output_key] = result
                completed.append(task_id)
                _logger.debug("task %s completed: %s", task_id, tool_name)

            except asyncio.TimeoutError:
                failed[task_id] = f"工具 '{tool_name}' 超时（{timeout_sec}s）"
                _logger.warning("task %s timeout: %s after %ds", task_id, tool_name, timeout_sec)
            except Exception as exc:
                failed[task_id] = f"{type(exc).__name__}: {exc}"
                _logger.warning("task %s failed: %s - %s", task_id, tool_name, exc)
            finally:
                events[task_id].set()

        # 并行启动所有任务（依赖关系由 Event 控制）
        await asyncio.gather(*(run_task(t) for t in tasks), return_exceptions=True)

        duration = time.monotonic() - start
        total = len(tasks)
        n_completed = len(completed)
        n_failed = len(failed)

        if n_failed == 0:
            status = "completed"
        elif n_completed > 0:
            status = "partial"
        else:
            status = "failed"

        return {
            "status": status,
            "outputs": outputs,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "report": outputs.get("report", ""),
            "duration_sec": round(duration, 2),
        }
