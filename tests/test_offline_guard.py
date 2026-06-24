from __future__ import annotations

import os
import socket

import pytest

from tests.offline_guard import OfflineNetworkError, offline_guard_status


def test_sensitive_environment_is_cleared_before_app_imports() -> None:
    assert offline_guard_status()["pre_activation_imports"] == []
    for key in (
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "FEISHU_APP_SECRET",
        "WECOM_TOKEN",
        "SSH_PASSWORD",
        "SERVER_PASSWORD",
    ):
        assert key not in os.environ


def test_external_network_is_blocked_with_stack_and_target() -> None:
    with pytest.raises(OfflineNetworkError) as exc:
        socket.create_connection(("dashscope.aliyuncs.com", 443), timeout=0.01)

    text = str(exc.value)
    assert "dashscope.aliyuncs.com:443" in text
    assert "test_external_network_is_blocked" in text
    assert offline_guard_status()["blocked_network_call_count"] >= 1


def test_localhost_network_is_not_blocked_by_guard() -> None:
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    except OfflineNetworkError:
        raise
    except OSError:
        pass
