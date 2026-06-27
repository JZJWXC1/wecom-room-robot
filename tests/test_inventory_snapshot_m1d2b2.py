from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.inventory_read_models import (
    FALLBACK_LEGACY_WHOLE_REQUEST,
    FALLBACK_STRICT,
    READ_MODE_DISABLED,
    READ_MODE_PRIMARY,
    REASON_FALLBACK_NOT_ALLOWED_AFTER_READ,
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    InventoryReadError,
)
from app.services.inventory_read_provider import SnapshotInventoryReadProvider
from app.services.inventory_read_router import InventoryReadRouter
from app.services.inventory_sensitive_access import (
    REASON_SHEET_ARTIFACT_MISMATCH,
    sheet_artifacts_for_context,
)
from app.services.inventory_snapshot_cutover import (
    PreparedOutboundPackage,
    build_local_snapshot,
    default_replay_cases,
    evaluate_cutover_readiness,
    legacy_removal_report,
    ready_readiness_state,
    rehearse_rollback,
    run_primary_replay,
    stability_replay_cases,
    strict_and_fallback_probe,
    synthetic_inventory_rows,
)
from app.services.inventory_snapshot_models import sanitize_for_log
from app.services.inventory_snapshot_offline import scan_safe_artifacts_for_canaries
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_shadow import scan_public_artifacts_for_sensitive_text
from tests.test_inventory_read_router import legacy_provider


def run(coro):
    return asyncio.run(coro)


async def noop_refresh() -> None:
    return None


def test_local_primary_replay_builds_fictional_snapshot_and_readiness_report(tmp_path: Path) -> None:
    root = tmp_path / "primary-replay"
    report = run_primary_replay(root)
    readiness = evaluate_cutover_readiness(root, replay_report=report, min_parity_cases=len(default_replay_cases()))
    payload = json.dumps({"report": report, "readiness": readiness}, ensure_ascii=False)

    assert report["ok"] is True
    assert report["case_count"] == len(default_replay_cases())
    assert all(item["parity_passed"] for item in report["cases"])
    assert report["public_artifact_scan"]["passed"] is True
    assert report["sheet_evidence"]
    assert report["prepared_outbound_package"]["send_actions"]
    assert readiness["ready"] is True
    assert "0007#" not in payload
    assert "SECRET_CANARY" not in payload
    assert "TOKEN_CANARY" not in payload
    assert "19900009999" not in payload
    assert str(tmp_path) not in payload


def test_primary_replay_is_self_contained_without_pytest_inventory_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "inventory_source", "local_image")
    report = run_primary_replay(tmp_path / "self-contained")
    readiness = evaluate_cutover_readiness(
        tmp_path / "self-contained",
        replay_report=report,
        min_parity_cases=len(default_replay_cases()),
    )

    assert report["ok"] is True
    assert all(item["parity_passed"] for item in report["cases"])
    assert readiness["ready"] is True


def test_primary_replay_twenty_case_parity_keeps_order_and_prices(tmp_path: Path) -> None:
    rows = synthetic_inventory_rows()
    cases = stability_replay_cases(rows)
    report = run_primary_replay(tmp_path / "twenty-case", rows=rows, cases=cases)
    readiness = evaluate_cutover_readiness(tmp_path / "twenty-case", replay_report=report)

    assert len(cases) == 20
    assert report["ok"] is True
    assert readiness["ready"] is True
    assert readiness["required_parity_cases"] == 20
    for item in report["cases"]:
        legacy = item["legacy"]
        snapshot = item["snapshot"]
        assert item["parity_passed"] is True
        assert [row["listing_id"] for row in legacy] == [row["listing_id"] for row in snapshot]
        assert [
            (row["listing_id"], row["rent_pay1"], row["rent_pay2"])
            for row in legacy
        ] == [
            (row["listing_id"], row["rent_pay1"], row["rent_pay2"])
            for row in snapshot
        ]


