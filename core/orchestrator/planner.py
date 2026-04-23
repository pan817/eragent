"""Plan and Solve 规划器（Planner）。

在 L3 兜底路径中，用一次 LLM 调用生成完整的工具执行计划（DAG 任务列表），
之后由 DAGExecutor 并行执行，替代 ReAct 的多轮串行。

职责划分：
- 骨架（本模块）：构建 LLM、缓存工具清单、调用 structured output、校验计划合法性。
- Prompt / 工具格式化：由 ``ModuleProvider.get_planning_prompt_template()`` 和
  ``ModuleProvider.format_tools_for_planning()`` 注入，解耦核心编排层和业务模块。

失败降级：任何失败（超时 / JSON 解析错误 / plannable=false / 校验不通过）
均返回 ``None`` 或 ``plannable=False`` 让调用方走 ReAct 兜底。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from config.settings import Settings, get_settings
from core.logging_utils import get_logger
from core.orchestrator.dag.registry import ToolRegistry
from core.orchestrator.dag.validator import DAGValidator

_logger = get_logger(__name__)


# 报告工具名与 DAGExecutor / DAGValidator 保持一致
_REPORT_TOOL_NAME = "generate_summary_report"

# LLM 未显式设置 timeout_sec 时的默认值（秒），与静态 DAG 模板对齐
_DEFAULT_TASK_TIMEOUT_SEC = 60
_DEFAULT_REPORT_TIMEOUT_SEC = 180


class PlannedTask(BaseModel):
    """规划器产出的单个 DAG 任务（字段与 DAG task dict 对齐）。"""

    task_id: str = Field(description="任务标识，计划内唯一，短字符串")
    tool_name: str = Field(description="工具名，必须存在于可用工具清单或为 generate_summary_report")
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="工具入参，填实际值，不使用占位符",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="前置任务 task_id 列表；数据查询任务为空",
    )
    output_key: str = Field(
        default="",
        description="DAGExecutor 汇总输出时使用的键名；为空时取 task_id",
    )
    timeout_sec: int = Field(
        default=0,
        description="任务超时秒数；0 表示使用默认值",
    )


class ExecutionPlan(BaseModel):
    """Plan and Solve 规划器生成的执行计划。"""

    plannable: bool = Field(
        description="true=可提前规划并交由 DAGExecutor 并行执行；false=需动态决策，降级到 ReAct",
    )
    reasoning: str = Field(
        default="",
        description="规划判断理由，一句话即可（用于 trace 与调试）",
    )
    tasks: list[PlannedTask] = Field(
        default_factory=list,
        description="可执行 DAG 任务列表；plannable=false 时留空",
    )
    report_scenario: str = Field(
        default="",
        description="报告节点的 scenario 描述，供 ReportAgent 生成最终报告",
    )


class Planner:
    """Plan and Solve 规划器主类。

    通过 ``ModuleProvider`` 解耦业务 Prompt 与工具格式化，核心只负责
    LLM 调用、结构化输出解析、计划校验和 DAG 任务转换。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        provider: Any | None = None,
    ) -> None:
        self._settings: Settings = settings or get_settings()
        self._provider: Any = provider
        self._llm: Any = None
        # 按工具名集合 hash 缓存 tools_section 文本（Q7）
        self._tools_section_cache: tuple[str, str] | None = None  # (fingerprint, text)

    def _ensure_llm(self) -> Any:
        """延迟构建 structured-output LLM 客户端。"""
        if self._llm is not None:
            return self._llm

        from core.llm.model_factory import build_chat_model

        cfg = self._settings.plan_and_solve
        llm_cfg = self._settings.llm_fast if cfg.use_fast_model else self._settings.llm
        base_llm = build_chat_model(
            llm_cfg,
            disable_thinking=True,
            max_tokens_override=cfg.max_planning_tokens or None,
        )
        self._llm = base_llm.with_structured_output(ExecutionPlan)
        return self._llm

    def _tools_fingerprint(self, tools: list[Any]) -> str:
        """按工具名集合计算缓存指纹。"""
        names = sorted(
            getattr(t, "name", None) or getattr(t, "__name__", "") for t in tools
        )
        blob = "\n".join(n for n in names if n)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def _format_tools_section(self, tools: list[Any]) -> str:
        fingerprint = self._tools_fingerprint(tools)
        cached = self._tools_section_cache
        if cached is not None and cached[0] == fingerprint:
            return cached[1]

        if self._provider is not None and hasattr(
            self._provider, "format_tools_for_planning"
        ):
            text = self._provider.format_tools_for_planning(tools)
        else:
            text = self._default_format_tools(tools)

        self._tools_section_cache = (fingerprint, text)
        return text

    @staticmethod
    def _default_format_tools(tools: list[Any]) -> str:
        """Provider 未注入时的兜底格式化（仅测试场景使用）。"""
        lines: list[str] = []
        for tool in tools:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
            desc = (getattr(tool, "description", "") or "").strip().split("\n", 1)[0]
            args = getattr(tool, "args", {}) or {}
            arg_repr = ", ".join(args.keys()) if args else "无参数"
            if name:
                lines.append(f"- `{name}` ({arg_repr}) — {desc}" if desc else f"- `{name}` ({arg_repr})")
        return "\n".join(lines) if lines else "（无可用工具）"

    def _build_prompt(
        self,
        *,
        query: str,
        tools_section: str,
        params: dict[str, Any],
        time_range_days: int,
    ) -> str:
        """渲染 Planning Prompt。"""
        if self._provider is not None and hasattr(
            self._provider, "get_planning_prompt_template"
        ):
            template = self._provider.get_planning_prompt_template()
        else:
            # 最小兜底模板（测试用；生产路径由 Provider 提供）
            template = (
                "你是执行规划器。根据查询和可用工具生成 ExecutionPlan。\n"
                "## 可用工具\n{tools_section}\n\n"
                "## 用户查询\n{query}\n\n"
                "## 已解析参数\n{params_json}\n\n"
                "## 时间范围\n最近 {time_range_days} 天（0=不限）\n"
            )

        params_json = json.dumps(params, ensure_ascii=False, indent=2)
        prompt = (
            template.replace("{tools_section}", tools_section)
            .replace("{query}", query)
            .replace("{params_json}", params_json)
            .replace("{time_range_days}", str(time_range_days))
        )
        return prompt

    async def plan(
        self,
        *,
        query: str,
        tools: list[Any],
        params: dict[str, Any],
        time_range_days: int,
    ) -> ExecutionPlan:
        """为用户查询生成 ExecutionPlan。

        Args:
            query: 增强后的用户查询（已指代消解）。
            tools: 当前模式下可用的 LangChain @tool 列表。
            params: 已解析参数（来自路由层）。
            time_range_days: 时间范围（0 表示不限）。

        Returns:
            ExecutionPlan：plannable=True 时 tasks 非空；plannable=False 时
            调用方应降级到 ReAct。
        """
        tools_section = self._format_tools_section(tools)
        prompt = self._build_prompt(
            query=query,
            tools_section=tools_section,
            params=params,
            time_range_days=time_range_days,
        )
        llm = self._ensure_llm()

        try:
            plan_obj: Any = await llm.ainvoke(prompt)
        except Exception as exc:
            _logger.warning(
                "planner LLM call failed: %s: %s", type(exc).__name__, exc,
            )
            return ExecutionPlan(
                plannable=False,
                reasoning=f"llm_error: {type(exc).__name__}",
            )

        if isinstance(plan_obj, ExecutionPlan):
            return plan_obj
        if isinstance(plan_obj, dict):
            try:
                return ExecutionPlan.model_validate(plan_obj)
            except Exception as exc:
                _logger.warning("planner output not ExecutionPlan-shape: %s", exc)
                return ExecutionPlan(
                    plannable=False,
                    reasoning="invalid_output_shape",
                )
        _logger.warning(
            "planner output unexpected type=%s", type(plan_obj).__name__
        )
        return ExecutionPlan(plannable=False, reasoning="invalid_output_type")

    @staticmethod
    def plan_to_tasks(plan: ExecutionPlan) -> list[dict[str, Any]]:
        """把 ExecutionPlan 转换为 DAGExecutor 可执行的 task dict 列表。"""
        tasks: list[dict[str, Any]] = []
        for item in plan.tasks:
            is_report = item.tool_name == _REPORT_TOOL_NAME
            timeout = item.timeout_sec or (
                _DEFAULT_REPORT_TIMEOUT_SEC if is_report else _DEFAULT_TASK_TIMEOUT_SEC
            )
            output_key = item.output_key or (
                "report" if is_report else item.task_id
            )
            inputs = dict(item.inputs)
            if is_report and not inputs.get("scenario"):
                inputs["scenario"] = plan.report_scenario or "采购分析"
            tasks.append(
                {
                    "task_id": item.task_id,
                    "tool_name": item.tool_name,
                    "inputs": inputs,
                    "depends_on": list(item.depends_on),
                    "timeout_sec": timeout,
                    "output_key": output_key,
                }
            )
        return tasks

    @staticmethod
    def validate_plan(
        plan: ExecutionPlan,
        registry: ToolRegistry,
    ) -> list[str]:
        """校验计划合法性，返回错误列表（空=通过）。

        校验项：
          - plannable=True 时 tasks 至少 1 个
          - 结构校验复用 DAGValidator（工具白名单 / task_id 唯一 / 依赖合法 /
            无环 / 报告节点为终点 / 分析任务有数据依赖 / 任务数上限）
          - 必须且仅有一个 generate_summary_report 任务作为终点
        """
        if not plan.plannable:
            return []
        if not plan.tasks:
            return ["plannable=True but tasks is empty"]

        tasks = Planner.plan_to_tasks(plan)

        report_count = sum(
            1 for t in tasks if t["tool_name"] == _REPORT_TOOL_NAME
        )
        if report_count == 0:
            return ["plan missing terminal generate_summary_report task"]
        if report_count > 1:
            return [f"plan has {report_count} report tasks, expected exactly 1"]

        validator = DAGValidator(registry)
        ok, err = validator.validate(tasks)
        if not ok:
            return [err or "dag_validation_failed"]
        return []
