from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.services import inventory_read_turn
from app.services.inventory_read_models import (
    READ_MODE_PRIMARY,
    REASON_PRIMARY_READINESS_MISSING,
    REASON_SNAPSHOT_POINTER_MISSING,
    SOURCE_KIND_SNAPSHOT,
    InventoryReadError,
)
from app.services.inventory_read_provider import SnapshotInventoryReadProvider
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_models import InventorySourceMetadata, generate_listing_id
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_store import SnapshotStore


class FakeInventory:
    def __init__(self) -> None:
        self.rows = [
            {
                "区域": "拱墅万达\n北部软件园\n城北万象城",
                "小区": "合幢悦府",
                "房号": "6-1-1204B",
                "户型描述": "一室一厅",
                "押一付一": "1500",
                "看房方式密码": "1234#",
            }
        ]
        self.search_calls: list[tuple[str, int]] = []
        self.all_rows_calls: list[dict[str, Any]] = []

    def cache_meta(self) -> dict[str, Any]:
        return {"status": "success", "hash": "turn_fixture_hash", "row_count": len(self.rows)}

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        self.search_calls.append((query, limit))
        return self.rows[:limit]

    async def all_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.all_rows_calls.append(dict(kwargs))
        return self.rows[: int(kwargs.get("limit") or len(self.rows))]


def run(coro):
    return asyncio.run(coro)


def rewrite_index() -> dict[str, Any]:
    return {"signature": "rewrite-fixture", "row_count": 1, "room_index": []}


def test_customer_context_unrecognized_mode_falls_back_to_disabled_legacy() -> None:
    context = inventory_read_turn.create_customer_inventory_read_context(
        prefix="kf",
        open_kfid="open-kf",
        external_userid="external-user",
        content="合幢悦府有哪些",
        inventory_service=FakeInventory(),
        rewrite_index_loader=rewrite_index,
        inventory_snapshot_mode="primary-preview",
        msgids=["msg-1"],
        generation=1,
    )

    assert context.source_kind == "legacy"
    assert context.selection_mode == "disabled"
    assert context.snapshot_id == ""


def _snapshot_rows() -> list[dict[str, Any]]:
    return [
        {
            "area": "unit-area",
            "community": "Unit Garden",
            "room": "1-101A",
            "layout_desc": "one bedroom",
            "layout_type": "one_room",
            "price_yayi": "1800",
            "price_yaer": "1700",
            "remark": "utility note",
            "image": 1,
            "video": 1,
        }
    ]


def _publish_snapshot(root: Path):
    snapshot, report = SnapshotBuilder().build(
        _snapshot_rows(),
        InventorySourceMetadata(source_kind="unit_test", source_version="v1"),
        generated_at="2026-06-25T00:00:00Z",
    )
    assert report.ok
    SnapshotStore(root).write_snapshot(snapshot, report)
    return snapshot


def _ready_primary_readiness(**overrides: Any) -> dict[str, Any]:
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


def test_customer_context_primary_value_uses_snapshot_when_readiness_is_ready(tmp_path: Path) -> None:
    snapshot = _publish_snapshot(tmp_path)
    snapshot_provider = SnapshotInventoryReadProvider(SnapshotReader(tmp_path))
    context = inventory_read_turn.create_customer_inventory_read_context(
        prefix="kf",
        open_kfid="open-kf",
        external_userid="external-user",
        content="Unit Garden 1-101A video",
        inventory_service=FakeInventory(),
        rewrite_index_loader=rewrite_index,
        inventory_snapshot_mode=READ_MODE_PRIMARY,
        msgids=["msg-1"],
        generation=1,
        snapshot_provider=snapshot_provider,
        readiness_state=_ready_primary_readiness(),
    )

    assert context.source_kind == SOURCE_KIND_SNAPSHOT
    assert context.selection_mode == READ_MODE_PRIMARY
    assert context.snapshot_id == snapshot.snapshot_id
    assert context.source_hash == snapshot.source_hash

    rows, evidence = run(
        inventory_read_turn.search_rows_for_context(
            context,
            "Unit Garden 1-101A",
            inventory_service=FakeInventory(),
            rewrite_index_loader=rewrite_index,
            snapshot_provider=snapshot_provider,
            limit=1,
        )
    )
    assert rows
    assert evidence[0].listing_id == generate_listing_id("Unit Garden", "1-101A")
    assert evidence[0].snapshot_id == snapshot.snapshot_id


def test_customer_context_primary_without_readiness_state_fails_closed(tmp_path: Path) -> None:
    _publish_snapshot(tmp_path)
    snapshot_provider = SnapshotInventoryReadProvider(SnapshotReader(tmp_path))

    with pytest.raises(InventoryReadError) as excinfo:
        inventory_read_turn.create_customer_inventory_read_context(
            prefix="kf",
            open_kfid="open-kf",
            external_userid="external-user",
            content="Unit Garden 1-101A video",
            inventory_service=FakeInventory(),
            rewrite_index_loader=rewrite_index,
            inventory_snapshot_mode=READ_MODE_PRIMARY,
            msgids=["msg-1"],
            generation=1,
            snapshot_provider=snapshot_provider,
        )

    assert excinfo.value.code == REASON_PRIMARY_READINESS_MISSING
    assert "readiness_state" in excinfo.value.message


def test_customer_context_primary_pointer_missing_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InventoryReadError) as excinfo:
        inventory_read_turn.create_customer_inventory_read_context(
            prefix="kf",
            open_kfid="open-kf",
            external_userid="external-user",
            content="Unit Garden 1-101A video",
            inventory_service=FakeInventory(),
            rewrite_index_loader=rewrite_index,
            inventory_snapshot_mode=READ_MODE_PRIMARY,
            msgids=["msg-1"],
            generation=1,
            snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(tmp_path)),
        )

    assert excinfo.value.code == REASON_SNAPSHOT_POINTER_MISSING


def test_turn_helpers_preserve_legacy_rows_and_safe_evidence() -> None:
    inventory = FakeInventory()
    context = inventory_read_turn.create_local_inventory_read_context(
        scope="unit",
        inventory_service=inventory,
        rewrite_index_loader=rewrite_index,
    )
    rows, evidence = run(
        inventory_read_turn.search_rows_for_context(
            context,
            "合幢悦府",
            inventory_service=inventory,
            rewrite_index_loader=rewrite_index,
            limit=1,
        )
    )

    assert rows == inventory.rows
    assert inventory.search_calls == [("合幢悦府", 1)]
    payload = json.dumps([item.to_dict() for item in evidence], ensure_ascii=False)
    assert "1234#" not in payload
    assert "看房方式密码" not in payload
    assert evidence[0].source_hash == context.source_hash


def test_clear_fact_evidence_removes_rows_and_media_paths() -> None:
    evidence = {
        "inventory_rows": [{"小区": "合幢悦府"}],
        "target_rows": [{"小区": "合幢悦府"}],
        "image_rows": [{"小区": "合幢悦府"}],
        "video_rows": [{"小区": "合幢悦府"}],
        "image_paths": ["room.jpg"],
        "video_paths": ["room.mp4"],
        "missing_media": ["合幢悦府6-1-1204B:视频"],
    }

    inventory_read_turn.clear_fact_evidence(
        evidence,
        InventoryReadError("mixed_source_hash", "blocked"),
    )

    assert evidence["inventory_read_error"]["code"] == "mixed_source_hash"
    assert evidence["inventory_rows"] == []
    assert evidence["target_rows"] == []
    assert evidence["image_paths"] == []
    assert evidence["video_paths"] == []
