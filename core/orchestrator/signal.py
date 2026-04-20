"""
查询信号数据结构。

QuerySignal 是意图路由的中间表示，封装从用户请求中提取的结构化信息。
Level 1/2 由正则 + 规则填充，Level 3 由 LLM 填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentKind(str, Enum):
    """意图大类（intent_kind）。

    与 AnalysisType（分析子类型）解耦：路由器先判 intent_kind，
    再决定是否给出 AnalysisType。各 kind 的下游处理：

    - ANALYSIS：落入 10 类采购分析场景之一，走 DAG / ReAct 分析流程。
      意图模糊时也归此类（type=comprehensive），由 ReAct 尝试执行。
    - DATA_LOOKUP：纯事实查询/单据检索（如"查最新的 PO"），走 ReAct
      让 Agent 自由调用 query_* 工具，不强制做异常分析。
    - CLARIFICATION：**已废弃**。保留枚举值以兼容历史 trace 数据的反序列化，
      新流程中不再由路由器主动产出。意图模糊时归入 ANALYSIS/comprehensive，
      遵循"尽量回复"原则。
    - META：系统能力/数据元信息询问（"你能做什么"/"支持哪些场景"），
      由模板答复，零 LLM 调用。
    - RECALL：明确回溯历史会话内容（"上次的结果呢"），走 ReAct 但跳过
      实体继承与长期记忆写入，避免循环引用。
    - CHITCHAT：闲聊/问候/非业务/否定确认等真·非分析查询，友好拒答。
    - OUT_OF_SCOPE：业务相关但本系统不覆盖（如"库存周转"/"销售订单"），
      友好提示当前支持范围。
    """

    ANALYSIS = "analysis"
    DATA_LOOKUP = "data_lookup"
    CLARIFICATION = "clarification"
    META = "meta"
    RECALL = "recall"
    CHITCHAT = "chitchat"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass
class QuerySignal:
    """从用户请求中提取的结构化信号。

    Attributes:
        raw_query: 用户原始查询文本。
        intent_kind: 意图大类（见 :class:`IntentKind`）。默认为 ANALYSIS
            以保持向后兼容——旧路径只关心 analysis_type。
        keywords: 提取的业务关键词列表（intent_kind=ANALYSIS 时为
            AnalysisType 的字符串值；其他 kind 时为对应 sentinel）。
        entities: 识别的业务实体（vendor_id / po_number 等）。
        missing_params: 历史字段，CLARIFICATION 废弃后不再主动填充。
            保留以兼容旧 trace 数据反序列化。
        time_range_days: 提取的时间范围（天），None 表示未识别。
        route_level: 命中的路由层级（1 / 2 / 3）。
        confidence: 路由置信度（0.0~1.0）。
        dag_hint: L2.5 案例检索命中时传递的 DAG 定义，供 orchestrator
            跳过模板加载直接复用。None 表示未命中或未启用 L2.5。
        reasoning: 路由决策的可读说明，用于 trace 和调试。
    """

    raw_query: str
    intent_kind: IntentKind = IntentKind.ANALYSIS
    keywords: list[str] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    missing_params: list[str] = field(default_factory=list)
    time_range_days: int | None = None
    route_level: int = 0
    confidence: float = 0.0
    dag_hint: list[dict[str, Any]] | None = None
    reasoning: str = ""

    # 统一 LLM 路由新增字段
    is_cross_entity: bool = False
    """是否为跨实体关联查询（如"这个付款单对应的PO"）。"""

    resolved_query: str = ""
    """指代消解后的查询文本。空字符串表示未消解（与 raw_query 相同）。"""
