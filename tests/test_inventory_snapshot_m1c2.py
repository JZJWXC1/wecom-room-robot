from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from app.services.inventory_legacy_parser import spreadsheet_values_to_inventory_rows
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_legacy_adapter import LegacyInventoryToSnapshotAdapter
from app.services.inventory_snapshot_models import InventorySourceMetadata, now_utc_iso
from app.services.inventory_snapshot_offline import (
    InventorySnapshotOfflineComparisonRunner,
    scan_safe_artifacts_for_canaries,
)
from app.services.inventory_snapshot_reconciliation import reconcile_inventory_snapshot
from app.services.inventory_snapshot_shadow import (
    InventorySnapshotShadowCoordinator,
    get_inventory_snapshot_shadow_health,
)
from app.services.rewrite_inventory_index import build_rewrite_inventory_index


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "inventory_snapshot" / "offline_shadow_fixture.json"


def load_fixture() -> dict[str, list[list[Any]]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def metadata(version: str) -> InventorySourceMetadata:
    return InventorySourceMetadata(
        source_kind="m1c2_unit_test",
        source_version=version,
        extra={"offline": True},
    )


def m1c2_rows(*, password: str = "0007#", price: str = "3200", image: str = "有", video: str = "有") -> list[dict[str, Any]]:
    return [
        {
            "区域": "虚构万达板块",
            "小区": "晨星花园",
            "房号": "15-2-801B",
            "户型描述": "朝南一室，采光好",
            "户型分类": "一室",
            "押一付一": price,
            "押二付一": "3000",
            "看房方式密码": password,
            "备注": "民用水电，电费1元/度",
            "图片": image,
            "视频": video,
        }
    ]


def build_snapshot_from_rows(rows: list[dict[str, Any]]):
    adapted = LegacyInventoryToSnapshotAdapter().adapt_many(rows)
    snapshot, report = SnapshotBuilder().build(adapted, metadata("snapshot"), generated_at="2026-06-25T00:00:00Z")
    assert report.ok
    return snapshot, adapted


def report_json(report: Any) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)


def test_offline_runner_builds_shadow_health_and_safe_artifacts_without_cutover(tmp_path: Path) -> None:
    values = load_fixture()["success_values"]
    result = InventorySnapshotOfflineComparisonRunner().run(
        values=values,
        artifact_root=tmp_path / "m1c2-offline",
        fixture_name="m1c2_success",
        sync_run_id="m1c2-success-001",
    )

    assert result.ok is True
    assert result.legacy_row_count == 3
    assert result.shadow_result["status"] == "completed"
    assert result.shadow_result["mode"] == "shadow"
    assert result.shadow_result["reconciliation_passed"] is True
    assert result.shadow_result["blocking_count"] == 0
    assert result.health["ready_for_cutover_evaluation"] is True
    assert result.health["consecutive_passes"] == 1
    assert result.health["public_artifact_secret_scan_passed"] is True
    assert result.artifact_scan_passed is True

    shadow_root = result.artifact_root / "shadow"
    assert (shadow_root / "shadow_current_snapshot.json").exists()
    assert not (shadow_root / "current_snapshot.json").exists()
    assert (result.artifact_root / result.paths["legacy_summary"]).exists()
    assert (result.artifact_root / result.paths["snapshot_summary"]).exists()
    assert (result.artifact_root / result.paths["shadow_health"]).exists()
    assert (result.artifact_root / result.paths["execution_summary"]).exists()

    public_text = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in result.artifact_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".csv", ".txt", ".md"}
    )
    assert "PHONE_CANARY" not in public_text
    assert "SECRET_CANARY" not in public_text
    assert "19900009999" not in public_text
    assert "C:\\Users" not in public_text


def test_fixture_parser_preserves_real_table_shapes_and_blocks_cross_area_inheritance() -> None:
    fixture = load_fixture()
    success_rows = spreadsheet_values_to_inventory_rows(fixture["success_values"])
    edge_rows = spreadsheet_values_to_inventory_rows(fixture["edge_case_values"])

    assert len(success_rows) == 3
    assert success_rows[0]["区域"] == "虚构万达板块"
    assert success_rows[1]["小区"] == "晨星花园"
    assert success_rows[1]["房号"] == "3-1002B"
    assert success_rows[1]["视频"] == "无"
    assert success_rows[2]["区域"] == "虚构东站板块"
    assert success_rows[2]["房号"] == "T3-1540"
    assert success_rows[2]["押一付一"] == ""
    assert success_rows[2]["看房方式密码"].startswith("0008#")

    assert len(edge_rows) == 4
    assert edge_rows[-1]["区域"] == "虚构南站板块"
    assert edge_rows[-1].get("小区", "") == ""
    assert "PHONE_CANARY" not in json.dumps(edge_rows, ensure_ascii=False)
    assert "SECRET_CANARY" not in json.dumps(edge_rows, ensure_ascii=False)

    snapshot, report = SnapshotBuilder().build(success_rows, metadata("fixture-shape"))
    assert report.ok is True
    assert [listing.room_no for listing in snapshot.listings] == ["15-2-801B", "3-1002B", "T3-1540"]
    private_payload = json.dumps(snapshot.private_viewing_secrets, ensure_ascii=False)
    assert "0007#" in private_payload
    assert "0012#" in private_payload
    assert "0008#" in private_payload
    assert "0007#" not in json.dumps(snapshot.inventory_payload(), ensure_ascii=False)
    assert snapshot.listings[1].has_video is False
    assert snapshot.listings[2].has_image is False
    assert snapshot.listings[2].has_video is True


