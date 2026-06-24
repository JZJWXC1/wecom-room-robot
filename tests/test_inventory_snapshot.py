from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import re
import shutil
import stat

import pytest

from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_models import (
    InventoryListing,
    InventorySourceMetadata,
    generate_listing_id,
    generate_source_hash,
    generate_snapshot_id,
    sanitize_for_log,
)
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_store import SnapshotStore, SnapshotStoreError
from app.services.inventory_snapshot_validator import SnapshotValidator


FIXED_TIME = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
SYNTHETIC_PASSWORD = "012345#"
SECRET_CANARY = "TEST_SECRET_001234#"


def source_metadata() -> InventorySourceMetadata:
    return InventorySourceMetadata(
        source_kind="unit_test_rows",
        source_version="fixture-v1",
        source_modified_at="2026-06-24T10:00:00Z",
        sheet_metadata={"sheet": "synthetic"},
        revision="rev-1",
        range_ref="A1:I9",
    )


def base_rows() -> list[dict[str, object]]:
    return [
        {
            "source_record_id": "rec-001",
            "区域": "拱墅万达 北部软件园 城北万象城",
            "小区": "棠润府",
            "房号": "02-A",
            "户型描述": "一室一厅朝南",
            "户型分类": "一室一厅",
            "押一付一": "2500",
            "押二付一": "2300",
            "看房方式密码": SYNTHETIC_PASSWORD,
            "备注": "水30/月，电1元/度",
            "图片": "1",
            "视频": "有",
        }
    ]


def build_snapshot(rows: list[dict[str, object]] | None = None):
    return SnapshotBuilder().build(rows or base_rows(), source_metadata(), generated_at=FIXED_TIME)


def dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_same_input_same_build_time_generates_same_snapshot_id_and_utc_prefix() -> None:
    snapshot1, report1 = build_snapshot()
    snapshot2, report2 = build_snapshot()

    assert report1.ok
    assert report2.ok
    assert snapshot1.source_hash == snapshot2.source_hash
    assert snapshot1.snapshot_id == snapshot2.snapshot_id
    assert snapshot1.snapshot_id.startswith("20260624T120000Z_")
    assert snapshot1.source_hash[:12] in snapshot1.snapshot_id


def test_source_hash_is_content_identity_snapshot_id_is_build_identity() -> None:
    snapshot1, _ = SnapshotBuilder().build(base_rows(), source_metadata(), generated_at="2026-06-24T12:00:00Z")
    snapshot2, _ = SnapshotBuilder().build(base_rows(), source_metadata(), generated_at="2026-06-24T12:01:00Z")

    assert snapshot1.source_hash == snapshot2.source_hash
    assert snapshot1.snapshot_id != snapshot2.snapshot_id


def test_effective_field_change_changes_source_hash() -> None:
    snapshot1, _ = build_snapshot()
    rows = base_rows()
    rows[0]["押一付一"] = "2600"
    snapshot2, _ = build_snapshot(rows)

    assert snapshot1.source_hash != snapshot2.source_hash
    assert snapshot1.snapshot_id != snapshot2.snapshot_id


def test_password_change_changes_source_hash_and_snapshot_identity() -> None:
    snapshot1, _ = build_snapshot()
    rows = base_rows()
    rows[0]["看房方式密码"] = "654321#"
    snapshot2, _ = build_snapshot(rows)

    assert snapshot1.source_hash != snapshot2.source_hash
    assert snapshot1.snapshot_id != snapshot2.snapshot_id


def test_eol_and_bom_do_not_change_source_hash() -> None:
    left = generate_source_hash({"rows": [{"备注": "\ufeff水费30\r\n电费1"}]})
    right = generate_source_hash({"rows": [{"备注": "水费30\n电费1"}]})

    assert left == right


def test_row_order_is_part_of_source_hash_contract() -> None:
    rows = base_rows()
    second = dict(base_rows()[0])
    second["source_record_id"] = "rec-002"
    second["房号"] = "03-B"
    rows.append(second)

    snapshot1, _ = build_snapshot(rows)
    snapshot2, _ = build_snapshot(list(reversed(rows)))

    assert snapshot1.source_hash != snapshot2.source_hash


