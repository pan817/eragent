"""异步分析任务基础设施。

提供 TaskRegistry（进程内任务状态机）、EventBus（SSE 事件总线）
和对应的 Pydantic 数据模型，供 ``api/routes/analyze_async.py`` 消费。
"""

from core.tasks.events import (
    EventBus,
    EventBusProtocol,
    MemoryEventBus,
    get_event_bus,
    init_event_bus,
    shutdown_event_bus,
)
from core.tasks.registry import (
    TaskEntry,
    TaskRegistry,
    get_task_registry,
    init_task_registry,
    shutdown_task_registry,
)
from core.tasks.schemas import (
    AnalysisTaskAck,
    AnalysisTaskSnapshot,
    TaskState,
)
from core.tasks.schemas import TERMINAL_STATES

__all__ = [
    "AnalysisTaskAck",
    "AnalysisTaskSnapshot",
    "EventBus",
    "EventBusProtocol",
    "MemoryEventBus",
    "TERMINAL_STATES",
    "TaskEntry",
    "TaskRegistry",
    "TaskState",
    "get_event_bus",
    "get_task_registry",
    "init_event_bus",
    "init_task_registry",
    "shutdown_event_bus",
    "shutdown_task_registry",
]