def test_edge_fixture_reports_invalid_price_duplicate_and_missing_community() -> None:
    rows = spreadsheet_values_to_inventory_rows(load_fixture()["edge_case_values"])
    snapshot, report = SnapshotBuilder().build(rows, metadata("edge-cases"))
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert len(snapshot.listings) == 1
    assert report.ok is True
    assert report.deduplicated_rows[0]["reason"] == "identical_duplicate"
    assert {item["reason"] for item in report.rejected_rows} == {"invalid_monthly_rent", "missing_community"}
    assert "PHONE_CANARY" not in payload
    assert "SECRET_CANARY" not in payload
    assert "19900009999" not in payload


def test_reconciliation_compares_blocking_warning_media_and_password_without_secret_values() -> None:
    snapshot, adapted_snapshot_rows = build_snapshot_from_rows(
        m1c2_rows(password="0012#", price="3300", image="无", video="无")
    )
    legacy_rows = LegacyInventoryToSnapshotAdapter().adapt_many(
        [
            {
                **m1c2_rows(password="0007#", price="3200", image="有", video="有")[0],
                "备注": "水电按表收费；网络自理",
            }
        ]
    )
    legacy_index = build_rewrite_inventory_index(legacy_rows, cache_meta={"hash": "legacy"})

    report = reconcile_inventory_snapshot(
        legacy_rows=legacy_rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    text = report_json(report)

    assert adapted_snapshot_rows[0]["看房方式密码"] == "0012#"
    assert report.passed is False
    assert report.severity_counts["blocking"] >= 2
    assert any(item["code"] == "field_mismatch.rent_pay1" for item in report.field_mismatches)
    assert any(item["code"] == "field_mismatch.password_match" for item in report.field_mismatches)
    assert any(item["code"] == "field_mismatch.utility_summary" for item in report.field_mismatches)
    assert any(item["code"] == "field_mismatch.has_image" for item in report.field_mismatches)
    assert any(item["code"] == "field_mismatch.has_video" for item in report.field_mismatches)
    assert not any(item["code"] == "rewrite_index_sensitive_field_present" for item in report.rewrite_index_mismatches)
    assert all("source_row_ref" in item["listing"] for item in report.field_mismatches if "listing" in item)
    assert "0007#" not in text
    assert "0012#" not in text


def test_duplicate_sync_run_and_same_source_hash_do_not_advance_readiness_gate(tmp_path: Path) -> None:
    runner = InventorySnapshotOfflineComparisonRunner()
    artifact_root = tmp_path / "m1c2-duplicate"
    values = load_fixture()["success_values"]

    first = runner.run(
        values=values,
        artifact_root=artifact_root,
        fixture_name="same-source",
        sync_run_id="sync-run-001",
    )
    duplicate = runner.run(
        values=values,
        artifact_root=artifact_root,
        fixture_name="same-source",
        sync_run_id="sync-run-001",
    )
    repeated_source = runner.run(
        values=values,
        artifact_root=artifact_root,
        fixture_name="same-source",
        sync_run_id="sync-run-002",
    )

    assert first.shadow_result["status"] == "completed"
    assert duplicate.shadow_result["status"] == "duplicate_skipped"
    assert repeated_source.shadow_result["status"] == "completed"
    assert first.shadow_result["source_hash"] == repeated_source.shadow_result["source_hash"]
    assert first.health["consecutive_passes"] == 1
    assert duplicate.health["consecutive_passes"] == 1
    assert repeated_source.health["consecutive_passes"] == 1
    assert len(list((artifact_root / "shadow" / "reports").glob("*_reconciliation.json"))) == 2

    strict_health = get_inventory_snapshot_shadow_health(
        root=artifact_root / "shadow",
        mode="shadow",
        required_consecutive_passes=2,
    ).to_dict()
    assert strict_health["ready_for_cutover_evaluation"] is False
    assert "insufficient_consecutive_passes" in strict_health["not_ready_reasons"]


def test_shadow_health_requires_distinct_successes_and_resets_after_blocking(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    first_rows = m1c2_rows(price="3200")
    second_rows = m1c2_rows(price="3250")

    first = InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
        legacy_rows=first_rows,
        source_metadata=metadata("v1"),
        legacy_rewrite_index=build_rewrite_inventory_index(first_rows),
        sync_run_id="distinct-1",
    )
    assert first["ok"] is True
    health_after_one = get_inventory_snapshot_shadow_health(
        root=root,
        mode="shadow",
        required_consecutive_passes=2,
    ).to_dict()
    assert health_after_one["consecutive_passes"] == 1
    assert health_after_one["ready_for_cutover_evaluation"] is False

    second = InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
        legacy_rows=second_rows,
        source_metadata=metadata("v2"),
        legacy_rewrite_index=build_rewrite_inventory_index(second_rows),
        sync_run_id="distinct-2",
    )
    assert second["ok"] is True
    health_after_two = get_inventory_snapshot_shadow_health(
        root=root,
        mode="shadow",
        required_consecutive_passes=2,
    ).to_dict()
    assert health_after_two["consecutive_passes"] == 2
    assert health_after_two["ready_for_cutover_evaluation"] is True

    class MismatchBuilder(SnapshotBuilder):
        def build(self, rows_arg, source_metadata_arg, **kwargs):  # type: ignore[override]
            return super().build(
                LegacyInventoryToSnapshotAdapter().adapt_many(m1c2_rows(price="3999")),
                source_metadata_arg,
                **kwargs,
            )

    blocking = InventorySnapshotShadowCoordinator(
        mode="shadow",
        root=root,
        builder_factory=MismatchBuilder,
    ).run(
        legacy_rows=m1c2_rows(price="3200"),
        source_metadata=metadata("blocking"),
        legacy_rewrite_index=build_rewrite_inventory_index(m1c2_rows(price="3200")),
        sync_run_id="distinct-blocking",
    )
    health_after_blocking = get_inventory_snapshot_shadow_health(root=root, mode="shadow").to_dict()
    assert blocking["ok"] is True
    assert blocking["reconciliation_passed"] is False
    assert health_after_blocking["consecutive_passes"] == 0
    assert health_after_blocking["consecutive_failures"] == 1
    assert "blocking_mismatches_present" in health_after_blocking["not_ready_reasons"]

    recovery_rows = m1c2_rows(price="3400")
    InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
        legacy_rows=recovery_rows,
        source_metadata=metadata("recovery"),
        legacy_rewrite_index=build_rewrite_inventory_index(recovery_rows),
        sync_run_id="distinct-recovery",
    )
    health_after_recovery = get_inventory_snapshot_shadow_health(
        root=root,
        mode="shadow",
        required_consecutive_passes=1,
    ).to_dict()
    assert health_after_recovery["consecutive_passes"] == 1
    assert health_after_recovery["consecutive_failures"] == 0
    assert health_after_recovery["ready_for_cutover_evaluation"] is True


