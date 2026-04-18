"""Repository 注入管理。"""

from __future__ import annotations

from modules.p2p.repository import P2PRepository

_repository: P2PRepository | None = None


def set_repository(repo: P2PRepository) -> None:
    """注入 P2PRepository 实例（服务启动时调用）。"""
    global _repository
    _repository = repo


def _get_repository() -> P2PRepository:
    """获取已注入的 Repository 实例。"""
    if _repository is None:
        raise RuntimeError(
            "P2PRepository 未初始化。请确保在服务启动时调用 set_repository()。"
        )
    return _repository
