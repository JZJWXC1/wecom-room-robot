from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.region_inventory_utils import media_file_match_key, normalize_key, safe_name


@dataclass(frozen=True)
class RegionSyncSource:
    name: str
    app_token: str
    table_id: str
    view_id: str = ""
    region: str = ""
    target_area_title: str = ""
    split_by_area: bool = False
    area_field: str = ""
    area_title_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegionSyncSource":
        return cls(
            name=str(data.get("name") or data.get("区域名") or "").strip(),
            app_token=str(data.get("app_token") or data.get("appToken") or "").strip(),
            table_id=str(data.get("table_id") or data.get("tableId") or "").strip(),
            view_id=str(data.get("view_id") or data.get("viewId") or "").strip(),
            region=str(data.get("region") or data.get("区域") or "").strip(),
            target_area_title=str(
                data.get("target_area_title") or data.get("targetAreaTitle") or data.get("目标区域标题") or ""
            ).strip(),
            split_by_area=bool(
                data.get("split_by_area")
                or data.get("splitByArea")
                or data.get("按区域拆分")
                or data.get("单表按区域拆分")
            ),
            area_field=str(
                data.get("area_field") or data.get("areaField") or data.get("区域字段") or ""
            ).strip(),
            area_title_map={
                str(key).strip(): str(value).strip()
                for key, value in (
                    data.get("area_title_map")
                    or data.get("areaTitleMap")
                    or data.get("区域映射")
                    or {}
                ).items()
                if str(key).strip() and str(value).strip()
            },
        )

    @property
    def area_title(self) -> str:
        return self.target_area_title or self.region or self.name

    def validate(self) -> None:
        if not self.app_token or (not self.table_id and not self.split_by_area) or (not self.split_by_area and not self.area_title):
            missing = []
            if not self.app_token:
                missing.append("app_token")
            if not self.table_id and not self.split_by_area:
                missing.append("table_id")
            if not self.split_by_area and not self.area_title:
                missing.append("target_area_title")
            source_name = self.name or self.region or "unnamed"
            raise ValueError(
                f"Invalid Feishu region sync source {source_name}; missing {', '.join(missing)}"
            )


@dataclass
class RegionInventoryRow:
    area_title: str
    community: str
    room_no: str
    layout: str = ""
    layout_class: str = ""
    rent_one: str = ""
    rent_two: str = ""
    password: str = ""
    remark: str = ""
    record_id: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    note_links: list[str] = field(default_factory=list)
    note_documents: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize_key(self.community, self.room_no)

    @property
    def folder_name(self) -> str:
        return safe_name(f"{self.community}{self.room_no}") or self.record_id or "unnamed"


@dataclass(frozen=True)
class AreaRowInsertion:
    area_title: str
    insert_before_row: int
    count: int
    inherit_style: str = "BEFORE"


@dataclass(frozen=True)
class SheetRowDeletion:
    start_row: int
    count: int


@dataclass(frozen=True)
class AreaLabelRepair:
    area_title: str
    start_row: int
    end_row: int
    label: str


@dataclass(frozen=True)
class SectionTitleRepair:
    area_title: str
    row_number: int


@dataclass(frozen=True)
class CommunityMergeRepair:
    community: str
    start_row: int
    end_row: int


@dataclass(frozen=True)
class RowHeightRepair:
    start_row: int
    end_row: int
    height_px: int


@dataclass
class ExistingMediaIndex:
    files_by_name: dict[str, int] = field(default_factory=dict)
    sizes_by_key: dict[str, set[int]] = field(default_factory=dict)

    @classmethod
    def from_drive_items(cls, items: list[dict[str, Any]]) -> "ExistingMediaIndex":
        index = cls()
        for item in items:
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            index.add(name, int(item.get("size") or 0))
        return index

    def add(self, name: str, size: int) -> None:
        self.files_by_name[name] = size
        self.sizes_by_key.setdefault(media_file_match_key(name), set()).add(size)

    def has(self, name: str, size: int) -> bool:
        exact_size = self.files_by_name.get(name)
        if exact_size is not None and (exact_size == 0 or exact_size == size):
            return True
        return any(existing_size in {0, size} for existing_size in self.sizes_by_key.get(media_file_match_key(name), set()))

    def has_name(self, name: str) -> bool:
        return media_file_match_key(name) in self.sizes_by_key


@dataclass
class RegionSyncResult:
    ok: bool
    dry_run: bool
    source_count: int = 0
    source_failures: list[dict[str, str]] = field(default_factory=list)
    rows_read: int = 0
    rows_written: int = 0
    rows_removed: int = 0
    rows_inserted: int = 0
    sheet_rows_deleted: int = 0
    style_ranges_updated: int = 0
    area_label_ranges_repaired: int = 0
    section_title_ranges_repaired: int = 0
    community_ranges_repaired: int = 0
    row_height_ranges_repaired: int = 0
    rich_text_fallback: str = ""
    areas_updated: list[str] = field(default_factory=list)
    media_uploaded: int = 0
    media_skipped: int = 0
    media_transcoded: int = 0
    media_failed: list[dict[str, str]] = field(default_factory=list)
    unsupported_note_links: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "source_count": self.source_count,
            "source_failures": self.source_failures,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_removed": self.rows_removed,
            "rows_inserted": self.rows_inserted,
            "sheet_rows_deleted": self.sheet_rows_deleted,
            "style_ranges_updated": self.style_ranges_updated,
            "area_label_ranges_repaired": self.area_label_ranges_repaired,
            "section_title_ranges_repaired": self.section_title_ranges_repaired,
            "community_ranges_repaired": self.community_ranges_repaired,
            "row_height_ranges_repaired": self.row_height_ranges_repaired,
            "rich_text_fallback": self.rich_text_fallback,
            "areas_updated": self.areas_updated,
            "media_uploaded": self.media_uploaded,
            "media_skipped": self.media_skipped,
            "media_transcoded": self.media_transcoded,
            "media_failed": self.media_failed,
            "unsupported_note_links": self.unsupported_note_links,
        }