def test_shadow_health_secret_scan_ignores_phone_like_sha256_digest(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    snapshot_id = "20260625T000000Z_abcdef123456"
    snapshot_dir = root / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True)
    phone_like_digest = "a19900009999b" + ("0" * 51)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "inventory_snapshot.v1",
                "snapshot_id": snapshot_id,
                "files": {
                    "inventory_json": {
                        "path": "inventory.json",
                        "sha256": phone_like_digest,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    current_time = now_utc_iso()
    (root / "shadow_status.json").write_text(
        json.dumps(
            {
                "last_snapshot_id": snapshot_id,
                "last_attempt_at": current_time,
                "last_success_at": current_time,
                "last_reconciliation_passed": True,
                "last_blocking_count": 0,
                "last_warning_count": 0,
                "consecutive_passes": 1,
                "consecutive_failures": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    health = get_inventory_snapshot_shadow_health(
        root=root,
        mode="shadow",
        required_consecutive_passes=1,
    ).to_dict()
    scan_passed, issues = scan_safe_artifacts_for_canaries(root)

    assert health["public_artifact_secret_scan_passed"] is True
    assert health["ready_for_cutover_evaluation"] is True
    assert scan_passed is True
    assert issues == []


def test_shadow_health_secret_scan_still_blocks_public_phone_text(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    snapshot_id = "20260625T000000Z_abcdef654321"
    snapshot_dir = root / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True)
    phone = "199" + "0000" + "9999"
    (snapshot_dir / "inventory.json").write_text(
        json.dumps({"remark": f"联系 {phone}"}, ensure_ascii=False),
        encoding="utf-8",
    )
    current_time = now_utc_iso()
    (root / "shadow_status.json").write_text(
        json.dumps(
            {
                "last_snapshot_id": snapshot_id,
                "last_attempt_at": current_time,
                "last_success_at": current_time,
                "last_reconciliation_passed": True,
                "last_blocking_count": 0,
                "last_warning_count": 0,
                "consecutive_passes": 1,
                "consecutive_failures": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    health = get_inventory_snapshot_shadow_health(
        root=root,
        mode="shadow",
        required_consecutive_passes=1,
    ).to_dict()
    scan_passed, issues = scan_safe_artifacts_for_canaries(root)

    assert health["ready_for_cutover_evaluation"] is False
    assert health["public_artifact_secret_scan_passed"] is False
    assert "public_artifact_secret_scan_failed" in health["not_ready_reasons"]
    assert scan_passed is False
    assert issues == ["snapshots/20260625T000000Z_abcdef654321/inventory.json"]


def test_health_handles_disabled_never_run_stale_corrupt_and_safe_output(tmp_path: Path) -> None:
    disabled = get_inventory_snapshot_shadow_health(root=tmp_path / "disabled", mode="disabled").to_dict()
    never_run = get_inventory_snapshot_shadow_health(root=tmp_path / "never", mode="shadow").to_dict()

    assert disabled["status"] == "disabled"
    assert disabled["ready_for_cutover_evaluation"] is False
    assert never_run["status"] == "never_run"
    assert never_run["stale_reason"] == "no_shadow_status"

    root = tmp_path / "shadow"
    rows = m1c2_rows()
    InventorySnapshotShadowCoordinator(mode="shadow", root=root).run(
        legacy_rows=rows,
        source_metadata=metadata("stale"),
        legacy_rewrite_index=build_rewrite_inventory_index(rows),
        sync_run_id="stale-1",
    )
    status_path = root / "shadow_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["last_attempt_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

    stale = get_inventory_snapshot_shadow_health(root=root, mode="shadow", stale_seconds=60).to_dict()
    assert stale["status"] == "stale"
    assert stale["stale_reason"] == "last_attempt_stale"
    assert "last_attempt_stale" in stale["not_ready_reasons"]

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    (corrupt_root / "shadow_status.json").write_text("{not-json", encoding="utf-8")
    corrupt = get_inventory_snapshot_shadow_health(root=corrupt_root, mode="shadow").to_dict()
    assert corrupt["status"] == "error"
    assert "shadow_status_unreadable" in corrupt["not_ready_reasons"]

    payload = json.dumps({**disabled, **never_run, **stale, **corrupt}, ensure_ascii=False)
    assert "PHONE_CANARY" not in payload
    assert "SECRET_CANARY" not in payload
    assert "C:\\Users" not in payload


def test_admin_refresh_invokes_shadow_once_only_after_legacy_index_success(monkeypatch, tmp_path: Path) -> None:
    import app.main as main

    rows = m1c2_rows()
    calls: dict[str, int] = {"refresh": 0, "index": 0, "shadow": 0}

    class FakeFrame:
        columns = ["小区", "房号"]

        def fillna(self, value: str) -> "FakeFrame":
            assert value == ""
            return self

        def to_dict(self, orient: str) -> list[dict[str, Any]]:
            assert orient == "records"
            return rows

        def __len__(self) -> int:
            return len(rows)

    async def fake_refresh() -> FakeFrame:
        calls["refresh"] += 1
        return FakeFrame()

    def fake_index(index_rows: list[dict[str, Any]]) -> dict[str, Any]:
        calls["index"] += 1
        assert index_rows is rows
        return {"ok": True, "signature": "legacy-signature"}

    def fake_shadow(**kwargs: Any) -> dict[str, Any]:
        calls["shadow"] += 1
        assert kwargs["legacy_rows"] is rows
        assert kwargs["source_kind"] == "admin_inventory_refresh"
        assert kwargs["sync_run_id"].startswith("admin_inventory_refresh:")
        return {"ok": True, "mode": "shadow", "status": "completed"}

    monkeypatch.setattr(main.inventory, "refresh", fake_refresh)
    monkeypatch.setattr(main.inventory, "_cache_meta", {"hash": "cache-hash"})
    monkeypatch.setattr(main, "_write_rewrite_inventory_index", fake_index)
    monkeypatch.setattr(main, "run_inventory_snapshot_shadow", fake_shadow)
    monkeypatch.setattr(main.settings, "rewrite_inventory_index_path", tmp_path / "rewrite.json")

    result = asyncio.run(main._refresh_inventory())

    assert result["ok"] is True
    assert calls == {"refresh": 1, "index": 1, "shadow": 1}


def test_customer_message_module_contains_no_shadow_call_outside_admin_refresh() -> None:
    import app.main as main

    source = Path(main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    call_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_inventory_snapshot_shadow"
    ]
    assert len(call_nodes) == 1

    node: ast.AST = call_nodes[0]
    function_name = ""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_name = node.name
            break
    assert function_name == "_refresh_inventory"


def test_region_inventory_script_passes_unique_sync_run_id_without_second_fetch(monkeypatch, tmp_path: Path) -> None:
    import scripts.sync_feishu_region_inventory as script

    rows = m1c2_rows()
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
        return {"row_count": len(index_rows), "signature": "index-signature"}

    def fake_shadow(**kwargs: Any) -> dict[str, Any]:
        calls["shadow"] += 1
        assert kwargs["legacy_rows"] is rows
        assert kwargs["sync_run_id"].startswith("feishu_region_inventory_sync:")
        return {"ok": True, "mode": "shadow", "status": "completed"}

    monkeypatch.setattr(script, "InventoryService", FakeInventoryService)
    monkeypatch.setattr(script, "write_rewrite_inventory_index", fake_write)
    monkeypatch.setattr(script, "run_inventory_snapshot_shadow", fake_shadow)
    monkeypatch.setattr(script.settings, "rewrite_inventory_index_path", tmp_path / "rewrite.json")

    result = asyncio.run(script.refresh_rewrite_inventory_index())

    assert result["ok"] is True
    assert calls == {"refresh": 1, "shadow": 1}


def test_region_inventory_runtime_artifacts_refreshes_media_manifest_from_same_rows(
    monkeypatch,
) -> None:
    import scripts.sync_feishu_region_inventory as script

    rows = [
        {"小区": "长浜龙吟轩", "房号": "11-1603"},
        {"小区": "新柠长木府", "房号": "3-1002A"},
    ]
    calls: dict[str, Any] = {"refresh": 0, "rewrite_rows": None, "manifest_rows": None}

    class FakeFrame:
        def fillna(self, value: str) -> "FakeFrame":
            return self

        def to_dict(self, orient: str) -> list[dict[str, Any]]:
            assert orient == "records"
            return rows

    class FakeInventoryService:
        cache_meta = {"hash": "cache-hash"}

        async def refresh(self) -> FakeFrame:
            calls["refresh"] += 1
            return FakeFrame()

    def fake_rewrite_payload(*, rows: list[dict[str, Any]], cache_meta: dict[str, Any]) -> dict[str, Any]:
        calls["rewrite_rows"] = rows
        return {"ok": True, "row_count": len(rows), "signature": cache_meta["hash"]}

    async def fake_refresh_media_manifest(manifest_rows: list[dict[str, Any]]) -> dict[str, Any]:
        calls["manifest_rows"] = manifest_rows
        listing_ids, labels = script.media_manifest_context_for_rows(manifest_rows)
        return {
            "ok": True,
            "listing_id_count": len(listing_ids),
            "labels": labels,
        }

    monkeypatch.setattr(script, "InventoryService", FakeInventoryService)
    monkeypatch.setattr(script, "build_rewrite_inventory_payload", fake_rewrite_payload)
    monkeypatch.setattr(script, "refresh_media_manifest", fake_refresh_media_manifest)

    result = asyncio.run(script.refresh_runtime_artifacts(sync_media_manifest=True))

    assert result["rewrite_index"]["ok"] is True
    assert result["media_manifest"]["ok"] is True
    assert result["media_manifest"]["listing_id_count"] == 2
    assert calls["refresh"] == 1
    assert calls["rewrite_rows"] is rows
    assert calls["manifest_rows"] is rows


def test_region_inventory_refresh_media_manifest_binds_target_drive_room_label(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import scripts.sync_feishu_region_inventory as script
    from app.services.media_manifest import MediaManifestProductionAdapter

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603"}]
    payloads = {"video-token": b"longham-video"}
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(payloads["video-token"]),
            }
        ],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payloads[file_token])
            return target_path

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", tmp_path / "room_database")
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))
    listing_ids, _labels = script.media_manifest_context_for_rows(rows)
    evidence = MediaManifestProductionAdapter.from_path(
        tmp_path / "room_database" / "media_manifest.json",
        local_root=tmp_path / "room_database",
    ).evidence_for_listing(listing_ids[0])

    assert result["ok"] is True
    assert result["video_count"] == 1
    assert result["report"]["bound_count"] == 1
    assert result["report"]["orphan_count"] == 0
    assert len(evidence) == 1
    assert evidence[0].media_type == "video"
    assert evidence[0].send_ready is True


def test_region_inventory_refresh_media_manifest_marks_expected_missing_video_degraded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import scripts.sync_feishu_region_inventory as script

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603", "视频": "有"}]
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            raise AssertionError("no files should be downloaded")

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", tmp_path / "room_database")
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))

    assert result["generated"] is True
    assert result["ok"] is False
    assert result["ready"] is False
    assert result["status"] == "degraded"
    assert result["blocking_count"] == 1
    assert result["report"]["missing_count"] == 1
    assert result["report"]["missing_sample"][0]["missing_kinds"] == ["video"]
    assert not (tmp_path / "room_database" / "media_manifest.json").exists()
    assert Path(result["candidate_path"]).is_file()


def test_region_inventory_refresh_media_manifest_keeps_previous_manifest_when_degraded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import hashlib

    import scripts.sync_feishu_region_inventory as script
    from app.services.inventory_snapshot_models import generate_listing_id
    from app.services.media_manifest import MEDIA_KIND_VIDEO, MediaItem, MediaManifest

    room_database = tmp_path / "room_database"
    old_listing_id = generate_listing_id("旧花园", "1-101")
    old_video_path = room_database / "video" / old_listing_id / "old.mp4"
    old_video_payload = b"old-send-ready-video"
    old_video_path.parent.mkdir(parents=True, exist_ok=True)
    old_video_path.write_bytes(old_video_payload)
    old_manifest = MediaManifest.build(
        listing_ids=[old_listing_id],
        items=[
            MediaItem(
                listing_id=old_listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name="old.mp4",
                relative_path=f"video/{old_listing_id}/old.mp4",
                local_path=f"video/{old_listing_id}/old.mp4",
                size=len(old_video_payload),
                sha256=hashlib.sha256(old_video_payload).hexdigest(),
                access_verified=True,
                wecom_sendable=True,
            )
        ],
    )
    manifest_path = room_database / "media_manifest.json"
    old_manifest.write_json(manifest_path)
    before = manifest_path.read_text(encoding="utf-8")

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603", "视频": "有"}]
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            raise AssertionError("no files should be downloaded")

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", room_database)
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert manifest_path.read_text(encoding="utf-8") == before
    assert Path(result["candidate_path"]).is_file()
    assert "11-1603" not in manifest_path.read_text(encoding="utf-8")


def test_region_inventory_missing_kind_publishes_and_quarantines_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # 口径变更(2026-07-05):期望素材缺失不再阻塞整份清单发布,
    # 已绑定素材照常发布,缺失进隔离台账。
    import json

    import scripts.sync_feishu_region_inventory as script
    from app.services.media_manifest import MediaManifestProductionAdapter

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603", "视频": "有", "图片": "有"}]
    payloads = {"video-token": b"candidate-review-video"}
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(payloads["video-token"]),
            }
        ],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payloads[file_token])
            return target_path

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", tmp_path / "room_database")
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))
    listing_ids, _labels = script.media_manifest_context_for_rows(rows)
    evidence = MediaManifestProductionAdapter.from_path(
        tmp_path / "room_database" / "media_manifest.json",
        local_root=tmp_path / "room_database",
    ).evidence_for_listing(listing_ids[0])

    assert result["ok"] is True
    assert result["published"] is True
    assert result["ready"] is False
    assert result["status"] == "published_with_quarantine"
    assert result["blocking_count"] == 0
    assert result["quarantine_count"] == 1
    assert result["report"]["missing_sample"][0]["missing_kinds"] == ["image"]
    assert result["candidate_path"] == ""
    assert len(evidence) == 1
    assert evidence[0].media_type == "video"
    assert Path(evidence[0].local_path).is_file()
    ledger = json.loads(Path(result["quarantine_path"]).read_text(encoding="utf-8"))
    assert ledger["missing_count"] == 1
    assert ledger["missing"][0]["missing_kinds"] == ["image"]


