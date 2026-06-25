from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from app.config import settings
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_legacy_adapter import LegacyInventoryToSnapshotAdapter
from app.services.inventory_snapshot_models import (
    InventorySourceMetadata,
    SnapshotValidationResult,
)
from app.services.inventory_snapshot_offline import scan_safe_artifacts_for_canaries
from app.services.inventory_snapshot_reconciliation import (
    compare_rewrite_inventory_index,
    load_legacy_rewrite_index,
    reconcile_inventory_snapshot,
)
from app.services.inventory_snapshot_shadow import (
    InventorySnapshotShadowCoordinator,
    build_shadow_source_metadata,
    parse_inventory_snapshot_mode,
)
from app.services.inventory_snapshot_store import SnapshotStoreError
from app.services.inventory_snapshot_validator import SnapshotValidator
from app.services.rewrite_inventory_index import build_rewrite_inventory_index, write_rewrite_inventory_index


def sample_rows(*, password: str = "1234#", price: str = "3200", room_no: str = "02-A") -> list[dict[str, Any]]:
    return [
        {
            "区域": "拱墅万达 北部软件园 城北万象城",
            "小区": "棠润府",
            "房号": room_no,
            "户型描述": "朝南一室一厅",
            "户型分类": "一室",
            "押一付一": price,
            "押二付一": "3000",
            "看房方式密码": password,
            "备注": "民水民电",
            "has_image": "1",
            "has_video": "1",
        }
    ]


def source_metadata(source_version: str = "test-v1") -> InventorySourceMetadata:
    return InventorySourceMetadata(
        source_kind="test_legacy_sync",
        source_version=source_version,
        extra={"test": True},
    )


def build_snapshot(rows: list[dict[str, Any]], *, attempt: int | None = None):
    adapted = LegacyInventoryToSnapshotAdapter().adapt_many(rows)
    snapshot, report = SnapshotBuilder().build(
        adapted,
        source_metadata(),
        generated_at="2026-01-01T00:00:00Z",
        attempt=attempt,
    )
    assert report.ok
    return snapshot, adapted


def write_legacy_index(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "legacy_rewrite_inventory_index.json"
    index = write_rewrite_inventory_index(rows, path=path, cache_meta={"hash": "legacy"})
    return path, index


def rewrite_index_payload(
    communities: list[dict[str, Any]],
    *,
    area_aliases: list[dict[str, str]] | None = None,
    room_index: list[dict[str, Any]] | None = None,
    row_count: int = 1,
) -> dict[str, Any]:
    return {
        "row_count": row_count,
        "areas": [{"name": "虚构区域"}],
        "area_aliases": area_aliases or [],
        "communities": communities,
        "room_index": room_index or [],
        "media_summary": {},
    }


def community_item(
    name: str,
    *,
    normalized: str = "",
    rooms: list[str] | None = None,
    price_range: list[int] | None = None,
    layouts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "normalized": normalized or name,
        "count": len(rooms or ["01"]),
        "area": "虚构区域",
        "rooms": rooms or ["01"],
        "layouts": layouts or {"一室": 1},
        "price_range": price_range or [3000, 3200],
    }


def test_default_mode_is_disabled() -> None:
    assert parse_inventory_snapshot_mode("").value == "disabled"
    assert parse_inventory_snapshot_mode("disabled").value == "disabled"


def test_disabled_mode_does_not_create_shadow_files(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    result = InventorySnapshotShadowCoordinator(mode="disabled", root=root).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata(),
    )

    assert result["status"] == "disabled"
    assert result["enabled"] is False
    assert not root.exists()


def test_illegal_mode_reports_config_error_without_shadow_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    result = InventorySnapshotShadowCoordinator(mode="primary", root=root).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata(),
    )

    assert result["ok"] is False
    assert result["status"] == "config_error"
    assert result["error_code"] == "invalid_inventory_snapshot_mode"
    assert not (root / "snapshots").exists()


