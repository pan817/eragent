"""Repository, GraphitiClient, and QueryBackend injection management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.p2p.schemas.protocol import GraphSchema, P2PRepositoryProtocol

if TYPE_CHECKING:
    from core.etl.client import GraphitiClient

_repository: P2PRepositoryProtocol | None = None
_graphiti_client: "GraphitiClient | None" = None
_query_backend: Any | None = None  # QueryBackend instance
_graph_schema: GraphSchema | None = None


def set_repository(repo: P2PRepositoryProtocol) -> None:
    """注入 P2PRepository 实例（服务启动时调用）。"""
    global _repository
    _repository = repo


def _get_repository() -> P2PRepositoryProtocol:
    """获取已注入的 Repository 实例。"""
    if _repository is None:
        raise RuntimeError(
            "P2PRepository 未初始化。请确保在服务启动时调用 set_repository()。"
        )
    return _repository


def set_graphiti_client(client: "GraphitiClient") -> None:
    """注入 GraphitiClient 实例（ETL 启动时调用）。"""
    global _graphiti_client
    _graphiti_client = client


def _get_graphiti_client() -> "GraphitiClient":
    """获取已注入的 GraphitiClient 实例。"""
    if _graphiti_client is None:
        raise RuntimeError(
            "GraphitiClient 未初始化。请确保 Neo4j + ETL 已启用并连接成功。"
        )
    return _graphiti_client


def set_graph_schema(schema: GraphSchema | None) -> None:
    global _graph_schema
    _graph_schema = schema


def _get_graph_schema() -> GraphSchema:
    if _graph_schema is None:
        raise RuntimeError("GraphSchema not injected")
    return _graph_schema


def set_query_backend(backend: Any) -> None:
    """注入 QueryBackend 实例（ETL 启动时调用）。"""
    global _query_backend
    _query_backend = backend


def _get_query_backend() -> Any:
    """获取 QueryBackend。未注入时自动降级为 PostgreSQLBackend。"""
    if _query_backend is not None:
        return _query_backend
    # Fallback: wrap P2PRepository as PostgreSQLBackend
    from core.etl.query_backend import PostgreSQLBackend
    return PostgreSQLBackend(_get_repository())
