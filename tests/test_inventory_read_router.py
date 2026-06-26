from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shutil
from typing import Any

import pandas as pd
import pytest

from app.services.inventory import InventoryService
from app.services.inventory_read_models import (
    FALLBACK_LEGACY_WHOLE_REQUEST,
    FALLBACK_STRICT,
    READ_MODE_DISABLED,
    READ_MODE_PRIMARY,
    READ_MODE_SHADOW,
    REASON_ALIAS_COVERAGE_FAILED,
    REASON_CONTEXT_PROVIDER_MISMATCH,
    REASON_FALLBACK_NOT_ALLOWED_AFTER_READ,
    REASON_MIXED_SOURCE_HASH,
    REASON_RECONCILIATION_BLOCKING,
    REASON_SECRET_SCAN_FAILED,
    REASON_SNAPSHOT_POINTER_MISSING,
    REASON_SNAPSHOT_READ_FAILED,
    REASON_SNAPSHOT_STALE,
    REASON_UNSUPPORTED_SCHEMA,
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadError,
    assert_evidence_consistency,
)
from app.services.inventory_read_provider import (
    LegacyInventoryReadProvider,
    SnapshotInventoryReadProvider,
)
from app.services.inventory_read_router import InventoryReadRouter
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_models import InventorySourceMetadata, generate_listing_id, now_utc_iso
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_store import SnapshotStore


DONGXIN_AREA = "东新园 杭氧 新天地"
WANDA_AREA = "拱墅万达 北部软件园 城北万象城"
SYNTHETIC_PASSWORD = "0007#"


def inventory_rows() -> list[dict[str, Any]]:
    return [
        {
            "区域": DONGXIN_AREA,
            "小区": "晨星花园",
            "房号": "1-101A",
            "户型描述": "朝南一室，民用水电",
            "户型分类": "一室",
            "押一付一": "1800",
            "押二付一": "1700",
            "看房方式密码": SYNTHETIC_PASSWORD,
            "备注": "民水民电",
            "图片": "有",
            "视频": "有",
        },
        {
            "区域": DONGXIN_AREA,
            "小区": "晨星花园",
            "房号": "1-102",
            "户型描述": "一室一厅带厅",
            "户型分类": "一室一厅",
            "押一付一": "3200",
            "押二付一": "3000",
            "看房方式密码": "提前联系管家",
            "备注": "水30，电1元/度",
            "图片": "有",
            "视频": "无",
        },
        {
            "区域": DONGXIN_AREA,
            "小区": "星河公寓",
            "房号": "2-201",
            "户型描述": "两室一厅，中文，全角符号",
            "户型分类": "两室一厅",
            "押一付一": "3500",
            "押二付一": "3300",
            "看房方式密码": "",
            "备注": "商水商电",
            "图片": "无",
            "视频": "有",
        },
        {
            "区域": WANDA_AREA,
            "小区": "云杉苑",
            "房号": "A-302",
            "户型描述": "开间，独立厨卫",
            "户型分类": "单间",
            "押一付一": "1600",
            "押二付一": "1500",
            "看房方式密码": "",
            "备注": "民水民电",
            "图片": "有",
            "视频": "有",
        },
    ]


def metadata(version: str = "read-router-fixture") -> InventorySourceMetadata:
    return InventorySourceMetadata(source_kind="read_router_unit_test", source_version=version)


def readiness(**overrides: Any) -> dict[str, Any]:
    payload = {
        "reconciliation_passed": True,
        "blocking_count": 0,
        "public_artifact_secret_scan_passed": True,
        "missing_valid_aliases": 0,
        "unresolved_aliases": 0,
        "active_alias_conflicts": 0,
        "unknown_canonical_areas": 0,
        "ambiguous_direct_mappings": 0,
    }
    payload.update(overrides)
    return payload


def publish_snapshot(root: Path, rows: list[dict[str, Any]] | None = None, *, version: str = "v1"):
    snapshot, report = SnapshotBuilder().build(
        rows or inventory_rows(),
        metadata(version),
        generated_at="2026-06-25T00:00:00Z",
    )
    assert report.ok
    SnapshotStore(root).write_snapshot(snapshot, report)
    return snapshot