def test_region_inventory_orphan_publishes_with_quarantine_ledger_without_download(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # 口径变更(2026-07-05):历史房源孤儿素材不阻塞发布、不再下载,
    # 明细进 _manual_review/media_manifest_quarantine.json。
    import json

    import scripts.sync_feishu_region_inventory as script
    from app.services.media_manifest import MediaManifestProductionAdapter

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603"}]
    payloads = {"video-token": b"bound-video", "orphan-token": b"stale-room-image"}
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [
            {"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"},
            {"name": "华丰欣苑20-2-802", "type": "folder", "token": "stale-room"},
        ],
        "room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(payloads["video-token"]),
            }
        ],
        "stale-room": [
            {
                "name": "华丰欣苑20-2-802-图片14.jpg",
                "type": "file",
                "token": "orphan-token",
                "size": len(payloads["orphan-token"]),
            }
        ],
    }
    downloaded_tokens: list[str] = []

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            downloaded_tokens.append(file_token)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payloads[file_token])
            return target_path

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", tmp_path / "room_database")
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))
    listing_ids, _labels = script.media_manifest_context_for_rows(rows)
    evidence = MediaManifestProductionAdapter.from_path(
        tmp_path / "room_database" / "media_manifest.json",
        local_root=tmp_path / "room_database",
    ).evidence_for_listing(listing_ids[0])

    assert result["ok"] is True
    assert result["status"] == "published_with_quarantine"
    assert result["blocking_count"] == 0
    assert result["report"]["orphan_count"] == 1
    assert downloaded_tokens == ["video-token"]
    assert len(evidence) == 1
    ledger = json.loads(Path(result["quarantine_path"]).read_text(encoding="utf-8"))
    assert ledger["orphan_count"] == 1
    assert ledger["orphan_items"][0]["source_path"].endswith("华丰欣苑20-2-802-图片14.jpg")
    assert ledger["orphan_items"][0]["reason"] == "missing_listing_id"


