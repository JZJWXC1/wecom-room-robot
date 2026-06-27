from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Callable

from app.config import settings
from app.services.inventory_read_models import (
    FALLBACK_STRICT,
    READ_MODE_DISABLED,
    READ_MODE_PRIMARY,
    READ_MODE_SHADOW,
    REASON_SOURCE_UNAVAILABLE,
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadError,
    InventoryReadHealth,
    assert_evidence_consistency,
)
from app.services.inventory_read_provider import LegacyInventoryReadProvider, SnapshotInventoryReadProvider
from app.services.inventory_read_router import InventoryReadRouter
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_shadow import scan_public_artifacts_for_sensitive_text


RewriteIndexLoader = Callable[[], dict[str, Any]]


class CustomerSnapshotProviderDisabled:
    source_kind = SOURCE_KIND_SNAPSHOT

    def health(self) -> InventoryReadHealth:
        raise _snapshot_disabled_error()

    async def search_inventory(self, *args: Any, **kwargs: Any) -> list[InventoryListingEvidence]:
        raise _snapshot_disabled_error()

    async def search_inventory_rows(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        raise _snapshot_disabled_error()

    async def all_inventory_rows(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        raise _snapshot_disabled_error()

    async def get_listing(self, *args: Any, **kwargs: Any) -> InventoryListingEvidence | None:
        raise _snapshot_disabled_error()

    async def get_listings(self, *args: Any, **kwargs: Any) -> list[InventoryListingEvidence]:
        raise _snapshot_disabled_error()

    async def get_rewrite_index(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _snapshot_disabled_error()

    async def get_inventory_metadata(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _snapshot_disabled_error()


CUSTOMER_SNAPSHOT_PROVIDER_DISABLED = CustomerSnapshotProviderDisabled()


def customer_inventory_read_mode(mode_value: Any) -> str:
    raw = str(mode_value or "").strip().lower()
    if raw == READ_MODE_PRIMARY:
        return READ_MODE_PRIMARY
    if raw == READ_MODE_SHADOW:
        return READ_MODE_SHADOW
    return READ_MODE_DISABLED


def create_customer_inventory_read_context(
    *,
    prefix: str,
    open_kfid: str,
    external_userid: str,
    content: str,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    inventory_snapshot_mode: Any,
    msgids: list[str] | None = None,
    generation: int | str = "",
    snapshot_provider: Any | None = None,
    readiness_state: Any | None = None,
    fallback_strategy: str | None = None,
) -> InventoryReadContext:
    request_id = f"{prefix}_req_{_safe_hash(open_kfid, external_userid)}"
    msgid_text = "|".join(str(item).strip() for item in (msgids or []) if str(item).strip())
    turn_basis = msgid_text or f"{generation}:{time.time_ns()}"
    turn_id = f"{prefix}_turn_{_safe_hash(request_id, generation, turn_basis, content)}"
    mode = customer_inventory_read_mode(inventory_snapshot_mode)
    router = _router(
        mode=mode,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
        snapshot_provider=snapshot_provider,
        readiness_state=readiness_state,
        fallback_strategy=fallback_strategy or settings.inventory_read_fallback_strategy,
    )
    decision = router.select_context(request_id=request_id, turn_id=turn_id)
    if decision.ok and decision.context is not None:
        return decision.context
    raise decision.error or InventoryReadError(
        REASON_SOURCE_UNAVAILABLE,
        "inventory read source selection failed",
    )


def create_local_inventory_read_context(
    *,
    scope: str,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
) -> InventoryReadContext:
    request_id = f"{scope}_req_{_safe_hash(scope, 'local')}"
    turn_id = f"{scope}_turn_{_safe_hash(scope, time.time_ns())}"
    decision = _router(
        mode=READ_MODE_DISABLED,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
    ).select_context(request_id=request_id, turn_id=turn_id)
    if decision.ok and decision.context is not None:
        return decision.context
    raise decision.error or InventoryReadError(
        REASON_SOURCE_UNAVAILABLE,
        "local inventory read source selection failed",
    )


def remember_context(
    context: dict[str, Any],
    inventory_read_context: InventoryReadContext,
) -> dict[str, Any]:
    context["inventory_read_context"] = inventory_read_context.to_log_dict()
    return context


async def metadata_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
) -> dict[str, Any]:
    meta = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
        snapshot_provider=snapshot_provider,
    ).get_inventory_metadata(inventory_read_context)
    return dict(meta or {})


async def rewrite_index_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
) -> dict[str, Any]:
    index = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
        snapshot_provider=snapshot_provider,
    ).get_rewrite_index(inventory_read_context)
    return dict(index or {})


async def search_rows_for_context(
    inventory_read_context: InventoryReadContext,
    query_state: Any,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    rows, evidence = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
        snapshot_provider=snapshot_provider,
    ).search_inventory_rows(
        query_state,
        inventory_read_context,
        limit=limit,
    )
    assert_evidence_consistency(inventory_read_context, evidence)
    return rows, evidence


async def all_rows_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
    limit: int = 500,
    refresh_if_needed: bool = True,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    rows, evidence = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
        snapshot_provider=snapshot_provider,
    ).all_inventory_rows(
        inventory_read_context,
        limit=limit,
        refresh_if_needed=refresh_if_needed,
    )
    assert_evidence_consistency(inventory_read_context, evidence)
    return rows, evidence