def test_shadow_mode_builds_snapshot_report_and_shadow_pointer(tmp_path: Path) -> None:
    rows = sample_rows()
    index_path, index = write_legacy_index(tmp_path, rows)

    result = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("same-source"),
        legacy_rewrite_index_path=index_path,
        legacy_rewrite_index=index,
    )

    root = tmp_path / "shadow"
    assert result["ok"] is True
    assert result["reconciliation_passed"] is True
    assert result["blocking_count"] == 0
    assert (root / "snapshots" / result["snapshot_id"] / "inventory.json").exists()
    assert (root / "reports" / f"{result['snapshot_id']}_reconciliation.json").exists()
    assert (root / "shadow_current_snapshot.json").exists()
    assert not (root / "current_snapshot.json").exists()


def test_reconciliation_blocks_price_mismatch() -> None:
    snapshot, _ = build_snapshot(sample_rows(price="3300"))

    report = reconcile_inventory_snapshot(
        legacy_rows=sample_rows(price="3200"),
        snapshot=snapshot,
    )

    assert report.passed is False
    assert report.severity_counts["blocking"] >= 1
    assert any(item["field"] == "rent_pay1" for item in report.field_mismatches)


def test_reconciliation_blocks_room_mismatch_as_missing_and_extra() -> None:
    snapshot, _ = build_snapshot(sample_rows(room_no="03-A"))

    report = reconcile_inventory_snapshot(
        legacy_rows=sample_rows(room_no="02-A"),
        snapshot=snapshot,
    )

    assert report.passed is False
    assert report.missing_in_snapshot
    assert report.extra_in_snapshot


def test_reconciliation_blocks_missing_and_extra_listing() -> None:
    rows = sample_rows() + [
        {
            **sample_rows()[0],
            "小区": "运河宸园",
            "房号": "05B",
            "看房方式密码": "",
        }
    ]
    snapshot, _ = build_snapshot(rows[:1])

    report = reconcile_inventory_snapshot(legacy_rows=rows, snapshot=snapshot)

    assert report.passed is False
    assert report.missing_in_snapshot
    assert not report.extra_in_snapshot


def test_reconciliation_blocks_duplicate_legacy_and_snapshot_records() -> None:
    rows = sample_rows()
    snapshot, _ = build_snapshot(rows)
    snapshot.listings.append(snapshot.listings[0])

    report = reconcile_inventory_snapshot(legacy_rows=rows + rows, snapshot=snapshot)

    assert report.passed is False
    assert report.duplicate_legacy_records
    assert report.duplicate_snapshot_records


def test_password_match_outputs_boolean_only_and_no_secret_canary(tmp_path: Path) -> None:
    canary = "SECRET_CANARY_123456#"
    rows = sample_rows(password=canary)
    snapshot, _ = build_snapshot(rows)

    report = reconcile_inventory_snapshot(legacy_rows=rows, snapshot=snapshot)
    text = report.to_json()

    assert report.passed is True
    assert "SECRET_CANARY" not in text
    assert canary not in text
    assert "password_match" in text


