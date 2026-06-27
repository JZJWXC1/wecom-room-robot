from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping

from app.services.inventory_snapshot_models import now_utc_iso, sanitize_for_log


SOURCE_KIND_LEGACY = "legacy"
SOURCE_KIND_SNAPSHOT = "snapshot"
READ_MODE_DISABLED = "disabled"
READ_MODE_SHADOW = "shadow"
READ_MODE_PRIMARY = "primary"
FALLBACK_STRICT = "strict"
FALLBACK_LEGACY_WHOLE_REQUEST = "legacy_whole_request"

SUPPORTED_SOURCE_KINDS = {SOURCE_KIND_LEGACY, SOURCE_KIND_SNAPSHOT}
SUPPORTED_READ_MODES = {READ_MODE_DISABLED, READ_MODE_SHADOW, READ_MODE_PRIMARY}
SUPPORTED_FALLBACK_STRATEGIES = {FALLBACK_STRICT, FALLBACK_LEGACY_WHOLE_REQUEST}

REASON_SNAPSHOT_POINTER_MISSING = "snapshot_pointer_missing"
REASON_SNAPSHOT_STALE = "snapshot_stale"
REASON_SNAPSHOT_INTEGRITY_FAILED = "snapshot_integrity_failed"
REASON_RECONCILIATION_BLOCKING = "reconciliation_blocking"
REASON_PRIMARY_READINESS_MISSING = "primary_readiness_missing"
REASON_PRIMARY_READINESS_MISMATCH = "primary_readiness_mismatch"
REASON_SECRET_SCAN_FAILED = "secret_scan_failed"
REASON_ALIAS_COVERAGE_FAILED = "alias_coverage_failed"
REASON_UNSUPPORTED_SCHEMA = "unsupported_schema"
REASON_SNAPSHOT_READ_FAILED = "snapshot_read_failed"
REASON_MISSING_SNAPSHOT = "missing_snapshot"
REASON_SOURCE_UNAVAILABLE = "source_unavailable"
REASON_FALLBACK_USED = "fallback_used"
REASON_FALLBACK_NOT_ALLOWED_AFTER_READ = "fallback_not_allowed_after_read"
REASON_INVALID_MODE = "invalid_inventory_read_mode"
REASON_CONTEXT_PROVIDER_MISMATCH = "context_provider_mismatch"
REASON_CONTEXT_SNAPSHOT_MISMATCH = "context_snapshot_mismatch"
REASON_MIXED_SOURCE_EVIDENCE = "mixed_source_evidence"
REASON_MIXED_SOURCE_HASH = "mixed_source_hash"

SENSITIVE_OUTPUT_KEYS = {
    "viewing",
    "viewing_text",
    "raw_viewing_text",
    "password",
    "password_text",
    "token",
    "secret",
    "phone",
    "mobile",
    "private_link",
    "看房方式密码",
    "看房密码",
    "密码",
}


