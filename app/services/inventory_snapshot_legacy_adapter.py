from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.services.region_inventory_models import RegionInventoryRow


ADAPTER_REMOVAL_MILESTONE = "M1D"


@dataclass(frozen=True)
class LegacyInventoryToSnapshotAdapter:
    """Map already-parsed legacy inventory records into SnapshotBuilder rows.

    removal_milestone=M1D. This adapter is intentionally a narrow field boundary:
    it does not call Feishu, generate customer replies, or reimplement business
    normalization.
    """

    removal_milestone: str = ADAPTER_REMOVAL_MILESTONE

    def adapt_many(self, records: Iterable[Any]) -> list[dict[str, Any]]:
        return [self.adapt(record, row_number=index) for index, record in enumerate(records, start=1)]

    def adapt(self, record: Any, *, row_number: int | None = None) -> dict[str, Any]:
        if isinstance(record, RegionInventoryRow):
            return self._adapt_region_row(record, row_number=row_number)
        if isinstance(record, dict):
            return self._adapt_mapping(record, row_number=row_number)
        raise TypeError(f"unsupported legacy inventory row type: {type(record).__name__}")

    def _adapt_region_row(
        self,
        row: RegionInventoryRow,
        *,
        row_number: int | None,
    ) -> dict[str, Any]:
        has_image, has_video = _media_flags(row.attachments, row.note_documents, row.note_links)
        payload: dict[str, Any] = {
            "区域": row.area_title,
            "小区": row.community,
            "房号": row.room_no,
            "户型描述": row.layout,
            "户型分类": row.layout_class,
            "押一付一": row.rent_one,
            "押二付一": row.rent_two,
            "看房方式密码": row.password,
            "备注": row.remark,
            "source_record_id": row.record_id,
            "has_image": has_image,
            "has_video": has_video,
            "__adapter_removal_milestone": self.removal_milestone,
        }
        if row_number is not None:
            payload["source_row_number"] = row_number
        return payload

    def _adapt_mapping(self, row: dict[str, Any], *, row_number: int | None) -> dict[str, Any]:
        payload = {str(key): _stringify(value) for key, value in row.items()}
        if "record_id" in row and "source_record_id" not in payload:
            payload["source_record_id"] = _stringify(row.get("record_id"))
        if row_number is not None and "source_row_number" not in payload and "__row_number" not in payload:
            payload["source_row_number"] = row_number
        payload["__adapter_removal_milestone"] = self.removal_milestone
        return payload


def _media_flags(
    attachments: list[dict[str, Any]],
    note_documents: list[dict[str, str]],
    note_links: list[str],
) -> tuple[bool, bool]:
    has_image = False
    has_video = False
    for item in attachments:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("name", "file_name", "mime_type", "type", "media_type")
        ).lower()
        has_image = has_image or any(marker in text for marker in ("image", ".jpg", ".jpeg", ".png", ".webp"))
        has_video = has_video or any(marker in text for marker in ("video", ".mp4", ".mov", ".avi", ".m4v"))
    has_image = has_image or bool(note_documents) or bool(note_links)
    return has_image, has_video


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\ufeff", "").strip()
