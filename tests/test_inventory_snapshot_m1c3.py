from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket
from typing import Any

from app.services.inventory_snapshot_models import now_utc_iso
from app.services.inventory_snapshot_shadow_observer import (
    ShadowObservationOptions,
    collect_shadow_observation,
    format_shadow_observation,
)
from app.services.inventory_snapshot_shadow_preflight import (
    ShadowPreflightOptions,
    run_shadow_preflight,
)


def make_shadow_state(
    root: Path,
    *,
    status: str = "healthy",
    blocking_count: int = 0,
    warning_count: int = 0,
    stale: bool = False,
    corrupt_status: bool = False,
    safe_error_message: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = "20260625T010203Z_abcdef123456"
    if corrupt_status:
        (root / "shadow_status.json").write_text("{not-json", encoding="utf-8")
        return
    attempt_at = now_utc_iso()
    if stale:
        attempt_at = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    passed = status == "healthy" and blocking_count == 0
    payload = {
        "schema_version": "inventory_snapshot_shadow_status.v2",
        "last_attempt_at": attempt_at,
        "last_success_at": attempt_at if passed else "",
        "last_sync_run_id": "sync-001",
        "source_version": "source-v1",
        "source_hash": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "last_source_hash": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "last_counted_source_hash": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "snapshot_id": snapshot_id,
        "last_snapshot_id": snapshot_id,
        "reconciliation_passed": passed,
        "last_reconciliation_passed": passed,
        "blocking_count": blocking_count,
        "last_blocking_count": blocking_count,
        "warning_count": warning_count,
        "last_warning_count": warning_count,
        "consecutive_passes": 3 if passed else 0,
        "consecutive_failures": 0 if passed else 1,
        "duration_ms": 123,
        "error_code": "" if status != "error" else "forced",
        "safe_error_message": safe_error_message,
    }
    (root / "shadow_status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    snapshot_dir = root / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "inventory.json").write_text('{"ok": true, "中文": "安全"}', encoding="utf-8")
    report = {
        "passed": passed,
        "legacy_record_count": 2,
        "snapshot_record_count": 2,
        "matched_count": 2,
        "severity_counts": {"blocking": blocking_count, "warning": warning_count, "info": 0},
    }
    report_path = root / "reports" / f"{snapshot_id}_reconciliation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    pointer = {
        "snapshot_id": snapshot_id,
        "source_hash": payload["source_hash"],
        "report_path": f"reports/{snapshot_id}_reconciliation.json",
    }
    (root / "shadow_current_snapshot.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")


def test_shadow_cli_healthy_human_output_utf8(capsys, tmp_path: Path) -> None:
    import scripts.check_inventory_snapshot_shadow as cli

    make_shadow_state(tmp_path / "shadow")
    exit_code = cli.main(["--root", str(tmp_path / "shadow"), "--mode", "shadow"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "InventorySnapshot Shadow 观察" in output
    assert "mode: shadow" in output
    assert "status: healthy" in output
    assert "ready_for_cutover_evaluation: true" in output
    assert "中文" not in output


def test_shadow_cli_never_run_disabled_stale_blocking_and_corrupt(capsys, tmp_path: Path) -> None:
    import scripts.check_inventory_snapshot_shadow as cli

    cases = [
        (["--root", str(tmp_path / "never"), "--mode", "shadow"], "never_run"),
        (["--root", str(tmp_path / "disabled"), "--mode", "disabled"], "disabled"),
    ]
    make_shadow_state(tmp_path / "stale", stale=True)
    cases.append((["--root", str(tmp_path / "stale"), "--mode", "shadow", "--stale-seconds", "60"], "stale"))
    make_shadow_state(tmp_path / "blocking", status="blocking", blocking_count=1)
    cases.append((["--root", str(tmp_path / "blocking"), "--mode", "shadow"], "blocking"))
    make_shadow_state(tmp_path / "corrupt", corrupt_status=True)
    cases.append((["--root", str(tmp_path / "corrupt"), "--mode", "shadow"], "error"))

    for args, expected_status in cases:
        assert cli.main(args) == 0
        output = capsys.readouterr().out
        assert f"status: {expected_status}" in output


def test_shadow_cli_json_is_safe_and_does_not_modify_files(capsys, tmp_path: Path) -> None:
    import scripts.check_inventory_snapshot_shadow as cli

    root = tmp_path / "shadow"
    make_shadow_state(
        root,
        status="error",
        safe_error_message="SECRET_CANARY_M1C3 token abc 19900009999 C:\\Users\\someone\\file.txt",
    )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert cli.main(["--root", str(root), "--mode", "shadow", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert before == after
    assert payload["status"] == "error"
    assert "SECRET_CANARY" not in output
    assert "token abc" not in output
    assert "19900009999" not in output
    assert "C:\\Users" not in output


def test_observer_public_output_has_no_secret_phone_or_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    make_shadow_state(root, status="error", safe_error_message="TEST_SECRET_X 19900009999 C:\\Users\\secret\\x")
    payload = collect_shadow_observation(ShadowObservationOptions(root=root, mode="shadow"))
    text = json.dumps(payload, ensure_ascii=False) + format_shadow_observation(payload)

    assert "TEST_SECRET" not in text
    assert "19900009999" not in text
    assert "C:\\Users" not in text


def make_project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "room_database").mkdir()
    (root / "app" / "main.py").write_text(
        "async def _refresh_inventory():\n"
        "    run_inventory_snapshot_shadow()\n",
        encoding="utf-8",
    )
    (root / "data" / "inventory_cache.csv").write_text("小区,房号\n虚构,1-101\n", encoding="utf-8")
    (root / "data" / "rewrite_inventory_index.json").write_text('{"row_count": 1}', encoding="utf-8")
    (root / "room_database" / "inventory_1.png").write_bytes(b"png")
    return root


def preflight_options(root: Path, **overrides: Any) -> ShadowPreflightOptions:
    kwargs = {
        "mode": "shadow",
        "project_root": root,
        "shadow_root": root / "data" / "inventory_snapshots_shadow",
        "production_snapshot_root": root / "data" / "inventory_snapshots",
        "inventory_cache_path": Path("data/inventory_cache.csv"),
        "rewrite_inventory_index_path": Path("data/rewrite_inventory_index.json"),
        "inventory_image_glob": "room_database/inventory_*.png",
        "room_database_path": Path("room_database"),
        "min_free_bytes": 1,
    }
    kwargs.update(overrides)
    return ShadowPreflightOptions(**kwargs)


def test_preflight_success_no_network_and_no_production_pointer_write(monkeypatch, tmp_path: Path) -> None:
    project = make_project_root(tmp_path)
    production_root = project / "data" / "inventory_snapshots"
    production_root.mkdir()
    pointer = production_root / "current_snapshot.json"
    pointer.write_text('{"snapshot_id": "old"}', encoding="utf-8")
    before = pointer.read_bytes()

    def fail_socket(*args: Any, **kwargs: Any) -> socket.socket:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", fail_socket)
    result = run_shadow_preflight(preflight_options(project))

    assert result["ok"] is True
    assert result["network_access"] == "not_attempted"
    assert result["writes"] == "none"
    assert pointer.read_bytes() == before


def test_preflight_detects_illegal_mode_and_shadow_production_overlap(tmp_path: Path) -> None:
    project = make_project_root(tmp_path)
    illegal = run_shadow_preflight(preflight_options(project, mode="primary"))
    overlap = run_shadow_preflight(
        preflight_options(
            project,
            shadow_root=project / "data" / "inventory_snapshots",
            production_snapshot_root=project / "data" / "inventory_snapshots",
        )
    )

    assert illegal["ok"] is False
    assert any(item["name"] == "mode" and item["severity"] == "error" for item in illegal["checks"])
    assert overlap["ok"] is False
    assert any(item["name"] == "shadow_production_path_isolation" and item["status"] == "overlap" for item in overlap["checks"])


def test_preflight_detects_snapshot_reader_in_customer_path(tmp_path: Path) -> None:
    project = make_project_root(tmp_path)
    (project / "app" / "main.py").write_text(
        "from app.services.inventory_snapshot_reader import SnapshotReader\n"
        "async def handle_customer_message():\n"
        "    return SnapshotReader('data/inventory_snapshots')\n",
        encoding="utf-8",
    )
    result = run_shadow_preflight(preflight_options(project))

    assert result["ok"] is False
    assert any(item["name"] == "production_pointer_reader" for item in result["checks"])


def test_preflight_cli_json_uses_safe_path_labels(capsys, tmp_path: Path) -> None:
    import scripts.preflight_inventory_snapshot_shadow as cli

    project = make_project_root(tmp_path)
    old_root = cli.PROJECT_ROOT
    cli.PROJECT_ROOT = project
    try:
        exit_code = cli.main(
            [
                "--json",
                "--mode",
                "shadow",
                "--shadow-root",
                str(project / "data" / "inventory_snapshots_shadow"),
                "--production-snapshot-root",
                str(project / "data" / "inventory_snapshots"),
                "--inventory-cache-path",
                "data/inventory_cache.csv",
                "--rewrite-inventory-index-path",
                "data/rewrite_inventory_index.json",
                "--inventory-image-glob",
                "room_database/inventory_*.png",
                "--room-database-path",
                "room_database",
                "--min-free-bytes",
                "1",
            ]
        )
    finally:
        cli.PROJECT_ROOT = old_root
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["ok"] is True
    assert "<absolute>/" in payload["shadow_root_label"]
    assert str(tmp_path) not in output
