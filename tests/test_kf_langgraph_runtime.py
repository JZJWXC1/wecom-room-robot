from __future__ import annotations

from app.config import Settings
from app.services import config_check
from app.services import kf_langgraph_runtime


def test_langgraph_smoke_graph_routes_inventory_table() -> None:
    result = kf_langgraph_runtime.invoke_kf_langgraph_smoke(
        "房源表发我一下",
        conversation_id="test-conversation",
        checkpointer=kf_langgraph_runtime.build_memory_checkpointer(),
    )

    assert result["route"] == "send_inventory_table"
    assert result["trace"] == ["rewrite_intent", "planner"]


def test_langgraph_smoke_graph_routes_video_note_word() -> None:
    result = kf_langgraph_runtime.invoke_kf_langgraph_smoke(
        "这个房间笔记发我看看",
        conversation_id="test-video",
        checkpointer=kf_langgraph_runtime.build_memory_checkpointer(),
    )

    assert result["route"] == "resolve_room_video"


def test_langgraph_runtime_config_reads_project_settings(tmp_path) -> None:
    checkpoint_path = tmp_path / "kf_langgraph.sqlite"
    app_settings = Settings(
        app_env="test",
        KF_LANGGRAPH_ENABLED=True,
        KF_LANGGRAPH_CHECKPOINT_PATH=checkpoint_path,
        KF_LANGGRAPH_SMOKE_THREAD_ID="thread-from-env",
    )

    config = kf_langgraph_runtime.KfLangGraphRuntimeConfig.from_settings(app_settings)

    assert config.enabled is True
    assert config.checkpoint_path == checkpoint_path
    assert config.smoke_thread_id == "thread-from-env"


def test_config_status_marks_disabled_langgraph_unhealthy_in_production(monkeypatch) -> None:
    monkeypatch.setattr(config_check.settings, "kf_dual_llm_mode", "production")
    monkeypatch.setattr(config_check.settings, "kf_langgraph_enabled", False)

    status = config_check.get_config_status()

    assert "KF_LANGGRAPH_ENABLED_REQUIRED_FOR_PRODUCTION" in status["missing"]
    assert status["langgraph"]["errors"] == ["KF_LANGGRAPH_ENABLED_REQUIRED_FOR_PRODUCTION"]
    assert status["langgraph"]["required_for_production"] is True


def test_config_status_marks_missing_langgraph_package_unhealthy_in_production(monkeypatch) -> None:
    monkeypatch.setattr(config_check.settings, "kf_dual_llm_mode", "production")
    monkeypatch.setattr(config_check.settings, "kf_langgraph_enabled", True)
    monkeypatch.setattr(config_check, "_installed_version", lambda package_name: "")

    status = config_check.get_config_status()

    assert "LANGGRAPH_PACKAGE_REQUIRED_FOR_PRODUCTION" in status["missing"]
    assert "LANGGRAPH_PACKAGE_REQUIRED_FOR_PRODUCTION" in status["langgraph"]["errors"]
    assert status["ok"] is False


def test_sqlite_checkpointer_can_persist_smoke_graph(tmp_path) -> None:
    checkpointer = kf_langgraph_runtime.build_sqlite_checkpointer(
        tmp_path / "checkpoints.sqlite"
    )

    result = kf_langgraph_runtime.invoke_kf_langgraph_smoke(
        "照片有吗",
        conversation_id="sqlite-smoke",
        checkpointer=checkpointer,
    )

    assert result["route"] == "resolve_room_image"
    assert (tmp_path / "checkpoints.sqlite").exists()
