from __future__ import annotations

from datetime import UTC, datetime
import re
import time
from typing import Any

from app.services.inventory_snapshot_models import (
    GENERATOR_VERSION,
    INVENTORY_SNAPSHOT_SCHEMA_VERSION,
    InventoryListing,
    InventorySnapshot,
    InventorySnapshotManifest,
    InventorySourceMetadata,
    InventorySyncReport,
    SnapshotValidationResult,
    generate_listing_id,
    generate_snapshot_id,
    generate_source_hash,
    normalize_listing_identity,
    now_utc_iso,
    redact_sensitive_text,
    sanitize_for_log,
)
from app.services.region_inventory_constants import area_alias_index_entries


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "area": ("区域", "商圈", "板块", "位置", "area"),
    "community": ("小区", "社区", "楼盘", "小区名", "community"),
    "room_no": ("房号", "房间号", "编号", "门牌", "room", "room_no"),
    "layout_desc": ("户型描述", "户型", "描述", "户型详情", "户型介绍", "layout_desc"),
    "layout_type": ("户型分类", "户型标签", "房型", "layout_type"),
    "rent_monthly_pay1": ("押一付一", "押一付", "押一", "押一付一月租金", "price_yayi"),
    "rent_monthly_pay2": ("押二付一", "押二付", "押二", "押二付一月租金", "price_yaer"),
    "viewing_text": ("看房方式密码", "看房方式/密码", "看房方式", "看房密码", "密码", "viewing"),
    "remark": ("备注", "水电", "水电费", "说明", "utility", "remark"),
    "availability": ("房态", "状态", "出租状态", "availability", "status"),
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

PROMOTIONAL_MARKERS = ("欢迎", "推荐", "全佣", "免押", "联系方式", "电话", "咨询", "优惠")
RENT_MIN = 100
RENT_MAX = 100000


