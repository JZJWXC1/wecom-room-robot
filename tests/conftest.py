from __future__ import annotations

import os

import pytest

from tests.offline_guard import activate_offline_test_mode


activate_offline_test_mode()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_ONLINE_QA") == "1":
        return
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if "online" in item.keywords:
            deselected.append(item)
        else:
            kept.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
