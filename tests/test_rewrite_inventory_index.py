from __future__ import annotations

import json

from app.services.rewrite_inventory_index import (
    build_rewrite_inventory_index,
    load_rewrite_inventory_index,
    sanitize_rewrite_inventory_index,
    slice_rewrite_inventory_index,
    write_rewrite_inventory_index,
)


SECRET_CANARY = "VIEWING_SECRET_CANARY_135790#"
MISFILED_PHONE = "19900009999"
MISFILED_PASSWORD = "2468#"
MISFILED_TOKEN = "access_token=TEST_TOKEN_PUBLIC_REWRITE_123"
MISFILED_SECRET = "TEST_SECRET_PUBLIC_REWRITE_2468#"
PHONE_LIKE_HASH = "a19900009999b" + ("0" * 51)
PUBLIC_TEXT_PHONE = "18812345678"


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


def test_rewrite_inventory_index_redacts_misfiled_public_column_secrets() -> None:
    rows = inventory_rows()
    keys = list(rows[0].keys())
    layout_key = keys[3]
    remark_key = keys[8]
    rows[0][layout_key] = f"{rows[0][layout_key]} {MISFILED_PHONE} {MISFILED_TOKEN}"
    rows[0][remark_key] = f"{rows[0][remark_key]} {MISFILED_PASSWORD} {MISFILED_SECRET}"

    index = build_rewrite_inventory_index(rows)
    payload = dump(index)

    assert MISFILED_PHONE not in payload
    assert MISFILED_PASSWORD not in payload
    assert "TEST_TOKEN_PUBLIC_REWRITE_123" not in payload
    assert MISFILED_SECRET not in payload


def test_sanitize_rewrite_inventory_index_preserves_field_semantics_boundary_metadata() -> None:
    index = {
        "field_semantics": {
            "看房方式密码": (
                "字段语义是敏感边界，包含看房密码、空出时间、提前联系等看房方式信息；"
                f"误填示例 {MISFILED_PASSWORD} {PUBLIC_TEXT_PHONE} {MISFILED_TOKEN}"
            )
        },
        "room_index": [
            {
                "community": "棠润府",
                "room_no": "10-1004C",
                "看房方式密码": SECRET_CANARY,
            }
        ],
    }

    sanitized = sanitize_rewrite_inventory_index(index)
    payload = dump(sanitized)

    assert "看房方式密码" in sanitized["field_semantics"]
    assert "敏感边界" in sanitized["field_semantics"]["看房方式密码"]
    assert "看房方式密码" not in sanitized["room_index"][0]
    assert SECRET_CANARY not in payload
    assert MISFILED_PASSWORD not in payload
    assert PUBLIC_TEXT_PHONE not in payload
    assert "TEST_TOKEN_PUBLIC_REWRITE_123" not in payload


def test_sanitize_rewrite_inventory_index_preserves_hash_fields_but_redacts_public_text() -> None:
    index = {
        "signature": PHONE_LIKE_HASH,
        "source_hash": PHONE_LIKE_HASH,
        "cache_meta": {
            "hash": PHONE_LIKE_HASH,
            "sha256": PHONE_LIKE_HASH,
            "description": f"public note phone {PUBLIC_TEXT_PHONE} {MISFILED_TOKEN}",
        },
        "room_index": [
            {
                "community": "Unit Garden",
                "room": "1-101A",
                "source_hash": PHONE_LIKE_HASH,
                "signature": PHONE_LIKE_HASH,
                "sha256": PHONE_LIKE_HASH,
                "remark": f"remark {PUBLIC_TEXT_PHONE} {MISFILED_PASSWORD}",
                "description": f"description {MISFILED_TOKEN} {MISFILED_SECRET}",
                "viewing_summary": {
                    "has_password": True,
                    "note": f"nested note {PUBLIC_TEXT_PHONE}",
                },
            }
        ],
    }

    sanitized = sanitize_rewrite_inventory_index(index)
    payload = dump(sanitized)

    assert sanitized["signature"] == PHONE_LIKE_HASH
    assert sanitized["source_hash"] == PHONE_LIKE_HASH
    assert sanitized["cache_meta"]["hash"] == PHONE_LIKE_HASH
    assert sanitized["cache_meta"]["sha256"] == PHONE_LIKE_HASH
    assert sanitized["room_index"][0]["source_hash"] == PHONE_LIKE_HASH
    assert sanitized["room_index"][0]["signature"] == PHONE_LIKE_HASH
    assert sanitized["room_index"][0]["sha256"] == PHONE_LIKE_HASH
    assert PUBLIC_TEXT_PHONE not in payload
    assert MISFILED_PASSWORD not in payload
    assert "TEST_TOKEN_PUBLIC_REWRITE_123" not in payload
    assert MISFILED_SECRET not in payload
