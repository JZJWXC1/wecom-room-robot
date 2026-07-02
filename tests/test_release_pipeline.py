import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_unattended_runtime, rehearse_release_pipeline


def _powershell_or_skip() -> str:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required for rag-v2-test-gates.ps1 behavior test")
    return powershell


def _run_historical_failure_gate(*args: str) -> subprocess.CompletedProcess[str]:
    powershell = _powershell_or_skip()
    env = dict(os.environ)
    env["APP_ENV"] = "test"
    env["KF_DUAL_LLM_MODE"] = "shadow"
    env["RUN_ONLINE_QA"] = "0"
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path.cwd() / "scripts" / "rag-v2-test-gates.ps1"),
            "-RunHistoricalFailureGateOnly",
            *args,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )


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
    assert result["health_contract"]["ok"] is True
    assert result["server_ops_approval_guard"]["ok"] is True
    assert result["health_contract"]["samples"]["ok_false"]["reason"] == "ok_false"
    assert result["health_contract"]["samples"]["service_mismatch"]["reason"] == "service_mismatch"
    assert result["unattended_env_summary"]["env_file_read"] is False
    assert result["candidate_manifest"]["excluded_runtime_paths_present_in_manifest"] == []
    assert all(result["candidate_manifest"]["required_paths"].values())
    report = tmp_path / "release_rehearsal_report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True


def test_release_preflight_graph_pipeline_runs_local_stategraph(tmp_path: Path) -> None:
    result = rehearse_release_pipeline.run_release_preflight_graph_pipeline(
        Path.cwd(),
        tmp_path,
        version="graph-preflight-test",
        local_tests_command=[sys.executable, "-c", "print('local-ok')"],
        random_guard_command=[sys.executable, "-c", "print('random-ok')"],
        timeout_seconds=30,
    )

    assert result["status"] in {"passed", "blocked"}
    expected_prefix = [
        "release_preflight:local_tests",
        "release_preflight:random_guard",
        "release_preflight:config_check",
    ]
    assert result["trace"][:3] == expected_prefix
    assert result["local_tests"]["stdout_tail"].strip() == "local-ok"
    assert result["random_guard"]["stdout_tail"].strip() == "random-ok"
    report_path = Path(result["report"]["path"])
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if result["status"] == "blocked":
        assert result["blocked_stage"] == "config_check"
        assert result["release_rehearsal"] == {}
        assert report["status"] == "blocked"
        return

    assert result["ok"] is True
    assert result["trace"] == [
        *expected_prefix,
        "release_preflight:release_rehearsal",
        "release_preflight:write_report",
    ]
    assert result["release_rehearsal"]["ok"] is True
    assert report["status"] == "passed"


def test_release_pipeline_cli_defaults_to_graph_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def fake_graph(project_dir: Path, rehearsal_root: Path, **kwargs: object) -> dict:
        calls.append("graph")
        assert project_dir == Path.cwd()
        assert rehearsal_root == tmp_path
        assert kwargs["version"] == "cli-default-graph"
        assert kwargs["fail_fast"] is True
        return {"ok": True, "schema_version": "graph", "trace": ["release_preflight:local_tests"]}

    def fail_legacy(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("release CLI must default to StateGraph preflight")

    monkeypatch.setattr(rehearse_release_pipeline, "run_release_preflight_graph_pipeline", fake_graph)
    monkeypatch.setattr(rehearse_release_pipeline, "rehearse_release_pipeline", fail_legacy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rehearse_release_pipeline.py",
            "--rehearsal-root",
            str(tmp_path),
            "--version",
            "cli-default-graph",
        ],
    )

    assert rehearse_release_pipeline.main() == 0
    assert calls == ["graph"]
    assert json.loads(capsys.readouterr().out)["schema_version"] == "graph"


