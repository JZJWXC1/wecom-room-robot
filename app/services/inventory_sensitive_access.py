from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Protocol

from app.config import settings
from app.services.inventory_read_models import (
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadError,
    make_evidence_id,
)
from app.services.inventory_snapshot_models import (
    generate_listing_id,
    now_utc_iso,
    sanitize_for_log,
)
from app.services.inventory_snapshot_reader import SnapshotReader


PURPOSE_ASK_VIEWING = "ask_viewing"
PURPOSE_ASK_PASSWORD = "ask_password"
PURPOSE_PASSWORD_FAILED = "password_failed"
PURPOSE_DOOR_CANNOT_OPEN = "door_cannot_open"
PURPOSE_AVAILABILITY_VIEWING = "availability_viewing"

REASON_VIEWING_ACCESS_BLOCKED = "viewing_access_blocked"
REASON_VIEWING_LISTING_MISSING = "viewing_listing_missing"
REASON_VIEWING_BATCH_PASSWORD_BLOCKED = "viewing_batch_password_blocked"
REASON_SHEET_ARTIFACT_MISSING = "sheet_artifact_missing"
REASON_SHEET_ARTIFACT_MISMATCH = "sheet_artifact_mismatch"

_PASSWORD_PATTERN = re.compile(r"\d{3,8}#?")


class ViewingAccessError(InventoryReadError):
    pass


class InventorySheetArtifactError(InventoryReadError):
    pass


@dataclass(frozen=True)
class SecretValue:
    _raw: str = field(repr=False)
    source_evidence_id: str = ""

    @property
    def has_secret(self) -> bool:
        return bool(self._raw)

    @property
    def masked_value(self) -> str:
        return "[REDACTED]" if self._raw else ""

    def reveal_for_authorized_render(self) -> str:
        return self._raw

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "has_secret": self.has_secret,
            "masked_value": self.masked_value,
            "source_evidence_id": self.source_evidence_id,
        }

    def __repr__(self) -> str:
        return f"SecretValue({self.to_log_dict()!r})"

    def __str__(self) -> str:
        return self.masked_value


@dataclass(frozen=True)
class ViewingAccessRequest:
    request_id: str
    turn_id: str
    task_id: str
    decision_id: str
    inventory_read_context: InventoryReadContext
    listing_id: str
    purpose: str
    user_explicitly_requested: bool
    audience: str
    requested_fields: tuple[str, ...] = ("viewing",)
    batch_size: int = 1

    def __post_init__(self) -> None:
        if not self.listing_id:
            raise ViewingAccessError(
                REASON_VIEWING_LISTING_MISSING,
                "viewing access requires a unique listing_id",
            )
        if self.decision_id != self.inventory_read_context.decision_id:
            raise ViewingAccessError(
                REASON_VIEWING_ACCESS_BLOCKED,
                "viewing access decision_id must match inventory read context",
                details={
                    "decision_id": self.decision_id,
                    "context_decision_id": self.inventory_read_context.decision_id,
                },
            )

    @property
    def requests_password(self) -> bool:
        return "password" in set(self.requested_fields)


