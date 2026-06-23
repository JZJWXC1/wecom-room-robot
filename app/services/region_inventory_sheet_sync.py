from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.region_inventory_constants import (
    AREA_LABEL_STYLE,
    SECTION_TITLE_STYLE,
    TARGET_HEADERS,
)
from app.services.region_inventory_models import (
    AreaLabelRepair,
    CommunityMergeRepair,
    RegionInventoryRow,
    RegionSyncResult,
    RowHeightRepair,
    SectionTitleRepair,
)
from app.services.region_inventory_sheet import (
    build_area_label_repairs,
    build_community_merge_repairs,
    build_data_cell_style_updates,
    build_layout_preserving_updates,
    build_rich_layout_cell_updates,
    build_row_height_repairs,
    build_section_title_repairs,
    build_top_sync_date_update,
    current_sync_date_text,
    group_rows_by_community,
    matched_area_title,
    normalize_matrix,
    plan_area_row_deletions,
    plan_area_row_insertions,
    plan_trailing_blank_row_deletion,
    simulate_row_deletion,
    simulate_row_insertions,
    spreadsheet_write_matrix,
)


class RegionInventorySheetSyncer:
    def __init__(self, *, client: Any) -> None:
        self.client = client

    async def sync_target_sheet(
        self,
        *,
        rows_by_area: dict[str, list[RegionInventoryRow]],
        failed_areas: set[str],
        area_titles: list[str],
        dry_run: bool,
        result: RegionSyncResult,
    ) -> None:
        writable_rows_by_area = {
            area: group_rows_by_community(rows)
            for area, rows in rows_by_area.items()
            if area not in failed_areas
        }
        if not writable_rows_by_area:
            return

        sheet_values = await self.client.read_spreadsheet_values(
            spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
            sheet_id=settings.feishu_region_sync_target_sheet_id or None,
        )
        existing = normalize_matrix(sheet_values.get("values") or [], width=len(TARGET_HEADERS))
        required_rows_by_area = {
            area: len(rows)
            for area, rows in writable_rows_by_area.items()
        }
        trailing_deletion = plan_trailing_blank_row_deletion(
            existing,
            all_area_titles=area_titles,
            minimum_rows_by_area=required_rows_by_area,
        )
        result.sheet_rows_deleted = trailing_deletion.count if trailing_deletion else 0
        if trailing_deletion:
            if dry_run:
                existing = simulate_row_deletion(existing, trailing_deletion)
            else:
                await self.client.delete_spreadsheet_rows(
                    spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                    sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                    start_row=trailing_deletion.start_row,
                    count=trailing_deletion.count,
                )
                sheet_values = await self.client.read_spreadsheet_values(
                    spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                    sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                )
                existing = normalize_matrix(sheet_values.get("values") or [], width=len(TARGET_HEADERS))

        area_row_deletions = plan_area_row_deletions(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.sheet_rows_deleted += sum(plan.count for plan in area_row_deletions)
        if area_row_deletions:
            if dry_run:
                for deletion in sorted(
                    area_row_deletions,
                    key=lambda item: item.start_row,
                    reverse=True,
                ):
                    existing = simulate_row_deletion(existing, deletion)
            else:
                for deletion in sorted(
                    area_row_deletions,
                    key=lambda item: item.start_row,
                    reverse=True,
                ):
                    await self.client.delete_spreadsheet_rows(
                        spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                        sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                        start_row=deletion.start_row,
                        count=deletion.count,
                    )
                sheet_values = await self.client.read_spreadsheet_values(
                    spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                    sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                )
                existing = normalize_matrix(sheet_values.get("values") or [], width=len(TARGET_HEADERS))

        row_insertions = plan_area_row_insertions(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.rows_inserted = sum(plan.count for plan in row_insertions)
        if row_insertions:
            if dry_run:
                existing = simulate_row_insertions(existing, row_insertions)
            else:
                for plan in sorted(
                    row_insertions,
                    key=lambda item: item.insert_before_row,
                    reverse=True,
                ):
                    await self.client.insert_spreadsheet_rows(
                        spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                        sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                        start_row=plan.insert_before_row,
                        count=plan.count,
                        inherit_style=plan.inherit_style,
                    )
                sheet_values = await self.client.read_spreadsheet_values(
                    spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                    sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                )
                existing = normalize_matrix(sheet_values.get("values") or [], width=len(TARGET_HEADERS))

        updates, stats = build_layout_preserving_updates(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
            rich_layout=False,
        )
        result.rows_written = stats["rows_written"]
        result.rows_removed = stats["rows_removed"]
        result.areas_updated = stats["areas_updated"]

        style_updates = build_data_cell_style_updates(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.style_ranges_updated = sum(len(update["ranges"]) for update in style_updates)

        area_label_repairs = build_area_label_repairs(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.area_label_ranges_repaired = len(area_label_repairs)

        section_title_repairs = build_section_title_repairs(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.section_title_ranges_repaired = len(section_title_repairs)

        community_merge_repairs = build_community_merge_repairs(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.community_ranges_repaired = len(community_merge_repairs)

        row_height_repairs = build_row_height_repairs(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        result.row_height_ranges_repaired = len(row_height_repairs)

        rich_layout_cell_updates = build_rich_layout_cell_updates(
            existing,
            writable_rows_by_area,
            all_area_titles=area_titles,
        )
        top_sync_date_update = build_top_sync_date_update(existing, current_sync_date_text())
        if dry_run:
            return

        for update in updates:
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=update["start_cell"],
                values=spreadsheet_write_matrix(update["values"]),
            )
        if area_label_repairs:
            await self.repair_area_label_ranges(area_label_repairs)
        if community_merge_repairs:
            await self.repair_community_ranges(community_merge_repairs)
        if section_title_repairs:
            await self.repair_section_title_ranges(section_title_repairs)
        if style_updates:
            await self.client.batch_update_spreadsheet_styles(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                updates=style_updates,
            )
        if row_height_repairs:
            await self.repair_row_heights(row_height_repairs)
        if rich_layout_cell_updates and not result.rich_text_fallback:
            try:
                for update in rich_layout_cell_updates:
                    await self.client.write_spreadsheet_values(
                        spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                        sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                        start_cell=update["start_cell"],
                        values=spreadsheet_write_matrix(update["values"]),
                    )
            except Exception as exc:
                result.rich_text_fallback = str(exc)
        if top_sync_date_update:
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=top_sync_date_update["start_cell"],
                values=top_sync_date_update["values"],
            )

    async def repair_row_heights(self, repairs: list[RowHeightRepair]) -> None:
        for repair in repairs:
            await self.client.update_spreadsheet_row_height(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_row=repair.start_row,
                end_row=repair.end_row,
                height_px=repair.height_px,
            )

    async def repair_area_label_ranges(self, repairs: list[AreaLabelRepair]) -> None:
        first_row = min(repair.start_row for repair in repairs)
        last_row = max(repair.end_row for repair in repairs)
        await self.client.unmerge_spreadsheet_cells(
            spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
            sheet_id=settings.feishu_region_sync_target_sheet_id or None,
            range_name=f"A{first_row}:A{last_row}",
        )
        for repair in repairs:
            await self.client.merge_spreadsheet_cells(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                range_name=f"A{repair.start_row}:A{repair.end_row}",
            )
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=f"A{repair.start_row}",
                values=[[repair.label]],
            )
        await self.client.batch_update_spreadsheet_styles(
            spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
            sheet_id=settings.feishu_region_sync_target_sheet_id or None,
            updates=[
                {
                    "ranges": [f"A{repair.start_row}:A{repair.end_row}" for repair in repairs],
                    "style": dict(AREA_LABEL_STYLE),
                }
            ],
        )

    async def repair_section_title_ranges(self, repairs: list[SectionTitleRepair]) -> None:
        for repair in repairs:
            range_name = f"A{repair.row_number}:I{repair.row_number}"
            await self.client.unmerge_spreadsheet_cells(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                range_name=range_name,
            )
            await self.client.merge_spreadsheet_cells(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                range_name=range_name,
            )
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=f"A{repair.row_number}",
                values=[[repair.area_title]],
            )
        await self.client.batch_update_spreadsheet_styles(
            spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
            sheet_id=settings.feishu_region_sync_target_sheet_id or None,
            updates=[
                {
                    "ranges": [f"A{repair.row_number}:I{repair.row_number}" for repair in repairs],
                    "style": dict(SECTION_TITLE_STYLE),
                }
            ],
        )

    async def repair_community_ranges(self, repairs: list[CommunityMergeRepair]) -> None:
        for repair in repairs:
            await self.client.unmerge_spreadsheet_cells(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                range_name=f"B{repair.start_row}:B{repair.end_row}",
            )
            if repair.end_row > repair.start_row:
                await self.client.merge_spreadsheet_cells(
                    spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                    sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                    range_name=f"B{repair.start_row}:B{repair.end_row}",
                )
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=f"B{repair.start_row}",
                values=[[repair.community]],
            )

    async def write_area_headers(self, rewritten: list[list[str]], area_titles: list[str]) -> None:
        for row_number, row in enumerate(rewritten, start=1):
            area = matched_area_title(row, area_titles)
            if not area:
                continue
            await self.client.write_spreadsheet_values(
                spreadsheet_token=settings.feishu_region_sync_target_spreadsheet_token,
                sheet_id=settings.feishu_region_sync_target_sheet_id or None,
                start_cell=f"A{row_number}",
                values=[[area]],
            )