def test_legacy_rewrite_index_viewing_is_flagged_but_not_copied(tmp_path: Path) -> None:
    canary = "VIEWING_CANARY_654321#"
    rows = sample_rows(password=canary)
    snapshot, _ = build_snapshot(rows)
    _, legacy_index = write_legacy_index(tmp_path, rows)

    report = reconcile_inventory_snapshot(
        legacy_rows=rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    text = report.to_json()

    assert report.legacy_sensitive_field_present is True
    assert "legacy_sensitive_field_present" in text
    assert canary not in text
    assert "VIEWING_CANARY" not in text


def test_rewrite_community_set_ignores_order_after_normalization() -> None:
    left = rewrite_index_payload(
        [
            community_item("晨星花园", normalized="晨星花园", rooms=["01"]),
            community_item("运河宸园", normalized="运河宸园", rooms=["02"]),
        ],
        row_count=2,
    )
    right = rewrite_index_payload(
        [
            community_item("运河宸园", normalized="运河宸园", rooms=["02"]),
            community_item("晨星花园", normalized="晨星花园", rooms=["01"]),
        ],
        row_count=2,
    )

    mismatches, sensitive_present = compare_rewrite_inventory_index(left, right)

    assert sensitive_present is False
    assert not [item for item in mismatches if item["code"] == "rewrite_index_mismatch.communities"]


def test_rewrite_duplicate_community_warns_without_false_missing() -> None:
    legacy = rewrite_index_payload(
        [
            community_item("晨星花园", normalized="晨星花园", rooms=["01"]),
            community_item("晨星 花园", normalized="晨星花园", rooms=["01"]),
        ]
    )
    snapshot = rewrite_index_payload([community_item("晨星花园", normalized="晨星花园", rooms=["01"])])

    mismatches, _ = compare_rewrite_inventory_index(legacy, snapshot)
    codes = [item["code"] for item in mismatches]

    assert "rewrite_index_duplicate_community" in codes
    assert "rewrite_index_mismatch.communities" not in codes
    assert all(item.get("severity") != "blocking" for item in mismatches)


def test_rewrite_community_fullwidth_and_normal_spaces_normalize_equal() -> None:
    legacy = rewrite_index_payload(
        [community_item("晨星　花园", normalized="晨星花园", rooms=["01"])]
    )
    snapshot = rewrite_index_payload(
        [community_item("晨星 花园", normalized="晨星 花园", rooms=["01"])]
    )

    mismatches, _ = compare_rewrite_inventory_index(legacy, snapshot)

    assert not [item for item in mismatches if item["code"] == "rewrite_index_mismatch.communities"]


def test_area_aliases_do_not_enter_rewrite_community_set() -> None:
    legacy = rewrite_index_payload(
        [community_item("石桥铭苑", normalized="石桥铭苑")],
        area_aliases=[{"alias": "石桥", "canonical": "石桥街道 华丰 石桥 永佳 半山"}],
    )
    snapshot = rewrite_index_payload([community_item("石桥铭苑", normalized="石桥铭苑")])

    mismatches, _ = compare_rewrite_inventory_index(legacy, snapshot)

    assert any(item["code"] == "rewrite_index_mismatch.area_aliases" for item in mismatches)
    assert not [item for item in mismatches if item["code"] == "rewrite_index_mismatch.communities"]
    assert all(
        item["severity"] == "warning"
        for item in mismatches
        if item["code"] == "rewrite_index_mismatch.area_aliases"
    )


def test_missing_rewrite_community_remains_blocking() -> None:
    legacy = rewrite_index_payload(
        [
            community_item("晨星花园", normalized="晨星花园", rooms=["01"]),
            community_item("运河宸园", normalized="运河宸园", rooms=["02"]),
        ],
        row_count=2,
    )
    snapshot = rewrite_index_payload(
        [community_item("晨星花园", normalized="晨星花园", rooms=["01"])],
        row_count=2,
    )

    mismatches, _ = compare_rewrite_inventory_index(legacy, snapshot)

    assert any(
        item["code"] == "rewrite_index_mismatch.communities" and item["severity"] == "blocking"
        for item in mismatches
    )


def test_extra_rewrite_community_remains_blocking() -> None:
    legacy = rewrite_index_payload(
        [community_item("晨星花园", normalized="晨星花园", rooms=["01"])],
        row_count=2,
    )
    snapshot = rewrite_index_payload(
        [
            community_item("晨星花园", normalized="晨星花园", rooms=["01"]),
            community_item("运河宸园", normalized="运河宸园", rooms=["02"]),
        ],
        row_count=2,
    )

    mismatches, _ = compare_rewrite_inventory_index(legacy, snapshot)

    assert any(
        item["code"] == "rewrite_index_mismatch.communities" and item["severity"] == "blocking"
        for item in mismatches
    )


def test_current_batch_legacy_index_is_not_replaced_by_historical_path(tmp_path: Path) -> None:
    rows = sample_rows()
    current_index = build_rewrite_inventory_index(rows)
    historical_index = rewrite_index_payload([community_item("历史小区", normalized="历史小区")])
    historical_path = tmp_path / "rewrite_inventory_index.json"
    historical_path.write_text(json.dumps(historical_index, ensure_ascii=False), encoding="utf-8")

    assert load_legacy_rewrite_index(historical_path, current_index) == current_index

    result = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("current-index"),
        legacy_rewrite_index_path=historical_path,
        legacy_rewrite_index=current_index,
        sync_run_id="current-index-001",
    )

    assert result["ok"] is True
    assert result["blocking_count"] == 0