@dataclass(frozen=True)
class ViewingInstructionEvidence:
    evidence_id: str
    decision_id: str
    source_kind: str
    source_hash: str
    schema_version: str
    listing_id: str
    room: str
    purpose: str
    has_password: bool
    needs_contact: bool
    future_or_unavailable: bool
    contact_numbers: tuple[str, ...] = ()
    reason: str = ""
    snapshot_id: str = ""
    viewing_text: SecretValue | None = None
    safe_viewing_text: str = ""
    fetched_at: str = field(default_factory=now_utc_iso)

    def to_log_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "evidence_id": self.evidence_id,
                "decision_id": self.decision_id,
                "source_kind": self.source_kind,
                "snapshot_id": self.snapshot_id,
                "source_hash": self.source_hash,
                "schema_version": self.schema_version,
                "listing_id": self.listing_id,
                "room": self.room,
                "purpose": self.purpose,
                "has_password": self.has_password,
                "needs_contact": self.needs_contact,
                "future_or_unavailable": self.future_or_unavailable,
                "contact_numbers": list(self.contact_numbers),
                "reason": self.reason,
                "viewing_text": self.viewing_text.to_log_dict() if self.viewing_text else {},
                "safe_viewing_text": self.safe_viewing_text,
                "fetched_at": self.fetched_at,
            }
        )

    def to_public_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        viewing = self.safe_viewing_text
        if include_secret and self.viewing_text is not None:
            viewing = self.viewing_text.reveal_for_authorized_render()
        return sanitize_for_log(
            {
                "evidence_id": self.evidence_id,
                "decision_id": self.decision_id,
                "source_kind": self.source_kind,
                "snapshot_id": self.snapshot_id,
                "source_hash": self.source_hash,
                "schema_version": self.schema_version,
                "listing_id": self.listing_id,
                "room": self.room,
                "viewing": viewing,
                "has_password": self.has_password,
                "needs_contact": self.needs_contact,
                "future_or_unavailable": self.future_or_unavailable,
                "contact_numbers": list(self.contact_numbers),
                "reason": self.reason,
            }
        )

    def to_legacy_rule_dict(self, *, include_secret: bool = True) -> dict[str, Any]:
        viewing = self.safe_viewing_text
        if include_secret and self.viewing_text is not None:
            viewing = self.viewing_text.reveal_for_authorized_render()
        return {
            "room": self.room,
            "viewing": viewing,
            "has_password": self.has_password,
            "needs_contact": self.needs_contact,
            "future_or_unavailable": self.future_or_unavailable,
            "contact_numbers": list(self.contact_numbers),
            "reason": self.reason,
            "normalized": viewing.replace(" ", ""),
            "listing_id": self.listing_id,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class InventorySheetArtifactEvidence:
    evidence_id: str
    decision_id: str
    source_kind: str
    source_hash: str
    schema_version: str
    artifact_kind: str
    safe_filename: str
    relative_path: str
    sha256: str
    byte_size: int
    mime_type: str
    generated_at: str
    snapshot_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "evidence_id": self.evidence_id,
                "decision_id": self.decision_id,
                "source_kind": self.source_kind,
                "snapshot_id": self.snapshot_id,
                "source_hash": self.source_hash,
                "schema_version": self.schema_version,
                "artifact_kind": self.artifact_kind,
                "safe_filename": self.safe_filename,
                "relative_path": self.relative_path,
                "sha256": self.sha256,
                "byte_size": self.byte_size,
                "mime_type": self.mime_type,
                "generated_at": self.generated_at,
            }
        )


@dataclass(frozen=True)
class InventorySheetArtifactResult:
    paths: tuple[Path, ...]
    evidence: tuple[InventorySheetArtifactEvidence, ...]
    error: dict[str, Any] = field(default_factory=dict)


class InventoryViewingAccessProvider(Protocol):
    async def get_viewing_instruction(self, request: ViewingAccessRequest) -> ViewingInstructionEvidence:
        ...


class LegacyInventoryViewingAccessProvider:
    source_kind = SOURCE_KIND_LEGACY

    def __init__(
        self,
        rows_by_listing_id: dict[str, dict[str, Any]],
        *,
        row_labeler: Callable[[dict[str, Any]], str],
        viewing_text_getter: Callable[[dict[str, Any]], str],
        contact_numbers: tuple[str, ...],
    ) -> None:
        self.rows_by_listing_id = rows_by_listing_id
        self.row_labeler = row_labeler
        self.viewing_text_getter = viewing_text_getter
        self.contact_numbers = contact_numbers

    async def get_viewing_instruction(self, request: ViewingAccessRequest) -> ViewingInstructionEvidence:
        _ensure_context_kind(request.inventory_read_context, self.source_kind)
        row = self.rows_by_listing_id.get(request.listing_id)
        if row is None:
            raise ViewingAccessError(
                REASON_VIEWING_LISTING_MISSING,
                "legacy viewing row not found for listing_id",
                details={"listing_id": request.listing_id},
            )
        viewing = str(self.viewing_text_getter(row) or "")
        room = str(self.row_labeler(row) or request.listing_id)
        return _viewing_evidence_from_text(
            request=request,
            viewing=viewing,
            room=room,
            contact_numbers=self.contact_numbers,
        )


