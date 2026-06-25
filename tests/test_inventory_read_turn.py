from __future__ import annotations

import asyncio
import json
from typing import Any

from app.services import inventory_read_turn
from app.services.inventory_read_models import InventoryReadError


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


def test_customer_context_primary_value_falls_back_to_disabled_legacy() -> None:
    context = inventory_read_turn.create_customer_inventory_read_context(
        prefix="kf",
        open_kfid="open-kf",
        external_userid="external-user",
        content="合幢悦府有哪些",
        inventory_service=FakeInventory(),
        rewrite_index_loader=rewrite_index,
        inventory_snapshot_mode="primary",
        msgids=["msg-1"],
        generation=1,
    )

    assert context.source_kind == "legacy"
    assert context.selection_mode == "disabled"
    assert context.snapshot_id == ""


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
