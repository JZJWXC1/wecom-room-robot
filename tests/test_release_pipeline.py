import json
from pathlib import Path

from scripts import check_unattended_runtime, rehearse_release_pipeline


def test_release_rehearsal_creates_current_pointer_and_rolls_back(tmp_path: Path) -> None:
    project_dir = Path.cwd()
    result = rehearse_release_pipeline.rehearse_release_pipeline(
        project_dir,
        tmp_path,
        version="test-release",
    )

    assert result["ok"] is True
    assert result["candidate_manifest"]["version"] == "test-release"
    assert result["rollback"]["ok"] is True
    assert result["rollback"]["from_version"] == "test-release"
    assert result["rollback"]["to_version"] == "previous-good"
    assert result["current_pointer"]["version"] == "previous-good"
    assert result["candidate_manifest"]["excluded_runtime_paths_present_in_manifest"] == []
    assert all(result["candidate_manifest"]["required_paths"].values())
    report = tmp_path / "release_rehearsal_report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True


def test_release_rehearsal_env_summary_does_not_print_secret_values(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env_file = project / ".env"
    env_file.write_text(
        "WECOM_KF_SECRET=super-secret-value\n"
        "FEISHU_APP_SECRET=another-secret-value\n",
        encoding="utf-8",
    )

    summary = rehearse_release_pipeline.build_unattended_env_summary(project)
    dumped = json.dumps(summary, ensure_ascii=False)

    assert summary["env_file_exists"] is True
    assert summary["secret_values_printed"] is False
    assert "super-secret-value" not in dumped
    assert "another-secret-value" not in dumped


def test_server_ops_requires_approve_deploy_before_credential_load() -> None:
    guard = rehearse_release_pipeline.inspect_server_ops_approval_guard(Path.cwd())

    assert guard["requires_approve_deploy"] is True
    assert guard["approval_guard_before_credential_load"] is True


def test_unattended_health_payload_parser_requires_ok_service() -> None:
    assert check_unattended_runtime._health_payload_status('{"ok": true, "service": "wecom-room-robot-agentic-rag"}') == "ok"
    assert check_unattended_runtime._health_payload_status('{"ok": false, "service": "wecom-room-robot-agentic-rag"}') == "unhealthy:ok_false"
    assert check_unattended_runtime._health_payload_status("not json").startswith("invalid_json:")