class SnapshotInventoryViewingAccessProvider:
    source_kind = SOURCE_KIND_SNAPSHOT

    def __init__(self, reader: SnapshotReader | None = None) -> None:
        self.reader = reader or _configured_snapshot_reader()

    async def get_viewing_instruction(self, request: ViewingAccessRequest) -> ViewingInstructionEvidence:
        context = request.inventory_read_context
        _ensure_context_kind(context, self.source_kind)
        snapshot_result = self.reader.get_snapshot(context.snapshot_id)
        if not snapshot_result.ok:
            raise ViewingAccessError(
                REASON_VIEWING_ACCESS_BLOCKED,
                snapshot_result.message,
                details={"code": snapshot_result.code, "status": snapshot_result.status},
            )
        snapshot = snapshot_result.value
        if snapshot.snapshot_id != context.snapshot_id or snapshot.source_hash != context.source_hash:
            raise ViewingAccessError(
                REASON_VIEWING_ACCESS_BLOCKED,
                "snapshot viewing source does not match context",
                details={"snapshot_id": snapshot.snapshot_id, "source_hash": snapshot.source_hash},
            )
        listing = next((item for item in snapshot.listings if item.listing_id == request.listing_id), None)
        if listing is None:
            raise ViewingAccessError(
                REASON_VIEWING_LISTING_MISSING,
                "snapshot listing not found for viewing access",
                details={"listing_id": request.listing_id},
            )
        secret = dict(snapshot.private_viewing_secrets.get(request.listing_id) or {})
        if not secret:
            raise ViewingAccessError(
                REASON_VIEWING_LISTING_MISSING,
                "snapshot private viewing record missing for listing_id",
                details={"listing_id": request.listing_id},
            )
        if str(secret.get("snapshot_id") or "") != context.snapshot_id:
            raise ViewingAccessError(
                REASON_VIEWING_ACCESS_BLOCKED,
                "snapshot private viewing record snapshot_id mismatch",
                details={"listing_id": request.listing_id},
            )
        viewing = str(secret.get("viewing_text") or "")
        return _viewing_evidence_from_text(
            request=request,
            viewing=viewing,
            room=f"{listing.community}{listing.room_no}",
            contact_numbers=(),
        )


class LegacyInventorySheetArtifactProvider:
    source_kind = SOURCE_KIND_LEGACY

    def __init__(
        self,
        *,
        refresh_func: Callable[[], Awaitable[Any]],
        list_paths_func: Callable[[], list[Path]],
    ) -> None:
        self.refresh_func = refresh_func
        self.list_paths_func = list_paths_func

    async def get_artifacts(self, context: InventoryReadContext) -> InventorySheetArtifactResult:
        _ensure_context_kind(context, self.source_kind)
        error: dict[str, Any] = {}
        try:
            await self.refresh_func()
        except Exception as exc:
            error = {
                "code": type(exc).__name__,
                "message": str(exc),
            }
        paths = tuple(Path(path) for path in self.list_paths_func())
        if not paths and not error:
            error = {
                "code": REASON_SHEET_ARTIFACT_MISSING,
                "message": "legacy inventory sheet PNG artifact is missing",
            }
        evidence = tuple(_artifact_evidence_for_path(context, path) for path in paths)
        return InventorySheetArtifactResult(paths=paths, evidence=evidence, error=sanitize_for_log(error))


