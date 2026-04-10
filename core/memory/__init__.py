"""长期记忆模块（PostgreSQL 持久化，按 user_id 隔离）。

短期记忆由 LangGraph PostgresSaver checkpointer 承担（thread_id = session_id），
通过 core.observability.checkpointer 注入 trace 监控，不在本包中管理。
"""

from core.memory.long_term import (
    LongTermMemory,
    MemoryRepository,
    ReportRepository,
    get_long_term_memory,
    get_memory_repository,
    get_report_repository,
    reset_long_term_memory,
    reset_repositories,
)
from core.memory.tables import memories_table, metadata_obj, reports_table

__all__ = [
    "LongTermMemory",
    "MemoryRepository",
    "ReportRepository",
    "get_long_term_memory",
    "get_memory_repository",
    "get_report_repository",
    "reset_long_term_memory",
    "reset_repositories",
    "memories_table",
    "reports_table",
    "metadata_obj",
]
