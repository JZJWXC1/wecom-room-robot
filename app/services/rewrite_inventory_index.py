from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.fuzzy_match import canonical_community_display, normalize_search_text


FIELD_SEMANTICS: dict[str, str] = {
    "区域": "房源所在区域/板块，用于区域筛选和区域别名归一。",
    "小区": "小区标准名。",
    "房号": "房源房号，与小区组成唯一房源。",
    "户型描述": "详细户型介绍和特点。",
    "户型分类": "标准户型标签。",
    "押一付一": "选择押一付一付款方式时对应的月租价格。",
    "押二付一": "选择押二付一付款方式时对应的月租价格。",
    "看房方式密码": "看房密码、空出时间、提前联系等看房方式信息。",
    "备注": "水电费收取方式。",
}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "区域": ("区域", "商圈", "板块", "位置", "area"),
    "小区": ("小区", "社区", "楼盘", "小区名", "community"),
    "房号": ("房号", "房间号", "room", "room_no"),
    "户型描述": ("户型描述", "户型", "户型详情", "户型介绍"),
    "户型分类": ("户型分类", "户型标签", "房型"),
    "押一付一": ("押一付一", "押一付", "押一", "押一付一月租金", "price_yayi"),
    "押二付一": ("押二付一", "押二付", "押二", "押二付一月租金", "price_yaer"),
    "看房方式密码": ("看房方式密码", "密码", "看房方式", "看房密码"),
    "备注": ("备注", "水电", "水电费", "说明"),
}

DEFAULT_AREA_ALIASES: dict[str, str] = {
    "万达": "拱墅万达 北部软件园 城北万象城",
    "拱墅万达": "拱墅万达 北部软件园 城北万象城",
    "北部软件园": "拱墅万达 北部软件园 城北万象城",
    "城北万象城": "拱墅万达 北部软件园 城北万象城",
    "新天地": "东新园 杭氧 新天地",
    "鑫天地": "东新园 杭氧 新天地",
    "新填地": "东新园 杭氧 新天地",
    "东新": "东新园 杭氧 新天地",
    "东新园": "东新园 杭氧 新天地",
    "杭氧": "东新园 杭氧 新天地",
    "石桥": "石桥街道 华丰 石桥 永佳 半山",
    "华丰": "石桥街道 华丰 石桥 永佳 半山",
    "永佳": "石桥街道 华丰 石桥 永佳 半山",
    "半山": "石桥街道 华丰 石桥 永佳 半山",
    "闸弄口": "闸弄口 新塘 元宝塘 东站",
    "新塘": "闸弄口 新塘 元宝塘 东站",
    "元宝塘": "闸弄口 新塘 元宝塘 东站",
    "东站": "闸弄口 新塘 元宝塘 东站",
}


def row_value(row: dict[str, Any], canonical_field: str) -> str:
    for name in FIELD_ALIASES.get(canonical_field, (canonical_field,)):
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def canonical_room_key(row: dict[str, Any]) -> str:
    community = row_value(row, "小区")
    room_no = row_value(row, "房号")
    return f"{community}{room_no}".strip()


def canonicalize_row(row: dict[str, Any]) -> dict[str, str]:
    return {field: row_value(row, field) for field in FIELD_SEMANTICS}


