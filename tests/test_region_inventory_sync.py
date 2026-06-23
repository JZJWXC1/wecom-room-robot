import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.services.feishu import FeishuClient
from app.services.region_inventory_constants import DRIVE_UPLOAD_SAFE_VIDEO_BYTES
from app.services.region_inventory_media import calculate_target_video_bitrate_kbps
from app.services.region_inventory_sync import (
    ExistingMediaIndex,
    RegionInventoryRow,
    RegionInventorySyncService,
    RegionSyncResult,
    RegionSyncSource,
    build_data_cell_style_updates,
    build_community_merge_repairs,
    build_layout_preserving_updates,
    build_rich_layout_cell_updates,
    build_row_height_repairs,
    build_section_title_repairs,
    build_top_sync_date_update,
    dedupe_rows,
    current_sync_date_text,
    DATA_ROW_HEIGHT_PX,
    extract_docx_media_attachments,
    extract_docx_mentions,
    DATA_CELL_STYLE,
    DATA_FONT_COLOR,
    folder_match_key,
    format_data_row,
    group_rows_by_community,
    media_file_match_key,
    NORMAL_ROOM_BACK_COLOR,
    plan_area_row_deletions,
    plan_area_row_insertions,
    plan_trailing_blank_row_deletion,
    RICH_TEXT_FONT_SIZE,
    resolve_ffmpeg_executable,
    rewrite_target_sheet_values,
    SECTION_TITLE_ROW_HEIGHT_PX,
    should_transcode_mov_upload_fallback,
)


AREA_WANDA = "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧"
AREA_SHIQIAO = "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧"
AREA_DONGXIN = "东新园 杭氧 新天地 成交全部全佣🧧"
AREA_DONGZHAN = "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧"
AREA_WANDA_FOLDER = "拱墅万达 北部软件园 城北万象城"


