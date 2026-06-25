from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from app.services.inventory_snapshot_builder import (
    FIELD_ALIASES,
    IMAGE_FIELD_ALIASES,
    VIDEO_FIELD_ALIASES,
    build_availability_summary,
    build_utility_summary,
    build_viewing_summary,
    parse_media_bool,
    parse_monthly_rent,
)
from app.services.inventory_snapshot_models import (
    InventoryListing,
    InventorySnapshot,
    generate_listing_id,
    normalize_listing_identity,
    redact_sensitive_text,
    sanitize_for_log,
)


REPORT_VERSION = "inventory_snapshot_reconciliation.v1"
COMPARE_FIELDS = (
    "area",
    "community",
    "room_no",
    "layout_desc",
    "layout_type",
    "rent_pay1",
    "rent_pay2",
    "utility_summary",
    "availability_summary",
    "has_image",
    "has_video",
    "has_password",
    "password_match",
)
BLOCKING_FIELDS = {"community", "room_no", "rent_pay1", "rent_pay2", "password_match"}
WARNING_FIELDS = {
    "area",
    "layout_desc",
    "layout_type",
    "utility_summary",
    "availability_summary",
    "has_image",
    "has_video",
}


@dataclass(frozen=True)
class ReconciliationListing:
    listing_id: str
    key: str
    source_row_ref: str
    area: str
    community: str
    room_no: str
    layout_desc: str
    layout_type: str
    rent_pay1: int | None
    rent_pay2: int | None
    utility_summary: dict[str, Any]
    availability_summary: dict[str, Any]
    has_image: bool
    has_video: bool
    has_password: bool
    password_text: str = field(repr=False, compare=False)

    def safe_ref(self) -> dict[str, str]:
        return {
            "listing_id": self.listing_id,
            "key": self.key,
            "source_row_ref": self.source_row_ref,
            "community": self.community,
            "room_no": self.room_no,
        }


@dataclass
class InventorySnapshotReconciliationReport:
    report_version: str
    generated_at: str
    source_version: str
    source_hash: str
    legacy_record_count: int
    snapshot_record_count: int
    matched_count: int
    missing_in_snapshot: list[dict[str, Any]] = field(default_factory=list)
    extra_in_snapshot: list[dict[str, Any]] = field(default_factory=list)
    duplicate_legacy_records: list[dict[str, Any]] = field(default_factory=list)
    duplicate_snapshot_records: list[dict[str, Any]] = field(default_factory=list)
    field_mismatches: list[dict[str, Any]] = field(default_factory=list)
    rewrite_index_mismatches: list[dict[str, Any]] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    passed: bool = True
    safe_summary: dict[str, Any] = field(default_factory=dict)
    legacy_sensitive_field_present: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "report_version": self.report_version,
            "generated_at": self.generated_at,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "legacy_record_count": self.legacy_record_count,
            "snapshot_record_count": self.snapshot_record_count,
            "matched_count": self.matched_count,
            "missing_in_snapshot": self.missing_in_snapshot,
            "extra_in_snapshot": self.extra_in_snapshot,
            "duplicate_legacy_records": self.duplicate_legacy_records,
            "duplicate_snapshot_records": self.duplicate_snapshot_records,
            "field_mismatches": self.field_mismatches,
            "rewrite_index_mismatches": self.rewrite_index_mismatches,
            "severity_counts": self.severity_counts,
            "passed": self.passed,
            "safe_summary": self.safe_summary,
            "legacy_sensitive_field_present": self.legacy_sensitive_field_present,
        }
        return sanitize_for_log(payload)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def reconcile_inventory_snapshot(
    *,
    legacy_rows: list[dict[str, Any]],
    snapshot: InventorySnapshot,
    legacy_rewrite_index: dict[str, Any] | None = None,
) -> InventorySnapshotReconciliationReport:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    legacy_items = [_legacy_listing(row) for row in legacy_rows]
    legacy_items = [item for item in legacy_items if item is not None]
    snapshot_items = [_snapshot_listing(listing, snapshot.private_viewing_secrets) for listing in snapshot.listings]
    legacy_by_id, duplicate_legacy = _index_by_listing_id(legacy_items)
    snapshot_by_id, duplicate_snapshot = _index_by_listing_id(snapshot_items)
    missing_ids = sorted(set(legacy_by_id) - set(snapshot_by_id))
    extra_ids = sorted(set(snapshot_by_id) - set(legacy_by_id))
    common_ids = sorted(set(legacy_by_id) & set(snapshot_by_id))

    severity_counts = {"blocking": 0, "warning": 0, "info": 0}
    field_mismatches: list[dict[str, Any]] = []
    for listing_id in common_ids:
        legacy = legacy_by_id[listing_id]
        current = snapshot_by_id[listing_id]
        field_mismatches.extend(_compare_listing(legacy, current, severity_counts))

    missing_in_snapshot = [
        _with_severity(legacy_by_id[item].safe_ref(), "blocking", code="missing_in_snapshot")
        for item in missing_ids
    ]
    extra_in_snapshot = [
        _with_severity(snapshot_by_id[item].safe_ref(), "blocking", code="extra_in_snapshot")
        for item in extra_ids
    ]
    duplicate_legacy_records = [
        _with_severity(item, "blocking", code="duplicate_legacy_record")
        for item in duplicate_legacy
    ]
    duplicate_snapshot_records = [
        _with_severity(item, "blocking", code="duplicate_snapshot_record")
        for item in duplicate_snapshot
    ]
    severity_counts["blocking"] += (
        len(missing_in_snapshot)
        + len(extra_in_snapshot)
        + len(duplicate_legacy_records)
        + len(duplicate_snapshot_records)
    )

    rewrite_mismatches, sensitive_present = compare_rewrite_inventory_index(
        legacy_rewrite_index or {},
        snapshot.rewrite_index,
    )
    for mismatch in rewrite_mismatches:
        severity = str(mismatch.get("severity") or "warning")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    passed = severity_counts.get("blocking", 0) == 0
    return InventorySnapshotReconciliationReport(
        report_version=REPORT_VERSION,
        generated_at=generated_at,
        source_version=snapshot.source_metadata.source_version,
        source_hash=snapshot.source_hash,
        legacy_record_count=len(legacy_items),
        snapshot_record_count=len(snapshot_items),
        matched_count=len(common_ids),
        missing_in_snapshot=missing_in_snapshot,
        extra_in_snapshot=extra_in_snapshot,
        duplicate_legacy_records=duplicate_legacy_records,
        duplicate_snapshot_records=duplicate_snapshot_records,
        field_mismatches=field_mismatches,
        rewrite_index_mismatches=rewrite_mismatches,
        severity_counts=severity_counts,
        passed=passed,
        legacy_sensitive_field_present=sensitive_present,
        safe_summary={
            "blocking_count": severity_counts.get("blocking", 0),
            "warning_count": severity_counts.get("warning", 0),
            "info_count": severity_counts.get("info", 0),
            "compare_fields": list(COMPARE_FIELDS),
            "password_policy": "password_match boolean only; raw viewing text is never written",
        },
    )