def test_merged_community_continuation_row_is_not_lost_in_snapshot_rewrite_compare() -> None:
    first = sample_rows(room_no="01")[0]
    second = {
        **sample_rows(room_no="02")[0],
        "区域": "",
        "小区": "",
    }
    snapshot, _ = build_snapshot([first, second])
    filled_legacy_rows = [first, {**second, "区域": first["区域"], "小区": first["小区"]}]
    legacy_index = build_rewrite_inventory_index(filled_legacy_rows)

    report = reconcile_inventory_snapshot(
        legacy_rows=filled_legacy_rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    community = snapshot.rewrite_index["communities"][0]

    assert community["name"] == first["小区"]
    assert community["rooms"] == ["01", "02"]
    assert report.severity_counts["blocking"] == 0


def test_area_title_row_is_not_treated_as_rewrite_community() -> None:
    area_title = {
        "区域": "虚构万达板块",
        "小区": "",
        "房号": "",
        "户型描述": "",
        "户型分类": "",
        "押一付一": "",
        "押二付一": "",
        "看房方式密码": "",
        "备注": "",
    }
    listing = {**sample_rows(room_no="01")[0], "区域": "", "小区": "晨星花园"}
    snapshot, _ = build_snapshot([area_title, listing])
    filled_legacy_rows = [{**listing, "区域": "虚构万达板块"}]
    legacy_index = build_rewrite_inventory_index(filled_legacy_rows)

    report = reconcile_inventory_snapshot(
        legacy_rows=filled_legacy_rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    community_names = {item["name"] for item in snapshot.rewrite_index["communities"]}

    assert "虚构万达板块" not in community_names
    assert community_names == {"晨星花园"}
    assert report.severity_counts["blocking"] == 0


def test_rewrite_sensitive_viewing_warning_does_not_leak_value() -> None:
    rows = sample_rows(password="SECRET_CANARY_246810#")
    snapshot, _ = build_snapshot(rows)
    legacy_index = build_rewrite_inventory_index(rows)

    report = reconcile_inventory_snapshot(
        legacy_rows=rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    text = report.to_json()

    assert any(item["code"] == "rewrite_index_sensitive_field_present" for item in report.rewrite_index_mismatches)
    assert "SECRET_CANARY" not in text
    assert "246810#" not in text


def test_shadow_public_artifact_scan_still_passes_after_rewrite_compare(tmp_path: Path) -> None:
    rows = sample_rows()
    result = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("public-scan"),
        legacy_rewrite_index=build_rewrite_inventory_index(rows),
        sync_run_id="public-scan-001",
    )

    scan_passed, issues = scan_safe_artifacts_for_canaries(tmp_path / "shadow")

    assert result["ok"] is True
    assert result["blocking_count"] == 0
    assert scan_passed is True
    assert issues == []


def test_local_m1c3_diagnostics_are_not_tracked_by_git() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "ls-files", ".local/m1c3-diagnostics"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.stdout.strip() == ""


def test_utf8_chinese_and_letter_room_roundtrip_in_shadow_report(tmp_path: Path) -> None:
    rows = sample_rows(room_no="A-08")
    rows[0]["小区"] = "绿城·春月锦庐"

    result = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("utf8"),
    )
    report_path = tmp_path / "shadow" / result["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert report["matched_count"] == 1
    assert report["safe_summary"]["blocking_count"] == 0


def test_same_source_hash_repeated_build_creates_new_identity_not_business_diff(tmp_path: Path) -> None:
    rows = sample_rows()
    first = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("repeat"),
    )
    second = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata("repeat"),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["source_hash"] == second["source_hash"]
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["blocking_count"] == 0


def test_builder_exception_is_structured_and_redacted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class FailingBuilder:
        def build(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom SECRET_CANARY_123456# C:\\Users\\secret\\file.txt")

    with caplog.at_level(logging.WARNING):
        result = InventorySnapshotShadowCoordinator(
            mode="shadow",
            root=tmp_path / "shadow",
            builder_factory=lambda: FailingBuilder(),
        ).run(
            legacy_rows=sample_rows(),
            source_metadata=source_metadata("builder-fail"),
        )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == "runtimeerror"
    assert "SECRET_CANARY" not in result["safe_error_message"]
    assert "123456#" not in result["safe_error_message"]
    assert "C:\\Users" not in result["safe_error_message"]
    assert "SECRET_CANARY" not in caplog.text
    assert "123456#" not in caplog.text


def test_validator_failure_is_structured(tmp_path: Path) -> None:
    class FailingValidator(SnapshotValidator):
        def validate_snapshot(self, snapshot):  # type: ignore[override]
            result = SnapshotValidationResult()
            result.add("error", "forced_validator_failure", "forced")
            return result

    result = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=tmp_path / "shadow",
        validator=FailingValidator(),
    ).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata("validator-fail"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "snapshot_store_error"
    assert "forced_validator_failure" in result["safe_error_message"]


def test_store_write_failure_is_structured(tmp_path: Path) -> None:
    class FailingStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def write_snapshot(self, *args: Any, **kwargs: Any) -> None:
            raise SnapshotStoreError("forced store failure")

    result = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=tmp_path / "shadow",
        store_factory=lambda root, validator: FailingStore(),
    ).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata("store-fail"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "snapshot_store_error"
    assert (tmp_path / "shadow" / "shadow_status.json").exists()


def test_snapshot_store_staging_is_cleaned_after_write_failure(tmp_path: Path) -> None:
    from app.services.inventory_snapshot_store import SnapshotStore

    root = tmp_path / "shadow"

    class FailingAfterStagingStore(SnapshotStore):
        def write_snapshot(self, snapshot, report, **kwargs):  # type: ignore[override]
            return super().write_snapshot(
                snapshot,
                report,
                activate=False,
                simulate_write_failure_after="inventory_json",
            )

    result = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=root,
        store_factory=lambda root_path, validator: FailingAfterStagingStore(root_path, validator=validator),
    ).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata("staging-cleanup"),
    )

    assert result["ok"] is False
    assert not list((root / "tmp").glob("*.tmp"))