def test_two_snapshot_versions_keep_turn_level_context_locked(tmp_path: Path) -> None:
    root = tmp_path / "turn-lock"
    first = build_local_snapshot(root, synthetic_inventory_rows(version="v1"), version="v1")
    router = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=legacy_provider(synthetic_inventory_rows(version="v1")),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
        readiness_state=ready_readiness_state(first.snapshot),
    )
    session = router.start_turn(request_id="lock", turn_id="turn-1")
    second = build_local_snapshot(root, synthetic_inventory_rows(version="v2"), version="v2")

    evidence = run(session.search_inventory("晨星花园1-101A", limit=1))

    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert session.context.snapshot_id == first.snapshot.snapshot_id
    assert evidence[0].snapshot_id == first.snapshot.snapshot_id
    assert evidence[0].rent_pay1 == 1800


def test_strict_fallback_and_half_turn_mixing_are_blocked(tmp_path: Path) -> None:
    probe = strict_and_fallback_probe(tmp_path / "empty-root")
    assert probe["strict"]["ok"] is False
    assert probe["legacy_whole_request"]["ok"] is True
    assert probe["legacy_whole_request"]["context"]["source_kind"] == SOURCE_KIND_LEGACY
    assert probe["legacy_whole_request"]["context"]["fallback_used"] is True

    root = tmp_path / "after-read"
    build = build_local_snapshot(root, synthetic_inventory_rows(), version="after-read")
    session = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_LEGACY_WHOLE_REQUEST,
        legacy_provider=legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
        readiness_state=ready_readiness_state(build.snapshot),
    ).start_turn(request_id="half-turn", turn_id="turn-1")
    run(session.search_inventory("晨星花园1-101A", limit=1))

    with pytest.raises(InventoryReadError) as excinfo:
        session.require_whole_request_fallback_allowed()
    assert excinfo.value.code == REASON_FALLBACK_NOT_ALLOWED_AFTER_READ


def test_fault_injection_blocks_pointer_manifest_private_and_png(tmp_path: Path) -> None:
    pointer_root = tmp_path / "pointer"
    build_local_snapshot(pointer_root, synthetic_inventory_rows(), version="pointer")
    (pointer_root / "current_snapshot.json").unlink()
    pointer_eval = evaluate_cutover_readiness(pointer_root)
    assert pointer_eval["ready"] is False
    assert "current_pointer_missing" in pointer_eval["not_ready_reasons"]

    manifest_root = tmp_path / "manifest"
    manifest_build = build_local_snapshot(manifest_root, synthetic_inventory_rows(), version="manifest")
    manifest_path = manifest_root / "snapshots" / manifest_build.snapshot.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["inventory_json"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_eval = evaluate_cutover_readiness(manifest_root)
    assert manifest_eval["ready"] is False
    assert "snapshot_integrity_failed" in manifest_eval["not_ready_reasons"]

    private_root = tmp_path / "private"
    private_build = build_local_snapshot(private_root, synthetic_inventory_rows(), version="private")
    private_path = private_root / "snapshots" / private_build.snapshot.snapshot_id / "private" / "viewing_secrets.json"
    private_payload = json.loads(private_path.read_text(encoding="utf-8"))
    private_payload["tampered"] = True
    private_path.write_text(json.dumps(private_payload, ensure_ascii=False), encoding="utf-8")
    private_eval = evaluate_cutover_readiness(private_root)
    assert private_eval["ready"] is False
    assert "snapshot_integrity_failed" in private_eval["not_ready_reasons"]

    png_root = tmp_path / "png"
    png_build = build_local_snapshot(png_root, synthetic_inventory_rows(), version="png", include_sheet_png=True)
    png_path = png_root / "snapshots" / png_build.snapshot.snapshot_id / "png" / "inventory.png"
    png_path.write_bytes(b"tampered-png")
    png_eval = evaluate_cutover_readiness(png_root)
    assert png_eval["ready"] is False
    assert "snapshot_integrity_failed" in png_eval["not_ready_reasons"]

    session = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(png_root)),
        readiness_state=ready_readiness_state(png_build.snapshot),
    ).select_context(request_id="png", turn_id="turn-1")
    assert session.ok is False


