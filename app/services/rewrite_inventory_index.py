from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.fuzzy_match import canonical_community_display, normalize_search_text
from app.services.region_inventory_constants import area_alias_index_entries


DEFAULT_AREA_ALIASES: dict[str, str] = {
    str(item["alias"]): str(item["canonical_area"])
    for item in area_alias_index_entries()
}


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

IMAGE_FIELD_ALIASES: tuple[str, ...] = (
    "图片",
    "房源图片",
    "图片数量",
    "image",
    "images",
    "image_count",
    "has_image",
)

VIDEO_FIELD_ALIASES: tuple[str, ...] = (
    "视频",
    "房源视频",
    "视频数量",
    "video",
    "videos",
    "video_count",
    "has_video",
)

SENSITIVE_SIGNATURE_FIELDS = frozenset({"看房方式密码"})
SENSITIVE_ROOM_INDEX_KEYS = frozenset(
    {
        "viewing",
        "viewing_text",
        "password",
        "passcode",
        "看房方式密码",
        "看房密码",
        "密码",
        "门锁密码",
    }
)


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
    alias_entries = area_alias_index_entries(extra_aliases=area_aliases)
    row_pairs = [
        (row, canonicalize_row(row))
        for row in rows
    ]
    row_pairs = [
        (raw, canonical)
        for raw, canonical in row_pairs
        if canonical.get("小区") or canonical.get("房号")
    ]
    raw_rows = [raw for raw, _ in row_pairs]
    canonical_rows = [canonical for _, canonical in row_pairs]
    media_summary = _media_summary(raw_rows, canonical_rows)
    payload_for_signature = {
        "rows": _signature_rows(canonical_rows),
        "area_aliases": alias_entries,
        "field_semantics": FIELD_SEMANTICS,
        "media_summary": media_summary,
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
        "area_aliases": alias_entries,
        "areas": _area_stats(canonical_rows),
        "communities": _community_stats(canonical_rows),
        "room_index": _room_index(canonical_rows),
        "similar_communities": _similar_communities(canonical_rows),
        "media_summary": media_summary,
        "viewing_summary": _viewing_summary(canonical_rows),
        "availability_summary": _availability_summary(canonical_rows),
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
    return sanitize_rewrite_inventory_index(value) if isinstance(value, dict) else {}


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
        "similar_communities": _limit_list(index.get("similar_communities") or [], limit=limit),
        "media_summary": index.get("media_summary") or {},
        "viewing_summary": index.get("viewing_summary") or {},
        "availability_summary": index.get("availability_summary") or {},
        "exact_area_hits": area_hits,
        "exact_community_hits": _limit_list(community_hits, limit=limit),
        "area_related_communities": _limit_list(related_communities, limit=limit),
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
                "viewing_summary": _viewing_summary(community_rows),
                "availability_summary": _availability_summary(community_rows),
            }
        )
    return result


def _room_index(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        key = canonical_room_key(row)
        if not key:
            continue
        viewing_summary = _viewing_room_summary(row)
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
                "has_viewing_text": bool(viewing_summary["has_viewing_text"]),
                "has_password": bool(viewing_summary["has_password"]),
                "needs_contact": bool(viewing_summary["needs_contact"]),
                "has_empty_out_hint": bool(viewing_summary["has_empty_out_hint"]),
                "viewing_mode": str(viewing_summary["viewing_mode"]),
                "viewing_summary": viewing_summary,
                "availability": _availability_status(row.get("看房方式密码") or ""),
                "utilities": row.get("备注") or "",
            }
        )
    return result


def _media_summary(raw_rows: list[dict[str, Any]], canonical_rows: list[dict[str, str]]) -> dict[str, Any]:
    image_rooms: list[str] = []
    video_rooms: list[str] = []
    known_image_count = 0
    known_video_count = 0
    for raw, canonical in zip(raw_rows, canonical_rows):
        label = canonical_room_key(canonical)
        image_value = _media_value(raw, IMAGE_FIELD_ALIASES)
        video_value = _media_value(raw, VIDEO_FIELD_ALIASES)
        if image_value is not None:
            known_image_count += 1
            if image_value and label:
                image_rooms.append(label)
        if video_value is not None:
            known_video_count += 1
            if video_value and label:
                video_rooms.append(label)
    return {
        "source": "inventory_row_media_fields_if_present",
        "note": "只有源数据含图片/视频字段时才标记；没有字段时状态为 unknown，不推测。",
        "room_count": len(canonical_rows),
        "known_image_status_count": known_image_count,
        "known_video_status_count": known_video_count,
        "unknown_image_status_count": max(len(canonical_rows) - known_image_count, 0),
        "unknown_video_status_count": max(len(canonical_rows) - known_video_count, 0),
        "rooms_with_images": image_rooms[:200],
        "rooms_with_videos": video_rooms[:200],
    }


def _media_value(row: dict[str, Any], aliases: tuple[str, ...]) -> bool | None:
    for alias in aliases:
        if alias not in row:
            continue
        value = row.get(alias)
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        text = str(value).strip().lower()
        if not text:
            return False
        if text in {"0", "false", "no", "none", "无", "没有", "暂无"}:
            return False
        return True
    return None


def _viewing_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    result = {
        "has_viewing_text": 0,
        "has_password": 0,
        "needs_contact": 0,
        "has_empty_out_hint": 0,
        "unknown": 0,
    }
    for row in rows:
        summary = _viewing_room_summary(row)
        if not summary["has_viewing_text"]:
            result["unknown"] += 1
        if summary["has_viewing_text"]:
            result["has_viewing_text"] += 1
        if summary["has_password"]:
            result["has_password"] += 1
        if summary["needs_contact"]:
            result["needs_contact"] += 1
        if summary["has_empty_out_hint"]:
            result["has_empty_out_hint"] += 1
    return result