class SnapshotInventorySheetArtifactProvider:
    source_kind = SOURCE_KIND_SNAPSHOT

    def __init__(self, reader: SnapshotReader | None = None) -> None:
        self.reader = reader or _configured_snapshot_reader()

    async def get_artifacts(self, context: InventoryReadContext) -> InventorySheetArtifactResult:
        _ensure_context_kind(context, self.source_kind)
        snapshot_result = self.reader.get_snapshot(context.snapshot_id)
        if not snapshot_result.ok:
            issue_codes = [str(issue.code) for issue in snapshot_result.issues]
            reason = (
                REASON_SHEET_ARTIFACT_MISMATCH
                if any("hash" in code or "size" in code for code in issue_codes)
                else REASON_SHEET_ARTIFACT_MISSING
            )
            raise InventorySheetArtifactError(
                reason,
                snapshot_result.message,
                details={"code": snapshot_result.code, "status": snapshot_result.status, "issues": issue_codes},
            )
        snapshot = snapshot_result.value
        if snapshot.snapshot_id != context.snapshot_id or snapshot.source_hash != context.source_hash:
            raise InventorySheetArtifactError(
                REASON_SHEET_ARTIFACT_MISMATCH,
                "snapshot sheet artifact source does not match context",
            )
        root = self.reader.root / "snapshots" / context.snapshot_id
        entries = _manifest_png_entries(snapshot.manifest.files)
        if not entries:
            raise InventorySheetArtifactError(
                REASON_SHEET_ARTIFACT_MISSING,
                "snapshot manifest does not declare inventory sheet PNG artifacts",
            )
        paths: list[Path] = []
        evidence: list[InventorySheetArtifactEvidence] = []
        for entry in entries:
            relative_path = str(entry.get("path") or "")
            path = (root / relative_path).resolve()
            if not path.is_file():
                raise InventorySheetArtifactError(
                    REASON_SHEET_ARTIFACT_MISSING,
                    "snapshot sheet PNG artifact is missing",
                    details={"path": relative_path},
                )
            expected_hash = str(entry.get("sha256") or "")
            expected_bytes = int(entry.get("bytes") or 0)
            actual_hash = _file_sha256(path)
            actual_bytes = path.stat().st_size
            if expected_hash and expected_hash != actual_hash:
                raise InventorySheetArtifactError(
                    REASON_SHEET_ARTIFACT_MISMATCH,
                    "snapshot sheet PNG hash mismatch",
                    details={"path": relative_path},
                )
            if expected_bytes and expected_bytes != actual_bytes:
                raise InventorySheetArtifactError(
                    REASON_SHEET_ARTIFACT_MISMATCH,
                    "snapshot sheet PNG byte size mismatch",
                    details={"path": relative_path},
                )
            paths.append(path)
            evidence.append(
                _artifact_evidence_for_path(
                    context,
                    path,
                    relative_path=relative_path,
                    sha256_value=actual_hash,
                    byte_size=actual_bytes,
                )
            )
        return InventorySheetArtifactResult(paths=tuple(paths), evidence=tuple(evidence))


def legacy_listing_id_for_row(row: dict[str, Any]) -> str:
    community = str(row.get("小区") or row.get("小区名") or row.get("community") or "").strip()
    room_no = str(row.get("房号") or row.get("房间号") or row.get("room_no") or "").strip()
    return generate_listing_id(community or "unknown", room_no or "unknown")


def purpose_from_text(content: str) -> str:
    text = str(content or "")
    if "密码不对" in text:
        return PURPOSE_PASSWORD_FAILED
    if "打不开" in text or "门打不开" in text:
        return PURPOSE_DOOR_CANNOT_OPEN
    if any(word in text for word in ("还没空", "未空", "空出")):
        return PURPOSE_AVAILABILITY_VIEWING
    if "密码" in text:
        return PURPOSE_ASK_PASSWORD
    return PURPOSE_ASK_VIEWING


def requested_fields_from_text(content: str) -> tuple[str, ...]:
    purpose = purpose_from_text(content)
    if purpose in {PURPOSE_ASK_PASSWORD, PURPOSE_PASSWORD_FAILED, PURPOSE_DOOR_CANNOT_OPEN}:
        return ("viewing", "password")
    if any(word in str(content or "") for word in ("自助", "怎么看", "看房", "今天能看")):
        return ("viewing", "password")
    return ("viewing",)


