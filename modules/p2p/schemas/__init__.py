"""P2P schema registry — multi-schema data source abstraction.

Each schema sub-package registers itself on import, providing a repository factory,
graph schema, and optional graph backend factory. The startup flow reads ``erp_schema``
from config and looks up the matching registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from modules.p2p.schemas.protocol import GraphSchema, P2PRepositoryProtocol


@dataclass
class SchemaRegistration:
    """Complete registration info for one ERP schema."""

    name: str
    repository_factory: Callable[[sessionmaker[Session]], P2PRepositoryProtocol]
    graph_schema: GraphSchema
    graph_backend_factory: Callable[..., Any] | None = None


class SchemaRegistry:
    """Schema registry — singleton, register at import time, lookup at runtime."""

    _schemas: dict[str, SchemaRegistration] = {}

    @classmethod
    def register(cls, registration: SchemaRegistration) -> None:
        cls._schemas[registration.name] = registration

    @classmethod
    def get(cls, name: str) -> SchemaRegistration:
        if name not in cls._schemas:
            available = ", ".join(cls._schemas.keys()) or "(empty)"
            raise ValueError(
                f"Unknown ERP schema '{name}'. Available: {available}"
            )
        return cls._schemas[name]

    @classmethod
    def available(cls) -> list[str]:
        return list(cls._schemas.keys())

    @classmethod
    def _clear(cls) -> None:
        """Reset registry (testing only)."""
        cls._schemas = {}


# Import all schema sub-packages to trigger auto-registration
import modules.p2p.schemas.oracle_ebs  # noqa: E402, F401
import modules.p2p.schemas.new_erp  # noqa: E402, F401
