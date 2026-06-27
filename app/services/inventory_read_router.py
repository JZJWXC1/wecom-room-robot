from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping

from app.services.inventory_read_models import (
    FALLBACK_LEGACY_WHOLE_REQUEST,
    FALLBACK_STRICT,
    READ_MODE_DISABLED,
    READ_MODE_PRIMARY,
    READ_MODE_SHADOW,
    REASON_ALIAS_COVERAGE_FAILED,
    REASON_FALLBACK_NOT_ALLOWED_AFTER_READ,
    REASON_INVALID_MODE,
    REASON_FALLBACK_USED,
    REASON_MISSING_SNAPSHOT,
    REASON_PRIMARY_READINESS_MISSING,
    REASON_PRIMARY_READINESS_MISMATCH,
    REASON_RECONCILIATION_BLOCKING,
    REASON_SECRET_SCAN_FAILED,
    REASON_SOURCE_UNAVAILABLE,
    REASON_SNAPSHOT_INTEGRITY_FAILED,
    REASON_SNAPSHOT_POINTER_MISSING,
    REASON_SNAPSHOT_READ_FAILED,
    REASON_SNAPSHOT_STALE,
    REASON_UNSUPPORTED_SCHEMA,
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    SUPPORTED_FALLBACK_STRATEGIES,
    SUPPORTED_READ_MODES,
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadDecision,
    InventoryReadError,
    InventoryReadHealth,
    assert_evidence_consistency,
    make_decision_id,
    now_utc_iso,
    stable_safe_hash,
)
from app.services.inventory_read_provider import (
    InventoryReadProvider,
    LegacyInventoryReadProvider,
    SnapshotInventoryReadProvider,
)
from app.services.inventory_snapshot_models import INVENTORY_SNAPSHOT_SCHEMA_VERSION, sanitize_for_log
from app.services.region_inventory_constants import validate_area_alias_definitions


ReadinessStateProvider = Callable[[], Mapping[str, Any]]