def test_reconciliation_blocking_does_not_make_shadow_execution_fail(tmp_path: Path) -> None:
    rows = sample_rows(price="3200")
    snapshot_rows = sample_rows(price="3300")

    class MismatchBuilder(SnapshotBuilder):
        def build(self, rows_arg, source_metadata_arg, **kwargs):  # type: ignore[override]
            adapted = LegacyInventoryToSnapshotAdapter().adapt_many(snapshot_rows)
            return super().build(adapted, source_metadata_arg, **kwargs)

    result = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=tmp_path / "shadow",
        builder_factory=MismatchBuilder,
    ).run(
        legacy_rows=rows,
        source_metadata=source_metadata("reconcile-blocking"),
    )

    assert result["ok"] is True
    assert result["reconciliation_passed"] is False
    assert result["blocking_count"] >= 1


def test_report_write_failure_is_structured_and_status_safe(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    root.mkdir()
    (root / "reports").write_text("not a directory", encoding="utf-8")

    result = InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata("report-fail"),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert (root / "shadow_status.json").exists()


def test_shadow_timeout_returns_without_success_status(tmp_path: Path) -> None:
    class SlowBuilder(SnapshotBuilder):
        def build(self, *args: Any, **kwargs: Any):
            time.sleep(0.05)
            return super().build(*args, **kwargs)

    result = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=tmp_path / "shadow",
        timeout_seconds=0.001,
        builder_factory=SlowBuilder,
    ).run(
        legacy_rows=sample_rows(),
        source_metadata=source_metadata("timeout"),
    )

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["error_code"] == "shadow_timeout"


def test_concurrent_shadow_runs_do_not_cross_overwrite_reports(tmp_path: Path) -> None:
    root = tmp_path / "shadow"

    def run_once() -> dict[str, Any]:
        return InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
            legacy_rows=sample_rows(),
            source_metadata=source_metadata("concurrent"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_once(), range(2)))

    assert any(result["ok"] for result in results)
    report_paths = [result["report_path"] for result in results if result.get("report_path")]
    assert len(report_paths) == len(set(report_paths))
    assert all((root / path).exists() for path in report_paths)