async def viewing_evidence_for_rows(
    *,
    context: InventoryReadContext,
    rows: list[dict[str, Any]],
    content: str,
    row_labeler: Callable[[dict[str, Any]], str],
    viewing_text_getter: Callable[[dict[str, Any]], str],
    contact_numbers: tuple[str, ...],
    snapshot_reader: SnapshotReader | None = None,
    task_id: str = "viewing",
    audience: str = "broker",
) -> tuple[list[ViewingInstructionEvidence], dict[str, Any]]:
    if not rows:
        return [], {"rooms": [], "contact_numbers": list(contact_numbers)}
    requested_fields = requested_fields_from_text(content)
    batch_size = len(rows)
    include_secret = "password" in requested_fields and batch_size == 1
    rows_by_listing_id: dict[str, dict[str, Any]] = {}
    duplicate_listing_ids: set[str] = set()
    for row in rows:
        listing_id = legacy_listing_id_for_row(row)
        if listing_id in rows_by_listing_id:
            duplicate_listing_ids.add(listing_id)
        rows_by_listing_id[listing_id] = row
    if duplicate_listing_ids:
        raise ViewingAccessError(
            REASON_VIEWING_ACCESS_BLOCKED,
            "viewing access requires unique listing_id per target row",
            details={"duplicate_listing_count": len(duplicate_listing_ids)},
        )
    provider: InventoryViewingAccessProvider
    if context.source_kind == SOURCE_KIND_SNAPSHOT:
        provider = SnapshotInventoryViewingAccessProvider(snapshot_reader)
    else:
        provider = LegacyInventoryViewingAccessProvider(
            rows_by_listing_id,
            row_labeler=row_labeler,
            viewing_text_getter=viewing_text_getter,
            contact_numbers=contact_numbers,
        )
    evidence: list[ViewingInstructionEvidence] = []
    legacy_rooms: list[dict[str, Any]] = []
    for row in rows:
        listing_id = legacy_listing_id_for_row(row)
        request = ViewingAccessRequest(
            request_id=context.request_id,
            turn_id=context.turn_id,
            task_id=task_id,
            decision_id=context.decision_id,
            inventory_read_context=context,
            listing_id=listing_id,
            purpose=purpose_from_text(content),
            user_explicitly_requested=True,
            audience=audience,
            requested_fields=requested_fields,
            batch_size=batch_size,
        )
        item = await provider.get_viewing_instruction(request)
        evidence.append(item)
        legacy_rooms.append(item.to_legacy_rule_dict(include_secret=include_secret))
    payload = {
        "rooms": legacy_rooms,
        "contact_numbers": list(contact_numbers),
    }
    if "password" in requested_fields and batch_size > 1:
        payload["batch_password_blocked"] = True
        payload["block_reason"] = REASON_VIEWING_BATCH_PASSWORD_BLOCKED
    assert_sensitive_evidence_consistency(context, viewing_evidence=evidence)
    return evidence, payload


async def sheet_artifacts_for_context(
    *,
    context: InventoryReadContext,
    refresh_func: Callable[[], Awaitable[Any]],
    list_paths_func: Callable[[], list[Path]],
    snapshot_reader: SnapshotReader | None = None,
) -> InventorySheetArtifactResult:
    if context.source_kind == SOURCE_KIND_SNAPSHOT:
        result = await SnapshotInventorySheetArtifactProvider(snapshot_reader).get_artifacts(context)
    else:
        result = await LegacyInventorySheetArtifactProvider(
            refresh_func=refresh_func,
            list_paths_func=list_paths_func,
        ).get_artifacts(context)
    assert_sensitive_evidence_consistency(context, sheet_evidence=list(result.evidence))
    return result


def assert_sensitive_evidence_consistency(
    context: InventoryReadContext,
    *,
    listing_evidence: list[InventoryListingEvidence | dict[str, Any]] | None = None,
    viewing_evidence: list[ViewingInstructionEvidence | dict[str, Any]] | None = None,
    sheet_evidence: list[InventorySheetArtifactEvidence | dict[str, Any]] | None = None,
) -> None:
    for item in [*(listing_evidence or []), *(viewing_evidence or []), *(sheet_evidence or [])]:
        if hasattr(item, "to_dict"):
            payload = item.to_dict()  # type: ignore[assignment]
        elif hasattr(item, "to_log_dict"):
            payload = item.to_log_dict()  # type: ignore[assignment]
        else:
            payload = dict(item)  # type: ignore[arg-type]
        if payload.get("decision_id") and payload.get("decision_id") != context.decision_id:
            raise InventoryReadError(
                "mixed_inventory_decision_id",
                "inventory evidence decision_id does not match context",
            )
        if str(payload.get("source_kind") or "") != context.source_kind:
            raise InventoryReadError(
                "mixed_inventory_source_kind",
                "inventory evidence source_kind does not match context",
            )
        if str(payload.get("source_hash") or "") != context.source_hash:
            raise InventoryReadError(
                "mixed_inventory_source_hash",
                "inventory evidence source_hash does not match context",
            )
        if str(payload.get("snapshot_id") or "") != context.snapshot_id:
            raise InventoryReadError(
                "mixed_inventory_snapshot_id",
                "inventory evidence snapshot_id does not match context",
            )