def test_generator_version_change_changes_source_hash() -> None:
    snapshot1, _ = SnapshotBuilder(generator_version="inventory_snapshot_core.v1").build(
        base_rows(), source_metadata(), generated_at=FIXED_TIME
    )
    snapshot2, _ = SnapshotBuilder(generator_version="inventory_snapshot_core.v2").build(
        base_rows(), source_metadata(), generated_at=FIXED_TIME
    )

    assert snapshot1.source_hash != snapshot2.source_hash
    assert snapshot2.manifest.generator_version == "inventory_snapshot_core.v2"


def test_snapshot_id_rejects_path_unsafe_attempt() -> None:
    source_hash = generate_source_hash({"rows": base_rows()})

    with pytest.raises(ValueError):
        generate_snapshot_id(source_hash, generated_at=FIXED_TIME, attempt="../bad")


def test_chinese_and_letter_room_no_round_trip_through_store(tmp_path) -> None:
    snapshot, report = build_snapshot()
    pointer = SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    reader = SnapshotReader(tmp_path)
    loaded = reader.get_current_snapshot()

    assert pointer is not None
    assert loaded.ok
    listing = loaded.value.listings[0]
    assert listing.community == "棠润府"
    assert listing.room_no == "02-A"
    assert listing.raw_room_no == "02-A"


def test_leading_zero_password_stays_private_string_and_not_public() -> None:
    snapshot, _ = build_snapshot()
    listing_id = snapshot.listings[0].listing_id

    assert snapshot.private_viewing_secrets[listing_id]["viewing_text"] == SYNTHETIC_PASSWORD
    assert isinstance(snapshot.private_viewing_secrets[listing_id]["viewing_text"], str)
    assert SYNTHETIC_PASSWORD not in dump_json(snapshot.inventory_payload())
    assert SYNTHETIC_PASSWORD not in dump_json(snapshot.rewrite_index)


def test_listing_id_is_stable_and_does_not_include_password() -> None:
    snapshot, _ = build_snapshot()
    listing = snapshot.listings[0]

    assert listing.listing_id == generate_listing_id("棠润府", "02-A")
    assert listing.listing_id == generate_listing_id(" 棠润府 ", "02-a")
    assert SYNTHETIC_PASSWORD not in listing.listing_id


def test_listing_id_collision_is_blocked_by_validator() -> None:
    snapshot, _ = build_snapshot()
    first = snapshot.listings[0]
    snapshot.listings.append(
        InventoryListing(
            listing_id=first.listing_id,
            source_record_id="rec-collision",
            source_row_number=99,
            raw_area="",
            area="东新园 杭氧 新天地",
            raw_community="杨乐府",
            community="杨乐府",
            raw_room_no="9-909",
            room_no="9-909",
        )
    )

    result = SnapshotValidator().validate_snapshot(snapshot)

    assert not result.ok
    assert any(issue.code == "duplicate_listing_id" for issue in result.errors)


def test_downfills_area_and_community_only() -> None:
    rows = [
        {"区域": "东新园 杭氧 新天地"},
        {"小区": "杨乐府", "房号": "1-101", "押一付一": "2800"},
        {"房号": "1-102", "押一付一": "", "押二付一": "", "看房方式密码": SYNTHETIC_PASSWORD},
    ]
    snapshot, report = build_snapshot(rows)

    assert report.ok
    assert [listing.room_no for listing in snapshot.listings] == ["1-101", "1-102"]
    assert snapshot.listings[1].area == "东新园 杭氧 新天地"
    assert snapshot.listings[1].community == "杨乐府"
    assert snapshot.listings[1].raw_community == ""
    assert snapshot.listings[1].rent_monthly_pay1 is None


def test_merged_community_does_not_cross_blank_or_area_title() -> None:
    rows = [
        {"区域": "拱墅万达 北部软件园 城北万象城"},
        {"小区": "棠润府", "房号": "1-101", "押一付一": "2500"},
        {},
        {"区域": "东新园 杭氧 新天地"},
        {"房号": "2-202", "押一付一": "2600"},
    ]
    snapshot, report = build_snapshot(rows)

    assert [listing.room_no for listing in snapshot.listings] == ["1-101"]
    assert report.rejected_rows[-1]["reason"] == "missing_community"


def test_filters_promotional_and_area_title_rows() -> None:
    rows = [
        {"区域": "拱墅万达 北部软件园 城北万象城"},
        {"小区": "欢迎咨询，可芝麻信用免押"},
        base_rows()[0],
    ]
    snapshot, report = build_snapshot(rows)
    reasons = {row["reason"] for row in report.filtered_rows}

    assert report.ok
    assert len(snapshot.listings) == 1
    assert {"area_title_row", "promotional_row"} <= reasons