def legacy_inventory(rows: list[dict[str, Any]] | None = None) -> InventoryService:
    service = InventoryService()
    service._cache = pd.DataFrame(rows or inventory_rows())
    service._cache_file_marker = None
    service._cache_meta = {
        "status": "success",
        "hash": "legacy_fixture_hash",
        "row_count": len(rows or inventory_rows()),
    }
    return service


def legacy_provider(rows: list[dict[str, Any]] | None = None) -> LegacyInventoryReadProvider:
    return LegacyInventoryReadProvider(
        legacy_inventory(rows),
        rewrite_index_loader=lambda: {
            "room_index": [
                {
                    "listing_id": "lst_fixture",
                    "viewing": SYNTHETIC_PASSWORD,
                    "has_password": True,
                }
            ]
        },
    )


def snapshot_provider(root: Path, *, max_age_seconds: int | None = None) -> SnapshotInventoryReadProvider:
    return SnapshotInventoryReadProvider(SnapshotReader(root, max_age_seconds=max_age_seconds))


def router(
    root: Path,
    *,
    mode: str = READ_MODE_DISABLED,
    fallback_strategy: str = FALLBACK_STRICT,
    readiness_state: dict[str, Any] | None = None,
    legacy: LegacyInventoryReadProvider | None = None,
    max_age_seconds: int | None = None,
    supported_schema_versions: tuple[str, ...] | None = None,
) -> InventoryReadRouter:
    kwargs: dict[str, Any] = {}
    if supported_schema_versions is not None:
        kwargs["supported_schema_versions"] = supported_schema_versions
    return InventoryReadRouter(
        mode=mode,
        fallback_strategy=fallback_strategy,
        legacy_provider=legacy or legacy_provider(),
        snapshot_provider=snapshot_provider(root, max_age_seconds=max_age_seconds),
        readiness_state=readiness_state,
        **kwargs,
    )


def run(coro):
    return asyncio.run(coro)


def listing_ids(evidence: list[InventoryListingEvidence]) -> list[str]:
    return [item.listing_id for item in evidence]


def public_payload(evidence: list[InventoryListingEvidence] | InventoryListingEvidence | dict[str, Any]) -> str:
    if isinstance(evidence, list):
        value = [item.to_dict() for item in evidence]
    elif isinstance(evidence, InventoryListingEvidence):
        value = evidence.to_dict()
    else:
        value = evidence
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_disabled_selects_only_legacy_and_does_not_read_snapshot_health(tmp_path: Path) -> None:
    class ExplodingSnapshotProvider(SnapshotInventoryReadProvider):
        def health(self):  # type: ignore[override]
            raise AssertionError("disabled mode must not touch snapshot")

    read_router = InventoryReadRouter(
        mode=READ_MODE_DISABLED,
        legacy_provider=legacy_provider(),
        snapshot_provider=ExplodingSnapshotProvider(SnapshotReader(tmp_path)),
    )
    session = read_router.start_turn(request_id="req-disabled", turn_id="turn-1")
    rows = run(session.search_inventory("晨星花园1-101A", limit=3))

    assert session.context.source_kind == SOURCE_KIND_LEGACY
    assert rows
    assert all(item.source_kind == SOURCE_KIND_LEGACY for item in rows)


def test_shadow_customer_results_stay_legacy_and_ignore_snapshot_errors(tmp_path: Path) -> None:
    read_router = router(tmp_path, mode=READ_MODE_SHADOW)
    session = read_router.start_turn(request_id="req-shadow", turn_id="turn-1")
    rows = run(session.search_inventory("云杉苑视频图片", limit=3))

    assert session.context.source_kind == SOURCE_KIND_LEGACY
    assert session.context.source_hash == "legacy_fixture_hash"
    assert "shadow_snapshot" in session.context.health_at_selection["details"]
    assert all(
        item.source_kind == SOURCE_KIND_LEGACY
        and item.source_hash == "legacy_fixture_hash"
        and not item.snapshot_id
        for item in rows
    )
    assert not any(item.source_kind == SOURCE_KIND_SNAPSHOT for item in rows)