class SnapshotBuilder:
    """Build an immutable local inventory snapshot from tabular source rows."""

    def __init__(
        self,
        *,
        area_aliases: dict[str, str] | None = None,
        generator_version: str = GENERATOR_VERSION,
    ) -> None:
        self.area_aliases = dict(area_aliases or {})
        self.generator_version = generator_version

    def build(
        self,
        rows: list[dict[str, Any]],
        source_metadata: InventorySourceMetadata,
        *,
        generated_at: datetime | str | None = None,
        source_payload: Any | None = None,
        attempt: int | str | None = None,
    ) -> tuple[InventorySnapshot, InventorySyncReport]:
        """Build snapshot models and a safe sync report without writing files."""
        started_at = time.monotonic()
        generated_at_iso = _generated_at_iso(generated_at)
        normalized_rows_for_hash = _rows_for_hash(rows)
        source_hash = generate_source_hash(
            source_payload
            if source_payload is not None
            else {
                "schema_version": INVENTORY_SNAPSHOT_SCHEMA_VERSION,
                "source_metadata": source_metadata.to_hash_payload(),
                "rows": normalized_rows_for_hash,
                "generator_version": self.generator_version,
            }
        )
        snapshot_id = generate_snapshot_id(source_hash, generated_at=generated_at_iso, attempt=attempt)
        validation_result = SnapshotValidationResult()
        listings: list[InventoryListing] = []
        private_viewing_secrets: dict[str, Any] = {}
        rejected_rows: list[dict[str, Any]] = []
        filtered_rows: list[dict[str, Any]] = []
        duplicate_rows: list[dict[str, Any]] = []
        deduplicated_rows: list[dict[str, Any]] = []
        current_area = ""
        current_community = ""
        listings_by_key: dict[str, InventoryListing] = {}
        duplicate_fingerprints: dict[str, dict[str, Any]] = {}

        for index, raw_row in enumerate(rows, start=1):
            source_row_number = _source_row_number(raw_row, index)
            safe_raw_fields = _safe_raw_fields(raw_row)
            canonical = _canonicalize_row(raw_row)
            if _is_empty_row(canonical):
                current_area = ""
                current_community = ""
                filtered_rows.append(_row_report(source_row_number, "empty_row", raw_row))
                continue
            if _is_header_row(canonical):
                filtered_rows.append(_row_report(source_row_number, "header_row", raw_row))
                continue

            raw_area = canonical["area"]
            raw_community = canonical["community"]
            raw_room_no = canonical["room_no"]

            if _is_area_title_row(canonical):
                current_area = raw_area
                current_community = ""
                filtered_rows.append(_row_report(source_row_number, "area_title_row", raw_row))
                continue
            if raw_area:
                if raw_area != current_area:
                    current_community = ""
                current_area = raw_area
            if raw_community:
                current_community = raw_community
            if _is_promotional_row(canonical):
                filtered_rows.append(_row_report(source_row_number, "promotional_row", raw_row))
                continue
            if _is_closed_listing(canonical):
                filtered_rows.append(_row_report(source_row_number, "closed_listing", raw_row))
                continue

            area = raw_area or current_area
            community = raw_community or current_community
            room_no = raw_room_no

            if not room_no:
                if _is_context_only_row(canonical):
                    filtered_rows.append(_row_report(source_row_number, "context_row", raw_row))
                else:
                    rejected_rows.append(_row_report(source_row_number, "missing_room_no", raw_row))
                continue
            if not community:
                rejected_rows.append(_row_report(source_row_number, "missing_community", raw_row))
                continue

            rent_pay1, rent_pay1_error = parse_monthly_rent(canonical["rent_monthly_pay1"])
            rent_pay2, rent_pay2_error = parse_monthly_rent(canonical["rent_monthly_pay2"])
            if rent_pay1_error or rent_pay2_error:
                rejected_rows.append(
                    _row_report(
                        source_row_number,
                        "invalid_monthly_rent",
                        raw_row,
                        details={
                            "rent_monthly_pay1": rent_pay1_error,
                            "rent_monthly_pay2": rent_pay2_error,
                        },
                    )
                )
                continue

            listing_id = generate_listing_id(community, room_no)
            viewing_text = canonical["viewing_text"]
            viewing_summary = build_viewing_summary(viewing_text)
            viewing_secret_ref = (
                f"private/viewing_secrets.json#{listing_id}"
                if viewing_summary["has_viewing_text"]
                else ""
            )
            source_record_id = _source_record_id(raw_row)
            listing = InventoryListing(
                listing_id=listing_id,
                source_record_id=source_record_id,
                source_row_number=source_row_number,
                raw_area=raw_area,
                area=area,
                raw_community=raw_community,
                community=community,
                raw_room_no=raw_room_no,
                room_no=str(room_no),
                layout_desc=canonical["layout_desc"],
                layout_type=canonical["layout_type"],
                raw_rent_monthly_pay1=canonical["rent_monthly_pay1"],
                rent_monthly_pay1=rent_pay1,
                raw_rent_monthly_pay2=canonical["rent_monthly_pay2"],
                rent_monthly_pay2=rent_pay2,
                viewing_secret_ref=viewing_secret_ref,
                viewing_summary=viewing_summary,
                remark=canonical["remark"],
                utility_summary=build_utility_summary(canonical["remark"]),
                availability_summary=build_availability_summary(viewing_text, canonical["availability"]),
                has_image=parse_media_bool(raw_row, IMAGE_FIELD_ALIASES),
                has_video=parse_media_bool(raw_row, VIDEO_FIELD_ALIASES),
                raw_fields=safe_raw_fields,
            )
            key = listing.listing_key
            fingerprint = _listing_fingerprint(listing, viewing_text)
            if key in listings_by_key:
                existing_fingerprint = duplicate_fingerprints[key]
                if existing_fingerprint == fingerprint:
                    existing = listings_by_key[key]
                    if source_record_id and source_record_id not in existing.source_record_ids:
                        existing.source_record_ids.append(source_record_id)
                    deduplicated_rows.append(
                        _duplicate_report(
                            source_row_number=source_row_number,
                            listing=existing,
                            reason="identical_duplicate",
                        )
                    )
                    continue
                duplicate_report = _duplicate_report(
                    source_row_number=source_row_number,
                    listing=listings_by_key[key],
                    reason="conflicting_duplicate",
                )
                duplicate_report["conflict_fields"] = _conflict_fields(existing_fingerprint, fingerprint)
                duplicate_rows.append(duplicate_report)
                validation_result.add(
                    "error",
                    "duplicate_listing_conflict",
                    "同一小区和房号出现冲突房源，不能发布快照。",
                    path=f"rows[{source_row_number}]",
                    context=duplicate_report,
                )
                continue

            listings_by_key[key] = listing
            duplicate_fingerprints[key] = fingerprint
            listings.append(listing)
            if viewing_summary["has_viewing_text"]:
                private_viewing_secrets[listing_id] = {
                    "listing_id": listing_id,
                    "snapshot_id": snapshot_id,
                    "source_row_number": source_row_number,
                    "viewing_text": str(viewing_text),
                    "has_password": bool(viewing_summary["has_password"]),
                    "availability_status": listing.availability_summary.get("status", "unknown"),
                }

        manifest = InventorySnapshotManifest(
            schema_version=INVENTORY_SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            source_hash=source_hash,
            source_version=source_metadata.source_version,
            source_modified_at=source_metadata.source_modified_at,
            generated_at=generated_at_iso,
            listing_count=len(listings),
            valid_listing_count=len(listings),
            rejected_row_count=len(rejected_rows),
            duplicate_count=len(duplicate_rows) + len(deduplicated_rows),
            files={
                "manifest": {"path": "manifest.json"},
                "inventory_json": {"path": "inventory.json"},
                "inventory_csv": {"path": "inventory.csv"},
                "rewrite_inventory_index": {"path": "rewrite_inventory_index.json"},
                "sync_report": {"path": "sync_report.json"},
                "png": {"path": "png/", "status": "reserved"},
            },
            generator_version=self.generator_version,
        )
        snapshot = InventorySnapshot(
            schema_version=INVENTORY_SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            source_hash=source_hash,
            generated_at=generated_at_iso,
            source_metadata=source_metadata,
            manifest=manifest,
            listings=listings,
            private_viewing_secrets=private_viewing_secrets,
        )
        snapshot.rewrite_index = build_safe_rewrite_inventory_index(
            snapshot,
            area_aliases=self.area_aliases,
        )
        report = InventorySyncReport(
            snapshot_id=snapshot_id,
            source_hash=source_hash,
            generated_at=generated_at_iso,
            rows_read=len(rows),
            valid_listing_count=len(listings),
            rejected_rows=rejected_rows,
            filtered_rows=filtered_rows,
            duplicate_rows=duplicate_rows,
            deduplicated_rows=deduplicated_rows,
            validation_result=validation_result,
            duration_seconds=round(time.monotonic() - started_at, 6),
            notes=[
                "押一付一/押二付一字段表示对应付款方式下的月租，不是押金金额。",
                "备注字段按当前业务语义表示水电收取方式。",
            ],
        )
        return snapshot, report


