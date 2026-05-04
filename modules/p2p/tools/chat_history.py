"""跨会话历史对话检索工具。

Agent 通过本工具搜索用户历史会话，用于跨 session 引用消解和续推。
"""

from __future__ import annotations

from langchain.tools import tool

from modules.p2p.tools._output import _clip_and_dump


@tool
async def search_my_chat_history(
    query: str,
    days: int = 30,
    limit: int = 5,
) -> str:
    """搜索当前用户的历史对话内容，按相关度返回会话片段或会话摘要。

    适用场景：
    - 用户引用过去某段时间的对话（"上周那家供应商"、"上次的分析"、
      "之前讨论的付款政策"），需要找回历史会话上下文
    - 用户问"上个月在采购合规上得出过什么结论"等会话主题级回顾

    不适用场景：
    - 当前会话内的引用 → 走短期记忆（LangGraph checkpointer），不调本工具
    - 需要查询业务数据（PO/供应商/发票指标）→ 调 query_*/analyze_* 工具
    - 用户偏好查询 → 长期记忆 USER_PREFERENCE 自动注入，无需调用

    Args:
        query: 用户原始问题或主题关键词（自然语言）
        days: 时间窗口（天），1-365，默认 30。0 表示不限
        limit: 返回会话条数，1-10，默认 5

    Returns:
        JSON 字符串：[{session_id, session_title, snippet, entities,
        created_at, match_type, relevance_score}]，按相关度倒序。
        无匹配时返回 "[]"。可用于继续追问、实体消解、结论引用。
    """
    from core.memory import get_chat_indexer
    from core.memory.manager import MemoryManager
    from core.tasks.context import get_current_user_id

    user_id = get_current_user_id()
    if not user_id:
        return "[]"

    from config.settings import get_settings

    settings = get_settings()
    cfg = settings.memory.chat_history
    clamped_days = max(1, min(days, cfg.search_max_days)) if days > 0 else None
    clamped_limit = max(1, min(limit, cfg.search_max_limit))

    from core.memory.short_term import ShortTermMemory

    manager = MemoryManager(settings=settings)

    results = await manager.search_chat_history(
        user_id=user_id,
        query=query,
        days=clamped_days,
        limit=clamped_limit,
    )

    output = [
        {
            "session_id": r.get("session_id", ""),
            "session_title": r.get("session_title", ""),
            "snippet": r.get("snippet", ""),
            "entities": r.get("entities", {}),
            "created_at": str(r.get("created_at", "")),
            "match_type": r.get("match_type", ""),
            "relevance_score": round(r.get("recency_score", r.get("base_score", 0.0)), 3),
        }
        for r in results
    ]
    return _clip_and_dump(output)
