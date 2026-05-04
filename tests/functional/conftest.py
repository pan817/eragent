"""functional 层自动标记 + 自动注入 seeded_repo。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _auto_inject_repo(inject_seeded_repo):
    """自动注入 seeded_repo 到工具全局变量。"""
    yield


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.functional)
