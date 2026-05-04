"""DAG 执行相关异常。"""

from __future__ import annotations


class ReportGenerationError(Exception):
    """报告生成失败。

    - ``code`` 是稳定的业务错误码，前端 / 日志可据此分类
    - ``message`` 是人类可读原因（中文）
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
