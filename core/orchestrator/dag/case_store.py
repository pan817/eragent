"""
DAG 案例存储器（自学习闭环）。

成功执行的 DAG 案例写入 Chroma，供 Level 2 路由检索复用。
形成：执行成功 → 沉淀到 Chroma → 下次相似请求命中历史案例 → 加载并适配。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.logging_utils import get_logger

_logger = get_logger(__name__)

_CASE_COLLECTION = "intent_cases"


class DAGCaseStore:
    """成功 DAG 案例存储器。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._store: Any = None

    def _ensure_store(self) -> Any:
        """延迟初始化 Chroma intent_cases collection。"""
        if self._store is not None:
            return self._store
        from core.knowledge.vector_store import VectorStore

        store = VectorStore.from_settings(self._settings, _CASE_COLLECTION)
        store.initialize()
        self._store = store
        return store

    async def store_successful_case(
        self,
        query: str,
        analysis_type: str,
        dag: list[dict[str, Any]],
        route_type: str,
        exec_result: dict[str, Any],
    ) -> None:
        """将成功执行的 DAG 案例写入 Chroma。

        仅存储真正成功的案例（status == completed 且无失败任务）。
        用 query hash 去重，相同 query 的新案例覆盖旧案例。

        Args:
            query: 用户原始查询。
            analysis_type: 分析类型字符串。
            dag: DAG 任务定义列表。
            route_type: 路由类型（DAG / ReAct）。
            exec_result: 执行结果字典。
        """
        if exec_result.get("status") != "completed":
            return
        if exec_result.get("failed_tasks"):
            return

        try:
            store = self._ensure_store()
        except Exception as exc:
            _logger.warning("case store init failed, skip: %s", exc)
            return

        query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
        doc_id = f"dag_case_{query_hash}"

        case_text = (
            f"成功案例：{query}\n"
            f"分析类型：{analysis_type}\n"
            f"任务数：{len(dag)}\n"
            f"执行时长：{exec_result.get('duration_sec', 0)}秒"
        )

        doc = {
            "id": doc_id,
            "text": case_text,
            "metadata": {
                "analysis_type": analysis_type,
                "route_type": route_type,
                "task_count": len(dag),
                "dag_definition": json.dumps(dag, ensure_ascii=False),
            },
        }

        try:
            store.add_documents([doc])
            _logger.info("DAG case stored: %s (type=%s)", doc_id, analysis_type)
        except Exception as exc:
            _logger.warning("DAG case store failed: %s", exc)