def load_legacy_rewrite_index(path: Path | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if path is None:
        return dict(fallback or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(fallback or {})
    return value if isinstance(value, dict) else dict(fallback or {})


def compare_rewrite_inventory_index(
    legacy_index: dict[str, Any],
    snapshot_index: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    mismatches: list[dict[str, Any]] = []
    if not legacy_index:
        return mismatches, False
    sensitive_present = _legacy_index_has_viewing(legacy_index)
    if sensitive_present:
        mismatches.append(
            {
                "severity": "warning",
                "code": "rewrite_index_sensitive_field_present",
                "field": "room_index.viewing",
                "legacy_sensitive_field_present": True,
                "message": "旧 rewrite index 含 viewing 字段；报告只记录布尔标记，不复制原文。",
            }
        )
    _compare_value(mismatches, "row_count", legacy_index.get("row_count"), snapshot_index.get("row_count"), "blocking")
    _compare_set(
        mismatches,
        "area_aliases",
        _alias_pairs(legacy_index.get("area_aliases") or []),
        _alias_pairs(snapshot_index.get("area_aliases") or []),
        "warning",
    )
    _compare_set(
        mismatches,
        "areas",
        _named_set(legacy_index.get("areas") or []),
        _named_set(snapshot_index.get("areas") or []),
        "blocking",
    )
    legacy_communities = _community_map(legacy_index.get("communities") or [])
    snapshot_communities = _community_map(snapshot_index.get("communities") or [])
    snapshot_layouts_by_community = _layouts_by_community(snapshot_index.get("room_index") or [])
    _compare_set(mismatches, "communities", set(legacy_communities), set(snapshot_communities), "blocking")
    for name in sorted(set(legacy_communities) & set(snapshot_communities)):
        legacy = legacy_communities[name]
        current = snapshot_communities[name]
        _compare_set(
            mismatches,
            f"communities[{name}].rooms",
            set(str(item) for item in legacy.get("rooms") or []),
            set(str(item) for item in current.get("rooms") or []),
            "blocking",
        )
        _compare_value(
            mismatches,
            f"communities[{name}].price_range",
            legacy.get("price_range") or [],
            current.get("price_range") or [],
            "blocking",
        )
        _compare_value(
            mismatches,
            f"communities[{name}].layouts",
            legacy.get("layouts") or {},
            current.get("layouts") or snapshot_layouts_by_community.get(name, {}),
            "warning",
        )
    _compare_media_summary(mismatches, legacy_index.get("media_summary") or {}, snapshot_index.get("media_summary") or {})
    return mismatches, sensitive_present


def _legacy_listing(row: dict[str, Any]) -> ReconciliationListing | None:
    community = _row_value(row, FIELD_ALIASES["community"])
    room_no = _row_value(row, FIELD_ALIASES["room_no"])
    if not community or not room_no:
        return None
    rent_pay1, _ = parse_monthly_rent(_row_value(row, FIELD_ALIASES["rent_monthly_pay1"]))
    rent_pay2, _ = parse_monthly_rent(_row_value(row, FIELD_ALIASES["rent_monthly_pay2"]))
    viewing_text = _row_value(row, FIELD_ALIASES["viewing_text"])
    listing_id = generate_listing_id(community, room_no)
    return ReconciliationListing(
        listing_id=listing_id,
        key=f"{normalize_listing_identity(community)}\0{normalize_listing_identity(room_no)}",
        source_row_ref=_source_row_ref(row),
        area=_row_value(row, FIELD_ALIASES["area"]),
        community=community,
        room_no=room_no,
        layout_desc=_row_value(row, FIELD_ALIASES["layout_desc"]),
        layout_type=_row_value(row, FIELD_ALIASES["layout_type"]),
        rent_pay1=rent_pay1,
        rent_pay2=rent_pay2,
        utility_summary=build_utility_summary(_row_value(row, FIELD_ALIASES["remark"])),
        availability_summary=build_availability_summary(
            viewing_text,
            _row_value(row, FIELD_ALIASES["availability"]),
        ),
        has_image=parse_media_bool(row, IMAGE_FIELD_ALIASES),
        has_video=parse_media_bool(row, VIDEO_FIELD_ALIASES),
        has_password=bool(build_viewing_summary(viewing_text).get("has_password")),
        password_text=viewing_text,
    )


def _snapshot_listing(
    listing: InventoryListing,
    private_viewing_secrets: dict[str, Any],
) -> ReconciliationListing:
    secret = private_viewing_secrets.get(listing.listing_id) or {}
    viewing_text = str(secret.get("viewing_text") or "")
    return ReconciliationListing(
        listing_id=listing.listing_id,
        key=listing.listing_key,
        source_row_ref=str(listing.source_row_number),
        area=listing.area,
        community=listing.community,
        room_no=listing.room_no,
        layout_desc=listing.layout_desc,
        layout_type=listing.layout_type,
        rent_pay1=listing.rent_monthly_pay1,
        rent_pay2=listing.rent_monthly_pay2,
        utility_summary=dict(listing.utility_summary),
        availability_summary=dict(listing.availability_summary),
        has_image=listing.has_image,
        has_video=listing.has_video,
        has_password=bool(listing.viewing_summary.get("has_password")),
        password_text=viewing_text,
    )


def _compare_listing(
    legacy: ReconciliationListing,
    current: ReconciliationListing,
    severity_counts: dict[str, int],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    comparisons = {
        "area": (_normalize_loose(legacy.area), _normalize_loose(current.area)),
        "community": (_normalize_loose(legacy.community), _normalize_loose(current.community)),
        "room_no": (_normalize_loose(legacy.room_no), _normalize_loose(current.room_no)),
        "layout_desc": (_normalize_loose(legacy.layout_desc), _normalize_loose(current.layout_desc)),
        "layout_type": (_normalize_loose(legacy.layout_type), _normalize_loose(current.layout_type)),
        "rent_pay1": (legacy.rent_pay1, current.rent_pay1),
        "rent_pay2": (legacy.rent_pay2, current.rent_pay2),
        "utility_summary": (legacy.utility_summary, current.utility_summary),
        "availability_summary": (legacy.availability_summary, current.availability_summary),
        "has_image": (legacy.has_image, current.has_image),
        "has_video": (legacy.has_video, current.has_video),
        "has_password": (legacy.has_password, current.has_password),
    }
    for field_name, (legacy_value, snapshot_value) in comparisons.items():
        if legacy_value == snapshot_value:
            continue
        severity = "blocking" if field_name in BLOCKING_FIELDS else "warning"
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        entry = {
            "severity": severity,
            "code": f"field_mismatch.{field_name}",
            "listing": legacy.safe_ref(),
            "field": field_name,
            "legacy": _safe_compare_value(legacy_value),
            "snapshot": _safe_compare_value(snapshot_value),
        }
        mismatches.append(entry)
    password_match = legacy.password_text == current.password_text
    if bool(legacy.has_password) != bool(current.has_password) or (
        legacy.has_password and current.has_password and not password_match
    ):
        severity_counts["blocking"] = severity_counts.get("blocking", 0) + 1
        mismatches.append(
            {
                "severity": "blocking",
                "code": "field_mismatch.password_match",
                "listing": legacy.safe_ref(),
                "field": "password_match",
                "legacy": {"has_password": bool(legacy.has_password)},
                "snapshot": {
                    "has_password": bool(current.has_password),
                    "password_match": bool(password_match),
                },
            }
        )
    return mismatches


def _index_by_listing_id(items: list[ReconciliationListing]) -> tuple[dict[str, ReconciliationListing], list[dict[str, Any]]]:
    indexed: dict[str, ReconciliationListing] = {}
    duplicates: list[dict[str, Any]] = []
    for item in items:
        if item.listing_id in indexed:
            duplicates.append(item.safe_ref())
            continue
        indexed[item.listing_id] = item
    return indexed, duplicates


def _row_value(row: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value is not None and str(value).strip():
            return str(value).replace("\ufeff", "").strip()
    return ""


def _normalize_loose(value: Any) -> str:
    return " ".join(str(value or "").replace("，", ",").replace("。", ".").split())


def _safe_compare_value(value: Any) -> Any:
    return sanitize_for_log(redact_sensitive_text(value) if isinstance(value, str) else value)


def _with_severity(value: dict[str, Any], severity: str, *, code: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, **sanitize_for_log(value)}


def _legacy_index_has_viewing(index: dict[str, Any]) -> bool:
    for item in index.get("room_index") or []:
        if isinstance(item, dict) and ("viewing" in item or "看房方式密码" in item):
            return True
    return False


def _compare_value(
    mismatches: list[dict[str, Any]],
    field_name: str,
    legacy_value: Any,
    snapshot_value: Any,
    severity: str,
) -> None:
    if legacy_value == snapshot_value:
        return
    mismatches.append(
        {
            "severity": severity,
            "code": f"rewrite_index_mismatch.{field_name}",
            "field": field_name,
            "legacy": _safe_compare_value(legacy_value),
            "snapshot": _safe_compare_value(snapshot_value),
        }
    )


def _compare_set(
    mismatches: list[dict[str, Any]],
    field_name: str,
    legacy_value: set[Any],
    snapshot_value: set[Any],
    severity: str,
) -> None:
    if legacy_value == snapshot_value:
        return
    mismatches.append(
        {
            "severity": severity,
            "code": f"rewrite_index_mismatch.{field_name}",
            "field": field_name,
            "missing_in_snapshot": sorted(str(item) for item in legacy_value - snapshot_value)[:80],
            "extra_in_snapshot": sorted(str(item) for item in snapshot_value - legacy_value)[:80],
        }
    )


def _compare_media_summary(
    mismatches: list[dict[str, Any]],
    legacy: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    legacy_known_image = int(legacy.get("known_image_status_count") or 0)
    legacy_known_video = int(legacy.get("known_video_status_count") or 0)
    if legacy_known_image:
        _compare_value(
            mismatches,
            "media_summary.rooms_with_images",
            len(legacy.get("rooms_with_images") or []),
            snapshot.get("rooms_with_images"),
            "warning",
        )
    if legacy_known_video:
        _compare_value(
            mismatches,
            "media_summary.rooms_with_videos",
            len(legacy.get("rooms_with_videos") or []),
            snapshot.get("rooms_with_videos"),
            "warning",
        )


def _alias_pairs(items: list[Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, dict):
            alias = str(item.get("alias") or "").strip()
            canonical = str(item.get("canonical") or "").strip()
            if alias or canonical:
                result.add((alias, canonical))
    return result


def _named_set(items: list[Any]) -> set[str]:
    return {str(item.get("name") or "").strip() for item in items if isinstance(item, dict) and str(item.get("name") or "").strip()}


def _community_map(items: list[Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("name") or "").strip(): item
        for item in items
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


def _layouts_by_community(items: list[Any]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        community = str(item.get("community") or "").strip()
        layout = str(item.get("layout_type") or item.get("layout") or "").strip()
        if not community or not layout:
            continue
        bucket = result.setdefault(community, {})
        bucket[layout] = bucket.get(layout, 0) + 1
    return result


def _source_row_ref(row: dict[str, Any]) -> str:
    for key in ("source_row_number", "__row_number", "row_number", "行号"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""
