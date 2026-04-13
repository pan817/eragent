"""
DAG 执行器。

基于 asyncio 的拓扑排序并行执行引擎。
无依赖的节点并行执行，有依赖的节点等待前置完成后执行。
报告节点（generate_summary_report）交由 ReportAgent 处理。
每个节点的执行过程记录到全链路 trace（span_type="dag.task"）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from core.logging_utils import get_logger
from core.observability.middleware import record_span, _truncate_text
from core.orchestrator.dag.registry import ToolRegistry

_logger = get_logger(__name__)

# 报告工具名，由 ReportAgent 而非 ToolRegistry 处理
_REPORT_TOOLS = {"generate_summary_report", "generate_chart"}


class DAGExecutionError(Exception):
    """DAG 执行异常。"""


class DAGExecutor:
    """DAG 并行执行器。"""

    def __init__(
        self,
        registry: ToolRegistry,
        report_agent: Any = None,
    ) -> None:
        self._registry = registry
        self._report_agent = report_agent

    async def execute(
        self,
        tasks: list[dict[str, Any]],
        long_term_context: str = "",
        output_mode_prompt: str = "",
    ) -> dict[str, Any]:
        """执行 DAG 任务列表，记录完整执行过程到 trace。

        Args:
            tasks: DAG 任务定义列表。
            long_term_context: 长期记忆上下文文本，注入 ReportAgent 报告生成。
            output_mode_prompt: 输出模式格式指令，注入 ReportAgent。
        """
        start = time.monotonic()
        outputs: dict[str, str] = {}
        completed: list[str] = []
        failed: dict[str, str] = {}

        # 用 Event 跟踪每个任务完成状态
        events: dict[str, asyncio.Event] = {
            t["task_id"]: asyncio.Event() for t in tasks
        }

        async def run_task(task: dict[str, Any]) -> None:
            task_id = task["task_id"]
            tool_name = task.get("tool_name", "")
            timeout_sec = task.get("timeout_sec", 780)

            with record_span("dag.task", f"{task_id}:{tool_name}") as span_attrs:
                span_attrs["task_id"] = task_id
                span_attrs["tool_name"] = tool_name
                span_attrs["depends_on"] = task.get("depends_on", [])
                span_attrs["inputs"] = task.get("inputs", {})

                # 等待所有依赖完成
                for dep_id in task.get("depends_on", []):
                    await events[dep_id].wait()
                    if dep_id in failed:
                        failed[task_id] = f"前置任务 '{dep_id}' 失败"
                        span_attrs["status"] = "skipped"
                        span_attrs["skip_reason"] = failed[task_id]
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
                                    long_term_context=long_term_context,
                                    output_mode_prompt=output_mode_prompt,
                                ),
                                timeout=timeout_sec,
                            )
                            output_key = task.get("output_key", task_id)
                            outputs[output_key] = report_text
                            completed.append(task_id)
                            span_attrs["status"] = "completed"
                            span_attrs["output_length"] = len(report_text)
                        except Exception as exc:
                            failed[task_id] = str(exc)
                            span_attrs["status"] = "failed"
                            span_attrs["error"] = str(exc)
                    else:
                        completed.append(task_id)
                        span_attrs["status"] = "skipped_no_report_agent"
                    events[task_id].set()
                    return

                # 常规工具执行
                tool_fn = self._registry.get(tool_name)
                if tool_fn is None:
                    failed[task_id] = f"工具 '{tool_name}' 未注册"
                    span_attrs["status"] = "failed"
                    span_attrs["error"] = failed[task_id]
                    events[task_id].set()
                    return

                try:
                    inputs = task.get("inputs", {})
                    # 只过滤未替换的占位符（{xxx}），保留空字符串（合法的"全部"语义）
                    clean_inputs = {
                        k: v for k, v in inputs.items()
                        if not (isinstance(v, str) and v.startswith("{") and v.endswith("}"))
                    }
                    span_attrs["clean_inputs"] = clean_inputs

                    # 显式记录 tool span（DAG 路径不经过 LangChain 中间件）
                    with record_span("tool", tool_name) as tool_attrs:
                        tool_attrs["tool"] = tool_name
                        tool_attrs["args"] = clean_inputs
                        result = await asyncio.wait_for(
                            tool_fn.ainvoke(clean_inputs),
                            timeout=timeout_sec,
                        )
                        tool_attrs["output"] = _truncate_text(result)

                    output_key = task.get("output_key", task_id)
                    outputs[output_key] = result
                    completed.append(task_id)
                    span_attrs["status"] = "completed"
                    span_attrs["output"] = _truncate_text(result)
                    _logger.debug("task %s completed: %s", task_id, tool_name)

                except asyncio.TimeoutError:
                    failed[task_id] = f"工具 '{tool_name}' 超时（{timeout_sec}s）"
                    span_attrs["status"] = "timeout"
                    span_attrs["timeout_sec"] = timeout_sec
                    _logger.warning("task %s timeout: %s after %ds", task_id, tool_name, timeout_sec)
                except Exception as exc:
                    failed[task_id] = f"{type(exc).__name__}: {exc}"
                    span_attrs["status"] = "failed"
                    span_attrs["error"] = str(exc)
                    _logger.warning("task %s failed: %s - %s", task_id, tool_name, exc)
                finally:
                    events[task_id].set()

        # DAG 整体 span
        with record_span("dag", "dag_execution") as dag_attrs:
            dag_attrs["task_count"] = len(tasks)
            dag_attrs["task_ids"] = [t["task_id"] for t in tasks]
            dag_attrs["tools"] = [t.get("tool_name", "") for t in tasks]

            await asyncio.gather(*(run_task(t) for t in tasks), return_exceptions=True)

            duration = time.monotonic() - start

            if not failed:
                status = "completed"
            elif completed:
                status = "partial"
            else:
                status = "failed"

            dag_attrs["status"] = status
            dag_attrs["completed_tasks"] = completed
            dag_attrs["failed_tasks"] = failed
            dag_attrs["duration_sec"] = round(duration, 2)

        return {
            "status": status,
            "outputs": outputs,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "report": outputs.get("report", ""),
            "duration_sec": round(duration, 2),
        }