class FakeRegionSyncClient:
    def __init__(self) -> None:
        self.base_client = FeishuClient()
        self.records_by_table: dict[tuple[str, str], list[dict]] = {}
        self.tables_by_app: dict[str, list[dict]] = {}
        self.target_values: list[list[str]] = []
        self.written_values: list[list[list[str]]] = []
        self.written_ranges: list[tuple[str, list[list]]] = []
        self.folder_files: dict[str, list[dict]] = {}
        self.created_folders: list[tuple[str, str]] = []
        self.uploaded_files: list[tuple[str, str, bytes]] = []
        self.downloads: dict[str, bytes] = {}
        self.unmerged_ranges: list[str] = []
        self.inserted_rows: list[tuple[int, int, str]] = []
        self.deleted_rows: list[tuple[int, int]] = []
        self.merged_ranges: list[str] = []
        self.style_updates: list[dict] = []
        self.row_heights: list[tuple[int, int, int]] = []

    def _record_to_row(self, record: dict) -> dict[str, str]:
        return self.base_client._record_to_row(record)

    def extract_attachments(self, record: dict) -> list[dict]:
        return self.base_client.extract_attachments(record)

    async def list_bitable_records(
        self,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        view_id: str | None = None,
        page_size: int = 500,
    ) -> list[dict]:
        return self.records_by_table[(app_token or "", table_id or "")]

    async def list_bitable_tables(
        self,
        *,
        app_token: str | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        return self.tables_by_app.get(app_token or "", [])

    async def read_spreadsheet_values(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> dict:
        return {"values": self.target_values}

    async def write_spreadsheet_values(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_cell: str = "A1",
        values: list[list],
    ) -> dict:
        self.written_values.append(values)
        self.written_ranges.append((start_cell, values))
        return {"revision": 2}

    async def unmerge_spreadsheet_cells(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        range_name: str,
    ) -> dict:
        self.unmerged_ranges.append(range_name)
        return {}

    async def insert_spreadsheet_rows(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        count: int,
        inherit_style: str = "BEFORE",
    ) -> dict:
        self.inserted_rows.append((start_row, count, inherit_style))
        insert_at = start_row - 1
        self.target_values[insert_at:insert_at] = [[""] * 9 for _ in range(count)]
        return {"revision": 2}

    async def delete_spreadsheet_rows(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        count: int,
    ) -> dict:
        self.deleted_rows.append((start_row, count))
        delete_at = start_row - 1
        del self.target_values[delete_at: delete_at + count]
        return {"revision": 2}

    async def merge_spreadsheet_cells(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        range_name: str,
        merge_type: str = "MERGE_ALL",
    ) -> dict:
        self.merged_ranges.append(range_name)
        return {"revision": 2}

    async def batch_update_spreadsheet_styles(
        self,
        *,
        updates: list[dict],
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> dict:
        self.style_updates.extend(updates)
        return {"revision": 2}

    async def update_spreadsheet_row_height(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        end_row: int,
        height_px: int,
    ) -> dict:
        self.row_heights.append((start_row, end_row, height_px))
        return {"revision": 2}

    async def list_folder_files(self, folder_token: str) -> list[dict]:
        return list(self.folder_files.get(folder_token, []))

    async def create_drive_folder(self, *, parent_folder_token: str, name: str) -> dict:
        token = f"folder_{len(self.created_folders) + 1}"
        self.created_folders.append((parent_folder_token, name))
        self.folder_files.setdefault(parent_folder_token, []).append(
            {"name": name, "type": "folder", "token": token}
        )
        self.folder_files[token] = []
        return {"token": token}

    async def download_attachment(
        self,
        *,
        file_token: str,
        target_path: Path,
        download_url: str = "",
    ) -> Path:
        target_path.write_bytes(self.downloads[file_token])
        return target_path

    async def upload_drive_file(
        self,
        *,
        parent_folder_token: str,
        file_path: Path,
        file_name: str | None = None,
    ) -> dict:
        name = file_name or file_path.name
        content = file_path.read_bytes()
        self.uploaded_files.append((parent_folder_token, name, content))
        self.folder_files.setdefault(parent_folder_token, []).append(
            {"name": name, "type": "file", "token": f"file_{len(self.uploaded_files)}", "size": len(content)}
        )
        return {"file_token": f"file_{len(self.uploaded_files)}"}


class RegionInventorySyncTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_source_rows_and_falls_back_to_note_title(self) -> None:
        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = FakeRegionSyncClient()
        service = RegionInventorySyncService(client=client, sources=[source])

        rows = service._normalize_records(
            [
                {
                    "record_id": "rec_1",
                    "fields": {
                        "房源笔记": "棠润府1-602A",
                        "户型描述": "两室一厅",
                        "押一付一月租金": "2600",
                        "押二付一 月租金": "2400",
                        "看房方式": "160188#",
                        "状态": "在租",
                        "视频": [{"name": "看房视频.mp4", "file_token": "video_token"}],
                    },
                },
                {
                    "record_id": "rec_2",
                    "fields": {
                        "小区": "棠润府",
                        "房号": "1-603A",
                        "状态": "已租",
                    },
                },
            ],
            source,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].community, "棠润府")
        self.assertEqual(rows[0].room_no, "1-602A")
        self.assertEqual(rows[0].layout, "两室一厅")
        self.assertEqual(rows[0].rent_one, "2600")
        self.assertEqual(rows[0].rent_two, "2400")
        self.assertEqual(rows[0].password, "160188#")
        self.assertEqual(rows[0].attachments[0]["file_token"], "video_token")

    def test_dedupes_by_community_and_room_with_latest_record_winning(self) -> None:
        rows = dedupe_rows(
            [
                RegionInventoryRow(AREA_WANDA, "棠润府", "1-602A", rent_one="2500"),
                RegionInventoryRow(AREA_WANDA, "棠润府", "1－602A", rent_one="2600"),
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rent_one, "2600")

    def test_groups_rows_by_community_after_dedupe(self) -> None:
        rows = group_rows_by_community(
            [
                RegionInventoryRow(AREA_WANDA, "A小区", "1-101", rent_one="2500"),
                RegionInventoryRow(AREA_WANDA, "B小区", "2-201"),
                RegionInventoryRow(AREA_WANDA, "A小区", "1-102"),
                RegionInventoryRow(AREA_WANDA, "B小区", "2-202"),
                RegionInventoryRow(AREA_WANDA, "A小区", "1-101", rent_one="2600"),
            ]
        )

        self.assertEqual(
            [(row.community, row.room_no, row.rent_one) for row in rows],
            [
                ("A小区", "1-101", "2600"),
                ("A小区", "1-102", ""),
                ("B小区", "2-201", ""),
                ("B小区", "2-202", ""),
            ],
        )

    def test_current_sync_date_text_uses_month_day_without_leading_zero(self) -> None:
        self.assertEqual(current_sync_date_text(datetime(2026, 6, 19, 8, 0)), "6.19")

    def test_builds_top_sync_date_update_without_touching_contact_numbers(self) -> None:
        existing = [
            ["可芝麻信用免押金+超低价短租", "", "", "", "", "", "", "", ""],
            ["欢迎带看，推荐50%+全佣🧧  6.13", "", "", "", "", "", "", "", ""],
            ["看房联系方式：18758141785/13282125992/19941091943", "", "", "", "", "", "", "", ""],
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
        ]

        update = build_top_sync_date_update(existing, "6.19")

        self.assertEqual(
            update,
            {"start_cell": "A2", "values": [["欢迎带看，推荐50%+全佣🧧  6.19"]]},
        )

    def test_appends_top_sync_date_when_title_has_no_existing_date(self) -> None:
        existing = [
            ["可芝麻信用免押金+超低价短租", "", "", "", "", "", "", "", ""],
            ["欢迎带看，推荐50%+全佣🧧", "", "", "", "", "", "", "", ""],
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
        ]

        update = build_top_sync_date_update(existing, "6.19")

        self.assertEqual(
            update,
            {"start_cell": "A1", "values": [["可芝麻信用免押金+超低价短租  6.19"]]},
        )

    def test_rewrites_target_area_and_preserves_unupdated_area(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            ["", "旧小区", "1-102", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "石桥旧房", "2-201", "", "", "", "", "", ""],
        ]
        rewritten, stats = rewrite_target_sheet_values(
            existing,
            {
                AREA_WANDA: [
                    RegionInventoryRow(
                        AREA_WANDA,
                        "新小区",
                        "3-301",
                        layout="一室一厅",
                        rent_one="2100",
                    )
                ]
            },
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(stats["areas_updated"], [AREA_WANDA])
        self.assertEqual(stats["rows_removed"], 1)
        self.assertEqual(rewritten[2][0], "")
        self.assertEqual(rewritten[2][1], "新小区")
        self.assertEqual(rewritten[3][0], AREA_SHIQIAO)
        self.assertEqual(rewritten[4][1], "石桥旧房")

    def test_rewrite_restores_missing_area_header_in_default_order(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧万达", "1-101", "", "", "", "", "", ""],
            ["", "旧石桥", "2-201", "", "", "", "", "", ""],
        ]
        rewritten, stats = rewrite_target_sheet_values(
            existing,
            {
                AREA_WANDA: [RegionInventoryRow(AREA_WANDA, "新万达", "1-101")],
                AREA_SHIQIAO: [RegionInventoryRow(AREA_SHIQIAO, "新石桥", "2-201")],
            },
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(stats["areas_updated"], [AREA_WANDA, AREA_SHIQIAO])
        self.assertEqual(rewritten[1][0], AREA_WANDA)
        self.assertEqual(rewritten[2][1], "新万达")
        self.assertEqual(rewritten[3][0], AREA_SHIQIAO)
        self.assertEqual(rewritten[4][1], "新石桥")

    def test_layout_preserving_update_stops_at_any_existing_section_title(self) -> None:
        existing = [
            ["顶部标题", "", "", "", "", "", "", "", ""],
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧万达", "1-101", "", "", "", "", "", ""],
            ["文三路 学院路 翠苑 全佣🧧", "", "", "", "", "", "", "", ""],
            ["", "旧文三路", "9-901", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "旧石桥", "2-201", "", "", "", "", "", ""],
        ]

        updates, stats = build_layout_preserving_updates(
            existing,
            {AREA_WANDA: [RegionInventoryRow(AREA_WANDA, "新万达", "3-301")]},
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(stats["rows_written"], 1)
        self.assertEqual(updates, [
            {"start_cell": "C4", "values": [["3-301", "", "", "", "", "", ""]]},
            {"start_cell": "B4", "values": [["新万达"]]},
        ])

    def test_layout_preserving_update_refuses_to_overflow_into_untracked_section(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧万达", "1-101", "", "", "", "", "", ""],
            ["文三路 学院路 翠苑 全佣🧧", "", "", "", "", "", "", "", ""],
            ["", "旧文三路", "9-901", "", "", "", "", "", ""],
        ]

        with self.assertRaisesRegex(RuntimeError, "预留行数不足"):
            build_layout_preserving_updates(
                existing,
                {
                    AREA_WANDA: [
                        RegionInventoryRow(AREA_WANDA, "新万达", "3-301"),
                        RegionInventoryRow(AREA_WANDA, "新万达", "3-302"),
                    ]
                },
                all_area_titles=[AREA_WANDA],
            )

    def test_plans_row_insertions_inside_existing_area_template(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧万达", "1-101", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "旧石桥", "2-201", "", "", "", "", "", ""],
        ]

        plans = plan_area_row_insertions(
            existing,
            {
                AREA_WANDA: [
                    RegionInventoryRow(AREA_WANDA, "新万达", "3-301"),
                    RegionInventoryRow(AREA_WANDA, "新万达", "3-302"),
                ]
            },
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].insert_before_row, 4)
        self.assertEqual(plans[0].count, 1)
        self.assertEqual(plans[0].inherit_style, "BEFORE")

    def test_plans_last_area_row_insertion_before_last_existing_row(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "旧石桥", "2-201", "", "", "", "", "", ""],
        ]

        plans = plan_area_row_insertions(
            existing,
            {
                AREA_SHIQIAO: [
                    RegionInventoryRow(AREA_SHIQIAO, "新石桥", "2-201"),
                    RegionInventoryRow(AREA_SHIQIAO, "新石桥", "2-202"),
                ]
            },
            all_area_titles=[AREA_SHIQIAO],
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].insert_before_row, 3)
        self.assertEqual(plans[0].count, 1)

    def test_plans_area_row_deletions_for_extra_template_tail(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "棠润府", "1-101", "", "", "", "", "", ""],
            ["", "旧小区", "1-102", "", "", "", "", "", ""],
            ["", "旧小区", "", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "石桥旧房", "2-201", "", "", "", "", "", ""],
        ]

        plans = plan_area_row_deletions(
            existing,
            {AREA_WANDA: [RegionInventoryRow(AREA_WANDA, "棠润府", "1-101")]},
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].start_row, 4)
        self.assertEqual(plans[0].count, 2)

    def test_data_styles_use_uniform_background_and_black_font(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧万达", "1-101", "", "", "", "", "", ""],
            ["", "旧万达", "1-102", "", "", "", "", "", ""],
            ["", "旧万达", "1-103", "", "", "", "", "", ""],
        ]

        updates = build_data_cell_style_updates(
            existing,
            {
                AREA_WANDA: [
                    RegionInventoryRow(AREA_WANDA, "棠润府", "1-602A", layout="两室一厅"),
                    RegionInventoryRow(AREA_WANDA, "棠润府", "1-603A", layout="（整）100㎡整租两室两卫"),
                ]
            },
            all_area_titles=[AREA_WANDA],
        )

        self.assertEqual(
            updates,
            [
                {
                    "ranges": ["B3:I5"],
                    "style": DATA_CELL_STYLE,
                }
            ],
        )
        self.assertEqual(updates[0]["style"]["backColor"], NORMAL_ROOM_BACK_COLOR)
        self.assertEqual(updates[0]["style"]["foreColor"], DATA_FONT_COLOR)
        self.assertEqual(updates[0]["style"]["font"]["fontSize"], "16pt/1.5")
        self.assertEqual(updates[0]["style"]["hAlign"], 1)
        self.assertEqual(updates[0]["style"]["vAlign"], 1)
        self.assertEqual(updates[0]["style"]["borderType"], "FULL_BORDER")

    def test_whole_rent_only_adds_layout_prefix(self) -> None:
        normal = RegionInventoryRow(AREA_WANDA, "棠润府", "1-602A", layout="两室一厅")
        whole = RegionInventoryRow(AREA_WANDA, "棠润府", "1-603A", layout="（整）100㎡整租两室两卫")

        self.assertEqual(format_data_row(normal, rich_layout=False)[1], "两室一厅")
        self.assertEqual(format_data_row(whole, rich_layout=False)[1], "（整）100㎡整租两室两卫")
        rich = format_data_row(whole, rich_layout=True)[1]
        self.assertEqual(rich[0]["text"], "（整）")
        self.assertEqual(rich[0]["segmentStyle"]["foreColor"], "#FF0000")
        self.assertEqual(rich[0]["segmentStyle"]["fontSize"], RICH_TEXT_FONT_SIZE)
        self.assertEqual(rich[1]["text"], "100㎡整租两室两卫")
        self.assertEqual(rich[1]["segmentStyle"]["foreColor"], "#000000")
        self.assertEqual(rich[1]["segmentStyle"]["fontSize"], RICH_TEXT_FONT_SIZE)

    def test_builds_rich_layout_updates_only_for_whole_rent_layout_cells(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            ["", "", "1-102", "", "", "", "", "", ""],
        ]
        rows = [
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-101", layout="两室一厅"),
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-102", layout="（整）100㎡整租两室两卫"),
        ]

        updates = build_rich_layout_cell_updates(existing, {AREA_WANDA: rows}, all_area_titles=[AREA_WANDA])

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["start_cell"], "D4")
        rich = updates[0]["values"][0][0]
        self.assertEqual(rich[0]["text"], "（整）")
        self.assertEqual(rich[0]["segmentStyle"]["fontSize"], RICH_TEXT_FONT_SIZE)

    def test_plans_section_title_and_community_merges(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            ["", "", "1-102", "", "", "", "", "", ""],
            ["", "另一个", "2-101", "", "", "", "", "", ""],
        ]
        rows = [
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-101"),
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-102"),
            RegionInventoryRow(AREA_WANDA, "杭行荟", "2-101"),
        ]

        titles = build_section_title_repairs(existing, {AREA_WANDA: rows}, all_area_titles=[AREA_WANDA])
        communities = build_community_merge_repairs(existing, {AREA_WANDA: rows}, all_area_titles=[AREA_WANDA])

        self.assertEqual(titles[0].row_number, 2)
        self.assertEqual(
            [(item.community, item.start_row, item.end_row) for item in communities],
            [("棠润府", 3, 4), ("杭行荟", 5, 5)],
        )

    def test_community_merges_group_repeated_community_blocks(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            ["", "", "1-102", "", "", "", "", "", ""],
            ["", "另一个", "2-101", "", "", "", "", "", ""],
            ["", "", "2-102", "", "", "", "", "", ""],
        ]
        rows = [
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-101"),
            RegionInventoryRow(AREA_WANDA, "杭行荟", "2-101"),
            RegionInventoryRow(AREA_WANDA, "棠润府", "1-102"),
            RegionInventoryRow(AREA_WANDA, "杭行荟", "2-102"),
        ]

        updates, _ = build_layout_preserving_updates(
            existing,
            {AREA_WANDA: rows},
            all_area_titles=[AREA_WANDA],
        )
        communities = build_community_merge_repairs(existing, {AREA_WANDA: rows}, all_area_titles=[AREA_WANDA])

        self.assertIn({"start_cell": "B3", "values": [["棠润府"]]}, updates)
        self.assertIn({"start_cell": "B5", "values": [["杭行荟"]]}, updates)
        self.assertEqual(
            [(item.community, item.start_row, item.end_row) for item in communities],
            [("棠润府", 3, 4), ("杭行荟", 5, 6)],
        )

    def test_plans_uniform_row_heights_for_titles_and_data_blocks(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            ["", "", "1-102", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "旧石桥", "2-201", "", "", "", "", "", ""],
        ]

        repairs = build_row_height_repairs(
            existing,
            {
                AREA_WANDA: [
                    RegionInventoryRow(AREA_WANDA, "棠润府", "1-101"),
                    RegionInventoryRow(AREA_WANDA, "棠润府", "1-102"),
                ],
                AREA_SHIQIAO: [
                    RegionInventoryRow(AREA_SHIQIAO, "石桥铭苑", "2-201"),
                ],
            },
            all_area_titles=[AREA_WANDA, AREA_SHIQIAO],
        )

        self.assertEqual(
            [(item.start_row, item.end_row, item.height_px) for item in repairs],
            [
                (2, 2, SECTION_TITLE_ROW_HEIGHT_PX),
                (3, 4, DATA_ROW_HEIGHT_PX),
                (5, 5, SECTION_TITLE_ROW_HEIGHT_PX),
                (6, 6, DATA_ROW_HEIGHT_PX),
            ],
        )

    def test_trailing_blank_rows_are_not_counted_as_table_area(self) -> None:
        existing = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "棠润府", "1-602A", "一室一厅", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]

        deletion = plan_trailing_blank_row_deletion(
            existing,
            all_area_titles=[AREA_WANDA],
            minimum_rows_by_area={AREA_WANDA: 1},
        )

        self.assertIsNotNone(deletion)
        self.assertEqual((deletion.start_row, deletion.count), (4, 2))

    async def test_sync_dry_run_does_not_write_or_upload_media(self) -> None:
        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = FakeRegionSyncClient()
        client.records_by_table[("app_1", "table_1")] = [
            {
                "record_id": "rec_1",
                "fields": {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "状态": "在租",
                    "视频": [{"name": "看房视频.mp4", "file_token": "video_token"}],
                },
            }
        ]
        client.target_values = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        result = await service.sync(dry_run=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_written"], 1)
        self.assertEqual(client.written_values, [])
        self.assertEqual(client.created_folders, [])
        self.assertEqual(client.uploaded_files, [])

    async def test_single_summary_source_splits_rows_by_area_and_auto_discovers_table(self) -> None:
        source = RegionSyncSource(
            name="四区汇总",
            app_token="app_summary",
            table_id="",
            split_by_area=True,
            area_field="区域",
        )
        client = FakeRegionSyncClient()
        client.tables_by_app["app_summary"] = [{"table_id": "table_summary", "name": "汇总"}]
        client.records_by_table[("app_summary", "table_summary")] = [
            {
                "record_id": "rec_1",
                "fields": {
                    "区域": "万达",
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "状态": "在租",
                },
            },
            {
                "record_id": "rec_2",
                "fields": {
                    "区域": "石桥街道 华丰 石桥 永佳 半山",
                    "小区": "永佳新苑",
                    "房号": "2-703",
                    "状态": "在租",
                },
            },
            {
                "record_id": "rec_3",
                "fields": {
                    "区域": "东新园",
                    "小区": "东方茂",
                    "房号": "T3-1540",
                    "状态": "已租",
                },
            },
        ]
        client.target_values = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
            [AREA_DONGXIN, "", "", "", "", "", "", "", ""],
            ["", "旧东新", "9-901", "", "", "", "", "", ""],
            [AREA_DONGZHAN, "", "", "", "", "", "", "", ""],
            ["", "旧东站", "8-801", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        result = await service.sync(dry_run=True, sync_media=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_read"], 2)
        self.assertEqual(
            result["areas_updated"],
            [AREA_WANDA, AREA_SHIQIAO, AREA_DONGXIN, AREA_DONGZHAN],
        )
        self.assertEqual(result["sheet_rows_deleted"], 2)
        self.assertEqual(client.written_values, [])

    async def test_sync_inserts_missing_template_rows_before_writing(self) -> None:
        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = FakeRegionSyncClient()
        client.records_by_table[("app_1", "table_1")] = [
            {"record_id": "rec_1", "fields": {"小区": "棠润府", "房号": "1-602A", "状态": "在租"}},
            {"record_id": "rec_2", "fields": {"小区": "棠润府", "房号": "1-603A", "状态": "在租"}},
        ]
        client.target_values = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "旧小区", "1-101", "", "", "", "", "", ""],
            [AREA_SHIQIAO, "", "", "", "", "", "", "", ""],
            ["", "石桥旧房", "2-201", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        result = await service.sync(dry_run=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_inserted"], 1)
        self.assertEqual(client.inserted_rows, [(4, 1, "BEFORE")])
        self.assertIn(
            [["1-602A", " ", " ", " ", " ", " ", " "], ["1-603A", " ", " ", " ", " ", " ", " "]],
            client.written_values,
        )
        self.assertEqual(result["style_ranges_updated"], 1)
        data_style_updates = [
            update for update in client.style_updates
            if update["style"].get("backColor") == NORMAL_ROOM_BACK_COLOR
        ]
        self.assertEqual(len(data_style_updates), 1)
        self.assertEqual(data_style_updates[0]["style"]["font"]["fontSize"], "16pt/1.5")
        self.assertEqual(client.row_heights, [(2, 2, SECTION_TITLE_ROW_HEIGHT_PX), (3, 4, DATA_ROW_HEIGHT_PX)])

    async def test_sync_updates_top_date_after_successful_sheet_write(self) -> None:
        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = FakeRegionSyncClient()
        client.records_by_table[("app_1", "table_1")] = [
            {
                "record_id": "rec_1",
                "fields": {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "状态": "在租",
                },
            }
        ]
        client.target_values = [
            ["可芝麻信用免押金+超低价短租", "", "", "", "", "", "", "", ""],
            ["欢迎带看，推荐50%+全佣🧧  6.13", "", "", "", "", "", "", "", ""],
            ["看房联系方式：18758141785/13282125992/19941091943", "", "", "", "", "", "", "", ""],
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        with patch("app.services.region_inventory_sheet_sync.current_sync_date_text", return_value="6.19"):
            result = await service.sync(dry_run=False, sync_media=False)

        self.assertTrue(result["ok"])
        self.assertIn(("C6", [["1-602A", " ", " ", " ", " ", " ", " "]]), client.written_ranges)
        self.assertEqual(
            client.written_ranges[-1],
            ("A2", [["欢迎带看，推荐50%+全佣🧧  6.19"]]),
        )
        self.assertNotIn(
            ("A3", [["看房联系方式：18758141785/13282125992/19941091943"]]),
            client.written_ranges,
        )

    async def test_sync_restores_rich_whole_rent_prefix_without_rewriting_data_block(self) -> None:
        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = FakeRegionSyncClient()
        client.records_by_table[("app_1", "table_1")] = [
            {
                "record_id": "rec_1",
                "fields": {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "户型描述": "（整）100㎡整租两室两卫",
                    "状态": "在租",
                },
            },
        ]
        client.target_values = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        result = await service.sync(dry_run=False, sync_media=False)

        self.assertTrue(result["ok"])
        self.assertIn(
            ("C3", [["1-602A", "（整）100㎡整租两室两卫", " ", " ", " ", " ", " "]]),
            client.written_ranges,
        )
        self.assertEqual(client.written_ranges[-1][0], "D3")
        rich = client.written_ranges[-1][1][0][0]
        self.assertEqual(rich[0]["text"], "（整）")
        self.assertEqual(rich[0]["segmentStyle"]["foreColor"], "#FF0000")
        self.assertEqual(rich[0]["segmentStyle"]["fontSize"], RICH_TEXT_FONT_SIZE)
        self.assertEqual(rich[1]["text"], "100㎡整租两室两卫")

    async def test_media_failure_does_not_block_sheet_write(self) -> None:
        class MediaFailingClient(FakeRegionSyncClient):
            async def list_folder_files(self, folder_token: str) -> list[dict]:
                raise RuntimeError("drive unavailable")

        source = RegionSyncSource(
            name="万达",
            app_token="app_1",
            table_id="table_1",
            target_area_title=AREA_WANDA,
        )
        client = MediaFailingClient()
        client.records_by_table[("app_1", "table_1")] = [
            {
                "record_id": "rec_1",
                "fields": {
                    "小区": "棠润府",
                    "房号": "1-602A",
                    "状态": "在租",
                },
            }
        ]
        client.target_values = [
            ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            [AREA_WANDA, "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]
        service = RegionInventorySyncService(client=client, sources=[source])

        result = await service.sync(dry_run=False)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(client.written_values), 1)
        self.assertEqual(result["media_failed"][0]["room"], AREA_WANDA)
        self.assertIn("drive unavailable", result["media_failed"][0]["error"])

    async def test_sync_area_media_creates_room_folder_and_skips_duplicate(self) -> None:
        previous_folder_token = settings.feishu_region_sync_target_drive_folder_token
        try:
            settings.feishu_region_sync_target_drive_folder_token = "root"
            client = FakeRegionSyncClient()
            client.folder_files["root"] = [
                {"name": AREA_WANDA_FOLDER, "type": "folder", "token": "area_old"}
            ]
            client.downloads["image_token"] = b"image-bytes"
            service = RegionInventorySyncService(client=client, sources=[])
            row = RegionInventoryRow(
                AREA_WANDA,
                "棠润府",
                "1-602A",
                attachments=[{"name": "客厅.jpg", "file_token": "image_token"}],
            )
            result = RegionSyncResult(ok=True, dry_run=False)

            await service._sync_area_media(AREA_WANDA, [row], result)
            await service._sync_area_media(AREA_WANDA, [row], result)

            self.assertEqual(result.media_uploaded, 1)
            self.assertEqual(result.media_skipped, 1)
            self.assertNotIn(("root", AREA_WANDA), client.created_folders)
            self.assertIn(("area_old", "棠润府1-602A"), client.created_folders)
            self.assertEqual(client.uploaded_files[0][1], "客厅.jpg")
            self.assertEqual(client.uploaded_files[0][2], b"image-bytes")
        finally:
            settings.feishu_region_sync_target_drive_folder_token = previous_folder_token

    async def test_room_folder_matching_reuses_existing_folder_with_special_spaces(self) -> None:
        client = FakeRegionSyncClient()
        client.folder_files["area_old"] = [
            {"name": "兴业杨家府\u00a03-601", "type": "folder", "token": "room_old"}
        ]
        service = RegionInventorySyncService(client=client, sources=[])

        token = await service._ensure_child_folder("area_old", "兴业杨家府3-601")

        self.assertEqual(token, "room_old")
        self.assertEqual(client.created_folders, [])
        self.assertEqual(folder_match_key("兴业杨家府\u00a03-601"), folder_match_key("兴业杨家府3-601"))

    def test_existing_media_index_matches_normalized_file_name_and_size(self) -> None:
        index = ExistingMediaIndex.from_drive_items(
            [{"name": "石桥铭苑 6-1102-图片12.jpg", "size": 5}]
        )

        self.assertTrue(index.has("石桥铭苑6-1102-图片12.jpg", 5))
        self.assertFalse(index.has("石桥铭苑6-1102-图片12.jpg", 6))
        self.assertTrue(index.has_name("石桥铭苑6-1102-图片12.jpg"))
        self.assertEqual(
            media_file_match_key("石桥铭苑 6-1102-图片12.jpg"),
            media_file_match_key("石桥铭苑6-1102-图片12.jpg"),
        )

    async def test_existing_converted_mp4_skips_source_mov(self) -> None:
        client = FakeRegionSyncClient()
        client.downloads["mov_token"] = b"mov-bytes"
        service = RegionInventorySyncService(client=client, sources=[])
        result = RegionSyncResult(ok=True, dry_run=False)
        existing = ExistingMediaIndex.from_drive_items([{"name": "copy_abc.mp4", "size": 123}])

        with tempfile.TemporaryDirectory() as directory:
            await service._sync_attachment(
                {"name": "copy_abc.mov", "file_token": "mov_token"},
                "room_token",
                existing,
                Path(directory),
                RegionInventoryRow(AREA_WANDA, "皋塘运都", "9-2-402B"),
                result,
            )

        self.assertEqual(result.media_skipped, 1)
        self.assertEqual(result.media_uploaded, 0)
        self.assertEqual(client.uploaded_files, [])

    async def test_mov_upload_params_error_transcodes_to_mp4_and_uploads(self) -> None:
        class MovFailingClient(FakeRegionSyncClient):
            async def upload_drive_file(
                self,
                *,
                parent_folder_token: str,
                file_path: Path,
                file_name: str | None = None,
            ) -> dict:
                if str(file_name or file_path.name).lower().endswith(".mov"):
                    raise RuntimeError("Feishu upload failed: params error")
                return await super().upload_drive_file(
                    parent_folder_token=parent_folder_token,
                    file_path=file_path,
                    file_name=file_name,
                )

        def fake_transcode(source: Path, target: Path, *, timeout: int = 240, max_bytes: int = DRIVE_UPLOAD_SAFE_VIDEO_BYTES) -> Path:
            target.write_bytes(b"mp4-bytes")
            return target

        client = MovFailingClient()
        client.downloads["mov_token"] = b"mov-bytes"
        service = RegionInventorySyncService(client=client, sources=[])
        result = RegionSyncResult(ok=True, dry_run=False)
        row = RegionInventoryRow(AREA_WANDA, "皋塘运都", "9-2-402B")

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.services.region_inventory_sync.transcode_video_to_mp4",
            fake_transcode,
        ):
            await service._sync_attachment(
                {"name": "copy.mov", "file_token": "mov_token"},
                "room_token",
                ExistingMediaIndex(),
                Path(directory),
                row,
                result,
            )

        self.assertEqual(result.media_uploaded, 1)
        self.assertEqual(result.media_transcoded, 1)
        self.assertEqual(result.media_failed, [])
        self.assertEqual(client.uploaded_files[0][1], "copy.mp4")
        self.assertEqual(client.uploaded_files[0][2], b"mp4-bytes")
        self.assertTrue(should_transcode_mov_upload_fallback(".mov", RuntimeError("params error")))
        self.assertTrue(should_transcode_mov_upload_fallback(".mp4", RuntimeError("params error")))

    async def test_large_mp4_transcodes_before_uploading_to_drive(self) -> None:
        def fake_transcode(source: Path, target: Path, *, timeout: int = 240, max_bytes: int = DRIVE_UPLOAD_SAFE_VIDEO_BYTES) -> Path:
            self.assertGreater(source.stat().st_size, DRIVE_UPLOAD_SAFE_VIDEO_BYTES)
            target.write_bytes(b"compressed-mp4")
            return target

        client = FakeRegionSyncClient()
        client.downloads["mp4_token"] = b"x" * (DRIVE_UPLOAD_SAFE_VIDEO_BYTES + 1)
        service = RegionInventorySyncService(client=client, sources=[])
        result = RegionSyncResult(ok=True, dry_run=False)

        with tempfile.TemporaryDirectory() as directory, patch(
            "app.services.region_inventory_sync.transcode_video_to_mp4",
            fake_transcode,
        ):
            await service._sync_attachment(
                {"name": "lv_0_20260616121852.mp4", "file_token": "mp4_token"},
                "room_token",
                ExistingMediaIndex(),
                Path(directory),
                RegionInventoryRow(AREA_SHIQIAO, "兴业杨家府", "10-1-1205"),
                result,
            )

        self.assertEqual(result.media_uploaded, 1)
        self.assertEqual(result.media_transcoded, 1)
        self.assertEqual(result.media_failed, [])
        self.assertEqual(client.uploaded_files[0][1], "lv_0_20260616121852.mp4")
        self.assertEqual(client.uploaded_files[0][2], b"compressed-mp4")

    def test_video_bitrate_targets_near_twenty_mb_upload_limit(self) -> None:
        duration_seconds = 60
        video_kbps = calculate_target_video_bitrate_kbps(
            duration_seconds=duration_seconds,
            max_bytes=DRIVE_UPLOAD_SAFE_VIDEO_BYTES,
        )
        estimated_bytes = int(((video_kbps + 96) * 1000 / 8) * duration_seconds) + 256 * 1024

        self.assertLessEqual(estimated_bytes, DRIVE_UPLOAD_SAFE_VIDEO_BYTES)
        self.assertGreaterEqual(estimated_bytes, int(DRIVE_UPLOAD_SAFE_VIDEO_BYTES * 0.95))

    def test_resolves_ffmpeg_from_optional_imageio_package(self) -> None:
        class FakeImageioFfmpeg:
            @staticmethod
            def get_ffmpeg_exe() -> str:
                return str(fake_ffmpeg)

        with tempfile.TemporaryDirectory() as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg-custom.exe"
            fake_ffmpeg.write_bytes(b"binary")
            with patch("app.services.region_inventory_sync.shutil.which", return_value=""), patch.dict(
                "sys.modules",
                {"imageio_ffmpeg": FakeImageioFfmpeg},
            ):
                resolved = resolve_ffmpeg_executable()

        self.assertEqual(resolved, str(fake_ffmpeg))

    async def test_sync_area_media_does_not_create_missing_area_folder(self) -> None:
        previous_folder_token = settings.feishu_region_sync_target_drive_folder_token
        try:
            settings.feishu_region_sync_target_drive_folder_token = "root"
            client = FakeRegionSyncClient()
            client.folder_files["root"] = []
            service = RegionInventorySyncService(client=client, sources=[])
            row = RegionInventoryRow(
                AREA_WANDA,
                "棠润府",
                "1-602A",
                attachments=[{"name": "客厅.jpg", "file_token": "image_token"}],
            )
            result = RegionSyncResult(ok=True, dry_run=False)

            with self.assertRaisesRegex(RuntimeError, "原有区域文件夹"):
                await service._sync_area_media(AREA_WANDA, [row], result)

            self.assertEqual(client.created_folders, [])
        finally:
            settings.feishu_region_sync_target_drive_folder_token = previous_folder_token

    def test_extracts_docx_mentions_and_docx_media(self) -> None:
        record = {
            "fields": {
                "房源笔记": [
                    {
                        "type": "mention",
                        "mentionType": "Docx",
                        "token": "doc_token",
                        "text": "棠润府1-602A",
                        "link": "https://example.feishu.cn/docx/doc_token",
                    }
                ]
            }
        }
        blocks = [
            {"block_type": 27, "image": {"token": "image_token", "mime_type": "image/jpeg"}},
            {"block_type": 23, "file": {"file_token": "video_token", "name": "看房视频.mp4"}},
        ]

        self.assertEqual(
            extract_docx_mentions(record),
            [
                {
                    "token": "doc_token",
                    "title": "棠润府1-602A",
                    "url": "https://example.feishu.cn/docx/doc_token",
                }
            ],
        )
        self.assertEqual(
            extract_docx_media_attachments(blocks, "棠润府1-602A"),
            [
                {"name": "棠润府1-602A-图片01.jpg", "file_token": "image_token"},
                {"name": "看房视频.mp4", "file_token": "video_token"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
