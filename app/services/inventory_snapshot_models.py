from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any


INVENTORY_SNAPSHOT_SCHEMA_VERSION = "inventory_snapshot.v1"
CURRENT_POINTER_SCHEMA_VERSION = "inventory_snapshot_pointer.v1"
GENERATOR_VERSION = "inventory_snapshot_core.v1"
FIELD_MAPPING_VERSION = "inventory_snapshot_fields.v1"
REDACTED_VALUE = "[REDACTED]"


_HASHED_PASSWORD_PATTERN = re.compile(r"(?<!\d)\d{3,8}#(?!\d)")
_PASSWORD_CONTEXT_PATTERN = re.compile(
    r"((?:看房方式|看房|门锁|门禁|钥匙|密码)[^0-9A-Za-z#]{0,12})([A-Za-z0-9][A-Za-z0-9_#-]{2,31})"
)
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_SECRET_CANARY_PATTERN = re.compile(r"TEST_SECRET_[A-Za-z0-9_#-]+")
_CANARY_PATTERN = re.compile(r"\b[\w-]*canary[\w#-]*", re.IGNORECASE)
_TOKEN_CONTEXT_PATTERN = re.compile(r"\b(token|access_token|secret)\s*[:= ]+\S+", re.IGNORECASE)
SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}(?:_[A-Za-z0-9][A-Za-z0-9_-]{0,31})?$")
LISTING_ID_PATTERN = re.compile(r"^lst_[0-9a-f]{16}$")
INTERNAL_SAFE_ID_PATTERN = re.compile(r"^(?:ird|evd)_[0-9a-f]{16}$")
_SENSITIVE_KEY_TOKENS = (
    "password",
    "secret",
    "token",
    "viewing_text",
    "private",
    "phone",
    "mobile",
    "密码",
    "看房方式",
    "看房密码",
)
_SAFE_SENSITIVE_SUMMARY_KEYS = {
    "has_password",
    "has_viewing_text",
    "viewing_summary",
    "viewing_mode",
    "availability_summary",
    "availability_status",
    "viewing_secret_ref",
    "public_artifact_secret_scan_passed",
}