def test_refresh_cache_reuses_same_rows_for_shadow_without_second_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.refresh_rag_inventory_cache as script

    rows = sample_rows()
    calls = {"refresh": 0, "shadow": 0}

    class FakeFrame:
        columns = ["小区", "房号"]

        def fillna(self, value: str) -> "FakeFrame":
            return self

        def to_dict(self, orient: str) -> list[dict[str, Any]]:
            assert orient == "records"
            return rows

        def __len__(self) -> int:
            return len(rows)

    class FakeInventoryService:
        cache_meta = {"hash": "cache-hash"}

        async def refresh(self) -> FakeFrame:
            calls["refresh"] += 1
            return FakeFrame()

    def fake_write(index_rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        assert index_rows is rows
        return {"row_count": len(index_rows), "signature": "legacy-index-signature"}

    def fake_shadow(**kwargs: Any) -> dict[str, Any]:
        calls["shadow"] += 1
        assert kwargs["legacy_rows"] is rows
        return {"ok": True, "mode": "shadow", "status": "completed"}

    monkeypatch.setattr(script, "InventoryService", FakeInventoryService)
    monkeypatch.setattr(script, "write_rewrite_inventory_index", fake_write)
    monkeypatch.setattr(script, "run_inventory_snapshot_shadow", fake_shadow)
    monkeypatch.setattr(script.settings, "inventory_cache_path", tmp_path / "cache.csv")
    monkeypatch.setattr(script.settings, "rewrite_inventory_index_path", tmp_path / "index.json")

    result = asyncio.run(script.refresh_cache())

    assert result["ok"] is True
    assert calls == {"refresh": 1, "shadow": 1}


def test_legacy_success_with_shadow_failure_still_returns_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.refresh_rag_inventory_cache as script

    rows = sample_rows()

    class FakeFrame:
        columns = ["小区", "房号"]

        def fillna(self, value: str) -> "FakeFrame":
            return self

        def to_dict(self, orient: str) -> list[dict[str, Any]]:
            return rows

        def __len__(self) -> int:
            return len(rows)

    class FakeInventoryService:
        cache_meta = {"hash": "cache-hash"}

        async def refresh(self) -> FakeFrame:
            return FakeFrame()

    monkeypatch.setattr(script, "InventoryService", FakeInventoryService)
    monkeypatch.setattr(
        script,
        "write_rewrite_inventory_index",
        lambda index_rows, **kwargs: {"row_count": len(index_rows), "signature": "sig"},
    )
    monkeypatch.setattr(
        script,
        "run_inventory_snapshot_shadow",
        lambda **kwargs: {"ok": False, "status": "failed", "error_code": "forced"},
    )
    monkeypatch.setattr(script.settings, "inventory_cache_path", tmp_path / "cache.csv")
    monkeypatch.setattr(script.settings, "rewrite_inventory_index_path", tmp_path / "index.json")

    result = asyncio.run(script.refresh_cache())

    assert result["ok"] is True
    assert result["inventory_snapshot_shadow"]["ok"] is False


def test_old_region_sync_failure_does_not_publish_false_shadow_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.sync_feishu_region_inventory as script

    async def fake_run_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
        return {"ok": False, "reason": "legacy_failed"}

    async def fail_refresh() -> dict[str, Any]:
        raise AssertionError("shadow/index refresh should not run after legacy failure")

    monkeypatch.setattr(script, "run_sync", fake_run_sync)
    monkeypatch.setattr(script, "refresh_rewrite_inventory_index", fail_refresh)
    monkeypatch.setattr(script.settings, "feishu_region_sync_sources", "[{}]")
    monkeypatch.setattr(script.settings, "feishu_region_sync_state_path", tmp_path / "state.json")
    monkeypatch.setattr(script.sys, "argv", ["sync_feishu_region_inventory.py", "--no-lock"])

    exit_code = script.main()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert state["last_run"]["ok"] is False
    assert "inventory_snapshot_shadow" not in state["last_run"]


def test_build_shadow_source_metadata_marks_m1d_removal() -> None:
    metadata = build_shadow_source_metadata(
        source_kind="test",
        source_version="v1",
        cache_meta={"hash": "abc"},
    )

    assert metadata.extra["adapter"] == "LegacyInventoryToSnapshotAdapter"
    assert metadata.extra["adapter_removal_milestone"] == "M1D"
    assert metadata.extra["shadow_mode_removal_milestone"] == "M1D"