class InventoryReadError(RuntimeError):
    """Structured local read-routing error with safe serialization."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = _freeze_json(details or {})

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "code": self.code,
                "message": self.message,
                "details": _plain_json(self.details),
            }
        )


@dataclass(frozen=True)
class InventoryReadHealth:
    status: str
    source_kind: str = ""
    code: str = ""
    message: str = ""
    checked_at: str = field(default_factory=now_utc_iso)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze_json(sanitize_for_log(dict(self.details))))

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "status": self.status,
                "source_kind": self.source_kind,
                "code": self.code,
                "message": self.message,
                "checked_at": self.checked_at,
                "details": _plain_json(self.details),
            }
        )


@dataclass(frozen=True)
class InventoryReadContext:
    request_id: str
    turn_id: str
    source_kind: str
    source_hash: str
    schema_version: str
    selected_at: str
    decision_id: str
    snapshot_id: str = ""
    fallback_used: bool = False
    fallback_reason: str = ""
    health_at_selection: Mapping[str, Any] = field(default_factory=dict)
    selection_mode: str = READ_MODE_DISABLED

    def __post_init__(self) -> None:
        if self.source_kind not in SUPPORTED_SOURCE_KINDS:
            raise InventoryReadError(
                REASON_CONTEXT_PROVIDER_MISMATCH,
                f"unsupported source_kind: {self.source_kind}",
            )
        if self.source_kind == SOURCE_KIND_LEGACY and self.snapshot_id:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "legacy context must not carry snapshot_id",
            )
        if self.source_kind == SOURCE_KIND_SNAPSHOT and not self.snapshot_id:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "snapshot context requires snapshot_id",
            )
        object.__setattr__(
            self,
            "health_at_selection",
            _freeze_json(sanitize_for_log(dict(self.health_at_selection))),
        )

    def to_log_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "request_id": self.request_id,
                "turn_id": self.turn_id,
                "source_kind": self.source_kind,
                "snapshot_id": self.snapshot_id,
                "source_hash": self.source_hash,
                "schema_version": self.schema_version,
                "selected_at": self.selected_at,
                "fallback_used": self.fallback_used,
                "fallback_reason": self.fallback_reason,
                "health_at_selection": _plain_json(self.health_at_selection),
                "selection_mode": self.selection_mode,
                "decision_id": self.decision_id,
            }
        )


@dataclass(frozen=True)
class InventoryListingEvidence:
    evidence_id: str
    decision_id: str
    listing_id: str
    source_kind: str
    source_hash: str
    schema_version: str
    area: str
    community: str
    room_no: str
    layout_desc: str = ""
    layout_type: str = ""
    rent_pay1: int | None = None
    rent_pay2: int | None = None
    utility_summary: Mapping[str, Any] = field(default_factory=dict)
    availability_summary: Mapping[str, Any] = field(default_factory=dict)
    has_image: bool = False
    has_video: bool = False
    fetched_at: str = field(default_factory=now_utc_iso)
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "room_no", str(self.room_no or ""))
        object.__setattr__(self, "utility_summary", _freeze_json(sanitize_for_log(dict(self.utility_summary))))
        object.__setattr__(
            self,
            "availability_summary",
            _freeze_json(sanitize_for_log(dict(self.availability_summary))),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "evidence_id": self.evidence_id,
            "decision_id": self.decision_id,
            "listing_id": self.listing_id,
            "source_kind": self.source_kind,
            "snapshot_id": self.snapshot_id,
            "source_hash": self.source_hash,
            "schema_version": self.schema_version,
            "area": self.area,
            "community": self.community,
            "room_no": self.room_no,
            "layout_desc": self.layout_desc,
            "layout_type": self.layout_type,
            "rent_pay1": self.rent_pay1,
            "rent_pay2": self.rent_pay2,
            "utility_summary": _plain_json(self.utility_summary),
            "availability_summary": _plain_json(self.availability_summary),
            "has_image": self.has_image,
            "has_video": self.has_video,
            "fetched_at": self.fetched_at,
        }
        return sanitize_for_log(_strip_sensitive_keys(payload))


@dataclass(frozen=True)
class InventoryReadDecision:
    ok: bool
    mode: str
    fallback_strategy: str
    decision_id: str
    context: InventoryReadContext | None = None
    error: InventoryReadError | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "ok": self.ok,
                "mode": self.mode,
                "fallback_strategy": self.fallback_strategy,
                "decision_id": self.decision_id,
                "context": self.context.to_log_dict() if self.context else None,
                "error": self.error.to_dict() if self.error else None,
                "reasons": list(self.reasons),
            }
        )


def make_decision_id(*parts: Any) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return "ird_" + sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_evidence_id(context: InventoryReadContext, listing_id: str) -> str:
    payload = "\0".join(
        [
            context.decision_id,
            context.source_kind,
            context.snapshot_id,
            context.source_hash,
            listing_id,
        ]
    )
    return "evd_" + sha256(payload.encode("utf-8")).hexdigest()[:16]


def ensure_provider_context(source_kind: str, context: InventoryReadContext) -> None:
    if source_kind != context.source_kind:
        raise InventoryReadError(
            REASON_CONTEXT_PROVIDER_MISMATCH,
            "provider source_kind does not match InventoryReadContext",
            details={"provider_source_kind": source_kind, "context_source_kind": context.source_kind},
        )


def assert_evidence_consistency(
    context: InventoryReadContext,
    evidence: list[InventoryListingEvidence],
) -> None:
    if not evidence:
        return
    source_kinds = {item.source_kind for item in evidence}
    if source_kinds != {context.source_kind}:
        raise InventoryReadError(
            REASON_MIXED_SOURCE_EVIDENCE,
            "evidence source_kind must match the request context",
            details={"source_kinds": sorted(source_kinds), "context_source_kind": context.source_kind},
        )
    if context.source_kind == SOURCE_KIND_SNAPSHOT:
        snapshot_ids = {item.snapshot_id for item in evidence}
        if snapshot_ids != {context.snapshot_id}:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "snapshot evidence must use the context snapshot_id",
                details={"snapshot_ids": sorted(snapshot_ids), "context_snapshot_id": context.snapshot_id},
            )
    source_hashes = {item.source_hash for item in evidence}
    if source_hashes != {context.source_hash}:
        raise InventoryReadError(
            REASON_MIXED_SOURCE_HASH,
            "one evidence result set must not mix multiple source_hash values",
            details={"source_hashes": sorted(source_hashes), "context_source_hash": context.source_hash},
        )
    decision_ids = {item.decision_id for item in evidence}
    if decision_ids != {context.decision_id}:
        raise InventoryReadError(
            REASON_CONTEXT_PROVIDER_MISMATCH,
            "evidence decision_id must match the request context",
            details={"decision_ids": sorted(decision_ids), "context_decision_id": context.decision_id},
        )


def _strip_sensitive_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in SENSITIVE_OUTPUT_KEYS or key_text.lower() in SENSITIVE_OUTPUT_KEYS:
                continue
            result[key_text] = _strip_sensitive_keys(item)
        return result
    if isinstance(value, list):
        return [_strip_sensitive_keys(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_sensitive_keys(item) for item in value]
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def stable_safe_hash(value: Any) -> str:
    payload = json.dumps(sanitize_for_log(_plain_json(value)), ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()
