from __future__ import annotations

import json

from app.services.rewrite_inventory_index import (
    build_rewrite_inventory_index,
    load_rewrite_inventory_index,
    slice_rewrite_inventory_index,
    write_rewrite_inventory_index,
)


SECRET_CANARY = "VIEWING_SECRET_CANARY_135790#"


def inventory_rows() -> list[dict[str, str]]:
    return [
        {
            "区域": "拱墅万达",
            "小区": "棠润府",
            "房号": "10-1004C",
            "户型描述": "一室一厅独立厨卫",
            "户型分类": "一室一厅",
            "押一付一": "2600",
            "押二付一": "2300",
            "看房方式密码": f"{SECRET_CANARY} 看房提前联系",
            "备注": "水30/月，电1元/度",
        }
    ]


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_rewrite_inventory_index_redacts_viewing_secret_from_room_index() -> None:
    index = build_rewrite_inventory_index(inventory_rows())
    payload = dump(index)
    room_item = index["room_index"][0]

    assert SECRET_CANARY not in payload
    assert "viewing" not in room_item
    assert room_item["has_password"] is True
    assert room_item["viewing_mode"] == "password_available"
    assert room_item["viewing_summary"]["has_password"] is True


def test_rewrite_inventory_index_slice_redacts_historical_viewing_field() -> None:
    index = build_rewrite_inventory_index(inventory_rows())
    index["room_index"][0]["viewing"] = SECRET_CANARY

    sliced = slice_rewrite_inventory_index(index, query="棠润府10-1004C怎么看")
    payload = dump(sliced)
    hit = sliced["room_ref_hits"][0]

    assert SECRET_CANARY not in payload
    assert "viewing" not in hit
    assert hit["has_password"] is True
    assert hit["viewing_mode"] == "password_available"


def test_load_rewrite_inventory_index_sanitizes_historical_file(tmp_path) -> None:
    index = build_rewrite_inventory_index(inventory_rows())
    index["room_index"][0]["viewing"] = SECRET_CANARY
    path = tmp_path / "rewrite_inventory_index.json"
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    loaded = load_rewrite_inventory_index(path)
    payload = dump(loaded)

    assert SECRET_CANARY not in payload
    assert "viewing" not in loaded["room_index"][0]


def test_write_rewrite_inventory_index_never_persists_viewing_secret(tmp_path) -> None:
    path = tmp_path / "rewrite_inventory_index.json"
    write_rewrite_inventory_index(inventory_rows(), path=path)
    payload = path.read_text(encoding="utf-8")

    assert SECRET_CANARY not in payload
    assert '"viewing"' not in payload