class InventoryReadRouter:
    """Select exactly one inventory source for a local request/RAG turn."""

    def __init__(
        self,
        *,
        mode: str = READ_MODE_DISABLED,
        fallback_strategy: str = FALLBACK_STRICT,
        legacy_provider: InventoryReadProvider | None = None,
        snapshot_provider: SnapshotInventoryReadProvider | None = None,
        readiness_state: Mapping[str, Any] | ReadinessStateProvider | None = None,
        supported_schema_versions: tuple[str, ...] = (INVENTORY_SNAPSHOT_SCHEMA_VERSION,),
        shadow_probe_snapshot_health: bool = True,
    ) -> None:
        self.mode = str(mode or READ_MODE_DISABLED).strip().lower()
        self.fallback_strategy = str(fallback_strategy or FALLBACK_STRICT).strip().lower()
        self.legacy_provider = legacy_provider or LegacyInventoryReadProvider()
        self.snapshot_provider = snapshot_provider or SnapshotInventoryReadProvider()
        self.readiness_state = readiness_state
        self.supported_schema_versions = supported_schema_versions
        self.shadow_probe_snapshot_health = shadow_probe_snapshot_health

    def start_turn(self, *, request_id: str, turn_id: str) -> "InventoryReadSession":
        decision = self.select_context(request_id=request_id, turn_id=turn_id)
        if not decision.ok or decision.context is None:
            error = decision.error or InventoryReadError(REASON_SNAPSHOT_READ_FAILED, "inventory read source selection failed")
            raise error
        provider = self._provider_for_context(decision.context)
        return InventoryReadSession(decision=decision, provider=provider)

    def select_context(self, *, request_id: str, turn_id: str) -> InventoryReadDecision:
        selected_at = now_utc_iso()
        pre_decision_id = make_decision_id(request_id, turn_id, self.mode, selected_at)
        if self.mode not in SUPPORTED_READ_MODES:
            error = InventoryReadError(
                REASON_INVALID_MODE,
                "INVENTORY_READ_MODE must be one of: disabled, shadow, primary",
                details={"mode": self.mode},
            )
            return InventoryReadDecision(
                ok=False,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=pre_decision_id,
                error=error,
                reasons=(error.code,),
            )
        if self.fallback_strategy not in SUPPORTED_FALLBACK_STRATEGIES:
            error = InventoryReadError(
                "invalid_inventory_read_fallback_strategy",
                "fallback_strategy must be strict or legacy_whole_request",
                details={"fallback_strategy": self.fallback_strategy},
            )
            return InventoryReadDecision(
                ok=False,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=pre_decision_id,
                error=error,
                reasons=(error.code,),
            )
        if self.mode == READ_MODE_DISABLED:
            context = self._legacy_context(
                request_id=request_id,
                turn_id=turn_id,
                selected_at=selected_at,
                decision_id=pre_decision_id,
                selection_mode=self.mode,
            )
            return InventoryReadDecision(
                ok=True,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=context.decision_id,
                context=context,
            )
        if self.mode == READ_MODE_SHADOW:
            legacy_health = self.legacy_provider.health()
            shadow_health = (
                self._safe_snapshot_health()
                if self.shadow_probe_snapshot_health
                else InventoryReadHealth(
                    status="not_queried",
                    source_kind=SOURCE_KIND_SNAPSHOT,
                    message="customer chat shadow mode does not query snapshot in this milestone",
                )
            )
            context = self._legacy_context(
                request_id=request_id,
                turn_id=turn_id,
                selected_at=selected_at,
                decision_id=pre_decision_id,
                selection_mode=self.mode,
                health=InventoryReadHealth(
                    status=legacy_health.status,
                    source_kind=SOURCE_KIND_LEGACY,
                    message="shadow mode customer-visible source remains legacy",
                    checked_at=legacy_health.checked_at,
                    details={
                        "legacy": legacy_health.to_dict(),
                        "shadow_snapshot": shadow_health.to_dict(),
                    },
                ),
            )
            return InventoryReadDecision(
                ok=True,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=context.decision_id,
                context=context,
            )
        snapshot_context_or_error = self._primary_snapshot_context(
            request_id=request_id,
            turn_id=turn_id,
            selected_at=selected_at,
            decision_id=pre_decision_id,
        )
        if isinstance(snapshot_context_or_error, InventoryReadContext):
            return InventoryReadDecision(
                ok=True,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=snapshot_context_or_error.decision_id,
                context=snapshot_context_or_error,
            )
        if self.fallback_strategy == FALLBACK_LEGACY_WHOLE_REQUEST:
            fallback_context = self._legacy_context(
                request_id=request_id,
                turn_id=turn_id,
                selected_at=now_utc_iso(),
                decision_id=make_decision_id(request_id, turn_id, self.mode, "fallback", snapshot_context_or_error.code),
                selection_mode=self.mode,
                fallback_used=True,
                fallback_reason=snapshot_context_or_error.code,
            )
            return InventoryReadDecision(
                ok=True,
                mode=self.mode,
                fallback_strategy=self.fallback_strategy,
                decision_id=fallback_context.decision_id,
                context=fallback_context,
                error=snapshot_context_or_error,
                reasons=(REASON_FALLBACK_USED, snapshot_context_or_error.code),
            )
        return InventoryReadDecision(
            ok=False,
            mode=self.mode,
            fallback_strategy=self.fallback_strategy,
            decision_id=pre_decision_id,
            error=snapshot_context_or_error,
            reasons=(snapshot_context_or_error.code,),
        )

    def _legacy_context(
        self,
        *,
        request_id: str,
        turn_id: str,
        selected_at: str,
        decision_id: str,
        selection_mode: str,
        fallback_used: bool = False,
        fallback_reason: str = "",
        health: InventoryReadHealth | None = None,
    ) -> InventoryReadContext:
        selected_health = health or self.legacy_provider.health()
        details = dict(selected_health.details)
        source_hash = _legacy_source_hash_from_health(selected_health)
        schema_version = str(details.get("schema_version") or "legacy_inventory_service.v1")
        return InventoryReadContext(
            request_id=request_id,
            turn_id=turn_id,
            source_kind=SOURCE_KIND_LEGACY,
            snapshot_id="",
            source_hash=source_hash,
            schema_version=schema_version,
            selected_at=selected_at,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            health_at_selection=selected_health.to_dict(),
            selection_mode=selection_mode,
            decision_id=decision_id,
        )

    def _primary_snapshot_context(
        self,
        *,
        request_id: str,
        turn_id: str,
        selected_at: str,
        decision_id: str,
    ) -> InventoryReadContext | InventoryReadError:
        readiness = self._check_primary_readiness()
        if isinstance(readiness, InventoryReadError):
            return readiness
        snapshot, health, readiness_state = readiness
        return InventoryReadContext(
            request_id=request_id,
            turn_id=turn_id,
            source_kind=SOURCE_KIND_SNAPSHOT,
            snapshot_id=snapshot.snapshot_id,
            source_hash=snapshot.source_hash,
            schema_version=snapshot.schema_version,
            selected_at=selected_at,
            fallback_used=False,
            fallback_reason="",
            health_at_selection={
                "snapshot_health": health.to_dict(),
                "primary_readiness": sanitize_for_log(dict(readiness_state)),
            },
            selection_mode=self.mode,
            decision_id=decision_id,
        )

    def _check_primary_readiness(self) -> tuple[Any, InventoryReadHealth, Mapping[str, Any]] | InventoryReadError:
        reader = self.snapshot_provider.reader
        pointer = reader.get_current_pointer()
        if not pointer.ok:
            issue_codes = {
                str(issue.get("code") if isinstance(issue, Mapping) else getattr(issue, "code", ""))
                for issue in pointer.issues
            }
            if pointer.status == "missing":
                code = REASON_SNAPSHOT_POINTER_MISSING
            elif "pointer_snapshot_missing" in issue_codes:
                code = REASON_MISSING_SNAPSHOT
            else:
                code = REASON_SNAPSHOT_INTEGRITY_FAILED
            return InventoryReadError(code, pointer.message, details=pointer.to_dict())
        snapshot_result = reader.get_snapshot(pointer.value.snapshot_id)
        if not snapshot_result.ok:
            code = REASON_MISSING_SNAPSHOT if snapshot_result.status == "missing" else REASON_SNAPSHOT_INTEGRITY_FAILED
            return InventoryReadError(
                code,
                snapshot_result.message,
                details=snapshot_result.to_dict(),
            )
        health = self.snapshot_provider.health()
        if health.status == "stale":
            return InventoryReadError(REASON_SNAPSHOT_STALE, health.message, details=health.to_dict())
        if health.status not in {"ok"}:
            return InventoryReadError(REASON_SNAPSHOT_INTEGRITY_FAILED, health.message, details=health.to_dict())
        snapshot = snapshot_result.value
        if snapshot.schema_version not in self.supported_schema_versions:
            return InventoryReadError(
                REASON_UNSUPPORTED_SCHEMA,
                "snapshot schema_version is not supported by InventoryReadRouter",
                details={"schema_version": snapshot.schema_version, "supported": list(self.supported_schema_versions)},
            )
        alias_result = validate_area_alias_definitions()
        if not alias_result.ok:
            return InventoryReadError(
                REASON_ALIAS_COVERAGE_FAILED,
                "area alias definitions are not ready for primary inventory reads",
                details=alias_result.to_dict(),
            )
        raw_readiness_state = self._read_readiness_state()
        if not raw_readiness_state:
            return InventoryReadError(
                REASON_PRIMARY_READINESS_MISSING,
                "primary readiness_state is required before primary inventory reads",
                details={
                    "required_keys": [
                        "reconciliation_passed",
                        "blocking_count",
                        "public_artifact_secret_scan_passed",
                        "missing_valid_aliases",
                        "unresolved_aliases",
                        "active_alias_conflicts",
                        "unknown_canonical_areas",
                        "ambiguous_direct_mappings",
                        "snapshot_id",
                        "source_hash",
                    ]
                },
            )
        readiness_state = dict(raw_readiness_state)
        for key, value in alias_result.to_dict().items():
            readiness_state.setdefault(key, value)
        readiness_snapshot_id = str(readiness_state.get("snapshot_id") or "").strip()
        readiness_source_hash = str(readiness_state.get("source_hash") or "").strip()
        missing_binding_keys = [
            key
            for key, value in (
                ("snapshot_id", readiness_snapshot_id),
                ("source_hash", readiness_source_hash),
            )
            if not value
        ]
        if missing_binding_keys:
            return InventoryReadError(
                REASON_PRIMARY_READINESS_MISMATCH,
                "primary readiness must bind snapshot_id and source_hash to the current snapshot",
                details={
                    "missing_keys": missing_binding_keys,
                    "current_snapshot_id": snapshot.snapshot_id,
                    "current_source_hash": snapshot.source_hash,
                },
            )
        if readiness_snapshot_id != snapshot.snapshot_id:
            return InventoryReadError(
                REASON_PRIMARY_READINESS_MISMATCH,
                "primary readiness snapshot_id does not match current snapshot",
                details={
                    "readiness_snapshot_id": readiness_snapshot_id,
                    "current_snapshot_id": snapshot.snapshot_id,
                },
            )
        if readiness_source_hash != snapshot.source_hash:
            return InventoryReadError(
                REASON_PRIMARY_READINESS_MISMATCH,
                "primary readiness source_hash does not match current snapshot",
                details={
                    "readiness_source_hash": readiness_source_hash,
                    "current_source_hash": snapshot.source_hash,
                },
            )
        if readiness_state.get("reconciliation_passed") is not True:
            return InventoryReadError(
                REASON_RECONCILIATION_BLOCKING,
                "snapshot reconciliation has not passed",
                details=readiness_state,
            )
        if int(readiness_state.get("blocking_count") or 0) != 0:
            return InventoryReadError(
                REASON_RECONCILIATION_BLOCKING,
                "snapshot reconciliation still has blocking mismatches",
                details=readiness_state,
            )
        if readiness_state.get("public_artifact_secret_scan_passed") is not True:
            return InventoryReadError(
                REASON_SECRET_SCAN_FAILED,
                "snapshot public artifact secret scan has not passed",
                details=readiness_state,
            )
        alias_keys = (
            "missing_valid_aliases",
            "unresolved_aliases",
            "active_alias_conflicts",
            "unknown_canonical_areas",
            "ambiguous_direct_mappings",
        )
        if any(int(readiness_state.get(key) or 0) for key in alias_keys):
            return InventoryReadError(
                REASON_ALIAS_COVERAGE_FAILED,
                "snapshot alias coverage gate failed",
                details=readiness_state,
            )
        return snapshot, health, readiness_state

    def _read_readiness_state(self) -> Mapping[str, Any]:
        if callable(self.readiness_state):
            return self.readiness_state()
        return self.readiness_state or {}

    def _safe_snapshot_health(self) -> InventoryReadHealth:
        try:
            return self.snapshot_provider.health()
        except Exception as exc:
            return InventoryReadHealth(
                status="error",
                source_kind=SOURCE_KIND_SNAPSHOT,
                code=REASON_SOURCE_UNAVAILABLE,
                message=str(exc)[:300],
            )

    def _provider_for_context(self, context: InventoryReadContext) -> InventoryReadProvider:
        return self.snapshot_provider if context.source_kind == SOURCE_KIND_SNAPSHOT else self.legacy_provider