def test_promotional_text_with_room_no_is_not_filtered() -> None:
    rows = [
        {"区域": "拱墅万达 北部软件园 城北万象城"},
        {"小区": "棠润府", "房号": "1-101", "备注": "欢迎咨询免押服务", "押一付一": "2500"},
    ]
    snapshot, report = build_snapshot(rows)

    assert report.ok
    assert len(snapshot.listings) == 1
    assert snapshot.listings[0].room_no == "1-101"


def test_invalid_price_is_rejected_with_report() -> None:
    row = dict(base_rows()[0])
    row["押一付一"] = "价格待确认A"
    rows = [row]
    snapshot, report = build_snapshot(rows)

    assert report.ok
    assert snapshot.listings == []
    assert report.rejected_rows[0]["reason"] == "invalid_monthly_rent"


def test_price_contract_for_decimal_empty_pending_and_negative_values() -> None:
    rows = [
        {"小区": "棠润府", "房号": "1-101", "押一付一": "3900.0"},
        {"小区": "棠润府", "房号": "1-102", "押一付一": ""},
        {"小区": "棠润府", "房号": "1-103", "押一付一": "待定"},
        {"小区": "棠润府", "房号": "1-104", "押一付一": "-100"},
    ]
    snapshot, report = build_snapshot(rows)

    rents = {listing.room_no: listing.rent_monthly_pay1 for listing in snapshot.listings}
    assert rents == {"1-101": 3900, "1-102": None, "1-103": None}
    assert report.rejected_rows[-1]["reason"] == "invalid_monthly_rent"
    assert report.rejected_rows[-1]["details"]["rent_monthly_pay1"] == "negative_or_signed_value"


def test_conflicting_duplicate_blocks_snapshot_publication() -> None:
    duplicate = dict(base_rows()[0])
    duplicate["押一付一"] = "2600"
    duplicate["source_record_id"] = "rec-002"
    rows = [base_rows()[0], duplicate]
    snapshot, report = build_snapshot(rows)

    assert len(snapshot.listings) == 1
    assert not report.ok
    assert report.validation_result.errors[0].code == "duplicate_listing_conflict"
    assert report.duplicate_rows[0]["reason"] == "conflicting_duplicate"


def test_identical_duplicate_is_deduplicated_without_publication_error() -> None:
    duplicate = dict(base_rows()[0])
    duplicate["source_record_id"] = "rec-002"
    rows = [base_rows()[0], duplicate]
    snapshot, report = build_snapshot(rows)

    assert report.ok
    assert len(snapshot.listings) == 1
    assert report.deduplicated_rows[0]["reason"] == "identical_duplicate"
    assert snapshot.listings[0].source_record_ids == ["rec-001", "rec-002"]


def test_missing_community_or_room_goes_to_rejected_rows() -> None:
    rows = [
        {"区域": "万达", "房号": "1-101", "押一付一": "2500"},
        {"区域": "万达", "小区": "棠润府", "押一付一": "2500"},
    ]
    snapshot, report = build_snapshot(rows)
    reasons = [row["reason"] for row in report.rejected_rows]

    assert snapshot.listings == []
    assert "missing_community" in reasons
    assert "missing_room_no" in reasons


def test_rewrite_index_contains_safe_viewing_summary_not_password() -> None:
    snapshot, _ = build_snapshot()
    room_item = snapshot.rewrite_index["room_index"][0]
    payload = dump_json(snapshot.rewrite_index)

    assert room_item["has_password"] is True
    assert room_item["viewing_mode"] == "password_available"
    assert '"viewing_text":' not in payload
    assert SYNTHETIC_PASSWORD not in payload


def test_rewrite_index_does_not_contain_full_viewing_original() -> None:
    row = dict(base_rows()[0])
    row["看房方式密码"] = "提前联系管家后用 012345# 开门"
    snapshot, _ = build_snapshot([row])
    payload = dump_json(snapshot.rewrite_index)

    assert "提前联系管家" not in payload
    assert "012345#" not in payload
    assert snapshot.rewrite_index["room_index"][0]["viewing_mode"] == "password_available"


