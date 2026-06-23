from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.region_inventory_constants import (
    AREA_ALIASES,
    COMMUNITY_ALIASES,
    DEFAULT_AREA_TITLE_ALIASES,
    DEFAULT_TARGET_AREA_TITLES,
    LAYOUT_ALIASES,
    LAYOUT_CLASS_ALIASES,
    PASSWORD_ALIASES,
    REMARK_ALIASES,
    RENT_ONE_ALIASES,
    RENT_TWO_ALIASES,
    ROOM_ALIASES,
)
from app.services.region_inventory_media import extract_docx_mentions, extract_note_links
from app.services.region_inventory_models import RegionInventoryRow, RegionSyncSource
from app.services.region_inventory_utils import (
    is_not_renting,
    normalize_text,
    pick_value,
    room_reference_from_row,
)


def normalize_region_records(
    records: list[dict[str, Any]],
    source: RegionSyncSource,
    *,
    record_to_row: Callable[[dict[str, Any]], dict[str, str]],
    extract_attachments: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> list[RegionInventoryRow]:
    rows: list[RegionInventoryRow] = []
    unresolved_areas: list[str] = []
    for record in records:
        row = record_to_row(record)
        if is_not_renting(row):
            continue
        area_title = resolve_record_area_title(row, source)
        if not area_title:
            unresolved = pick_record_area_value(row, source) or str(record.get("record_id") or "").strip()
            unresolved_areas.append(unresolved or "blank")
            continue
        community = pick_value(row, COMMUNITY_ALIASES)
        room_no = pick_value(row, ROOM_ALIASES)
        if not community or not room_no:
            community, room_no = room_reference_from_row(row, community, room_no)
        if not community or not room_no:
            continue
        rows.append(
            RegionInventoryRow(
                area_title=area_title,
                community=community,
                room_no=room_no,
                layout=pick_value(row, LAYOUT_ALIASES),
                layout_class=pick_value(row, LAYOUT_CLASS_ALIASES),
                rent_one=pick_value(row, RENT_ONE_ALIASES),
                rent_two=pick_value(row, RENT_TWO_ALIASES),
                password=pick_value(row, PASSWORD_ALIASES),
                remark=pick_value(row, REMARK_ALIASES),
                record_id=str(record.get("record_id") or ""),
                attachments=extract_attachments(record),
                note_links=extract_note_links(record),
                note_documents=extract_docx_mentions(record),
            )
        )
    if unresolved_areas:
        examples = "、".join(unresolved_areas[:5])
        extra = f" 等 {len(unresolved_areas)} 条" if len(unresolved_areas) > 5 else ""
        raise ValueError(f"源表记录区域无法映射到目标区域：{examples}{extra}")
    return rows


def pick_record_area_value(row: dict[str, str], source: RegionSyncSource) -> str:
    if source.area_field:
        return str(row.get(source.area_field) or "").strip()
    return pick_value(row, AREA_ALIASES)


def resolve_record_area_title(row: dict[str, str], source: RegionSyncSource) -> str:
    if not source.split_by_area:
        return source.area_title
    return resolve_area_title(pick_record_area_value(row, source), source)


def resolve_area_title(value: str, source: RegionSyncSource | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    area_map: dict[str, str] = {}
    if source:
        area_map.update(source.area_title_map)
    area_map.update(DEFAULT_AREA_TITLE_ALIASES)
    area_map.update({title: title for title in DEFAULT_TARGET_AREA_TITLES})

    if raw in area_map:
        return area_map[raw]

    normalized = normalize_text(raw)
    normalized_map = {
        normalize_text(key): target
        for key, target in area_map.items()
        if key and target
    }
    if normalized in normalized_map:
        return normalized_map[normalized]

    for alias, target in area_map.items():
        alias_key = normalize_text(alias)
        if alias_key and alias_key in normalized:
            return target
    return ""