def safe_rule_evidence_for_summary(rule_evidence: dict[str, Any]) -> dict[str, Any]:
    payload = dict(rule_evidence or {})
    viewing = payload.get("viewing")
    if isinstance(viewing, dict):
        rooms = []
        for room in viewing.get("rooms") or []:
            if not isinstance(room, dict):
                continue
            item = dict(room)
            if item.get("has_password"):
                item["viewing"] = _mask_password(str(item.get("viewing") or ""))
                item["normalized"] = _mask_password(str(item.get("normalized") or ""))
            rooms.append(item)
        viewing = dict(viewing)
        viewing["rooms"] = rooms
        payload["viewing"] = viewing
    return sanitize_for_log(payload)


def _viewing_evidence_from_text(
    *,
    request: ViewingAccessRequest,
    viewing: str,
    room: str,
    contact_numbers: tuple[str, ...],
) -> ViewingInstructionEvidence:
    context = request.inventory_read_context
    has_password = bool(_PASSWORD_PATTERN.search(viewing))
    future_or_unavailable = bool(re.search(r"\d{1,2}\.\d{1,2}\s*空出|空出|未空|未入住", viewing))
    needs_contact = (
        not has_password
        or any(word in viewing for word in ("提前联系", "预约", "转租", "联系", "密码不对", "打不开"))
        or future_or_unavailable
    )
    evidence_id = make_evidence_id(context, f"{request.listing_id}:viewing:{request.purpose}")
    secret = (
        SecretValue(viewing, source_evidence_id=evidence_id)
        if viewing and request.user_explicitly_requested and request.requests_password and request.batch_size <= 1
        else None
    )
    return ViewingInstructionEvidence(
        evidence_id=evidence_id,
        decision_id=context.decision_id,
        source_kind=context.source_kind,
        snapshot_id=context.snapshot_id,
        source_hash=context.source_hash,
        schema_version=context.schema_version,
        listing_id=request.listing_id,
        room=room,
        purpose=request.purpose,
        has_password=has_password,
        needs_contact=needs_contact,
        future_or_unavailable=future_or_unavailable,
        contact_numbers=contact_numbers if needs_contact else (),
        reason="需联系确认/预约或还未空出" if needs_contact else "可按看房方式密码自助查看",
        viewing_text=secret,
        safe_viewing_text=_mask_password(viewing) if has_password else viewing,
    )


def _mask_password(value: str) -> str:
    text = str(value or "")
    masked = _PASSWORD_PATTERN.sub("", text).strip(" #，,；;。")
    return masked or ("可自助看房，具体密码按房源确认" if text else "")


def _ensure_context_kind(context: InventoryReadContext, source_kind: str) -> None:
    if context.source_kind != source_kind:
        raise InventoryReadError(
            "context_provider_mismatch",
            "sensitive inventory provider source_kind does not match context",
            details={"context_source_kind": context.source_kind, "provider_source_kind": source_kind},
        )


def _artifact_evidence_for_path(
    context: InventoryReadContext,
    path: Path,
    *,
    relative_path: str | None = None,
    sha256_value: str | None = None,
    byte_size: int | None = None,
) -> InventorySheetArtifactEvidence:
    relative = relative_path or _safe_relative_path(path)
    file_hash = sha256_value or _file_sha256(path)
    size = int(byte_size if byte_size is not None else path.stat().st_size)
    evidence_id = make_evidence_id(context, f"sheet:{relative}:{file_hash}")
    return InventorySheetArtifactEvidence(
        evidence_id=evidence_id,
        decision_id=context.decision_id,
        source_kind=context.source_kind,
        snapshot_id=context.snapshot_id,
        source_hash=context.source_hash,
        schema_version=context.schema_version,
        artifact_kind="inventory_sheet_png",
        safe_filename=path.name,
        relative_path=relative,
        sha256=file_hash,
        byte_size=size,
        mime_type="image/png",
        generated_at=now_utc_iso(),
    )


def _manifest_png_entries(files: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key, value in sorted(dict(files or {}).items()):
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "")
        if path.lower().endswith(".png"):
            entries.append(dict(value))
            continue
        if "png" in str(key).lower() and path.lower().endswith(".png"):
            entries.append(dict(value))
    return entries


def _safe_relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(settings.room_database_path.parent.resolve()).as_posix()
        except Exception:
            return path.name


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configured_snapshot_reader() -> SnapshotReader:
    max_age = int(getattr(settings, "inventory_snapshot_max_age_seconds", 0) or 0)
    return SnapshotReader(
        settings.inventory_snapshot_root,
        max_age_seconds=max_age if max_age > 0 else None,
    )