def build_inventory_snapshot(
    rows: list[dict[str, Any]],
    source_metadata: InventorySourceMetadata,
    *,
    generated_at: datetime | str | None = None,
    source_payload: Any | None = None,
) -> tuple[InventorySnapshot, InventorySyncReport]:
    """Convenience wrapper for building a snapshot with the default builder."""
    return SnapshotBuilder().build(
        rows,
        source_metadata,
        generated_at=generated_at,
        source_payload=source_payload,
    )


def build_safe_rewrite_inventory_index(
    snapshot: InventorySnapshot,
    *,
    area_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create the safe rewrite index without LLM calls or secret fields."""
    alias_entries = area_alias_index_entries(extra_aliases=area_aliases)
    room_index = [_rewrite_room_item(listing) for listing in snapshot.listings]
    areas = _area_index(snapshot.listings)
    communities = _community_index(snapshot.listings)
    return {
        "schema_version": "rewrite_inventory_index.snapshot.v1",
        "source": "inventory_snapshot",
        "snapshot_id": snapshot.snapshot_id,
        "source_hash": snapshot.source_hash,
        "generated_at": snapshot.generated_at,
        "row_count": len(snapshot.listings),
        "primary_key": "listing_id",
        "field_semantics": {
            "rent_monthly_pay1": "押一付一付款方式下的月租，不是押金金额。",
            "rent_monthly_pay2": "押二付一付款方式下的月租，不是押金金额。",
            "remark": "备注字段表示水电费收取方式。",
            "viewing_summary": "只包含看房状态摘要，不含真实密码或原始看房文本。",
        },
        "area_aliases": alias_entries,
        "areas": areas,
        "communities": communities,
        "room_index": room_index,
        "media_summary": _media_summary(snapshot.listings),
        "viewing_summary": _aggregate_viewing_summary(snapshot.listings),
        "availability_summary": _aggregate_availability_summary(snapshot.listings),
    }


def parse_monthly_rent(value: Any) -> tuple[int | None, str]:
    """Parse monthly rent while preserving empty or pending prices as null."""
    text = _stringify_cell(value)
    if not text or text in {"无", "暂无", "待定", "面议", "-", "—", "/"}:
        return None, ""
    if re.search(r"(^|[^\d])-+\s*\d", text):
        return None, "negative_or_signed_value"
    matches = re.findall(r"\d+(?:\.\d+)?", text)
    if not matches:
        return None, "non_numeric"
    distinct = list(dict.fromkeys(matches))
    if len(distinct) > 1:
        return None, "ambiguous_numeric_values"
    number_text = distinct[0]
    if "." in number_text:
        amount_float = float(number_text)
        if not amount_float.is_integer():
            return None, "not_integer"
        amount = int(amount_float)
    else:
        amount = int(number_text)
    if amount < RENT_MIN or amount > RENT_MAX:
        return None, "out_of_range"
    return amount, ""


def build_viewing_summary(viewing_text: Any) -> dict[str, Any]:
    """Build a structured viewing summary without copying the raw text."""
    text = _stringify_cell(viewing_text)
    has_password = bool(re.search(r"(?<!\d)\d{3,8}#?(?!\d)", text))
    needs_contact = any(marker in text for marker in ("提前联系", "联系", "预约", "看房提前", "密码不对"))
    has_empty_out_hint = any(marker in text for marker in ("空出", "未空", "还没空", "待空", "转租"))
    if not text:
        viewing_mode = "unknown"
    elif has_password:
        viewing_mode = "password_available"
    elif needs_contact:
        viewing_mode = "contact_required"
    else:
        viewing_mode = "viewing_text_only"
    return {
        "has_viewing_text": bool(text),
        "has_password": has_password,
        "needs_contact": needs_contact,
        "has_empty_out_hint": has_empty_out_hint,
        "viewing_mode": viewing_mode,
    }


def build_availability_summary(viewing_text: Any, explicit_status: Any = "") -> dict[str, Any]:
    """Build a structured availability status from viewing/status fields."""
    text = f"{_stringify_cell(viewing_text)} {_stringify_cell(explicit_status)}".strip()
    if not text:
        status = "unknown"
    elif any(marker in text for marker in ("已租", "下架", "不租")):
        status = "closed"
    elif any(marker in text for marker in ("未空", "还没空", "待空")):
        status = "not_yet_vacant"
    elif "空出" in text or "转租" in text:
        status = "has_empty_out_hint"
    elif any(marker in text for marker in ("提前联系", "联系", "预约")):
        status = "needs_contact"
    elif re.search(r"(?<!\d)\d{3,8}#?(?!\d)", text):
        status = "password_available"
    else:
        status = "available_unknown_method"
    return {"status": status}


def build_utility_summary(remark: Any) -> dict[str, Any]:
    """Build a safe utility summary from the remark field."""
    text = _stringify_cell(remark)
    return {
        "has_utility_text": bool(text),
        "summary": redact_sensitive_text(text),
    }


def parse_media_bool(row: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    """Parse media presence from known image/video source columns."""
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
        text = _stringify_cell(value).lower()
        if not text or text in {"0", "false", "no", "none", "无", "没有", "暂无"}:
            return False
        return True
    return False


def _generated_at_iso(generated_at: datetime | str | None) -> str:
    if isinstance(generated_at, datetime):
        return generated_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(generated_at, str) and generated_at.strip():
        return generated_at.strip()
    return now_utc_iso()


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\ufeff", "").strip()


def _source_row_number(row: dict[str, Any], fallback: int) -> int:
    for key in ("source_row_number", "__row_number", "row_number", "行号"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback
    return fallback


def _source_record_id(row: dict[str, Any]) -> str | None:
    for key in ("source_record_id", "record_id", "recordId", "记录ID"):
        value = _stringify_cell(row.get(key))
        if value:
            return value
    return None


def _canonicalize_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: _row_value(row, aliases)
        for field, aliases in FIELD_ALIASES.items()
    }


def _row_value(row: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in row:
            value = _stringify_cell(row.get(alias))
            if value:
                return value
    return ""


def _safe_raw_fields(row: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in row.items():
        key_text = str(key)
        if key_text.startswith("__"):
            continue
        if _is_sensitive_source_key(key_text):
            continue
        else:
            result[key_text] = _stringify_cell(value)
    return result


def _rows_for_hash(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            str(key): _stringify_cell(value)
            for key, value in sorted(row.items(), key=lambda item: str(item[0]))
            if not str(key).startswith("__")
        }
        for row in rows
    ]


def _is_sensitive_source_key(key: str) -> bool:
    return any(marker in key for marker in ("密码", "看房方式", "看房密码")) or any(
        marker in key.lower()
        for marker in ("password", "viewing_text", "secret", "token")
    )


def _is_empty_row(canonical: dict[str, str]) -> bool:
    return not any(canonical.values())


def _is_header_row(canonical: dict[str, str]) -> bool:
    header_hits = 0
    expected = {
        "area": "区域",
        "community": "小区",
        "room_no": "房号",
        "layout_desc": "户型",
        "layout_type": "户型分类",
    }
    for field, label in expected.items():
        if canonical.get(field) == label:
            header_hits += 1
    return header_hits >= 2


def _is_area_title_row(canonical: dict[str, str]) -> bool:
    if not canonical.get("area") or canonical.get("room_no"):
        return False
    populated = [
        field
        for field, value in canonical.items()
        if value and field not in {"area", "community"}
    ]
    return not populated and not canonical.get("community")


def _is_context_only_row(canonical: dict[str, str]) -> bool:
    if canonical.get("room_no"):
        return False
    populated = {field for field, value in canonical.items() if value}
    return bool(populated) and populated <= {"area", "community"}


def _is_promotional_row(canonical: dict[str, str]) -> bool:
    if canonical.get("room_no"):
        return False
    text = "".join(canonical.values())
    return bool(text) and any(marker in text for marker in PROMOTIONAL_MARKERS)


def _is_closed_listing(canonical: dict[str, str]) -> bool:
    text = f"{canonical.get('availability', '')} {canonical.get('viewing_text', '')}".strip()
    return bool(text) and any(marker in text for marker in ("已租", "下架", "不租"))


def _row_report(
    source_row_number: int,
    reason: str,
    raw_row: dict[str, Any],
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "source_row_number": source_row_number,
        "reason": reason,
        "raw_row": _safe_raw_fields(raw_row),
    }
    if details:
        payload["details"] = details
    return sanitize_for_log(payload)


def _duplicate_report(
    *,
    source_row_number: int,
    listing: InventoryListing,
    reason: str,
) -> dict[str, Any]:
    return sanitize_for_log(
        {
            "source_row_number": source_row_number,
            "reason": reason,
            "listing_id": listing.listing_id,
            "community": listing.community,
            "room_no": listing.room_no,
        }
    )


def _listing_fingerprint(listing: InventoryListing, viewing_text: str) -> dict[str, Any]:
    return {
        "area": listing.area,
        "community": listing.community,
        "room_no": listing.room_no,
        "layout_desc": listing.layout_desc,
        "layout_type": listing.layout_type,
        "rent_monthly_pay1": listing.rent_monthly_pay1,
        "rent_monthly_pay2": listing.rent_monthly_pay2,
        "viewing_text": viewing_text,
        "remark": listing.remark,
        "has_image": listing.has_image,
        "has_video": listing.has_video,
    }


def _conflict_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return [
        field
        for field in sorted(set(left) | set(right))
        if left.get(field) != right.get(field)
    ]


def _rewrite_room_item(listing: InventoryListing) -> dict[str, Any]:
    prices = [
        price
        for price in (listing.rent_monthly_pay1, listing.rent_monthly_pay2)
        if isinstance(price, int)
    ]
    return {
        "listing_id": listing.listing_id,
        "area": listing.area,
        "community": listing.community,
        "room_no": listing.room_no,
        "normalized_community": listing.normalized_community,
        "normalized_room_no": listing.normalized_room_no,
        "layout_type": listing.layout_type,
        "price_range": [min(prices), max(prices)] if prices else [],
        "has_image": listing.has_image,
        "has_video": listing.has_video,
        "has_password": bool(listing.viewing_summary.get("has_password")),
        "viewing_mode": listing.viewing_summary.get("viewing_mode", "unknown"),
        "viewing_summary": dict(listing.viewing_summary),
        "availability_summary": dict(listing.availability_summary),
    }


def _area_index(listings: list[InventoryListing]) -> list[dict[str, Any]]:
    grouped: dict[str, list[InventoryListing]] = {}
    for listing in listings:
        if listing.area:
            grouped.setdefault(listing.area, []).append(listing)
    result: list[dict[str, Any]] = []
    for area, area_listings in sorted(grouped.items()):
        prices = _prices(area_listings)
        result.append(
            {
                "name": area,
                "normalized": normalize_listing_identity(area),
                "count": len(area_listings),
                "communities": sorted({listing.community for listing in area_listings if listing.community}),
                "price_range": [min(prices), max(prices)] if prices else [],
            }
        )
    return result


def _community_index(listings: list[InventoryListing]) -> list[dict[str, Any]]:
    grouped: dict[str, list[InventoryListing]] = {}
    for listing in listings:
        if listing.community:
            grouped.setdefault(listing.community, []).append(listing)
    result: list[dict[str, Any]] = []
    for community, community_listings in sorted(grouped.items()):
        prices = _prices(community_listings)
        first = community_listings[0]
        result.append(
            {
                "name": community,
                "normalized": first.normalized_community,
                "count": len(community_listings),
                "area": first.area,
                "rooms": [listing.room_no for listing in community_listings],
                "price_range": [min(prices), max(prices)] if prices else [],
            }
        )
    return result


def _prices(listings: list[InventoryListing]) -> list[int]:
    result: list[int] = []
    for listing in listings:
        for price in (listing.rent_monthly_pay1, listing.rent_monthly_pay2):
            if isinstance(price, int):
                result.append(price)
    return result


def _media_summary(listings: list[InventoryListing]) -> dict[str, int]:
    return {
        "room_count": len(listings),
        "rooms_with_images": sum(1 for listing in listings if listing.has_image),
        "rooms_with_videos": sum(1 for listing in listings if listing.has_video),
    }


def _aggregate_viewing_summary(listings: list[InventoryListing]) -> dict[str, int]:
    result = {
        "has_viewing_text": 0,
        "has_password": 0,
        "needs_contact": 0,
        "has_empty_out_hint": 0,
    }
    for listing in listings:
        for key in result:
            if listing.viewing_summary.get(key):
                result[key] += 1
    return result


def _aggregate_availability_summary(listings: list[InventoryListing]) -> dict[str, int]:
    result: dict[str, int] = {}
    for listing in listings:
        status = str(listing.availability_summary.get("status") or "unknown")
        result[status] = result.get(status, 0) + 1
    return result