def test_shadow_chat_mode_can_skip_snapshot_health_probe(tmp_path: Path) -> None:
    class ExplodingSnapshotProvider(SnapshotInventoryReadProvider):
        def health(self):  # type: ignore[override]
            raise AssertionError("chat shadow mode must not touch snapshot")

    read_router = InventoryReadRouter(
        mode=READ_MODE_SHADOW,
        legacy_provider=legacy_provider(),
        snapshot_provider=ExplodingSnapshotProvider(SnapshotReader(tmp_path)),
        shadow_probe_snapshot_health=False,
    )
    session = read_router.start_turn(request_id="req-shadow-chat", turn_id="turn-1")
    rows = run(session.search_inventory("云杉苑视频图片", limit=3))

    assert session.context.source_kind == SOURCE_KIND_LEGACY
    assert session.context.source_hash == "legacy_fixture_hash"
    assert session.context.health_at_selection["details"]["shadow_snapshot"]["status"] == "not_queried"
    assert rows


def test_primary_healthy_selects_snapshot_locally(tmp_path: Path) -> None:
    snapshot = publish_snapshot(tmp_path)
    session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-primary",
        turn_id="turn-1",
    )
    rows = run(session.search_inventory("晨星花园1-101A", limit=3))

    assert session.context.source_kind == SOURCE_KIND_SNAPSHOT
    assert session.context.snapshot_id == snapshot.snapshot_id
    assert listing_ids(rows) == [generate_listing_id("晨星花园", "1-101A")]
    assert all(item.snapshot_id == snapshot.snapshot_id for item in rows)


def test_primary_pointer_missing_strict_fails_and_whole_request_fallback_uses_legacy(tmp_path: Path) -> None:
    strict_decision = router(tmp_path, mode=READ_MODE_PRIMARY).select_context(
        request_id="req-missing",
        turn_id="turn-1",
    )
    fallback_session = router(
        tmp_path,
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_LEGACY_WHOLE_REQUEST,
    ).start_turn(request_id="req-missing", turn_id="turn-2")

    assert strict_decision.ok is False
    assert strict_decision.error is not None
    assert strict_decision.error.code == REASON_SNAPSHOT_POINTER_MISSING
    assert fallback_session.context.source_kind == SOURCE_KIND_LEGACY
    assert fallback_session.context.fallback_used is True
    assert fallback_session.context.fallback_reason == REASON_SNAPSHOT_POINTER_MISSING


def test_primary_readiness_gates_return_structured_reason_codes(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)

    cases = [
        (readiness(reconciliation_passed=False), REASON_RECONCILIATION_BLOCKING),
        (readiness(blocking_count=1), REASON_RECONCILIATION_BLOCKING),
        (readiness(public_artifact_secret_scan_passed=False), REASON_SECRET_SCAN_FAILED),
        (readiness(missing_valid_aliases=1), REASON_ALIAS_COVERAGE_FAILED),
    ]
    for state, expected_code in cases:
        decision = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=state).select_context(
            request_id=f"req-{expected_code}",
            turn_id="turn-1",
        )
        assert decision.ok is False
        assert decision.error is not None
        assert decision.error.code == expected_code


def test_primary_stale_and_unsupported_schema_are_blocked(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)
    pointer_path = tmp_path / "current_snapshot.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["activated_at"] = "2020-01-01T00:00:00Z"
    pointer_path.write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")

    stale = router(
        tmp_path,
        mode=READ_MODE_PRIMARY,
        readiness_state=readiness(),
        max_age_seconds=60,
    ).select_context(request_id="req-stale", turn_id="turn-1")
    unsupported = router(
        tmp_path,
        mode=READ_MODE_PRIMARY,
        readiness_state=readiness(),
        supported_schema_versions=("inventory_snapshot.v999",),
    ).select_context(request_id="req-schema", turn_id="turn-1")

    assert stale.error is not None
    assert stale.error.code == REASON_SNAPSHOT_STALE
    assert unsupported.error is not None
    assert unsupported.error.code == REASON_UNSUPPORTED_SCHEMA


def test_invalid_mode_is_config_error_not_silent_downgrade(tmp_path: Path) -> None:
    decision = router(tmp_path, mode="primary-now-please").select_context(request_id="req-bad", turn_id="turn-1")

    assert decision.ok is False
    assert decision.error is not None
    assert decision.error.code == "invalid_inventory_read_mode"


