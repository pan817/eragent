"""确定性 LLM mock。

functional 层需要绕过真实 LLM 调用（不依赖 API Key），但 bypass 检测和
参数提取仍走真实代码路径。仅 LLM 分类步骤被替换为确定性映射。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from core.orchestrator.router import _classify_bypass, _extract_params
from core.orchestrator.signal import IntentKind, QuerySignal
from modules.p2p.provider import P2PModuleProvider
from tests.fixtures.queries import CANARY_QUERIES, CanaryQuery


def _build_signal(cq: CanaryQuery) -> QuerySignal:
    """从 CanaryQuery 构造对应的 QuerySignal。"""
    intent = IntentKind(cq.expected_intent.lower())
    return QuerySignal(
        raw_query=cq.query,
        intent_kind=intent,
        keywords=[cq.expected_intent.lower()],
        confidence=0.95,
        route_level=1 if "DAG" in (cq.expected_route or "") else 3,
        reasoning=f"deterministic stub for: {cq.query}",
        is_cross_entity="cross_entity" in cq.tags,
    )


_QUERY_SIGNAL_MAP: dict[str, QuerySignal] = {
    cq.query: _build_signal(cq) for cq in CANARY_QUERIES
}


@pytest.fixture()
def deterministic_router(settings):
    """返回一个路由器，LLM 调用部分用查询映射表替代。

    bypass 检测和参数提取仍走真实代码路径。
    """
    from core.orchestrator.router import IntentRouter

    router = IntentRouter(settings=settings, provider=P2PModuleProvider())

    _original_route = router.route

    def _stubbed_route(
        query: str,
        analyst_role: str = "general",
        session_entities: dict[str, Any] | None = None,
    ) -> QuerySignal:
        bypass_result = _classify_bypass(query)
        if bypass_result is not None:
            return QuerySignal(
                raw_query=query,
                intent_kind=bypass_result,
                keywords=[bypass_result.value],
                confidence=0.95,
                route_level=0,
                reasoning=f"bypass: {bypass_result.value}",
            )

        extracted = _extract_params(query)

        if query in _QUERY_SIGNAL_MAP:
            signal = _QUERY_SIGNAL_MAP[query]
            merged_entities = {**signal.entities, **extracted}
            return dataclasses.replace(signal, entities=merged_entities)

        return QuerySignal(
            raw_query=query,
            intent_kind=IntentKind.ANALYSIS,
            keywords=["analysis"],
            confidence=0.5,
            route_level=3,
            entities=extracted,
        )

    router.route = _stubbed_route
    return router
