from __future__ import annotations

from dataclasses import replace
import json
import shutil
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.feishu import FeishuClient
from app.services.region_inventory_constants import (
    AREA_LABEL_STYLE,
    DATA_CELL_STYLE,
    DATA_FONT_COLOR,
    DATA_ROW_HEIGHT_PX,
    DEFAULT_TARGET_AREA_TITLES,
    NORMAL_ROOM_BACK_COLOR,
    RICH_TEXT_FONT_SIZE,
    SECTION_TITLE_STYLE,
    SECTION_TITLE_ROW_HEIGHT_PX,
    TARGET_HEADERS,
)
from app.services.region_inventory_media import (
    brief_error,
    extract_docx_media_attachments,
    extract_docx_mentions,
    resolve_ffmpeg_executable,
    should_transcode_mov_upload_fallback,
    transcode_video_to_mp4,
)
from app.services.region_inventory_media_sync import RegionInventoryMediaSyncer
from app.services.region_inventory_models import (
    AreaLabelRepair,
    AreaRowInsertion,
    CommunityMergeRepair,
    ExistingMediaIndex,
    RegionInventoryRow,
    RegionSyncResult,
    RegionSyncSource,
    RowHeightRepair,
    SectionTitleRepair,
    SheetRowDeletion,
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
    column_letter,
    count_room_rows,
    current_sync_date_text,
    dedupe_rows,
    find_area_preamble_end,
    find_area_title_rows,
    find_existing_area_section_limits,
    find_existing_area_sections,
    find_header_row_number,
    format_area_rows,
    format_community_column,
    format_data_row,
    format_layout_cell,
    format_layout_text,
    group_rows_by_community,
    is_section_title_row,
    is_whole_rent_row,
    matched_area_title,
    natural_room_sort_key,
    normalize_matrix,
    ordered_area_titles,
    plan_area_row_deletions,
    plan_area_row_insertions,
    plan_trailing_blank_row_deletion,
    rewrite_target_sheet_values,
    simulate_row_deletion,
    simulate_row_insertions,
    split_existing_area_sections,
    spreadsheet_write_matrix,
    trim_empty_section_end,
)
from app.services.region_inventory_sheet_sync import RegionInventorySheetSyncer
from app.services.region_inventory_source import normalize_region_records
from app.services.region_inventory_utils import (
    folder_match_key,
    media_file_match_key,
    normalize_key,
    normalize_room_no,
)