def _legacy_source_hash_from_health(health: InventoryReadHealth) -> str:
    details = dict(health.details)
    direct = str(details.get("source_hash") or "")
    if direct:
        return direct
    legacy = details.get("legacy")
    if isinstance(legacy, Mapping):
        legacy_details = legacy.get("details")
        if isinstance(legacy_details, Mapping):
            nested = str(legacy_details.get("source_hash") or "")
            if nested:
                return nested
    return stable_safe_hash(health.to_dict())


class InventoryReadSession:
    """Request-level wrapper that prevents mid-turn source changes and half fallback."""

    def __init__(
        self,
        *,
        decision: InventoryReadDecision,
        provider: InventoryReadProvider,
    ) -> None:
        if decision.context is None:
            raise InventoryReadError(REASON_SNAPSHOT_READ_FAILED, "InventoryReadSession requires a context")
        self.decision = decision
        self.context = decision.context
        self.provider = provider
        self._business_read_started = False

    @property
    def business_read_started(self) -> bool:
        return self._business_read_started

    async def search_inventory(self, query_state: Any, *, limit: int = 8) -> list[InventoryListingEvidence]:
        self._mark_business_read_started()
        evidence = await self.provider.search_inventory(query_state, self.context, limit=limit)
        assert_evidence_consistency(self.context, evidence)
        return evidence

    async def get_listing(self, listing_id: str) -> InventoryListingEvidence | None:
        self._mark_business_read_started()
        evidence = await self.provider.get_listing(listing_id, self.context)
        if evidence is not None:
            assert_evidence_consistency(self.context, [evidence])
        return evidence

    async def get_listings(self, listing_ids: list[str]) -> list[InventoryListingEvidence]:
        self._mark_business_read_started()
        evidence = await self.provider.get_listings(listing_ids, self.context)
        assert_evidence_consistency(self.context, evidence)
        return evidence

    async def get_rewrite_index(self) -> dict[str, Any]:
        self._mark_business_read_started()
        return await self.provider.get_rewrite_index(self.context)

    async def get_inventory_metadata(self) -> dict[str, Any]:
        self._mark_business_read_started()
        return await self.provider.get_inventory_metadata(self.context)

    def require_whole_request_fallback_allowed(self) -> None:
        if self._business_read_started:
            raise InventoryReadError(
                REASON_FALLBACK_NOT_ALLOWED_AFTER_READ,
                "legacy_whole_request fallback is only allowed before any business fact read starts",
                details={"decision_id": self.context.decision_id, "source_kind": self.context.source_kind},
            )

    def _mark_business_read_started(self) -> None:
        self._business_read_started = True

    def with_new_context(self, context: InventoryReadContext, provider: InventoryReadProvider) -> "InventoryReadSession":
        self.require_whole_request_fallback_allowed()
        return InventoryReadSession(
            decision=replace(self.decision, context=context, decision_id=context.decision_id),
            provider=provider,
        )