def test_log_serialization_redacts_sensitive_values() -> None:
    redacted = sanitize_for_log(
        {
            "看房方式密码": SYNTHETIC_PASSWORD,
            "nested": {"token": "unit-test-token"},
            "safe": "中文房源",
        }
    )

    assert redacted["看房方式密码"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["safe"] == "中文房源"


def test_unknown_fields_are_retained_without_sensitive_values() -> None:
    row = dict(base_rows()[0])
    row["朝向"] = "南"
    row["备注"] = f"水电正常，联系 19941091943，门锁密码 {SECRET_CANARY}"
    snapshot, _ = build_snapshot([row])
    public_payload = snapshot.inventory_payload(redact_sensitive=True)

    assert snapshot.listings[0].raw_fields["朝向"] == "南"
    assert "19941091943" not in dump_json(public_payload)
    assert SECRET_CANARY not in dump_json(public_payload)


def test_staging_write_success_commits_snapshot_and_pointer_atomically(tmp_path) -> None:
    snapshot, report = build_snapshot()
    store = SnapshotStore(tmp_path)
    pointer = store.write_snapshot(snapshot, report)

    assert pointer is not None
    assert (tmp_path / "snapshots" / snapshot.snapshot_id / "manifest.json").exists()
    assert (tmp_path / "current_snapshot.json").exists()
    assert not (tmp_path / "tmp" / f"{snapshot.snapshot_id}.tmp").exists()
    assert SnapshotReader(tmp_path).get_current_snapshot().ok


def test_public_manifest_does_not_declare_private_files(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    manifest = json.loads((tmp_path / "snapshots" / snapshot.snapshot_id / "manifest.json").read_text(encoding="utf-8"))

    assert "private_viewing_secrets" not in manifest["files"]
    assert "viewing_secrets.json" not in dump_json(manifest)


def test_public_artifacts_recursive_scan_excludes_secret_canary_and_phone(tmp_path) -> None:
    row = dict(base_rows()[0])
    row["看房方式密码"] = SECRET_CANARY
    row["备注"] = "水电正常，联系 19941091943"
    snapshot, report = build_snapshot([row])
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    snapshot_dir = tmp_path / "snapshots" / snapshot.snapshot_id

    for path in snapshot_dir.rglob("*"):
        if not path.is_file() or "private" in path.relative_to(snapshot_dir).parts:
            continue
        payload = path.read_text(encoding="utf-8-sig")
        assert SECRET_CANARY not in payload
        assert "19941091943" not in payload
    private_payload = (snapshot_dir / "private" / "viewing_secrets.json").read_text(encoding="utf-8")
    assert SECRET_CANARY in private_payload


def test_public_inventory_csv_does_not_contain_secret_canary(tmp_path) -> None:
    row = dict(base_rows()[0])
    row["看房方式密码"] = SECRET_CANARY
    snapshot, report = build_snapshot([row])
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)

    csv_payload = (tmp_path / "snapshots" / snapshot.snapshot_id / "inventory.csv").read_text(encoding="utf-8-sig")

    assert SECRET_CANARY not in csv_payload


def test_repr_exception_and_validation_report_do_not_expose_secret_canary(tmp_path) -> None:
    row = dict(base_rows()[0])
    row["看房方式密码"] = SECRET_CANARY
    duplicate = dict(row)
    duplicate["source_record_id"] = "rec-002"
    duplicate["押一付一"] = "2600"
    snapshot, report = build_snapshot([row, duplicate])

    assert SECRET_CANARY not in repr(snapshot)
    assert SECRET_CANARY not in repr(report)
    assert SECRET_CANARY not in repr(report.validation_result)
    with pytest.raises(SnapshotStoreError) as excinfo:
        SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    assert SECRET_CANARY not in str(excinfo.value)


def test_private_file_permissions_posix_or_explicit_windows_skip(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("Windows 使用 ACL，M1B 不声称 private 明文文件已被 chmod 加密。")
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    private_dir = tmp_path / "snapshots" / snapshot.snapshot_id / "private"
    private_file = private_dir / "viewing_secrets.json"

    assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


def test_staging_write_failure_keeps_previous_pointer(tmp_path) -> None:
    first, first_report = build_snapshot()
    store = SnapshotStore(tmp_path)
    first_pointer = store.write_snapshot(first, first_report)
    rows = base_rows()
    rows[0]["押一付一"] = "2600"
    second, second_report = build_snapshot(rows)

    with pytest.raises(SnapshotStoreError):
        store.write_snapshot(second, second_report, simulate_write_failure_after="inventory_json")

    pointer_data = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))
    assert first_pointer is not None
    assert pointer_data["snapshot_id"] == first.snapshot_id
    assert not (tmp_path / "snapshots" / second.snapshot_id).exists()
    assert not (tmp_path / "tmp" / f"{second.snapshot_id}.tmp").exists()