def extend_listing_evidence(
    target: list[InventoryListingEvidence],
    items: list[InventoryListingEvidence],
) -> None:
    existing = {item.evidence_id for item in target}
    for item in items:
        if item.evidence_id in existing:
            continue
        target.append(item)
        existing.add(item.evidence_id)


def evidence_for_rows(
    selected_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    all_evidence: list[InventoryListingEvidence],
) -> list[InventoryListingEvidence]:
    result: list[InventoryListingEvidence] = []
    used: set[int] = set()
    for selected in selected_rows:
        for index, row in enumerate(all_rows):
            if index in used or row != selected:
                continue
            if index < len(all_evidence):
                result.append(all_evidence[index])
                used.add(index)
            break
    return result


def clear_fact_evidence(evidence: dict[str, Any], exc: InventoryReadError) -> None:
    evidence["inventory_read_error"] = exc.to_dict()
    evidence["inventory_rows"] = []
    evidence["target_rows"] = []
    evidence["image_rows"] = []
    evidence["video_rows"] = []
    evidence["image_paths"] = []
    evidence["video_paths"] = []
    evidence["missing_media"] = []


def context_summary(context: dict[str, Any]) -> dict[str, str]:
    return {
        "decision_id": str(context.get("decision_id") or ""),
        "source_kind": str(context.get("source_kind") or ""),
        "source_hash": str(context.get("source_hash") or ""),
        "snapshot_id": str(context.get("snapshot_id") or ""),
        "selection_mode": str(context.get("selection_mode") or ""),
    }


def _router(
    *,
    mode: str,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
    readiness_state: Any | None = None,
    fallback_strategy: str = FALLBACK_STRICT,
) -> InventoryReadRouter:
    resolved_snapshot_provider = snapshot_provider or _configured_snapshot_provider()
    resolved_readiness_state = (
        readiness_state
        if readiness_state is not None or mode != READ_MODE_PRIMARY
        else (
            _primary_readiness_state_provider(resolved_snapshot_provider)
            if snapshot_provider is None
            else None
        )
    )
    return InventoryReadRouter(
        mode=mode,
        fallback_strategy=fallback_strategy,
        legacy_provider=LegacyInventoryReadProvider(
            inventory_service,
            rewrite_index_loader=rewrite_index_loader,
        ),
        snapshot_provider=resolved_snapshot_provider,
        readiness_state=resolved_readiness_state,
        shadow_probe_snapshot_health=False,
    )


def _provider_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    snapshot_provider: Any | None = None,
) -> Any:
    if inventory_read_context.source_kind == SOURCE_KIND_LEGACY:
        return LegacyInventoryReadProvider(
            inventory_service,
            rewrite_index_loader=rewrite_index_loader,
        )
    return snapshot_provider or _configured_snapshot_provider()


def _safe_hash(*parts: Any) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _snapshot_disabled_error() -> InventoryReadError:
    return InventoryReadError(
        REASON_SOURCE_UNAVAILABLE,
        "customer inventory read path must not query snapshot in this milestone",
    )


def _configured_snapshot_provider() -> SnapshotInventoryReadProvider:
    return SnapshotInventoryReadProvider(_configured_snapshot_reader())


def _configured_snapshot_reader() -> SnapshotReader:
    max_age = int(getattr(settings, "inventory_snapshot_max_age_seconds", 0) or 0)
    return SnapshotReader(
        settings.inventory_snapshot_root,
        max_age_seconds=max_age if max_age > 0 else None,
    )


def _primary_readiness_state_provider(snapshot_provider: Any) -> Callable[[], dict[str, Any]]:
    def load() -> dict[str, Any]:
        explicit = _load_primary_readiness_file()
        if explicit:
            return explicit
        reader = getattr(snapshot_provider, "reader", None)
        if not isinstance(reader, SnapshotReader):
            return {}
        return _derive_primary_readiness_from_current_snapshot(reader)

    return load


def _load_primary_readiness_file() -> dict[str, Any]:
    path = Path(settings.inventory_snapshot_primary_readiness_path)
    if not path.exists() or path.name in {".env", ""}:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return dict(payload)


def _derive_primary_readiness_from_current_snapshot(reader: SnapshotReader) -> dict[str, Any]:
    pointer = reader.get_current_pointer()
    if not pointer.ok:
        return {}
    snapshot_id = str(pointer.value.snapshot_id)
    snapshot_dir = reader.root / "snapshots" / snapshot_id
    report_path = snapshot_dir / "sync_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(report, dict):
        return {}
    validation = report.get("validation_result") if isinstance(report.get("validation_result"), dict) else {}
    error_count = int(validation.get("error_count") or 0) if isinstance(validation, dict) else 0
    report_ok = bool(report.get("ok")) and error_count == 0
    scan = scan_public_artifacts_for_sensitive_text(reader.root, snapshot_id=snapshot_id)
    return {
        "schema_version": "inventory_snapshot_primary_readiness.v1",
        "readiness_source": "current_snapshot_sync_report",
        "snapshot_id": snapshot_id,
        "source_hash": str(pointer.value.source_hash),
        "reconciliation_passed": report_ok,
        "blocking_count": error_count,
        "public_artifact_secret_scan_passed": bool(scan.get("passed")),
        "public_artifact_secret_scan_files": int(scan.get("files_scanned") or 0),
        "audit_reason": "current pointer, sync_report validation, and public artifact scan",
    }