def _viewing_room_summary(row: dict[str, str]) -> dict[str, Any]:
    return _viewing_text_summary(row.get("看房方式密码") or "")


def _viewing_text_summary(viewing: Any) -> dict[str, Any]:
    text = str(viewing or "").strip()
    has_viewing_text = bool(text)
    has_password = bool(re.search(r"(?<!\d)\d{3,8}#?(?!\d)", text))
    needs_contact = any(word in text for word in ("提前联系", "联系", "预约", "看房提前", "密码不对"))
    has_empty_out_hint = any(word in text for word in ("空出", "未空", "还没空", "转租", "待空"))
    if not has_viewing_text:
        viewing_mode = "unknown"
    elif has_password:
        viewing_mode = "password_available"
    elif needs_contact:
        viewing_mode = "contact_required"
    else:
        viewing_mode = "viewing_text_only"
    return {
        "has_viewing_text": has_viewing_text,
        "has_password": has_password,
        "needs_contact": needs_contact,
        "has_empty_out_hint": has_empty_out_hint,
        "viewing_mode": viewing_mode,
    }


def _availability_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        status = _availability_status(row.get("看房方式密码") or "")
        result[status] = result.get(status, 0) + 1
    return result


def _availability_status(viewing: str) -> str:
    text = viewing.strip()
    if not text:
        return "unknown"
    if any(word in text for word in ("未空", "还没空", "待空")):
        return "not_yet_vacant"
    if "空出" in text or "转租" in text:
        return "has_empty_out_hint"
    if any(word in text for word in ("提前联系", "联系", "预约")):
        return "needs_contact"
    if re.search(r"\d{3,8}#?", text):
        return "password_available"
    return "viewing_text_only"


def _similar_communities(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    communities = sorted({row.get("小区") or "" for row in rows if row.get("小区")})
    normalized = {name: normalize_search_text(name) for name in communities}
    result: list[dict[str, Any]] = []
    for name in communities:
        options: list[dict[str, Any]] = []
        name_norm = normalized[name]
        name_chars = {char for char in name_norm if "\u4e00" <= char <= "\u9fff"}
        for other in communities:
            if other == name:
                continue
            other_norm = normalized[other]
            if not name_norm or not other_norm:
                continue
            score = SequenceMatcher(None, name_norm, other_norm).ratio()
            shared_chars = name_chars & {char for char in other_norm if "\u4e00" <= char <= "\u9fff"}
            contains_relation = name_norm in other_norm or other_norm in name_norm
            if contains_relation or (score >= 0.35 and len(shared_chars) >= 2):
                options.append(
                    {
                        "name": other,
                        "normalized": other_norm,
                        "score": round(score, 3),
                    }
                )
        if options:
            options.sort(key=lambda item: (-float(item["score"]), item["name"]))
            result.append(
                {
                    "name": name,
                    "normalized": name_norm,
                    "options": options[:8],
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
        if str(item.get("status") or "active") != "active":
            continue
        if alias and normalize_search_text(alias) in query_text:
            hits.append(
                {
                    "alias": alias,
                    "canonical": item.get("canonical_area") or item.get("canonical") or "",
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
            hits.append(_sanitize_room_index_item(item))
            continue
        room_no = normalize_search_text(str(item.get("room_no") or ""))
        if room_no and room_no in query_text:
            hits.append(_sanitize_room_index_item(item))
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


def _signature_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        safe = {
            field: value
            for field, value in row.items()
            if field not in SENSITIVE_SIGNATURE_FIELDS
        }
        viewing_summary = _viewing_room_summary(row)
        safe["viewing_summary"] = viewing_summary
        safe["availability"] = _availability_status(row.get("看房方式密码") or "")
        safe["viewing_mode"] = str(viewing_summary["viewing_mode"])
        result.append(safe)
    return result


def sanitize_rewrite_inventory_index(index: dict[str, Any]) -> dict[str, Any]:
    result = dict(index)
    room_index = result.get("room_index")
    if isinstance(room_index, list):
        result["room_index"] = [
            _sanitize_room_index_item(item)
            for item in room_index
            if isinstance(item, dict)
        ]
    return result


def _sanitize_room_index_item(item: dict[str, Any]) -> dict[str, Any]:
    raw_viewing = _first_sensitive_viewing_value(item)
    result = {
        key: value
        for key, value in item.items()
        if str(key).strip() not in SENSITIVE_ROOM_INDEX_KEYS
    }
    summary = result.get("viewing_summary")
    if not isinstance(summary, dict):
        summary = _viewing_text_summary(raw_viewing)
    else:
        summary = dict(summary)
        if "viewing_mode" not in summary:
            summary["viewing_mode"] = _viewing_mode_from_summary(summary)
    result["viewing_summary"] = summary
    result.setdefault("has_viewing_text", bool(summary.get("has_viewing_text")))
    result.setdefault("has_password", bool(summary.get("has_password")))
    result.setdefault("needs_contact", bool(summary.get("needs_contact")))
    result.setdefault("has_empty_out_hint", bool(summary.get("has_empty_out_hint")))
    result.setdefault("viewing_mode", str(summary.get("viewing_mode") or "unknown"))
    if "availability" not in result and raw_viewing:
        result["availability"] = _availability_status(str(raw_viewing))
    return result


def _first_sensitive_viewing_value(item: dict[str, Any]) -> str:
    for key in SENSITIVE_ROOM_INDEX_KEYS:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _viewing_mode_from_summary(summary: dict[str, Any]) -> str:
    if not summary.get("has_viewing_text"):
        return "unknown"
    if summary.get("has_password"):
        return "password_available"
    if summary.get("needs_contact"):
        return "contact_required"
    return "viewing_text_only"