def test_manifest_write_failure_keeps_previous_pointer(tmp_path) -> None:
    first, first_report = build_snapshot()
    store = SnapshotStore(tmp_path)
    store.write_snapshot(first, first_report)
    rows = base_rows()
    rows[0]["押一付一"] = "2650"
    second, second_report = build_snapshot(rows)

    with pytest.raises(SnapshotStoreError):
        store.write_snapshot(second, second_report, simulate_write_failure_after="manifest")

    pointer_data = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))
    assert pointer_data["snapshot_id"] == first.snapshot_id
    assert not (tmp_path / "snapshots" / second.snapshot_id).exists()


def test_private_secret_write_failure_keeps_previous_pointer_and_no_public_publish(tmp_path) -> None:
    first, first_report = build_snapshot()
    store = SnapshotStore(tmp_path)
    store.write_snapshot(first, first_report)
    rows = base_rows()
    rows[0]["押一付一"] = "2660"
    second, second_report = build_snapshot(rows)

    with pytest.raises(SnapshotStoreError):
        store.write_snapshot(second, second_report, simulate_write_failure_after="private_viewing_secrets")

    pointer_data = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))
    assert pointer_data["snapshot_id"] == first.snapshot_id
    assert not (tmp_path / "snapshots" / second.snapshot_id).exists()
    assert not (tmp_path / "tmp" / f"{second.snapshot_id}.tmp").exists()


def test_current_pointer_replace_failure_keeps_old_pointer(tmp_path) -> None:
    first, first_report = build_snapshot()
    store = SnapshotStore(tmp_path)
    store.write_snapshot(first, first_report)
    old_pointer_bytes = (tmp_path / "current_snapshot.json").read_bytes()
    rows = base_rows()
    rows[0]["押一付一"] = "2700"
    second, second_report = build_snapshot(rows)

    with pytest.raises(SnapshotStoreError):
        store.write_snapshot(second, second_report, simulate_pointer_failure=True)

    pointer_data = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))
    assert (tmp_path / "current_snapshot.json").read_bytes() == old_pointer_bytes
    assert pointer_data["snapshot_id"] == first.snapshot_id
    assert (tmp_path / "snapshots" / second.snapshot_id).exists()


def test_existing_snapshot_directory_is_not_overwritten(tmp_path) -> None:
    snapshot, report = build_snapshot()
    store = SnapshotStore(tmp_path)
    store.write_snapshot(snapshot, report)
    inventory_path = tmp_path / "snapshots" / snapshot.snapshot_id / "inventory.json"
    original_bytes = inventory_path.read_bytes()

    with pytest.raises(SnapshotStoreError):
        store.write_snapshot(snapshot, report)

    assert inventory_path.read_bytes() == original_bytes


def test_current_pointer_to_missing_snapshot_returns_structured_error(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    shutil.rmtree(tmp_path / "snapshots" / snapshot.snapshot_id)

    result = SnapshotReader(tmp_path).get_current_pointer()

    assert not result.ok
    assert result.status == "corrupt"
    assert result.code == "current_pointer_invalid"
    assert result.issues[0].code == "pointer_snapshot_missing"


def test_pointer_path_traversal_is_rejected(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    pointer = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))
    pointer["snapshot_path"] = f"snapshots/{snapshot.snapshot_id}/../evil"
    (tmp_path / "current_snapshot.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")

    result = SnapshotReader(tmp_path).get_current_pointer()

    assert not result.ok
    assert any(issue.code in {"pointer_path_unsafe", "pointer_path_snapshot_id_mismatch"} for issue in result.issues)


def test_reader_rejects_snapshot_id_path_traversal(tmp_path) -> None:
    result = SnapshotReader(tmp_path).get_snapshot("../20260624T120000Z_abcdefabcdef")

    assert not result.ok
    assert result.code == "invalid_snapshot_id"


def test_manifest_listing_count_matches_inventory() -> None:
    snapshot, _ = build_snapshot()
    result = SnapshotValidator().validate_snapshot(snapshot)

    assert result.ok
    assert snapshot.manifest.listing_count == len(snapshot.listings)
    assert snapshot.manifest.valid_listing_count == len(snapshot.listings)