def test_region_inventory_quarantined_listing_revives_after_row_added(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # 翰皋名府8-1403 时间线固化:素材先于房源表行到达时先进隔离台账,
    # 行补入后下一轮同步自动重绑定并发布,无需人工搬运。
    import json

    import scripts.sync_feishu_region_inventory as script
    from app.services.media_manifest import MediaManifestProductionAdapter

    payloads = {"video-token": b"anchor-video", "new-image-token": b"new-room-image"}
    tree = {
        "root": [{"name": "闸弄口 新塘 元宝塘 东站", "type": "folder", "token": "area"}],
        "area": [
            {"name": "骏塘名庭8-1101A", "type": "folder", "token": "anchor-room"},
            {"name": "翰皋名府8-1403", "type": "folder", "token": "new-room"},
        ],
        "anchor-room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(payloads["video-token"]),
            }
        ],
        "new-room": [
            {
                "name": "翰皋名府8-1403-图片01.jpg",
                "type": "file",
                "token": "new-image-token",
                "size": len(payloads["new-image-token"]),
            }
        ],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payloads[file_token])
            return target_path

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", tmp_path / "room_database")
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    anchor_row = {"小区": "骏塘名庭", "房号": "8-1101A"}
    new_row = {"小区": "翰皋名府", "房号": "8-1403"}

    first = asyncio.run(script.refresh_media_manifest([anchor_row]))
    assert first["ok"] is True
    assert first["status"] == "published_with_quarantine"
    first_ledger = json.loads(Path(first["quarantine_path"]).read_text(encoding="utf-8"))
    assert first_ledger["orphan_items"][0]["source_path"].endswith("翰皋名府8-1403-图片01.jpg")

    second = asyncio.run(script.refresh_media_manifest([anchor_row, new_row]))
    listing_ids, _labels = script.media_manifest_context_for_rows([anchor_row, new_row])
    new_listing_id = listing_ids[1]
    evidence = MediaManifestProductionAdapter.from_path(
        tmp_path / "room_database" / "media_manifest.json",
        local_root=tmp_path / "room_database",
    ).evidence_for_listing(new_listing_id)

    assert second["ok"] is True
    assert second["status"] == "ready"
    assert second["quarantine_count"] == 0
    assert second["quarantine_path"] == ""
    # 隔离台账随孤儿清零而移除,不留陈旧记录。
    assert not (tmp_path / "room_database" / "_manual_review" / "media_manifest_quarantine.json").exists()
    assert len(evidence) == 1
    assert evidence[0].media_type == "image"
    assert Path(evidence[0].local_path).is_file()