def test_release_pipeline_cli_requires_explicit_legacy_rehearsal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def fail_graph(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("--legacy-rehearsal must bypass graph preflight")

    def fake_legacy(project_dir: Path, rehearsal_root: Path, **kwargs: object) -> dict:
        calls.append("legacy")
        assert project_dir == Path.cwd()
        assert rehearsal_root == tmp_path
        assert kwargs["version"] == "cli-legacy"
        return {"ok": True, "schema_version": "legacy"}

    monkeypatch.setattr(rehearse_release_pipeline, "run_release_preflight_graph_pipeline", fail_graph)
    monkeypatch.setattr(rehearse_release_pipeline, "rehearse_release_pipeline", fake_legacy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rehearse_release_pipeline.py",
            "--rehearsal-root",
            str(tmp_path),
            "--version",
            "cli-legacy",
            "--legacy-rehearsal",
        ],
    )

    assert rehearse_release_pipeline.main() == 0
    assert calls == ["legacy"]
    assert json.loads(capsys.readouterr().out)["schema_version"] == "legacy"


def test_release_manifest_includes_untracked_release_source_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    service_dir = project / "app" / "services"
    scripts_dir = project / "scripts"
    service_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    (project / "app" / "main.py").write_text(
        "from app.services import fresh_graph\n",
        encoding="utf-8",
    )
    (service_dir / "__init__.py").write_text("", encoding="utf-8")
    (service_dir / "fresh_graph.py").write_text("VALUE = 1\n", encoding="utf-8")
    (scripts_dir / "server-ops.ps1").write_text("Require-DeployApproval\n", encoding="utf-8")

    subprocess.run(
        ["git", "init"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    subprocess.run(
        ["git", "add", "app/main.py", "app/services/__init__.py", "scripts/server-ops.ps1"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    manifest = rehearse_release_pipeline.build_release_manifest(
        project,
        version="untracked-source",
        git_head="test",
    )

    assert "app/services/fresh_graph.py" in manifest["untracked_release_source_paths_in_manifest"]
    assert manifest["excluded_runtime_paths_present_in_manifest"] == []


def test_release_rehearsal_env_summary_does_not_print_secret_values(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env_file = project / ".env"
    env_file.write_text(
        "\n".join(
            [
                "WECOM_KF_SECRET=" + "previous_secret",
                "FEISHU_APP_SECRET=" + "previous_key",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = rehearse_release_pipeline.build_unattended_env_summary(project)
    dumped = json.dumps(summary, ensure_ascii=False)

    assert summary["env_file_exists"] is True
    assert summary["env_file_read"] is False
    assert summary["required_env_completeness_checked"] is False
    assert summary["secret_values_printed"] is False
    assert "previous_secret" not in dumped
    assert "previous_key" not in dumped
    assert "skipped_in_local_rehearsal_to_avoid_reading_secrets" in dumped


def test_release_rehearsal_health_contract_matrix_blocks_bad_payloads() -> None:
    contract = rehearse_release_pipeline.validate_health_contract()

    assert contract["ok"] is True
    assert contract["samples"]["healthy"]["ok"] is True
    assert contract["samples"]["ok_false"] == {
        "ok": False,
        "service": "wecom-room-robot-agentic-rag",
        "reason": "ok_false",
    }
    assert contract["samples"]["service_mismatch"]["ok"] is False
    assert contract["samples"]["missing_service"]["ok"] is False


def test_server_ops_requires_approve_deploy_before_credential_load() -> None:
    guard = rehearse_release_pipeline.inspect_server_ops_approval_guard(Path.cwd())

    assert guard["ok"] is True
    assert guard["requires_approve_deploy"] is True
    assert guard["approval_guard_before_credential_load"] is True
    assert guard["approval_guard_line"] < guard["credential_load_line"]
    assert guard["function_definition_lines"]


def test_server_ops_approval_guard_ignores_definition_when_call_after_credential_load(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "server-ops.ps1").write_text(
        "\n".join(
            [
                'param([string]$ApproveDeploy = "")',
                "function Require-DeployApproval {",
                '    if ($ApproveDeploy -ne "APPROVE_DEPLOY") { throw "blocked" }',
                "}",
                '$CredentialFile = Join-Path (Get-Location) ".local/server-credentials.ps1"',
                "if (Test-Path $CredentialFile) {",
                "    $credentialText = Get-Content -Path $CredentialFile -Raw",
                "}",
                "Require-DeployApproval",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    guard = rehearse_release_pipeline.inspect_server_ops_approval_guard(project)

    assert guard["ok"] is False
    assert guard["requires_approve_deploy"] is True
    assert guard["approval_guard_before_credential_load"] is False
    assert guard["function_definition_lines"] == [2]
    assert guard["approval_guard_line"] == 9
    assert guard["credential_load_line"] == 7


def test_release_rehearsal_ok_requires_approval_guard_before_credential_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_guard(project_dir: Path) -> dict[str, bool]:
        return {
            "ok": False,
            "requires_approve_deploy": True,
            "approval_guard_before_credential_load": False,
        }

    monkeypatch.setattr(rehearse_release_pipeline, "inspect_server_ops_approval_guard", fake_guard)

    result = rehearse_release_pipeline.rehearse_release_pipeline(
        Path.cwd(),
        tmp_path,
        version="bad-approval-order",
    )
    report = json.loads((tmp_path / "release_rehearsal_report.json").read_text(encoding="utf-8"))

    assert result["ok"] is False
    assert report["ok"] is False
    assert result["server_ops_approval_guard"]["requires_approve_deploy"] is True
    assert result["server_ops_approval_guard"]["approval_guard_before_credential_load"] is False


def test_release_rehearsal_release_ready_false_when_approval_call_after_credential_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "server-ops.ps1").write_text(
        "\n".join(
            [
                'param([string]$ApproveDeploy = "")',
                "function Require-DeployApproval {",
                '    if ($ApproveDeploy -ne "APPROVE_DEPLOY") { throw "blocked" }',
                "}",
                '$CredentialFile = Join-Path (Get-Location) ".local/server-credentials.ps1"',
                "$credentialText = Get-Content -Path $CredentialFile -Raw",
                "Require-DeployApproval",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_manifest(project_dir: Path, *, version: str, git_head: str) -> dict[str, object]:
        return {
            "schema_version": rehearse_release_pipeline.RELEASE_REHEARSAL_SCHEMA_VERSION,
            "version": version,
            "git_head": git_head,
            "file_count": len(rehearse_release_pipeline.REQUIRED_RELEASE_PATHS),
            "files_sha256": "sanitized-test-digest",
            "required_paths": {path: True for path in rehearse_release_pipeline.REQUIRED_RELEASE_PATHS},
            "excluded_runtime_paths_present_in_manifest": [],
        }

    monkeypatch.setattr(rehearse_release_pipeline, "build_release_manifest", fake_manifest)
    monkeypatch.setattr(rehearse_release_pipeline, "_git_output", lambda project_dir, args: "test-head")

    result = rehearse_release_pipeline.rehearse_release_pipeline(
        project,
        tmp_path / "rehearsal",
        version="bad-approval-order",
    )

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert result["candidate_manifest"]["excluded_runtime_paths_present_in_manifest"] == []
    assert all(result["candidate_manifest"]["required_paths"].values())
    assert result["health_contract"]["ok"] is True
    assert result["server_ops_approval_guard"]["requires_approve_deploy"] is True
    assert result["server_ops_approval_guard"]["approval_guard_before_credential_load"] is False
    assert result["server_ops_approval_guard"]["approval_guard_line"] > result["server_ops_approval_guard"]["credential_load_line"]


def test_historical_failure_gate_fails_closed_when_fixture_missing(tmp_path: Path) -> None:
    missing_fixture = tmp_path / "missing_historical_failures.json"

    completed = _run_historical_failure_gate(
        "-HistoricalFailuresFixture",
        str(missing_fixture),
    )

    assert completed.returncode != 0
    assert "historical failure replay QA" in completed.stdout + completed.stderr
    assert "L4 requires" in completed.stdout + completed.stderr


def test_historical_failure_gate_blocks_bad_artifact_and_sanitizes_hash(tmp_path: Path) -> None:
    fixture = tmp_path / "historical_failures_synthetic_sanitized.json"
    fixture.write_text(
        json.dumps(
            {
                "schema": "historical_failures_synthetic_sanitized.v1",
                "windows": [
                    {"id": "hist_001", "source": "synthetic_sanitized_fixture", "turns": ["房源表发我"]},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    long_hash = "a" * 64
    fake_phone = "1" + ("3" * 10)
    fake_token = "sk-" + ("x" * 24)
    fake_signature = "raw-prod-signature"
    fake_password = "abcd#"
    artifact = tmp_path / "bad_historical_artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "full_suite_completed": True,
                "quality_status": {
                    "passed": False,
                    "high_count": 1,
                    "medium_count": 0,
                    "exit_code": 3,
                },
                "summary": {"usable_for_release": False},
                "canonical_result_hash": long_hash,
                "raw_signature": fake_signature,
                "token_value": fake_token,
                "contact_text": fake_phone,
                "viewing_password": fake_password,
                "windows": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _run_historical_failure_gate(
        "-HistoricalFailuresFixture",
        str(fixture),
        "-HistoricalFailuresArtifact",
        str(artifact),
    )
    combined_output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert "historical failure replay QA" in combined_output
    assert "high=1" in combined_output or "blocks release" in combined_output
    assert long_hash not in combined_output
    sanitized_artifact = artifact.read_text(encoding="utf-8")
    assert long_hash not in sanitized_artifact
    assert fake_signature not in sanitized_artifact
    assert fake_token not in sanitized_artifact
    assert fake_phone not in sanitized_artifact
    assert fake_password not in sanitized_artifact


def test_historical_failure_gate_surfaces_runner_artifact_with_stderr_logging() -> None:
    completed = _run_historical_failure_gate(
        "-HistoricalFailuresFixture",
        "tests/fixtures/qa/historical_failures_synthetic_sanitized.json",
    )
    combined_output = completed.stdout + completed.stderr

    assert "historical failure replay QA" in combined_output
    assert "ARTIFACT" in combined_output
    assert "QA_SCOPE" in combined_output
    if completed.returncode == 0:
        assert "QA artifact gate passed high=0 medium=0" in combined_output
    else:
        assert "QA artifact blocks release" in combined_output
    assert "NativeCommandError" not in combined_output


def test_external_capture_handles_large_stderr_without_deadlock(tmp_path: Path) -> None:
    fixture = tmp_path / "historical_failures_synthetic_sanitized.json"
    fixture.write_text(
        json.dumps(
            {
                "schema": "historical_failures_synthetic_sanitized.v1",
                "windows": [
                    {"id": "hist_001", "source": "synthetic_sanitized_fixture", "turns": ["房源表发我"]},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "good_historical_artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "full_suite_completed": True,
                "quality_status": {"passed": True, "high_count": 0, "medium_count": 0, "exit_code": 0},
                "summary": {"usable_for_release": True},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python = tmp_path / "fake_python.cmd"
    fake_python.write_text(
        "@echo off\n"
        "for /L %%i in (1,1,2500) do echo stderr-line-%%i 1>&2\n"
        f"echo ARTIFACT {artifact}\n"
        "exit /b 0\n",
        encoding="utf-8",
    )

    powershell = _powershell_or_skip()
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path.cwd() / "scripts" / "rag-v2-test-gates.ps1"),
            "-RunHistoricalFailureGateOnly",
            "-HistoricalFailuresFixture",
            str(fixture),
            "-Python",
            str(fake_python),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    combined_output = completed.stdout + completed.stderr

    assert completed.returncode == 0, combined_output
    assert "stderr-line-2500" in combined_output
    assert "QA artifact gate passed high=0 medium=0" in combined_output
    assert "NativeCommandError" not in combined_output


def test_external_capture_blocks_nonzero_exit_after_good_artifact(tmp_path: Path) -> None:
    fixture = tmp_path / "historical_failures_synthetic_sanitized.json"
    fixture.write_text(
        json.dumps(
            {
                "schema": "historical_failures_synthetic_sanitized.v1",
                "windows": [
                    {"id": "hist_001", "source": "synthetic_sanitized_fixture", "turns": ["房源表发我"]},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "good_historical_artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "full_suite_completed": True,
                "quality_status": {"passed": True, "high_count": 0, "medium_count": 0, "exit_code": 0},
                "summary": {"usable_for_release": True},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python = tmp_path / "fake_python_fails_after_artifact.cmd"
    fake_python.write_text(
        "@echo off\n"
        f"echo ARTIFACT {artifact}\n"
        "exit /b 7\n",
        encoding="utf-8",
    )

    powershell = _powershell_or_skip()
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path.cwd() / "scripts" / "rag-v2-test-gates.ps1"),
            "-RunHistoricalFailureGateOnly",
            "-HistoricalFailuresFixture",
            str(fixture),
            "-Python",
            str(fake_python),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    combined_output = completed.stdout + completed.stderr

    assert completed.returncode != 0, combined_output
    assert "QA artifact gate passed high=0 medium=0" in combined_output
    assert "Command failed with exit code 7" in combined_output
    assert "NativeCommandError" not in combined_output


def test_server_ops_propagates_remote_helper_failure(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required for server-ops.ps1 behavior test")

    project = tmp_path / "project"
    scripts = project / "scripts"
    local = project / ".local"
    scripts.mkdir(parents=True)
    local.mkdir()
    (scripts / "server-ops.ps1").write_text(
        (Path.cwd() / "scripts" / "server-ops.ps1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "server_exec.py").write_text(
        "import sys\nprint('fake remote failure', file=sys.stderr)\nsys.exit(7)\n",
        encoding="utf-8",
    )
    (local / "server-credentials.ps1").write_text(
        '$env:ROOM_ROBOT_SSH_PASSWORD="fake-password"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "server-ops.ps1"),
            "Status",
            "-HostName",
            "example.invalid",
            "-User",
            "deploy",
            "-ApproveDeploy",
            "APPROVE_DEPLOY",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "server_exec.py failed with exit code 7" in completed.stderr + completed.stdout


def test_server_ops_supports_ssh_host_user_and_bind_address_overrides() -> None:
    project = Path.cwd()
    server_ops = (project / "scripts" / "server-ops.ps1").read_text(encoding="utf-8")
    helper_sources = "\n".join(
        (project / path).read_text(encoding="utf-8")
        for path in (
            "scripts/server_exec.py",
            "scripts/server_upload.py",
            "scripts/server_download.py",
        )
    )

    assert "ROOM_ROBOT_SSH_HOST" in server_ops
    assert "ROOM_ROBOT_SSH_USER" in server_ops
    assert "ROOM_ROBOT_SSH_BIND_ADDRESS" in server_ops
    assert "--bind-address" in server_ops
    assert "ROOM_ROBOT_SSH_BIND_ADDRESS" in helper_sources
    assert "sock.bind((args.bind_address, 0))" in helper_sources


def test_unattended_health_payload_parser_requires_ok_service() -> None:
    assert check_unattended_runtime._health_payload_status('{"ok": true, "service": "wecom-room-robot-agentic-rag"}') == "ok"
    assert check_unattended_runtime._health_payload_status('{"ok": false, "service": "wecom-room-robot-agentic-rag"}') == "unhealthy:ok_false"
    assert check_unattended_runtime._health_payload_status("not json").startswith("invalid_json:")


def test_unattended_check_requires_media_manifest_in_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KF_DUAL_LLM_MODE", "production")
    monkeypatch.delenv("ROOM_DATABASE_PATH", raising=False)

    assert check_unattended_runtime._dual_llm_mode({}) == "production"
    assert check_unattended_runtime._manifest_status(tmp_path, {}) == "missing"

    monkeypatch.setenv("KF_DUAL_LLM_MODE", "shadow")
    assert check_unattended_runtime._dual_llm_mode({}) == "shadow"


def test_unattended_manifest_status_requires_send_ready_evidence(tmp_path: Path) -> None:
    from app.services.inventory_snapshot_models import generate_listing_id
    from app.services.media_manifest import BINDING_METHOD_LISTING_ID, MEDIA_KIND_VIDEO, MediaItem, MediaManifest

    listing_id = generate_listing_id("长浜龙吟轩", "11-1603")
    room_database = tmp_path / "room_database"
    manifest_path = room_database / "media_manifest.json"
    manifest = MediaManifest.build(
        listing_ids=[listing_id],
        items=[
            MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name="missing.mp4",
                relative_path=f"video/{listing_id}/missing.mp4",
                sha256="0" * 64,
                binding_method=BINDING_METHOD_LISTING_ID,
                access_verified=True,
            )
        ],
    )
    manifest.write_json(manifest_path)

    assert (
        check_unattended_runtime._manifest_status(
            tmp_path,
            {"ROOM_DATABASE_PATH": str(room_database)},
        )
        == "no_send_ready_evidence"
    )