def test_manifest_artifact_path_traversal_is_rejected(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    manifest_path = tmp_path / "snapshots" / snapshot.snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["inventory_csv"]["path"] = "../inventory.csv"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    result = SnapshotReader(tmp_path).get_snapshot(snapshot.snapshot_id)

    assert not result.ok
    assert any(issue.code == "manifest_file_path_unsafe" for issue in result.issues)


def test_paths_are_posix_relative_for_windows_and_linux(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    manifest = json.loads((tmp_path / "snapshots" / snapshot.snapshot_id / "manifest.json").read_text(encoding="utf-8"))
    pointer = json.loads((tmp_path / "current_snapshot.json").read_text(encoding="utf-8"))

    assert pointer["snapshot_path"] == f"snapshots/{snapshot.snapshot_id}"
    assert "\\" not in pointer["snapshot_path"]
    for entry in manifest["files"].values():
        assert "\\" not in entry["path"]


def test_artifacts_do_not_contain_hardcoded_absolute_paths(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    snapshot_dir = tmp_path / "snapshots" / snapshot.snapshot_id

    for path in snapshot_dir.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert not re.search(r"[A-Za-z]:\\\\", text)


def test_utf8_chinese_roundtrip_in_json_files(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    text = (tmp_path / "snapshots" / snapshot.snapshot_id / "inventory.json").read_text(encoding="utf-8")
    loaded = json.loads(text)

    assert "棠润府" in text
    assert loaded["listings"][0]["remark"] == "水30/月，电1元/度"


def test_reader_without_current_pointer_does_not_fallback_to_legacy_files(tmp_path) -> None:
    (tmp_path / "inventory_cache.csv").write_text("小区,房号\n棠润府,02-A\n", encoding="utf-8")

    result = SnapshotReader(tmp_path).get_current_snapshot()

    assert not result.ok
    assert result.status == "missing"
    assert result.code == "current_pointer_missing"


def test_snapshot_file_integrity_detects_modified_inventory_json(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    inventory_path = tmp_path / "snapshots" / snapshot.snapshot_id / "inventory.json"
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    payload["listing_count"] = 999
    inventory_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = SnapshotReader(tmp_path).get_snapshot(snapshot.snapshot_id)

    assert not result.ok
    assert result.status == "invalid"
    assert any(issue.code == "snapshot_file_hash_mismatch" for issue in result.issues)


def test_missing_manifest_file_makes_reader_and_health_invalid(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    (tmp_path / "snapshots" / snapshot.snapshot_id / "inventory.csv").unlink()

    result = SnapshotReader(tmp_path).get_snapshot(snapshot.snapshot_id)
    health = SnapshotReader(tmp_path).health()

    assert not result.ok
    assert result.status == "invalid"
    assert health.status == "corrupt"
    assert any(issue.code == "snapshot_file_missing" for issue in result.issues)


def test_private_file_integrity_detects_modified_secret_file(tmp_path) -> None:
    snapshot, report = build_snapshot()
    SnapshotStore(tmp_path).write_snapshot(snapshot, report)
    private_path = tmp_path / "snapshots" / snapshot.snapshot_id / "private" / "viewing_secrets.json"
    payload = json.loads(private_path.read_text(encoding="utf-8"))
    payload[next(iter(payload))]["viewing_text"] = "999999#"
    private_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = SnapshotReader(tmp_path).get_snapshot(snapshot.snapshot_id)

    assert not result.ok
    assert any(issue.code == "private_snapshot_file_hash_mismatch" for issue in result.issues)


def test_publish_lock_conflict_returns_explicit_error(tmp_path) -> None:
    snapshot, report = build_snapshot()
    lock_path = tmp_path / "locks" / "sync.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("held", encoding="utf-8")

    with pytest.raises(SnapshotStoreError, match="lock already held"):
        SnapshotStore(tmp_path).write_snapshot(snapshot, report)

    assert not (tmp_path / "current_snapshot.json").exists()


def test_validator_rejects_secret_fields_in_public_rewrite_index() -> None:
    snapshot, _ = build_snapshot()
    snapshot.rewrite_index["room_index"][0]["password"] = SYNTHETIC_PASSWORD
    result = SnapshotValidator().validate_snapshot(snapshot)

    assert not result.ok
    assert any(issue.code in {"public_payload_password_key", "public_payload_contains_password"} for issue in result.errors)