def test_snapshot_sheet_png_fault_is_reported_before_send(tmp_path: Path) -> None:
    root = tmp_path / "sheet-png"
    build = build_local_snapshot(root, synthetic_inventory_rows(), version="sheet", include_sheet_png=True)
    router = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
        readiness_state=ready_readiness_state(build.snapshot),
    )
    context = router.start_turn(request_id="sheet", turn_id="turn-1").context
    png_path = root / "snapshots" / build.snapshot.snapshot_id / "png" / "inventory.png"
    png_path.write_bytes(b"tampered-png-before-send")

    with pytest.raises(InventoryReadError) as excinfo:
        run(
            sheet_artifacts_for_context(
                context=context,
                refresh_func=noop_refresh,
                list_paths_func=lambda: [],
                snapshot_reader=SnapshotReader(root),
            )
        )
    assert excinfo.value.code == REASON_SHEET_ARTIFACT_MISMATCH


def test_sensitive_scan_regression_hash_ids_pass_and_business_text_blocks(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe-scan"
    safe_root.mkdir()
    phone_like_hash = "a19900009999b" + ("0" * 51)
    (safe_root / "manifest.json").write_text(
        json.dumps(
            {
                "files": {"inventory_json": {"sha256": phone_like_hash}},
                "source_hash": phone_like_hash,
                "snapshot_id": "20260625T010203Z_199000099999",
                "decision_id": "ird_17435665458f662a",
                "evidence_id": "evd_17435665458f662a",
                "listing_id": "lst_17435665458f662a",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scan = scan_public_artifacts_for_sensitive_text(safe_root)
    offline_passed, offline_issues = scan_safe_artifacts_for_canaries(safe_root)
    assert scan["passed"] is True
    assert offline_passed is True
    assert offline_issues == []

    blocked_cases = {
        "phone_desc.json": {"description": "联系 " + "199" + "0000" + "9999"},
        "nested_phone.json": {"items": [{"note": "电话 " + "199" + "0000" + "9999"}]},
        "password_key.json": {"password": "123"},
        "token_key.json": {"token": "abc"},
        "nested_credentials.json": {"items": [{"metadata": {"access_token": "abc"}}, {"credentials": ["x"]}]},
        "canary.json": {"note": "SECRET_CANARY_M1D2B2 TOKEN_CANARY_M1D2B2"},
        "manifest_leak.json": {"files": {}, "business_note": "联系 " + "199" + "0000" + "9999"},
    }
    for filename, payload in blocked_cases.items():
        case_root = tmp_path / filename
        case_root.mkdir()
        (case_root / filename).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        case_scan = scan_public_artifacts_for_sensitive_text(case_root)
        assert case_scan["passed"] is False, filename
        assert case_scan["findings"][0]["path"] == filename

    text_cases = {
        "token_text.txt": "本地演练 token_abc123 不得进入公开 artifact",
        "password_text.txt": "本地演练 password=123456 不得进入公开 artifact",
    }
    for filename, text in text_cases.items():
        case_root = tmp_path / filename
        case_root.mkdir()
        (case_root / filename).write_text(text, encoding="utf-8")
        case_scan = scan_public_artifacts_for_sensitive_text(case_root)
        assert case_scan["passed"] is False, filename
        assert case_scan["findings"][0]["path"] == filename


def test_sanitize_and_prepared_outbound_package_keep_internal_ids_but_strip_sensitive_text() -> None:
    phone = "199" + "0000" + "9999"
    package = PreparedOutboundPackage(
        text=f"本地演练文本，不应带出 {phone} SECRET_CANARY_M1D2B2 token_abc123 password=123456",
        metadata={
            "decision_id": "ird_17435665458f662a",
            "evidence_id": "evd_17435665458f662a",
            "token": "TOKEN_CANARY_M1D2B2",
            "password": "123456",
        },
        send_actions=(
            {
                "type": "text",
                "metadata": {
                    "decision_id": "ird_17435665458f662a",
                    "evidence_id": "evd_17435665458f662a",
                    "note": f"手机号 {phone} SECRET_CANARY_M1D2B2 token: abc123 password=123456",
                },
            },
        ),
    ).to_dict()
    payload = json.dumps(
        {"package": package, "sanitized": sanitize_for_log({"note": f"token abc123 password=123456 {phone}"})},
        ensure_ascii=False,
    )

    assert "ird_17435665458f662a" in payload
    assert "evd_17435665458f662a" in payload
    assert phone not in payload
    assert "SECRET_CANARY" not in payload
    assert "TOKEN_CANARY" not in payload
    assert "abc123" not in payload
    assert "123456" not in payload
    assert "token abc" not in payload
    assert "token_abc" not in payload
    assert "password=" not in payload


def test_cutover_readiness_report_redacts_secret_scan_findings(tmp_path: Path) -> None:
    root = tmp_path / "cutover-report-redaction"
    build = build_local_snapshot(root, synthetic_inventory_rows(), version="redaction")
    leak_path = root / "snapshots" / build.snapshot.snapshot_id / "leak.json"
    leak_path.write_text(
        json.dumps({"note": "token_abc123 password=123456"}, ensure_ascii=False),
        encoding="utf-8",
    )

    report = evaluate_cutover_readiness(root)
    payload = json.dumps(report, ensure_ascii=False)

    assert report["ready"] is False
    assert "public_artifact_secret_scan_failed" in report["not_ready_reasons"]
    assert "token_abc123" not in payload
    assert "password=123456" not in payload
    assert "abc123" not in payload
    assert "123456" not in payload


def test_concurrent_primary_replays_are_case_isolated_and_do_not_write_repo_data(tmp_path: Path) -> None:
    before = set(Path("data").glob("*")) if Path("data").exists() else set()
    roots = [tmp_path / "case-a", tmp_path / "case-b"]
    versions = ["v1", "v2"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(
                lambda item: run_primary_replay(item[0], synthetic_inventory_rows(version=item[1])),
                zip(roots, versions),
            )
        )
    after = set(Path("data").glob("*")) if Path("data").exists() else set()

    assert all(report["ok"] for report in reports)
    assert reports[0]["source_hash"] != reports[1]["source_hash"]
    assert reports[0]["snapshot_id"] != reports[1]["snapshot_id"]
    assert all(str(roots[index]) not in json.dumps(reports[index], ensure_ascii=False) for index in range(2))
    assert before == after


def test_local_primary_rollback_rehearsal_and_legacy_removal_report(tmp_path: Path) -> None:
    rollback = rehearse_rollback(tmp_path / "rollback")
    removal = legacy_removal_report()

    assert rollback["ok"] is True
    assert rollback["before_snapshot_id"] == rollback["from_snapshot_id"]
    assert rollback["after_snapshot_id"] == rollback["to_snapshot_id"]
    assert removal["removed_this_milestone"] == []
    assert {item["component"] for item in removal["retained"]} >= {
        "InventoryService",
        "legacy CSV/rewrite index/PNG",
        "LegacyInventoryReadProvider",
    }


def test_primary_replay_uses_snapshot_source_only_for_primary_cases(tmp_path: Path) -> None:
    root = tmp_path / "source-kind"
    build = build_local_snapshot(root, synthetic_inventory_rows(), version="source-kind")
    primary = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
        readiness_state=ready_readiness_state(build.snapshot),
    ).start_turn(request_id="source-kind", turn_id="turn-1")
    legacy = InventoryReadRouter(
        mode=READ_MODE_DISABLED,
        legacy_provider=legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
    ).start_turn(request_id="source-kind-legacy", turn_id="turn-1")

    primary_rows = run(primary.search_inventory("晨星花园", limit=2))
    legacy_rows = run(legacy.search_inventory("晨星花园", limit=2))

    assert primary.context.source_kind == SOURCE_KIND_SNAPSHOT
    assert legacy.context.source_kind == SOURCE_KIND_LEGACY
    assert {item.source_kind for item in primary_rows} == {SOURCE_KIND_SNAPSHOT}
    assert {item.source_kind for item in legacy_rows} == {SOURCE_KIND_LEGACY}
    with pytest.raises(InventoryReadError):
        run(SnapshotInventoryReadProvider(SnapshotReader(root)).search_inventory("晨星花园", legacy.context))