def now_utc_iso() -> str:
    """Return the current UTC time in snapshot artifact format."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime, treating naive values as UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def normalize_hash_text(value: str) -> str:
    """Normalize text before hashing without changing business-visible content."""
    text = str(value or "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def normalize_for_hash(value: Any) -> Any:
    """Recursively normalize values into a stable JSON-hashable shape."""
    if isinstance(value, dict):
        return {
            str(key): normalize_for_hash(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [normalize_for_hash(item) for item in value]
    if isinstance(value, PurePosixPath):
        return value.as_posix()
    if isinstance(value, datetime):
        parsed = value.astimezone(UTC)
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str):
        return normalize_hash_text(value)
    return value


def canonical_json(value: Any) -> str:
    """Serialize a value into canonical JSON for content hashing."""
    return json.dumps(
        normalize_for_hash(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def generate_source_hash(payload: Any) -> str:
    """Generate the deterministic source content identity for a normalized payload."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def generate_snapshot_id(
    source_hash: str,
    *,
    generated_at: datetime | str | None = None,
    attempt: int | str | None = None,
) -> str:
    """Generate a build identity: UTC timestamp plus the source hash prefix."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(source_hash or "")):
        raise ValueError("source_hash must be a 64-character SHA-256 hex digest")
    if isinstance(generated_at, datetime):
        timestamp = generated_at.astimezone(UTC)
    elif isinstance(generated_at, str) and generated_at.strip():
        timestamp = parse_iso_datetime(generated_at) or datetime.now(UTC)
        timestamp = timestamp.astimezone(UTC)
    else:
        timestamp = datetime.now(UTC)
    value = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{source_hash[:12]}"
    if attempt not in (None, "", 0):
        attempt_text = str(attempt)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", attempt_text):
            raise ValueError("attempt must be a short path-safe identifier")
        value = f"{value}_{attempt}"
    return value


def normalize_listing_identity(value: Any) -> str:
    """Normalize community and room identifiers for stable listing_id generation."""
    text = normalize_hash_text(str(value or "")).strip()
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def generate_listing_id(community: Any, room_no: Any) -> str:
    """Generate a stable listing_id from normalized community and room number."""
    payload = f"{normalize_listing_identity(community)}\0{normalize_listing_identity(room_no)}"
    return "lst_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_safe_snapshot_id(value: Any) -> bool:
    """Return whether a snapshot_id is safe to use as one path segment."""
    return bool(SNAPSHOT_ID_PATTERN.fullmatch(str(value or "")))


def is_safe_listing_id(value: Any) -> bool:
    """Return whether a listing_id follows the v1 collision-resistant shape."""
    return bool(LISTING_ID_PATTERN.fullmatch(str(value or "")))


def is_safe_relative_artifact_path(value: Any, *, allow_directory: bool = False) -> bool:
    """Return whether an artifact path is relative POSIX text without traversal."""
    text = str(value or "")
    if not text or "\\" in text or ":" in text or text.startswith(("/", "~")):
        return False
    if text.endswith("/") and not allow_directory:
        return False
    path = PurePosixPath(text)
    if path.is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in path.parts)


def looks_sensitive_key(key: str) -> bool:
    """Return whether a mapping key should be redacted before logging."""
    lowered = str(key or "").strip().lower()
    if lowered in _SAFE_SENSITIVE_SUMMARY_KEYS:
        return False
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def redact_sensitive_text(value: Any) -> str:
    """Redact passwords, phone numbers, and test canaries from free text."""
    text = str(value or "")
    if (
        SNAPSHOT_ID_PATTERN.fullmatch(text)
        or LISTING_ID_PATTERN.fullmatch(text)
        or INTERNAL_SAFE_ID_PATTERN.fullmatch(text)
        or re.fullmatch(r"[0-9a-f]{12,64}", text)
    ):
        return text
    text = _CANARY_PATTERN.sub(REDACTED_VALUE, text)
    text = _SECRET_CANARY_PATTERN.sub(REDACTED_VALUE, text)
    text = _TOKEN_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED_VALUE}", text)
    text = _PHONE_PATTERN.sub(REDACTED_VALUE, text)
    text = _HASHED_PASSWORD_PATTERN.sub(REDACTED_VALUE, text)
    return _PASSWORD_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED_VALUE}", text)


def sanitize_for_log(value: Any) -> Any:
    """Recursively redact sensitive content before logging or public serialization."""
    if is_dataclass(value):
        return {
            item.name: sanitize_for_log(getattr(value, item.name))
            for item in fields(value)
            if item.name != "private_viewing_secrets"
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if looks_sensitive_key(key_text):
                result[key_text] = REDACTED_VALUE
            else:
                result[key_text] = sanitize_for_log(item)
        return result
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


@dataclass(frozen=True)
class SnapshotValidationIssue:
    severity: str
    code: str
    message: str
    path: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "severity": self.severity,
                "code": self.code,
                "message": self.message,
                "path": self.path,
                "context": self.context,
            }
        )

    def __repr__(self) -> str:
        return f"SnapshotValidationIssue({self.to_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotValidationIssue":
        return cls(
            severity=str(data.get("severity") or ""),
            code=str(data.get("code") or ""),
            message=str(data.get("message") or ""),
            path=str(data.get("path") or ""),
            context=dict(data.get("context") or {}),
        )


@dataclass
class SnapshotValidationResult:
    issues: list[SnapshotValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[SnapshotValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[SnapshotValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def infos(self) -> list[SnapshotValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        path: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        self.issues.append(
            SnapshotValidationIssue(
                severity=severity,
                code=code,
                message=message,
                path=path,
                context=context or {},
            )
        )

    def extend(self, other: "SnapshotValidationResult") -> None:
        self.issues.extend(other.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.infos),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def __repr__(self) -> str:
        return f"SnapshotValidationResult({self.to_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotValidationResult":
        return cls(
            issues=[
                SnapshotValidationIssue.from_dict(item)
                for item in data.get("issues") or []
                if isinstance(item, dict)
            ]
        )


@dataclass
class InventorySourceMetadata:
    source_kind: str
    source_version: str = ""
    source_modified_at: str = ""
    sheet_metadata: dict[str, Any] = field(default_factory=dict)
    revision: str = ""
    range_ref: str = ""
    exported_xlsx_hash: str = ""
    field_mapping_version: str = FIELD_MAPPING_VERSION
    generator_version: str = GENERATOR_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        payload = {
            "source_kind": self.source_kind,
            "source_version": self.source_version,
            "source_modified_at": self.source_modified_at,
            "sheet_metadata": self.sheet_metadata,
            "revision": self.revision,
            "range_ref": self.range_ref,
            "exported_xlsx_hash": self.exported_xlsx_hash,
            "field_mapping_version": self.field_mapping_version,
            "generator_version": self.generator_version,
            "extra": self.extra,
        }
        return sanitize_for_log(payload) if redact_sensitive else payload

    def to_hash_payload(self) -> dict[str, Any]:
        """Return source metadata fields that participate in source_hash."""
        payload = self.to_dict(redact_sensitive=True)
        payload["generator_version"] = self.generator_version
        payload["field_mapping_version"] = self.field_mapping_version
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InventorySourceMetadata":
        return cls(
            source_kind=str(data.get("source_kind") or ""),
            source_version=str(data.get("source_version") or ""),
            source_modified_at=str(data.get("source_modified_at") or ""),
            sheet_metadata=dict(data.get("sheet_metadata") or {}),
            revision=str(data.get("revision") or ""),
            range_ref=str(data.get("range_ref") or ""),
            exported_xlsx_hash=str(data.get("exported_xlsx_hash") or ""),
            field_mapping_version=str(data.get("field_mapping_version") or FIELD_MAPPING_VERSION),
            generator_version=str(data.get("generator_version") or GENERATOR_VERSION),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class InventoryListing:
    listing_id: str
    source_record_id: str | None
    source_row_number: int
    raw_area: str
    area: str
    raw_community: str
    community: str
    raw_room_no: str
    room_no: str
    layout_desc: str = ""
    layout_type: str = ""
    raw_rent_monthly_pay1: str = ""
    rent_monthly_pay1: int | None = None
    raw_rent_monthly_pay2: str = ""
    rent_monthly_pay2: int | None = None
    viewing_secret_ref: str = ""
    viewing_summary: dict[str, Any] = field(default_factory=dict)
    remark: str = ""
    utility_summary: dict[str, Any] = field(default_factory=dict)
    availability_summary: dict[str, Any] = field(default_factory=dict)
    has_image: bool = False
    has_video: bool = False
    raw_fields: dict[str, str] = field(default_factory=dict)
    normalized_community: str = ""
    normalized_room_no: str = ""
    listing_key: str = ""
    source_record_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.normalized_community:
            self.normalized_community = normalize_listing_identity(self.community)
        if not self.normalized_room_no:
            self.normalized_room_no = normalize_listing_identity(self.room_no)
        if not self.listing_key:
            self.listing_key = f"{self.normalized_community}\0{self.normalized_room_no}"
        if self.source_record_id and self.source_record_id not in self.source_record_ids:
            self.source_record_ids.append(self.source_record_id)

    def to_dict(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        payload = {
            "listing_id": self.listing_id,
            "source_record_id": self.source_record_id,
            "source_row_number": self.source_row_number,
            "raw_area": self.raw_area,
            "area": self.area,
            "raw_community": self.raw_community,
            "community": self.community,
            "raw_room_no": self.raw_room_no,
            "room_no": self.room_no,
            "layout_desc": self.layout_desc,
            "layout_type": self.layout_type,
            "raw_rent_monthly_pay1": self.raw_rent_monthly_pay1,
            "rent_monthly_pay1": self.rent_monthly_pay1,
            "raw_rent_monthly_pay2": self.raw_rent_monthly_pay2,
            "rent_monthly_pay2": self.rent_monthly_pay2,
            "viewing_secret_ref": self.viewing_secret_ref,
            "viewing_summary": self.viewing_summary,
            "remark": self.remark,
            "utility_summary": self.utility_summary,
            "availability_summary": self.availability_summary,
            "has_image": self.has_image,
            "has_video": self.has_video,
            "raw_fields": self.raw_fields,
            "normalized_community": self.normalized_community,
            "normalized_room_no": self.normalized_room_no,
            "listing_key": self.listing_key,
            "source_record_ids": self.source_record_ids,
        }
        return sanitize_for_log(payload) if redact_sensitive else payload

    def to_log_dict(self) -> dict[str, Any]:
        return self.to_dict(redact_sensitive=True)

    def __repr__(self) -> str:
        return f"InventoryListing({self.to_log_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InventoryListing":
        return cls(
            listing_id=str(data.get("listing_id") or ""),
            source_record_id=(
                str(data.get("source_record_id"))
                if data.get("source_record_id") not in (None, "")
                else None
            ),
            source_row_number=int(data.get("source_row_number") or 0),
            raw_area=str(data.get("raw_area") or ""),
            area=str(data.get("area") or ""),
            raw_community=str(data.get("raw_community") or ""),
            community=str(data.get("community") or ""),
            raw_room_no=str(data.get("raw_room_no") or ""),
            room_no=str(data.get("room_no") or ""),
            layout_desc=str(data.get("layout_desc") or ""),
            layout_type=str(data.get("layout_type") or ""),
            raw_rent_monthly_pay1=str(data.get("raw_rent_monthly_pay1") or ""),
            rent_monthly_pay1=data.get("rent_monthly_pay1"),
            raw_rent_monthly_pay2=str(data.get("raw_rent_monthly_pay2") or ""),
            rent_monthly_pay2=data.get("rent_monthly_pay2"),
            viewing_secret_ref=str(data.get("viewing_secret_ref") or ""),
            viewing_summary=dict(data.get("viewing_summary") or {}),
            remark=str(data.get("remark") or ""),
            utility_summary=dict(data.get("utility_summary") or {}),
            availability_summary=dict(data.get("availability_summary") or {}),
            has_image=bool(data.get("has_image")),
            has_video=bool(data.get("has_video")),
            raw_fields={str(key): str(value) for key, value in dict(data.get("raw_fields") or {}).items()},
            normalized_community=str(data.get("normalized_community") or ""),
            normalized_room_no=str(data.get("normalized_room_no") or ""),
            listing_key=str(data.get("listing_key") or ""),
            source_record_ids=[str(item) for item in data.get("source_record_ids") or []],
        )


@dataclass
class InventorySnapshotManifest:
    schema_version: str
    snapshot_id: str
    source_hash: str
    source_version: str
    source_modified_at: str
    generated_at: str
    listing_count: int
    valid_listing_count: int
    rejected_row_count: int
    duplicate_count: int
    files: dict[str, Any] = field(default_factory=dict)
    generator_version: str = GENERATOR_VERSION

    def to_dict(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_hash": self.source_hash,
            "source_version": self.source_version,
            "source_modified_at": self.source_modified_at,
            "generated_at": self.generated_at,
            "listing_count": self.listing_count,
            "valid_listing_count": self.valid_listing_count,
            "rejected_row_count": self.rejected_row_count,
            "duplicate_count": self.duplicate_count,
            "files": self.files,
            "generator_version": self.generator_version,
        }
        return sanitize_for_log(payload) if redact_sensitive else payload

    def __repr__(self) -> str:
        return f"InventorySnapshotManifest({self.to_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InventorySnapshotManifest":
        return cls(
            schema_version=str(data.get("schema_version") or ""),
            snapshot_id=str(data.get("snapshot_id") or ""),
            source_hash=str(data.get("source_hash") or ""),
            source_version=str(data.get("source_version") or ""),
            source_modified_at=str(data.get("source_modified_at") or ""),
            generated_at=str(data.get("generated_at") or ""),
            listing_count=int(data.get("listing_count") or 0),
            valid_listing_count=int(data.get("valid_listing_count") or 0),
            rejected_row_count=int(data.get("rejected_row_count") or 0),
            duplicate_count=int(data.get("duplicate_count") or 0),
            files=dict(data.get("files") or {}),
            generator_version=str(data.get("generator_version") or GENERATOR_VERSION),
        )


@dataclass
class InventorySnapshot:
    schema_version: str
    snapshot_id: str
    source_hash: str
    generated_at: str
    source_metadata: InventorySourceMetadata
    manifest: InventorySnapshotManifest
    listings: list[InventoryListing] = field(default_factory=list)
    rewrite_index: dict[str, Any] = field(default_factory=dict)
    private_viewing_secrets: dict[str, Any] = field(default_factory=dict, repr=False)

    def inventory_payload(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_hash": self.source_hash,
            "generated_at": self.generated_at,
            "source_metadata": self.source_metadata.to_dict(redact_sensitive=redact_sensitive),
            "listing_count": len(self.listings),
            "listings": [
                listing.to_dict(redact_sensitive=redact_sensitive)
                for listing in self.listings
            ],
        }
        return sanitize_for_log(payload) if redact_sensitive else payload

    def to_dict(self, *, redact_sensitive: bool = True, include_rewrite_index: bool = True) -> dict[str, Any]:
        payload = self.inventory_payload(redact_sensitive=redact_sensitive)
        payload["manifest"] = self.manifest.to_dict(redact_sensitive=redact_sensitive)
        if include_rewrite_index:
            payload["rewrite_index"] = sanitize_for_log(self.rewrite_index) if redact_sensitive else self.rewrite_index
        return payload

    def to_json(self, *, redact_sensitive: bool = True) -> str:
        return json.dumps(self.to_dict(redact_sensitive=redact_sensitive), ensure_ascii=False, indent=2)

    def to_log_dict(self) -> dict[str, Any]:
        return self.to_dict(redact_sensitive=True)

    def __repr__(self) -> str:
        return f"InventorySnapshot({self.to_log_dict()!r})"

    @classmethod
    def from_inventory_payload(
        cls,
        data: dict[str, Any],
        *,
        manifest: InventorySnapshotManifest,
        rewrite_index: dict[str, Any] | None = None,
        private_viewing_secrets: dict[str, Any] | None = None,
    ) -> "InventorySnapshot":
        source_metadata = InventorySourceMetadata.from_dict(dict(data.get("source_metadata") or {}))
        return cls(
            schema_version=str(data.get("schema_version") or ""),
            snapshot_id=str(data.get("snapshot_id") or ""),
            source_hash=str(data.get("source_hash") or ""),
            generated_at=str(data.get("generated_at") or ""),
            source_metadata=source_metadata,
            manifest=manifest,
            listings=[
                InventoryListing.from_dict(item)
                for item in data.get("listings") or []
                if isinstance(item, dict)
            ],
            rewrite_index=rewrite_index or {},
            private_viewing_secrets=private_viewing_secrets or {},
        )


@dataclass
class InventorySnapshotHealth:
    status: str
    snapshot_id: str = ""
    message: str = ""
    age_seconds: int | None = None
    checked_at: str = field(default_factory=now_utc_iso)
    issues: list[SnapshotValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "status": self.status,
                "snapshot_id": self.snapshot_id,
                "message": self.message,
                "age_seconds": self.age_seconds,
                "checked_at": self.checked_at,
                "issues": [issue.to_dict() for issue in self.issues],
            }
        )

    def __repr__(self) -> str:
        return f"InventorySnapshotHealth({self.to_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InventorySnapshotHealth":
        return cls(
            status=str(data.get("status") or ""),
            snapshot_id=str(data.get("snapshot_id") or ""),
            message=str(data.get("message") or ""),
            age_seconds=data.get("age_seconds"),
            checked_at=str(data.get("checked_at") or now_utc_iso()),
            issues=[
                SnapshotValidationIssue.from_dict(item)
                for item in data.get("issues") or []
                if isinstance(item, dict)
            ],
        )


@dataclass
class InventorySyncReport:
    snapshot_id: str
    source_hash: str
    generated_at: str
    rows_read: int
    valid_listing_count: int
    rejected_rows: list[dict[str, Any]] = field(default_factory=list)
    filtered_rows: list[dict[str, Any]] = field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = field(default_factory=list)
    deduplicated_rows: list[dict[str, Any]] = field(default_factory=list)
    validation_result: SnapshotValidationResult = field(default_factory=SnapshotValidationResult)
    previous_snapshot_id: str = ""
    pointer_switched: bool = False
    duration_seconds: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.validation_result.ok

    def to_dict(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "snapshot_id": self.snapshot_id,
            "source_hash": self.source_hash,
            "generated_at": self.generated_at,
            "rows_read": self.rows_read,
            "valid_listing_count": self.valid_listing_count,
            "rejected_row_count": len(self.rejected_rows),
            "filtered_row_count": len(self.filtered_rows),
            "duplicate_count": len(self.duplicate_rows),
            "deduplicated_count": len(self.deduplicated_rows),
            "rejected_rows": self.rejected_rows,
            "filtered_rows": self.filtered_rows,
            "duplicate_rows": self.duplicate_rows,
            "deduplicated_rows": self.deduplicated_rows,
            "validation_result": self.validation_result.to_dict(),
            "previous_snapshot_id": self.previous_snapshot_id,
            "pointer_switched": self.pointer_switched,
            "duration_seconds": self.duration_seconds,
            "notes": self.notes,
        }
        return sanitize_for_log(payload) if redact_sensitive else payload

    def __repr__(self) -> str:
        return f"InventorySyncReport({self.to_dict()!r})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InventorySyncReport":
        return cls(
            snapshot_id=str(data.get("snapshot_id") or ""),
            source_hash=str(data.get("source_hash") or ""),
            generated_at=str(data.get("generated_at") or ""),
            rows_read=int(data.get("rows_read") or 0),
            valid_listing_count=int(data.get("valid_listing_count") or 0),
            rejected_rows=list(data.get("rejected_rows") or []),
            filtered_rows=list(data.get("filtered_rows") or []),
            duplicate_rows=list(data.get("duplicate_rows") or []),
            deduplicated_rows=list(data.get("deduplicated_rows") or []),
            validation_result=SnapshotValidationResult.from_dict(dict(data.get("validation_result") or {})),
            previous_snapshot_id=str(data.get("previous_snapshot_id") or ""),
            pointer_switched=bool(data.get("pointer_switched")),
            duration_seconds=data.get("duration_seconds"),
            notes=[str(item) for item in data.get("notes") or []],
        )


@dataclass
class CurrentSnapshotPointer:
    snapshot_id: str
    source_hash: str
    snapshot_path: str
    created_at: str
    activated_at: str
    row_count: int
    health: InventorySnapshotHealth = field(default_factory=lambda: InventorySnapshotHealth(status="ok"))
    schema_version: str = CURRENT_POINTER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_hash": self.source_hash,
            "snapshot_path": self.snapshot_path,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "row_count": self.row_count,
            "health": self.health.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CurrentSnapshotPointer":
        return cls(
            schema_version=str(data.get("schema_version") or ""),
            snapshot_id=str(data.get("snapshot_id") or ""),
            source_hash=str(data.get("source_hash") or ""),
            snapshot_path=str(data.get("snapshot_path") or ""),
            created_at=str(data.get("created_at") or ""),
            activated_at=str(data.get("activated_at") or ""),
            row_count=int(data.get("row_count") or 0),
            health=InventorySnapshotHealth.from_dict(dict(data.get("health") or {"status": "unknown"})),
        )


@dataclass
class SnapshotReadResult:
    ok: bool
    value: Any = None
    code: str = ""
    message: str = ""
    status: str = ""
    issues: list[SnapshotValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        return sanitize_for_log(
            {
                "ok": self.ok,
                "code": self.code,
                "message": self.message,
                "status": self.status,
                "issues": [issue.to_dict() for issue in self.issues],
                "value": value,
            }
        )

    def __repr__(self) -> str:
        return f"SnapshotReadResult({self.to_dict()!r})"