def test_context_is_immutable_and_log_serialization_is_safe(tmp_path: Path) -> None:
    session = router(tmp_path).start_turn(request_id="req-log", turn_id="turn-1")
    log_payload = session.context.to_log_dict()

    with pytest.raises(FrozenInstanceError):
        session.context.source_kind = SOURCE_KIND_SNAPSHOT  # type: ignore[misc]
    with pytest.raises(TypeError):
        session.context.health_at_selection["token"] = "unsafe"  # type: ignore[index]
    assert "token" not in json.dumps(log_payload, ensure_ascii=False).lower()
    assert SYNTHETIC_PASSWORD not in json.dumps(log_payload, ensure_ascii=False)


def test_context_provider_mismatch_and_mixed_evidence_are_blocked(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)
    legacy_session = router(tmp_path).start_turn(request_id="req-mismatch", turn_id="turn-1")
    snap_provider = snapshot_provider(tmp_path)

    with pytest.raises(InventoryReadError) as provider_error:
        run(snap_provider.search_inventory("晨星花园", legacy_session.context))
    assert provider_error.value.code == REASON_CONTEXT_PROVIDER_MISMATCH

    good = run(legacy_session.search_inventory("晨星花园1-101A", limit=1))[0]
    bad = InventoryListingEvidence(
        evidence_id="evd_bad",
        listing_id=good.listing_id,
        source_kind=SOURCE_KIND_LEGACY,
        source_hash="different_hash",
        schema_version=good.schema_version,
        area=good.area,
        community=good.community,
        room_no=good.room_no,
    )
    with pytest.raises(InventoryReadError) as mixed_error:
        assert_evidence_consistency(legacy_session.context, [good, bad])
    assert mixed_error.value.code == REASON_MIXED_SOURCE_HASH


def test_snapshot_context_locks_snapshot_id_across_pointer_update_and_deleted_old_snapshot_errors(tmp_path: Path) -> None:
    first = publish_snapshot(tmp_path, version="v1")
    session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-lock",
        turn_id="turn-1",
    )
    second_rows = inventory_rows()
    second_rows[0]["押一付一"] = "1900"
    second = publish_snapshot(tmp_path, second_rows, version="v2")

    rows = run(session.search_inventory("晨星花园1-101A", limit=1))
    assert session.context.snapshot_id == first.snapshot_id
    assert rows[0].snapshot_id == first.snapshot_id
    assert rows[0].rent_pay1 == 1800
    assert first.snapshot_id != second.snapshot_id

    shutil.rmtree(tmp_path / "snapshots" / first.snapshot_id)
    with pytest.raises(InventoryReadError) as excinfo:
        run(session.search_inventory("晨星花园1-101A", limit=1))
    assert excinfo.value.code == REASON_SNAPSHOT_READ_FAILED


def test_fallback_after_any_business_read_is_forbidden(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)
    session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-after-read",
        turn_id="turn-1",
    )
    run(session.search_inventory("晨星花园1-101A", limit=1))

    with pytest.raises(InventoryReadError) as excinfo:
        session.require_whole_request_fallback_allowed()
    assert excinfo.value.code == REASON_FALLBACK_NOT_ALLOWED_AFTER_READ


def test_evidence_and_rewrite_index_do_not_expose_password_or_viewing_text(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)
    session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-secret",
        turn_id="turn-1",
    )
    rows = run(session.search_inventory("晨星花园1-101A", limit=1))
    rewrite_index = run(session.get_rewrite_index())
    payload = public_payload(rows) + json.dumps(rewrite_index, ensure_ascii=False)

    assert SYNTHETIC_PASSWORD not in payload
    assert '"viewing_text":' not in payload
    assert '"viewing"' not in payload
    assert "看房方式密码" not in payload


def test_legacy_rewrite_index_preserves_existing_prompt_payload_for_parity(tmp_path: Path) -> None:
    legacy_rewrite = run(
        router(tmp_path).start_turn(
            request_id="req-legacy-prompt",
            turn_id="turn-1",
        ).get_rewrite_index()
    )

    assert legacy_rewrite["room_index"][0]["viewing"] == SYNTHETIC_PASSWORD


