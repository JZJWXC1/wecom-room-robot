from __future__ import annotations

from hashlib import sha256
import time
from typing import Any, Callable

from app.services.inventory_read_models import (
    READ_MODE_DISABLED,
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
from app.services.inventory_read_provider import LegacyInventoryReadProvider
from app.services.inventory_read_router import InventoryReadRouter


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
) -> InventoryReadContext:
    request_id = f"{prefix}_req_{_safe_hash(open_kfid, external_userid)}"
    msgid_text = "|".join(str(item).strip() for item in (msgids or []) if str(item).strip())
    turn_basis = msgid_text or f"{generation}:{time.time_ns()}"
    turn_id = f"{prefix}_turn_{_safe_hash(request_id, generation, turn_basis, content)}"
    router = _router(
        mode=customer_inventory_read_mode(inventory_snapshot_mode),
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
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
) -> dict[str, Any]:
    meta = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
    ).get_inventory_metadata(inventory_read_context)
    return dict(meta or {})


async def rewrite_index_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
) -> dict[str, Any]:
    index = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
    ).get_rewrite_index(inventory_read_context)
    return dict(index or {})


async def search_rows_for_context(
    inventory_read_context: InventoryReadContext,
    query_state: Any,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    rows, evidence = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
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
    limit: int = 500,
    refresh_if_needed: bool = True,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    rows, evidence = await _provider_for_context(
        inventory_read_context,
        inventory_service=inventory_service,
        rewrite_index_loader=rewrite_index_loader,
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
) -> InventoryReadRouter:
    return InventoryReadRouter(
        mode=mode,
        legacy_provider=LegacyInventoryReadProvider(
            inventory_service,
            rewrite_index_loader=rewrite_index_loader,
        ),
        snapshot_provider=CUSTOMER_SNAPSHOT_PROVIDER_DISABLED,  # type: ignore[arg-type]
        shadow_probe_snapshot_health=False,
    )


def _provider_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    inventory_service: Any,
    rewrite_index_loader: RewriteIndexLoader,
) -> Any:
    if inventory_read_context.source_kind == SOURCE_KIND_LEGACY:
        return LegacyInventoryReadProvider(
            inventory_service,
            rewrite_index_loader=rewrite_index_loader,
        )
    raise _snapshot_disabled_error()


def _safe_hash(*parts: Any) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _snapshot_disabled_error() -> InventoryReadError:
    return InventoryReadError(
        REASON_SOURCE_UNAVAILABLE,
        "customer inventory read path must not query snapshot in this milestone",
    )
