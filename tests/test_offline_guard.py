from __future__ import annotations

import os
import socket

import pytest

from tests.offline_guard import (
    OfflineNetworkError,
    activate_offline_test_mode,
    offline_guard_status,
    real_llm_api_tests_enabled,
)


def test_sensitive_environment_is_cleared_before_app_imports() -> None:
    assert offline_guard_status()["pre_activation_imports"] == []
    for key in (
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "FEISHU_APP_SECRET",
        "WECOM_TOKEN",
        "SSH_PASSWORD",
        "SERVER_PASSWORD",
    ):
        assert key not in os.environ


def test_offline_guard_provides_inventory_sheet_png_fixture() -> None:
    path = os.environ.get("INVENTORY_IMAGE_GLOB")

    assert path
    payload = open(path, "rb").read(8)
    assert payload == b"\x89PNG\r\n\x1a\n"


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


def test_real_llm_api_gate_requires_both_online_switches(monkeypatch) -> None:
    monkeypatch.delenv("RUN_ONLINE_QA", raising=False)
    monkeypatch.delenv("RUN_REAL_LLM_API_TESTS", raising=False)
    assert real_llm_api_tests_enabled() is False

    monkeypatch.setenv("RUN_ONLINE_QA", "1")
    assert real_llm_api_tests_enabled() is False

    monkeypatch.setenv("RUN_REAL_LLM_API_TESTS", "1")
    assert real_llm_api_tests_enabled() is True


def test_real_llm_api_gate_preserves_only_llm_keys(monkeypatch) -> None:
    monkeypatch.setenv("RUN_ONLINE_QA", "1")
    monkeypatch.setenv("RUN_REAL_LLM_API_TESTS", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("FEISHU_APP_SECRET", "must-be-cleared")

    activate_offline_test_mode()

    assert os.environ["DASHSCOPE_API_KEY"] == "test-dashscope-key"
    assert os.environ["DEEPSEEK_API_KEY"] == "test-deepseek-key"
    assert "FEISHU_APP_SECRET" not in os.environ