def build_rewrite_inventory_index(
    rows: list[dict[str, Any]],
    *,
    area_aliases: dict[str, str] | None = None,
    cache_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    aliases = dict(DEFAULT_AREA_ALIASES)
    aliases.update(area_aliases or {})
    canonical_rows = [canonicalize_row(row) for row in rows]
    canonical_rows = [row for row in canonical_rows if row.get("小区") or row.get("房号")]
    payload_for_signature = {
        "rows": canonical_rows,
        "aliases": aliases,
        "field_semantics": FIELD_SEMANTICS,
        "cache_meta": cache_meta or {},
    }
    signature = hashlib.sha256(
        json.dumps(payload_for_signature, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "source": "latest_inventory_rows",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signature": signature,
        "cache_meta": cache_meta or {},
        "row_count": len(canonical_rows),
        "primary_key": "小区 + 房号",
        "field_semantics": FIELD_SEMANTICS,
        "field_aliases": {key: list(value) for key, value in FIELD_ALIASES.items()},
        "area_aliases": [
            {"alias": alias, "canonical": canonical}
            for alias, canonical in aliases.items()
        ],
        "areas": _area_stats(canonical_rows),
        "communities": _community_stats(canonical_rows),
        "room_index": _room_index(canonical_rows),
        "rules": {
            "payment_fields": "押一付一/押二付一是对应付款方式下的月租价格，不是押金金额。",
            "utility_field": "备注字段是水电费收取方式。",
            "layout_detail_field": "户型描述字段是详细户型介绍和特点。",
            "viewing_field": "看房方式密码字段是密码、空出时间、提前联系等看房方式信息。",
            "business_scope": "只服务杭州当前房源表；命中区域别名时按索引归一，不追问城市。",
        },
    }


def write_rewrite_inventory_index(
    rows: list[dict[str, Any]],
    *,
    path: Path | None = None,
    area_aliases: dict[str, str] | None = None,
    cache_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    index = build_rewrite_inventory_index(
        rows,
        area_aliases=area_aliases,
        cache_meta=cache_meta,
    )
    target = path or settings.rewrite_inventory_index_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def load_rewrite_inventory_index(path: Path | None = None) -> dict[str, Any]:
    target = path or settings.rewrite_inventory_index_path
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def slice_rewrite_inventory_index(
    index: dict[str, Any],
    *,
    query: str,
    limit: int = 80,
) -> dict[str, Any]:
    if not index:
        return {}
    query_text = normalize_search_text(query)
    area_hits = _area_hits(index, query_text)
    community_hits = _community_hits(index, query_text)
    room_hits = _room_hits(index, query_text)
    related_area_names = {
        str(item.get("canonical") or "")
        for item in area_hits
        if str(item.get("canonical") or "").strip()
    }
    related_communities = _communities_for_areas(index, related_area_names)
    communities = community_hits or related_communities
    return {
        "source": index.get("source"),
        "generated_at": index.get("generated_at"),
        "signature": index.get("signature"),
        "row_count": index.get("row_count"),
        "primary_key": index.get("primary_key"),
        "field_semantics": index.get("field_semantics") or FIELD_SEMANTICS,
        "field_aliases": index.get("field_aliases") or {},
        "rules": index.get("rules") or {},
        "area_aliases": index.get("area_aliases") or [],
        "areas": _limit_list(index.get("areas") or [], limit=24),
        "exact_area_hits": area_hits,
        "exact_community_hits": _limit_list(communities, limit=limit),
        "room_ref_hits": _limit_list(room_hits, limit=limit),
        "community_examples": _limit_list(index.get("communities") or [], limit=limit),
    }


def _area_stats(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        area = row.get("区域") or ""
        if area:
            grouped.setdefault(area, []).append(row)
    result: list[dict[str, Any]] = []
    for area, area_rows in sorted(grouped.items(), key=lambda item: item[0]):
        prices = _prices_for_rows(area_rows)
        communities = sorted({row.get("小区") or "" for row in area_rows if row.get("小区")})
        item: dict[str, Any] = {
            "name": area,
            "count": len(area_rows),
            "communities": communities[:80],
            "layouts": _layout_distribution(area_rows),
        }
        if prices:
            item["price_range"] = [min(prices), max(prices)]
        result.append(item)
    return result


def _community_stats(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        community = row.get("小区") or ""
        if community:
            grouped.setdefault(community, []).append(row)
    result: list[dict[str, Any]] = []
    for community, community_rows in sorted(grouped.items(), key=lambda item: item[0]):
        prices = _prices_for_rows(community_rows)
        result.append(
            {
                "name": canonical_community_display(community),
                "normalized": normalize_search_text(community),
                "count": len(community_rows),
                "area": community_rows[0].get("区域") or "",
                "rooms": [row.get("房号") or "" for row in community_rows if row.get("房号")][:80],
                "layouts": _layout_distribution(community_rows),
                "price_range": [min(prices), max(prices)] if prices else [],
            }
        )
    return result


def _room_index(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        key = canonical_room_key(row)
        if not key:
            continue
        result.append(
            {
                "key": key,
                "area": row.get("区域") or "",
                "community": row.get("小区") or "",
                "room_no": row.get("房号") or "",
                "layout": row.get("户型分类") or "",
                "layout_description": row.get("户型描述") or "",
                "price_yayi": row.get("押一付一") or "",
                "price_yaer": row.get("押二付一") or "",
                "viewing": row.get("看房方式密码") or "",
                "utilities": row.get("备注") or "",
            }
        )
    return result


def _prices_for_rows(rows: list[dict[str, str]]) -> list[int]:
    prices: list[int] = []
    for row in rows:
        for key in ("押一付一", "押二付一"):
            value = row.get(key) or ""
            for match in re.findall(r"\d{3,5}", value):
                prices.append(int(match))
    return prices


def _layout_distribution(rows: list[dict[str, str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        layout = row.get("户型分类") or ""
        if layout:
            result[layout] = result.get(layout, 0) + 1
    return result


def _area_hits(index: dict[str, Any], query_text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for item in index.get("area_aliases") or []:
        alias = str(item.get("alias") or "")
        if alias and normalize_search_text(alias) in query_text:
            hits.append(
                {
                    "alias": alias,
                    "canonical": item.get("canonical") or "",
                    "type": "area_alias",
                }
            )
    return hits


def _community_hits(index: dict[str, Any], query_text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for item in index.get("communities") or []:
        name = str(item.get("name") or "")
        normalized = str(item.get("normalized") or normalize_search_text(name))
        if normalized and normalized in query_text:
            hits.append(item)
    return hits


def _room_hits(index: dict[str, Any], query_text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for item in index.get("room_index") or []:
        if normalize_search_text(str(item.get("key") or "")) in query_text:
            hits.append(item)
            continue
        room_no = normalize_search_text(str(item.get("room_no") or ""))
        if room_no and room_no in query_text:
            hits.append(item)
    return hits


def _communities_for_areas(index: dict[str, Any], area_names: set[str]) -> list[dict[str, Any]]:
    if not area_names:
        return []
    result: list[dict[str, Any]] = []
    area_tokens = {
        token
        for name in area_names
        for token in str(name).replace("\n", " ").split()
        if token
    }
    for community in index.get("communities") or []:
        area = str(community.get("area") or "")
        if area in area_names or any(token and token in area for token in area_tokens):
            result.append(community)
    return result


def _limit_list(items: Any, *, limit: int) -> list[Any]:
    return list(items or [])[:limit]
