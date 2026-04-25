"""P2P Repository compatibility bridge.

Real implementation lives in ``modules.p2p.schemas.oracle_ebs.repository``.
This module re-exports for backward compatibility so that existing code
(``from modules.p2p.repository import P2PRepository``) continues to work.
"""

from modules.p2p.schemas.oracle_ebs.repository import (  # noqa: F401
    OracleEBSRepository as P2PRepository,
    _apply_order_and_limit,
)

__all__ = ["P2PRepository", "_apply_order_and_limit"]