class RegionInventorySyncService:
    def __init__(
        self,
        *,
        client: FeishuClient | None = None,
        sources: list[RegionSyncSource] | None = None,
    ) -> None:
        self.client = client or FeishuClient()
        self.sources = sources if sources is not None else load_region_sync_sources()
        self._folder_cache: dict[tuple[str, str], str] = {}
        self.media_syncer = RegionInventoryMediaSyncer(
            client=self.client,
            folder_cache=self._folder_cache,
        )
        self.sheet_syncer = RegionInventorySheetSyncer(client=self.client)

    async def sync(self, *, dry_run: bool = False, sync_media: bool = True) -> dict[str, Any]:
        result = RegionSyncResult(ok=True, dry_run=dry_run, source_count=len(self.sources))
        rows_by_area: dict[str, list[RegionInventoryRow]] = {}
        failed_areas: set[str] = set()

        for source in self.sources:
            source_area_titles = self._source_area_titles(source)
            try:
                source = await self._resolve_source_table(source)
                source.validate()
                records = await self.client.list_bitable_records(
                    app_token=source.app_token,
                    table_id=source.table_id,
                    view_id=source.view_id,
                )
                rows = self._normalize_records(records, source)
                for area_title in source_area_titles:
                    rows_by_area.setdefault(area_title, [])
                for row in rows:
                    rows_by_area.setdefault(row.area_title, []).append(row)
                result.rows_read += len(rows)
            except Exception as exc:
                result.ok = False
                failed_areas.update(source_area_titles or [source.area_title])
                result.source_failures.append(
                    {"source": source.name or source.area_title, "error": str(exc)}
                )
                continue

            if sync_media and not dry_run:
                try:
                    rows_by_current_area: dict[str, list[RegionInventoryRow]] = {}
                    for row in rows:
                        rows_by_current_area.setdefault(row.area_title, []).append(row)
                    for area_title, area_rows in rows_by_current_area.items():
                        await self._sync_area_media(area_title, area_rows, result)
                except Exception as exc:
                    result.media_failed.append(
                        {
                            "room": source.area_title if not source.split_by_area else source.name or ",".join(source_area_titles),
                            "file": "*",
                            "error": str(exc),
                        }
                    )

        await self.sheet_syncer.sync_target_sheet(
            rows_by_area=rows_by_area,
            failed_areas=failed_areas,
            area_titles=ordered_area_titles(self._all_source_area_titles()),
            dry_run=dry_run,
            result=result,
        )

        return result.to_dict()

    async def _repair_row_heights(self, repairs: list[RowHeightRepair]) -> None:
        await self.sheet_syncer.repair_row_heights(repairs)

    async def _repair_area_label_ranges(self, repairs: list[AreaLabelRepair]) -> None:
        await self.sheet_syncer.repair_area_label_ranges(repairs)

    async def _repair_section_title_ranges(self, repairs: list[SectionTitleRepair]) -> None:
        await self.sheet_syncer.repair_section_title_ranges(repairs)

    async def _repair_community_ranges(self, repairs: list[CommunityMergeRepair]) -> None:
        await self.sheet_syncer.repair_community_ranges(repairs)

    async def _write_area_headers(self, rewritten: list[list[str]], area_titles: list[str]) -> None:
        await self.sheet_syncer.write_area_headers(rewritten, area_titles)

    def _normalize_records(
        self,
        records: list[dict[str, Any]],
        source: RegionSyncSource,
    ) -> list[RegionInventoryRow]:
        return normalize_region_records(
            records,
            source,
            record_to_row=self.client._record_to_row,
            extract_attachments=self.client.extract_attachments,
        )

    async def _resolve_source_table(self, source: RegionSyncSource) -> RegionSyncSource:
        if source.table_id:
            return source
        list_tables = getattr(self.client, "list_bitable_tables", None)
        if list_tables is None:
            raise ValueError("source table_id is empty and Feishu client cannot list bitable tables")
        tables = await list_tables(app_token=source.app_token)
        candidates = [table for table in tables if not table.get("is_deleted")] or tables
        if not candidates:
            raise ValueError(f"源多维表没有可用数据表：{source.app_token}")
        selected = candidates[0]
        table_id = str(selected.get("table_id") or selected.get("id") or "").strip()
        if not table_id:
            raise ValueError(f"源多维表数据表 ID 为空：{selected}")
        return replace(source, table_id=table_id)

    def _source_area_titles(self, source: RegionSyncSource) -> list[str]:
        if not source.split_by_area:
            return [source.area_title] if source.area_title else []
        mapped = [target for target in source.area_title_map.values() if target]
        return ordered_area_titles([*DEFAULT_TARGET_AREA_TITLES, *mapped])

    def _all_source_area_titles(self) -> list[str]:
        area_titles: list[str] = []
        for source in self.sources:
            area_titles.extend(self._source_area_titles(source))
        return area_titles

    def _refresh_media_syncer_tools(self) -> None:
        self.media_syncer.brief_error = brief_error
        self.media_syncer.extract_docx_media_attachments = extract_docx_media_attachments
        self.media_syncer.should_transcode_mov_upload_fallback = should_transcode_mov_upload_fallback
        self.media_syncer.transcode_video_to_mp4 = transcode_video_to_mp4

    async def _sync_area_media(
        self,
        area_title: str,
        rows: list[RegionInventoryRow],
        result: RegionSyncResult,
    ) -> None:
        self._refresh_media_syncer_tools()
        await self.media_syncer.sync_area_media(
            area_title,
            rows,
            result,
        )

    async def _find_existing_area_folder(self, parent_token: str, area_title: str) -> str:
        return await self.media_syncer.find_existing_area_folder(parent_token, area_title)

    async def _ensure_child_folder(self, parent_token: str, folder_name: str) -> str:
        return await self.media_syncer.ensure_child_folder(parent_token, folder_name)

    async def _sync_attachment(
        self,
        attachment: dict[str, Any],
        room_folder_token: str,
        existing: ExistingMediaIndex,
        work_dir: Path,
        row: RegionInventoryRow,
        result: RegionSyncResult,
    ) -> None:
        self._refresh_media_syncer_tools()
        await self.media_syncer.sync_attachment(
            attachment,
            room_folder_token,
            existing,
            work_dir,
            row,
            result,
        )

    async def _upload_media_file(
        self,
        *,
        room_folder_token: str,
        file_path: Path,
        file_name: str,
        existing: ExistingMediaIndex,
        result: RegionSyncResult,
    ) -> None:
        await self.media_syncer.upload_media_file(
            room_folder_token=room_folder_token,
            file_path=file_path,
            file_name=file_name,
            existing=existing,
            result=result,
        )

    async def _sync_note_document_media(
        self,
        document: dict[str, str],
        room_folder_token: str,
        existing: ExistingMediaIndex,
        work_dir: Path,
        row: RegionInventoryRow,
        result: RegionSyncResult,
    ) -> None:
        self._refresh_media_syncer_tools()
        await self.media_syncer.sync_note_document_media(
            document,
            room_folder_token,
            existing,
            work_dir,
            row,
            result,
        )


def load_region_sync_sources(config_text: str | None = None) -> list[RegionSyncSource]:
    raw = config_text if config_text is not None else settings.feishu_region_sync_sources
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("FEISHU_REGION_SYNC_SOURCES must be a JSON array")
    return [RegionSyncSource.from_dict(item) for item in data]