def test_legacy_provider_reuses_inventory_service_search_without_copying_query_engine(tmp_path: Path) -> None:
    class CountingInventory:
        cache_meta = {"status": "success", "hash": "counting_legacy_hash", "row_count": 1}

        def __init__(self) -> None:
            self.search_calls: list[tuple[str, int]] = []

        async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
            self.search_calls.append((query, limit))
            return inventory_rows()[:1]

        async def all_rows(self, *, limit: int = 500, refresh_if_needed: bool = True) -> list[dict[str, Any]]:
            return inventory_rows()[:limit]

    counting = CountingInventory()
    read_router = InventoryReadRouter(
        mode=READ_MODE_DISABLED,
        legacy_provider=LegacyInventoryReadProvider(counting),  # type: ignore[arg-type]
        snapshot_provider=snapshot_provider(tmp_path),
    )
    rows = run(read_router.start_turn(request_id="req-count", turn_id="turn-1").search_inventory({"query": "晨星"}, limit=2))

    assert counting.search_calls == [("晨星", 2)]
    assert rows[0].listing_id == generate_listing_id("晨星花园", "1-101A")


def test_legacy_and_snapshot_parity_on_synthetic_queries(tmp_path: Path) -> None:
    publish_snapshot(tmp_path)
    legacy_session = InventoryReadRouter(
        mode=READ_MODE_DISABLED,
        legacy_provider=legacy_provider(),
        snapshot_provider=snapshot_provider(tmp_path),
    ).start_turn(request_id="req-parity-legacy", turn_id="turn-1")
    snapshot_session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-parity-snapshot",
        turn_id="turn-1",
    )
    queries: list[Any] = [
        {"query": "新填地一室", "area": DONGXIN_AREA},
        {"query": "东新一室带厅", "area": DONGXIN_AREA},
        {"query": "新天地两室", "area": DONGXIN_AREA},
        {"query": "东新园3000-3500两室", "area": DONGXIN_AREA},
        "晨星花园1-101A",
        "晨星花园2000以内一室",
        "晨星花园一室带厅",
        "星河公寓两室",
        "云杉苑民水民电",
        "云杉苑视频图片",
        "云杉苑A-302",
        "星河公寓2－201，两室",
    ]

    for query in queries:
        legacy_rows = run(legacy_session.search_inventory(query, limit=8))
        snapshot_rows = run(snapshot_session.search_inventory(query, limit=8))
        assert listing_ids(snapshot_rows) == listing_ids(legacy_rows), query
        assert [item.room_no for item in snapshot_rows] == [item.room_no for item in legacy_rows], query
        assert [item.rent_pay1 for item in snapshot_rows] == [item.rent_pay1 for item in legacy_rows], query
        assert [item.rent_pay2 for item in snapshot_rows] == [item.rent_pay2 for item in legacy_rows], query
        assert [item.utility_summary for item in snapshot_rows] == [item.utility_summary for item in legacy_rows], query
        assert [item.availability_summary for item in snapshot_rows] == [item.availability_summary for item in legacy_rows], query
        assert [item.has_image for item in snapshot_rows] == [item.has_image for item in legacy_rows], query
        assert [item.has_video for item in snapshot_rows] == [item.has_video for item in legacy_rows], query
        assert all(item.source_kind == SOURCE_KIND_LEGACY for item in legacy_rows)
        assert all(item.source_kind == SOURCE_KIND_SNAPSHOT for item in snapshot_rows)
        assert SYNTHETIC_PASSWORD not in public_payload(legacy_rows + snapshot_rows)


def test_production_customer_path_uses_read_router_without_snapshot_reader() -> None:
    main_source = Path("app/main.py").read_text(encoding="utf-8")

    assert "inventory_read_turn" in main_source
    assert "InventoryReadRouter" not in main_source
    assert "SnapshotInventoryReadProvider" not in main_source
    assert "inventory_snapshot_reader" not in main_source


def test_router_does_not_access_network_or_production_data_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fail_socket(*args: Any, **kwargs: Any):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", fail_socket)
    before = set(Path("data").glob("*")) if Path("data").exists() else set()
    publish_snapshot(tmp_path)
    session = router(tmp_path, mode=READ_MODE_PRIMARY, readiness_state=readiness()).start_turn(
        request_id="req-local-only",
        turn_id="turn-1",
    )
    rows = run(session.search_inventory("晨星花园1-101A", limit=1))
    after = set(Path("data").glob("*")) if Path("data").exists() else set()

    assert rows
    assert before == after