def test_region_inventory_publish_removes_stale_candidate_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # 降级时代遗留的候选清单与候选文件在恢复发布后必须清理,
    # 防止运维把已发布素材误读为仍被隔离。
    import scripts.sync_feishu_region_inventory as script

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603"}]
    payloads = {"video-token": b"publish-video"}
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(payloads["video-token"]),
            }
        ],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payloads[file_token])
            return target_path

    room_database = tmp_path / "room_database"
    manual_review = room_database / "_manual_review"
    stale_candidate = manual_review / "media_manifest_candidate.json"
    stale_files_dir = manual_review / "media_manifest_candidate_files" / "images" / "lst_deadbeefdeadbeef"
    stale_files_dir.mkdir(parents=True, exist_ok=True)
    stale_candidate.write_text("{}", encoding="utf-8")
    (stale_files_dir / "old.jpg").write_bytes(b"stale")

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", room_database)
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")

    result = asyncio.run(script.refresh_media_manifest(rows))

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert (room_database / "media_manifest.json").is_file()
    assert not stale_candidate.exists()
    assert not (manual_review / "media_manifest_candidate_files").exists()


def test_region_inventory_ready_publish_failure_keeps_old_manifest_and_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import hashlib

    import scripts.sync_feishu_region_inventory as script
    from app.services.inventory_snapshot_models import generate_listing_id
    from app.services.media_manifest import (
        MEDIA_KIND_VIDEO,
        MediaItem,
        MediaManifest,
        MediaManifestProductionAdapter,
    )

    room_database = tmp_path / "room_database"
    listing_id = generate_listing_id("长浜龙吟轩", "11-1603")
    relative_path = f"video/{listing_id}/lv_0_20260627144514.mp4"
    old_video_payload = b"old-production-video"
    old_video_path = room_database / Path(relative_path)
    old_video_path.parent.mkdir(parents=True, exist_ok=True)
    old_video_path.write_bytes(old_video_payload)
    old_manifest = MediaManifest.build(
        listing_ids=[listing_id],
        items=[
            MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name="lv_0_20260627144514.mp4",
                relative_path=relative_path,
                local_path=relative_path,
                size=len(old_video_payload),
                sha256=hashlib.sha256(old_video_payload).hexdigest(),
                access_verified=True,
                wecom_sendable=True,
            )
        ],
    )
    manifest_path = room_database / "media_manifest.json"
    old_manifest.write_json(manifest_path)
    before_manifest = manifest_path.read_text(encoding="utf-8")

    rows = [{"小区": "长浜龙吟轩", "房号": "11-1603", "视频": "有"}]
    new_video_payload = b"new-ready-video-with-same-name"
    tree = {
        "root": [{"name": "东新园 杭氧 新天地", "type": "folder", "token": "area"}],
        "area": [{"name": "长浜龙吟轩11-1603", "type": "folder", "token": "room"}],
        "room": [
            {
                "name": "lv_0_20260627144514.mp4",
                "type": "file",
                "token": "video-token",
                "size": len(new_video_payload),
            }
        ],
    }

    class FakeFeishuClient:
        async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
            return list(tree.get(folder_token, []))

        async def download_file(self, file_token: str, target_path: Path) -> Path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(new_video_payload)
            return target_path

    def fail_manifest_write(manifest: Any, path: Path) -> None:
        raise RuntimeError("manifest write failed")

    monkeypatch.setattr(script, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(script.settings, "room_database_path", room_database)
    monkeypatch.setattr(script.settings, "feishu_region_sync_target_drive_folder_token", "root")
    monkeypatch.setattr(script, "_write_manifest_atomically", fail_manifest_write)

    try:
        asyncio.run(script.refresh_media_manifest(rows))
    except RuntimeError as exc:
        assert "manifest write failed" in str(exc)
    else:
        raise AssertionError("manifest write failure should bubble up")

    evidence = MediaManifestProductionAdapter.from_path(
        manifest_path,
        local_root=room_database,
    ).evidence_for_listing(listing_id)
    assert manifest_path.read_text(encoding="utf-8") == before_manifest
    assert old_video_path.read_bytes() == old_video_payload
    assert len(evidence) == 1
    assert Path(evidence[0].local_path).read_bytes() == old_video_payload


def test_region_inventory_media_expectations_accept_aliases_and_counts() -> None:
    import scripts.sync_feishu_region_inventory as script

    rows = [
        {
            "小区": "长浜龙吟轩",
            "房号": "11-1603",
            "房源视频": "2",
            "图片数量": 1,
        },
        {
            "小区": "新柠长木府",
            "房号": "3-1002A",
            "视频数量": "0",
            "房源图片": "无",
        },
    ]

    expected = script.expected_media_kinds_by_listing(rows)

    assert len(expected) == 1
    assert set(next(iter(expected.values()))) == {"image", "video"}


def test_region_inventory_manifest_context_requires_community_and_room() -> None:
    import scripts.sync_feishu_region_inventory as script

    rows = [
        {"小区": "长浜龙吟轩"},
        {"房号": "11-1603"},
        {"小区": "长浜龙吟轩", "房号": "11-1603"},
    ]

    listing_ids, labels = script.media_manifest_context_for_rows(rows)

    assert len(listing_ids) == 1
    assert labels == {listing_ids[0]: "长浜龙吟轩11-1603"}
