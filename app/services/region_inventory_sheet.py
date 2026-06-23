from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services.region_inventory_constants import (
    AREA_LABEL_FONT_COLOR,
    DATA_CELL_STYLE,
    DATA_FONT_COLOR,
    DATA_ROW_HEIGHT_PX,
    DEFAULT_AREA_LABELS,
    DEFAULT_TARGET_AREA_TITLES,
    LEADING_WHOLE_RENT_RE,
    RICH_TEXT_FONT_SIZE,
    SECTION_TITLE_ROW_HEIGHT_PX,
    TARGET_HEADERS,
    WHOLE_RENT_RE,
)
from app.services.region_inventory_models import (
    AreaLabelRepair,
    AreaRowInsertion,
    CommunityMergeRepair,
    RegionInventoryRow,
    RowHeightRepair,
    SectionTitleRepair,
    SheetRowDeletion,
)
from app.services.region_inventory_utils import (
    drive_area_folder_name,
    normalize_room_no,
    normalize_text,
)


def dedupe_rows(rows: list[RegionInventoryRow]) -> list[RegionInventoryRow]:
    deduped: dict[str, RegionInventoryRow] = {}
    order: list[str] = []
    for row in rows:
        if row.key not in deduped:
            order.append(row.key)
        deduped[row.key] = row
    return [deduped[key] for key in order]


def group_rows_by_community(rows: list[RegionInventoryRow]) -> list[RegionInventoryRow]:
    groups: dict[str, list[RegionInventoryRow]] = {}
    community_order: list[str] = []
    for row in dedupe_rows(rows):
        if row.community not in groups:
            community_order.append(row.community)
            groups[row.community] = []
        groups[row.community].append(row)
    return [
        row
        for community in community_order
        for row in groups[community]
    ]


def natural_room_sort_key(value: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", normalize_room_no(value))
    return tuple(int(part) if part.isdigit() else part.lower() for part in parts)


def normalize_matrix(values: list[list[Any]], *, width: int) -> list[list[str]]:
    return [
        [str(row[index] if index < len(row) else "").strip() for index in range(width)]
        for row in values
    ]


def column_letter(column_number: int) -> str:
    letters = ""
    while column_number > 0:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def spreadsheet_write_matrix(values: list[list[Any]]) -> list[list[Any]]:
    return [[cell if cell != "" else " " for cell in row] for row in values]


TOP_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:[./-]|月)\s*(\d{1,2})(?:\s*日)?(?!\d)")
TOP_DATE_HINTS = ("欢迎", "推荐", "全佣", "免押", "短租")
TOP_DATE_SKIP_HINTS = ("联系方式", "电话", "手机")


def current_sync_date_text(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return f"{now.month}.{now.day}"


def build_top_sync_date_update(
    existing: list[list[str]],
    sync_date_text: str,
) -> dict[str, Any] | None:
    header_row = find_header_row_number(existing) or min(len(existing) + 1, 5)
    candidate_rows = existing[: max(0, header_row - 1)]
    fallback: dict[str, Any] | None = None
    for row_index, row in enumerate(candidate_rows, start=1):
        for column_index, value in enumerate(row, start=1):
            text = str(value or "").strip()
            if not text:
                continue
            hint_text = normalize_text(text)
            if any(hint in hint_text for hint in TOP_DATE_SKIP_HINTS):
                continue
            if not any(hint in hint_text for hint in TOP_DATE_HINTS):
                continue
            start_cell = f"{column_letter(column_index)}{row_index}"
            match = TOP_DATE_RE.search(text)
            if match:
                updated = TOP_DATE_RE.sub(sync_date_text, text, count=1)
                if updated == text:
                    return None
                return {"start_cell": start_cell, "values": [[updated]]}
            fallback = fallback or {"start_cell": start_cell, "values": [[f"{text}  {sync_date_text}"]]}
    return fallback


def build_layout_preserving_updates(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
    rich_layout: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    updates: list[dict[str, Any]] = []
    areas_updated: list[str] = []
    rows_written = 0
    rows_removed = 0
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            raise RuntimeError(f"目标房源表缺少区域标题，停止同步以保护原有格式：{area}")
        start_row, end_row = section
        capacity = end_row - start_row + 1
        area_rows = group_rows_by_community(rows)
        if len(area_rows) > capacity:
            raise RuntimeError(
                f"区域模板预留行数不足，停止同步以保护原有格式：{area} "
                f"需要 {len(area_rows)} 行，当前只有 {capacity} 行"
            )
        old_count = count_room_rows(existing[start_row - 1 : end_row])
        data_values = [
            format_data_row(row, rich_layout=rich_layout) if index < len(area_rows) else [""] * 7
            for index, row in enumerate([*area_rows, *([None] * (capacity - len(area_rows)))])
            if row is not None or index >= len(area_rows)
        ]
        updates.append({"start_cell": f"C{start_row}", "values": data_values})
        community_values = format_community_column(area_rows, capacity)
        for offset, value in enumerate(community_values):
            if value:
                updates.append({"start_cell": f"B{start_row + offset}", "values": [[value]]})
        areas_updated.append(area)
        rows_written += len(area_rows)
        rows_removed += max(0, old_count - len(area_rows))
    return updates, {
        "areas_updated": areas_updated,
        "rows_written": rows_written,
        "rows_removed": rows_removed,
    }


def build_rich_layout_cell_updates(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    updates: list[dict[str, Any]] = []
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            raise RuntimeError(f"目标房源表缺少区域标题，停止同步以保护原有格式：{area}")
        start_row, end_row = section
        capacity = end_row - start_row + 1
        area_rows = group_rows_by_community(rows)
        if len(area_rows) > capacity:
            raise RuntimeError(
                f"区域模板预留行数不足，停止同步以保护原有格式：{area} "
                f"需要 {len(area_rows)} 行，当前只有 {capacity} 行"
            )
        for offset, row in enumerate(area_rows):
            if not is_whole_rent_row(row):
                continue
            value = format_layout_cell(row, rich_layout=True)
            if isinstance(value, list):
                updates.append({"start_cell": f"D{start_row + offset}", "values": [[value]]})
    return updates


def build_data_cell_style_updates(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    ranges: list[str] = []
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            raise RuntimeError(f"目标房源表缺少区域标题，停止同步以保护原有格式：{area}")
        start_row, end_row = section
        capacity = end_row - start_row + 1
        area_rows = group_rows_by_community(rows)
        if len(area_rows) > capacity:
            raise RuntimeError(
                f"区域模板预留行数不足，停止同步以保护原有格式：{area} "
                f"需要 {len(area_rows)} 行，当前只有 {capacity} 行"
            )
        ranges.append(f"B{start_row}:I{end_row}")
    if not ranges:
        return []
    return [
        {
            "ranges": ranges,
            "style": dict(DATA_CELL_STYLE),
        }
    ]


def build_area_label_repairs(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[AreaLabelRepair]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    repairs: list[AreaLabelRepair] = []
    for area in area_titles:
        if area not in rows_by_area:
            continue
        section = sections.get(area)
        if section is None:
            continue
        start_row, end_row = section
        if end_row >= start_row:
            repairs.append(
                AreaLabelRepair(
                    area_title=area,
                    start_row=start_row,
                    end_row=end_row,
                    label=area_display_label(area),
                )
            )
    return repairs


def build_section_title_repairs(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[SectionTitleRepair]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    title_rows = find_area_title_rows(existing, area_titles)
    return [
        SectionTitleRepair(area_title=area, row_number=row_number)
        for area, row_number in title_rows.items()
        if area in rows_by_area
    ]


def build_community_merge_repairs(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[CommunityMergeRepair]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    repairs: list[CommunityMergeRepair] = []
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            continue
        start_row, _ = section
        area_rows = group_rows_by_community(rows)
        group_start = start_row
        current = ""
        for offset, row in enumerate(area_rows):
            row_number = start_row + offset
            if row.community != current:
                if current:
                    repairs.append(
                        CommunityMergeRepair(
                            community=current,
                            start_row=group_start,
                            end_row=row_number - 1,
                        )
                    )
                current = row.community
                group_start = row_number
        if current:
            repairs.append(
                CommunityMergeRepair(
                    community=current,
                    start_row=group_start,
                    end_row=start_row + len(area_rows) - 1,
                )
            )
    return repairs


def build_row_height_repairs(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[RowHeightRepair]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(
        existing,
        area_titles,
        minimum_rows_by_area={area: len(group_rows_by_community(rows)) for area, rows in rows_by_area.items()},
    )
    title_rows = find_area_title_rows(existing, area_titles)
    repairs: list[RowHeightRepair] = []
    for area in area_titles:
        if area not in rows_by_area:
            continue
        title_row = title_rows.get(area)
        if title_row:
            repairs.append(
                RowHeightRepair(
                    start_row=title_row,
                    end_row=title_row,
                    height_px=SECTION_TITLE_ROW_HEIGHT_PX,
                )
            )
        section = sections.get(area)
        if section is None:
            continue
        start_row, _ = section
        area_rows = group_rows_by_community(rows_by_area[area])
        if area_rows:
            repairs.append(
                RowHeightRepair(
                    start_row=start_row,
                    end_row=start_row + len(area_rows) - 1,
                    height_px=DATA_ROW_HEIGHT_PX,
                )
            )
    return repairs


def is_whole_rent_row(row: RegionInventoryRow) -> bool:
    return bool(WHOLE_RENT_RE.search(" ".join([row.layout, row.layout_class])))


def plan_area_row_insertions(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[AreaRowInsertion]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_sections(existing, area_titles)
    plans: list[AreaRowInsertion] = []
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            raise RuntimeError(f"目标房源表缺少区域标题，停止同步以保护原有格式：{area}")
        start_row, end_row = section
        capacity = end_row - start_row + 1
        required = len(group_rows_by_community(rows))
        missing = required - capacity
        if missing <= 0:
            continue
        if capacity <= 0:
            raise RuntimeError(f"区域没有可复制格式的房源行，停止同步以保护原有格式：{area}")
        insert_before_row = end_row + 1
        if end_row >= len(existing):
            insert_before_row = end_row
        plans.append(
            AreaRowInsertion(
                area_title=area,
                insert_before_row=insert_before_row,
                count=missing,
                inherit_style="BEFORE",
            )
        )
    return plans


def plan_area_row_deletions(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> list[SheetRowDeletion]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    sections = find_existing_area_section_limits(existing, area_titles)
    deletions: list[SheetRowDeletion] = []
    for area, rows in rows_by_area.items():
        section = sections.get(area)
        if section is None:
            continue
        start_row, end_row = section
        capacity = end_row - start_row + 1
        required = len(group_rows_by_community(rows))
        excess = capacity - required
        if excess <= 0:
            continue
        deletions.append(SheetRowDeletion(start_row=start_row + required, count=excess))
    return deletions


def plan_trailing_blank_row_deletion(
    existing: list[list[str]],
    *,
    all_area_titles: list[str],
    minimum_rows_by_area: dict[str, int],
) -> SheetRowDeletion | None:
    sections = find_existing_area_sections(
        existing,
        all_area_titles,
        minimum_rows_by_area=minimum_rows_by_area,
    )
    if not sections:
        return None
    last_table_row = max(end_row for _, end_row in sections.values())
    if last_table_row >= len(existing):
        return None
    trailing = existing[last_table_row:]
    if any(any(str(cell).strip() for cell in row[: len(TARGET_HEADERS)]) for row in trailing):
        return None
    return SheetRowDeletion(start_row=last_table_row + 1, count=len(existing) - last_table_row)


def find_existing_area_section_limits(
    existing: list[list[str]],
    area_titles: list[str],
) -> dict[str, tuple[int, int]]:
    title_rows: list[tuple[str, int]] = []
    section_title_rows: list[int] = []
    header_row = find_header_row_number(existing)
    for index, row in enumerate(existing, start=1):
        if index <= header_row:
            continue
        if is_section_title_row(row):
            section_title_rows.append(index)
        area = matched_area_title(row, area_titles)
        if area:
            title_rows.append((area, index))
    sections: dict[str, tuple[int, int]] = {}
    for area, title_row in title_rows:
        next_title_row = next(
            (row_number for row_number in section_title_rows if row_number > title_row),
            len(existing) + 1,
        )
        sections[area] = (title_row + 1, next_title_row - 1)
    return sections


def simulate_row_insertions(
    existing: list[list[str]],
    plans: list[AreaRowInsertion],
) -> list[list[str]]:
    output = [row[:] for row in existing]
    for plan in sorted(plans, key=lambda item: item.insert_before_row, reverse=True):
        insert_at = max(0, plan.insert_before_row - 1)
        output[insert_at:insert_at] = [[""] * len(TARGET_HEADERS) for _ in range(plan.count)]
    return output


def simulate_row_deletion(existing: list[list[str]], plan: SheetRowDeletion) -> list[list[str]]:
    output = [row[:] for row in existing]
    delete_at = max(0, plan.start_row - 1)
    del output[delete_at: delete_at + plan.count]
    return output


def find_existing_area_sections(
    existing: list[list[str]],
    area_titles: list[str],
    *,
    minimum_rows_by_area: dict[str, int] | None = None,
) -> dict[str, tuple[int, int]]:
    title_rows: list[tuple[str, int]] = []
    section_title_rows: list[int] = []
    header_row = find_header_row_number(existing)
    for index, row in enumerate(existing, start=1):
        if index <= header_row:
            continue
        if is_section_title_row(row):
            section_title_rows.append(index)
        area = matched_area_title(row, area_titles)
        if area:
            title_rows.append((area, index))
    sections: dict[str, tuple[int, int]] = {}
    for area, title_row in title_rows:
        next_title_row = next(
            (row_number for row_number in section_title_rows if row_number > title_row),
            len(existing) + 1,
        )
        start_row = title_row + 1
        limit_row = next_title_row - 1
        trimmed_end = trim_empty_section_end(existing, start_row, limit_row)
        required = (minimum_rows_by_area or {}).get(area, 0)
        required_end = start_row + required - 1 if required > 0 else start_row
        sections[area] = (start_row, min(limit_row, max(trimmed_end, required_end)))
    return sections


def find_area_title_rows(existing: list[list[str]], area_titles: list[str]) -> dict[str, int]:
    title_rows: dict[str, int] = {}
    header_row = find_header_row_number(existing)
    for index, row in enumerate(existing, start=1):
        if index <= header_row:
            continue
        area = matched_area_title(row, area_titles)
        if area:
            title_rows[area] = index
    return title_rows


def trim_empty_section_end(existing: list[list[str]], start_row: int, limit_row: int) -> int:
    if limit_row < start_row:
        return start_row
    for row_number in range(min(limit_row, len(existing)), start_row - 1, -1):
        row = existing[row_number - 1] if row_number - 1 < len(existing) else []
        if any(str(cell).strip() for cell in row[1: len(TARGET_HEADERS)]):
            return row_number
    return start_row


def find_header_row_number(existing: list[list[str]]) -> int:
    for index, row in enumerate(existing, start=1):
        first = normalize_text(row[0] if row else "")
        second = normalize_text(row[1] if len(row) > 1 else "")
        if first == normalize_text("区域") and second == normalize_text("小区"):
            return index
    return 0


def is_section_title_row(row: list[str]) -> bool:
    first_cell = str(row[0] if row else "").strip()
    if not first_cell:
        return False
    if normalize_text(first_cell) in {normalize_text("区域"), normalize_text("小区")}:
        return False
    return not any(str(cell).strip() for cell in row[1: len(TARGET_HEADERS)])


def format_data_row(row: RegionInventoryRow, *, rich_layout: bool = True) -> list[Any]:
    return [
        row.room_no,
        format_layout_cell(row, rich_layout=rich_layout),
        row.layout_class,
        row.rent_one,
        row.rent_two,
        row.password,
        row.remark,
    ]


def format_layout_cell(row: RegionInventoryRow, *, rich_layout: bool = True) -> Any:
    layout = format_layout_text(row)
    if not rich_layout or not is_whole_rent_row(row) or not layout.startswith("（整）"):
        return layout
    rest = layout.removeprefix("（整）")
    return [
        {
            "text": "（整）",
            "type": "text",
            "segmentStyle": {
                "foreColor": AREA_LABEL_FONT_COLOR,
                "fontSize": RICH_TEXT_FONT_SIZE,
            },
        },
        {
            "text": rest,
            "type": "text",
            "segmentStyle": {
                "foreColor": DATA_FONT_COLOR,
                "fontSize": RICH_TEXT_FONT_SIZE,
            },
        },
    ]


def format_layout_text(row: RegionInventoryRow) -> str:
    layout = str(row.layout or "").strip()
    if not is_whole_rent_row(row):
        return layout
    cleaned = LEADING_WHOLE_RENT_RE.sub("", layout).strip()
    return f"（整）{cleaned}" if cleaned else "（整）"


def format_community_column(rows: list[RegionInventoryRow], capacity: int) -> list[str]:
    output: list[str] = []
    previous = ""
    for row in rows:
        output.append(row.community if row.community != previous else "")
        previous = row.community
    output.extend([""] * max(0, capacity - len(output)))
    return output[:capacity]


def area_display_label(area_title: str) -> str:
    if area_title in DEFAULT_AREA_LABELS:
        return DEFAULT_AREA_LABELS[area_title]
    cleaned = drive_area_folder_name(area_title)
    return "\n".join(part for part in cleaned.split() if part) or cleaned


def rewrite_target_sheet_values(
    existing: list[list[str]],
    rows_by_area: dict[str, list[RegionInventoryRow]],
    *,
    all_area_titles: list[str] | None = None,
) -> tuple[list[list[str]], dict[str, Any]]:
    area_titles = ordered_area_titles(all_area_titles or list(rows_by_area))
    existing_sections, preamble_end = split_existing_area_sections(existing, area_titles)
    output: list[list[str]] = [row[: len(TARGET_HEADERS)] for row in existing[:preamble_end]]
    areas_updated: list[str] = []
    old_rows_removed = 0
    rows_written = 0
    for area in area_titles:
        if area not in rows_by_area:
            if area in existing_sections:
                output.extend(existing_sections[area])
            continue
        old_section = existing_sections.get(area) or []
        old_count = count_room_rows(old_section[1:])
        area_rows = format_area_rows(area, group_rows_by_community(rows_by_area[area]))
        output.append([area, *[""] * (len(TARGET_HEADERS) - 1)])
        output.extend(area_rows)
        areas_updated.append(area)
        old_rows_removed += max(0, old_count - len(area_rows))
        rows_written += len(area_rows)
    while len(output) < len(existing):
        output.append([""] * len(TARGET_HEADERS))
    return output, {
        "areas_updated": areas_updated,
        "rows_written": rows_written,
        "rows_removed": old_rows_removed,
    }


def ordered_area_titles(area_titles: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(area_titles))
    ordered = [title for title in DEFAULT_TARGET_AREA_TITLES if title in deduped]
    ordered.extend(title for title in deduped if title not in ordered)
    return ordered


def split_existing_area_sections(
    existing: list[list[str]],
    area_titles: list[str],
) -> tuple[dict[str, list[list[str]]], int]:
    preamble_end = find_area_preamble_end(existing, area_titles)
    sections: dict[str, list[list[str]]] = {}
    current_area = ""
    for row in existing[preamble_end:]:
        normalized_row = row[: len(TARGET_HEADERS)]
        area = matched_area_title(normalized_row, area_titles)
        if area:
            current_area = area
            sections[current_area] = [normalized_row]
            continue
        if current_area:
            sections[current_area].append(normalized_row)
    return sections, preamble_end


def find_area_preamble_end(existing: list[list[str]], area_titles: list[str]) -> int:
    for index, row in enumerate(existing):
        if matched_area_title(row, area_titles):
            return index
    for index, row in enumerate(existing):
        first = normalize_text(row[0] if row else "")
        second = normalize_text(row[1] if len(row) > 1 else "")
        if first == normalize_text("区域") and second == normalize_text("小区"):
            return index + 1
    return len(existing)


def matched_area_title(row: list[str], area_titles: list[str]) -> str:
    first_cell = str(row[0] if row else "").strip()
    if not first_cell:
        return ""
    if any(str(cell).strip() for cell in row[1:]):
        return ""
    first_norm = normalize_text(first_cell)
    for title in area_titles:
        title_norm = normalize_text(title)
        if title_norm and (title_norm in first_norm or first_norm in title_norm):
            return title
    return ""


def count_room_rows(rows: list[list[str]]) -> int:
    return sum(1 for row in rows if len(row) > 2 and str(row[2]).strip())


def format_area_rows(area_title: str, rows: list[RegionInventoryRow]) -> list[list[str]]:
    output: list[list[str]] = []
    previous_community = ""
    for index, row in enumerate(rows):
        community = row.community if row.community != previous_community else ""
        previous_community = row.community
        output.append(
            [
                "",
                community,
                row.room_no,
                row.layout,
                row.layout_class,
                row.rent_one,
                row.rent_two,
                row.password,
                row.remark,
            ]
        )
    return output
