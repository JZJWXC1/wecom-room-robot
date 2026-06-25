from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from app.config import settings
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_legacy_adapter import (
    ADAPTER_REMOVAL_MILESTONE,
    LegacyInventoryToSnapshotAdapter,
)
from app.services.inventory_snapshot_models import (
    InventorySourceMetadata,
    InventorySnapshot,
    InventorySyncReport,
    now_utc_iso,
    parse_iso_datetime,
    redact_sensitive_text,
    sanitize_for_log,
)
from app.services.inventory_snapshot_reconciliation import (
    InventorySnapshotReconciliationReport,
    load_legacy_rewrite_index,
    reconcile_inventory_snapshot,
)
from app.services.inventory_snapshot_store import SnapshotStore, SnapshotStoreError
from app.services.inventory_snapshot_validator import SnapshotValidator


logger = logging.getLogger(__name__)
DEFAULT_SHADOW_TIMEOUT_SECONDS = 10.0
SHADOW_HEALTH_SCHEMA_VERSION = "inventory_snapshot_shadow_health.v1"
SHADOW_STATUS_SCHEMA_VERSION = "inventory_snapshot_shadow_status.v2"


class InventorySnapshotMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"


class InventorySnapshotShadowConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ShadowExecutionResult:
    ok: bool
    mode: str
    status: str
    sync_run_id: str = ""
    source_version: str = ""
    source_hash: str = ""
    snapshot_id: str = ""
    reconciliation_passed: bool = False
    blocking_count: int = 0
    warning_count: int = 0
    duration_ms: int = 0
    error_code: str = ""
    safe_error_message: str = ""
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "ok": self.ok,
                "mode": self.mode,
                "status": self.status,
                "sync_run_id": self.sync_run_id,
                "source_version": self.source_version,
                "source_hash_prefix": self.source_hash[:12],
                "source_hash": self.source_hash,
                "snapshot_id": self.snapshot_id,
                "reconciliation_passed": self.reconciliation_passed,
                "blocking_count": self.blocking_count,
                "warning_count": self.warning_count,
                "duration_ms": self.duration_ms,
                "error_code": self.error_code,
                "safe_error_message": self.safe_error_message,
                "report_path": self.report_path,
            }
        )


@dataclass(frozen=True)
class InventorySnapshotShadowHealth:
    schema_version: str
    mode: str
    status: str
    last_attempt_at: str = ""
    last_success_at: str = ""
    last_source_hash: str = ""
    last_snapshot_id: str = ""
    last_reconciliation_passed: bool = False
    last_blocking_count: int = 0
    last_warning_count: int = 0
    consecutive_passes: int = 0
    consecutive_failures: int = 0
    stale: bool = False
    stale_reason: str = ""
    ready_for_cutover_evaluation: bool = False
    not_ready_reasons: list[str] | None = None
    duration_ms: int = 0
    safe_error_code: str = ""
    safe_error_message: str = ""
    public_artifact_secret_scan_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "schema_version": self.schema_version,
                "mode": self.mode,
                "status": self.status,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_source_hash": self.last_source_hash,
                "last_snapshot_id": self.last_snapshot_id,
                "last_reconciliation_passed": self.last_reconciliation_passed,
                "last_blocking_count": self.last_blocking_count,
                "last_warning_count": self.last_warning_count,
                "consecutive_passes": self.consecutive_passes,
                "consecutive_failures": self.consecutive_failures,
                "stale": self.stale,
                "stale_reason": self.stale_reason,
                "ready_for_cutover_evaluation": self.ready_for_cutover_evaluation,
                "not_ready_reasons": list(self.not_ready_reasons or []),
                "duration_ms": self.duration_ms,
                "safe_error_code": self.safe_error_code,
                "safe_error_message": self.safe_error_message,
                "public_artifact_secret_scan_passed": self.public_artifact_secret_scan_passed,
            }
        )


class InventorySnapshotShadowCoordinator:
    """Non-blocking shadow integration for already-completed legacy sync rows."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        root: Path | None = None,
        timeout_seconds: float = DEFAULT_SHADOW_TIMEOUT_SECONDS,
        adapter: LegacyInventoryToSnapshotAdapter | None = None,
        builder_factory: Callable[[], SnapshotBuilder] | None = None,
        validator: SnapshotValidator | None = None,
        store_factory: Callable[[Path, SnapshotValidator], SnapshotStore] | None = None,
    ) -> None:
        self.mode_value = mode
        self.root = Path(root) if root is not None else Path(settings.inventory_snapshot_shadow_root)
        self.timeout_seconds = float(timeout_seconds)
        self.adapter = adapter or LegacyInventoryToSnapshotAdapter()
        self.builder_factory = builder_factory or SnapshotBuilder
        self.validator = validator or SnapshotValidator()
        self.store_factory = store_factory or (lambda root_path, validator: SnapshotStore(root_path, validator=validator))

    def run(
        self,
        *,
        legacy_rows: list[Any],
        source_metadata: InventorySourceMetadata,
        legacy_rewrite_index_path: Path | None = None,
        legacy_rewrite_index: dict[str, Any] | None = None,
        source_payload: Any | None = None,
        sync_run_id: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        attempt_at = now_utc_iso()
        run_id = _sync_run_id(sync_run_id)
        try:
            mode = parse_inventory_snapshot_mode(self.mode_value)
        except InventorySnapshotShadowConfigError as exc:
            result = ShadowExecutionResult(
                ok=False,
                mode=str(self.mode_value if self.mode_value is not None else settings.inventory_snapshot_mode),
                status="config_error",
                sync_run_id=run_id,
                source_version=source_metadata.source_version,
                duration_ms=_duration_ms(started_at),
                error_code="invalid_inventory_snapshot_mode",
                safe_error_message=_safe_error_message(exc),
            )
            self._write_status(result, attempt_at=attempt_at)
            return result.to_dict()

        if mode is InventorySnapshotMode.DISABLED:
            return {
                "ok": True,
                "enabled": False,
                "mode": mode.value,
                "status": "disabled",
                "sync_run_id": run_id,
            }

        if not self._mark_sync_run_started(run_id, attempt_at=attempt_at):
            result = ShadowExecutionResult(
                ok=True,
                mode=mode.value,
                status="duplicate_skipped",
                sync_run_id=run_id,
                source_version=source_metadata.source_version,
                duration_ms=_duration_ms(started_at),
            )
            return result.to_dict()

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inventory-snapshot-shadow")
        future = executor.submit(
            self._execute_shadow,
            legacy_rows,
            source_metadata,
            legacy_rewrite_index_path,
            legacy_rewrite_index,
            source_payload,
            run_id,
        )
        try:
            result = future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError:
            result = ShadowExecutionResult(
                ok=False,
                mode=mode.value,
                status="timeout",
                sync_run_id=run_id,
                source_version=source_metadata.source_version,
                duration_ms=_duration_ms(started_at),
                error_code="shadow_timeout",
                safe_error_message=f"shadow execution exceeded {self.timeout_seconds:.3f}s",
            )
            logger.warning(
                "inventory_snapshot_shadow_timeout mode=%s source_version=%s duration_ms=%s",
                mode.value,
                source_metadata.source_version,
                result.duration_ms,
            )
        except Exception as exc:
            result = ShadowExecutionResult(
                ok=False,
                mode=mode.value,
                status="failed",
                sync_run_id=run_id,
                source_version=source_metadata.source_version,
                duration_ms=_duration_ms(started_at),
                error_code=_error_code(exc),
                safe_error_message=_safe_error_message(exc),
            )
            logger.warning(
                "inventory_snapshot_shadow_failed mode=%s source_version=%s error_code=%s",
                mode.value,
                source_metadata.source_version,
                result.error_code,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not result.duration_ms:
            result = replace(result, duration_ms=_duration_ms(started_at))
        self._write_status(result, attempt_at=attempt_at)
        return result.to_dict()

    def _execute_shadow(
        self,
        legacy_rows: list[Any],
        source_metadata: InventorySourceMetadata,
        legacy_rewrite_index_path: Path | None,
        legacy_rewrite_index: dict[str, Any] | None,
        source_payload: Any | None,
        sync_run_id: str,
    ) -> ShadowExecutionResult:
        started_at = time.monotonic()
        adapted_rows = self.adapter.adapt_many(list(legacy_rows))
        builder = self.builder_factory()
        store = self.store_factory(self.root, self.validator)
        last_error: Exception | None = None

        for attempt in (None, 2, 3):
            snapshot, sync_report = builder.build(
                adapted_rows,
                source_metadata,
                source_payload=source_payload,
                attempt=attempt,
            )
            validation_result = self.validator.validate_snapshot(snapshot)
            sync_report.validation_result.extend(validation_result)
            if not sync_report.validation_result.ok:
                raise SnapshotStoreError(
                    "snapshot validation failed: "
                    + "; ".join(issue.code for issue in sync_report.validation_result.errors)
                )
            try:
                store.write_snapshot(snapshot, sync_report, activate=False)
                break
            except SnapshotStoreError as exc:
                last_error = exc
                if "snapshot already exists" in str(exc) and attempt != 3:
                    continue
                raise
        else:
            raise last_error or SnapshotStoreError("snapshot shadow write failed")

        legacy_index = load_legacy_rewrite_index(legacy_rewrite_index_path, legacy_rewrite_index)
        reconciliation = reconcile_inventory_snapshot(
            legacy_rows=adapted_rows,
            snapshot=snapshot,
            legacy_rewrite_index=legacy_index,
        )
        report_path = self._write_reconciliation_report(snapshot, reconciliation)
        self._write_shadow_pointer(snapshot, reconciliation, report_path)
        severity_counts = reconciliation.severity_counts
        return ShadowExecutionResult(
            ok=True,
            mode=InventorySnapshotMode.SHADOW.value,
            status="completed",
            sync_run_id=sync_run_id,
            source_version=source_metadata.source_version,
            source_hash=snapshot.source_hash,
            snapshot_id=snapshot.snapshot_id,
            reconciliation_passed=reconciliation.passed,
            blocking_count=int(severity_counts.get("blocking") or 0),
            warning_count=int(severity_counts.get("warning") or 0),
            duration_ms=_duration_ms(started_at),
            report_path=_relative_to_root(report_path, self.root),
        )

    def _write_reconciliation_report(
        self,
        snapshot: InventorySnapshot,
        reconciliation: InventorySnapshotReconciliationReport,
    ) -> Path:
        reports_dir = self.root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{snapshot.snapshot_id}_reconciliation.json"
        _atomic_write_json(report_path, reconciliation.to_dict())
        return report_path

    def _write_shadow_pointer(
        self,
        snapshot: InventorySnapshot,
        reconciliation: InventorySnapshotReconciliationReport,
        report_path: Path,
    ) -> None:
        payload = {
            "schema_version": "inventory_snapshot_shadow_pointer.v1",
            "snapshot_id": snapshot.snapshot_id,
            "source_hash": snapshot.source_hash,
            "snapshot_path": f"snapshots/{snapshot.snapshot_id}",
            "created_at": snapshot.generated_at,
            "updated_at": now_utc_iso(),
            "row_count": len(snapshot.listings),
            "reconciliation_passed": reconciliation.passed,
            "blocking_count": int(reconciliation.severity_counts.get("blocking") or 0),
            "warning_count": int(reconciliation.severity_counts.get("warning") or 0),
            "report_path": _relative_to_root(report_path, self.root),
            "production_pointer_switched": False,
        }
        _atomic_write_json(self.root / "shadow_current_snapshot.json", payload)

    def _write_status(self, result: ShadowExecutionResult, *, attempt_at: str) -> None:
        if result.status == "disabled":
            return
        try:
            existing = _read_json_safely(self.root / "shadow_status.json")
            last_success_at = str(existing.get("last_success_at") or "")
            last_counted_source_hash = str(existing.get("last_counted_source_hash") or "")
            consecutive_passes = int(existing.get("consecutive_passes") or 0)
            consecutive_failures = int(existing.get("consecutive_failures") or 0)
            if result.ok:
                if result.reconciliation_passed and result.blocking_count == 0:
                    last_success_at = now_utc_iso()
                    consecutive_failures = 0
                    if result.source_hash and result.source_hash != last_counted_source_hash:
                        consecutive_passes += 1
                        last_counted_source_hash = result.source_hash
                else:
                    consecutive_passes = 0
                    consecutive_failures += 1
            else:
                consecutive_passes = 0
                consecutive_failures += 1
            payload = {
                "schema_version": SHADOW_STATUS_SCHEMA_VERSION,
                "last_attempt_at": attempt_at,
                "last_success_at": last_success_at,
                "last_sync_run_id": result.sync_run_id,
                "source_version": result.source_version,
                "source_hash": result.source_hash,
                "last_source_hash": result.source_hash,
                "last_counted_source_hash": last_counted_source_hash,
                "snapshot_id": result.snapshot_id,
                "last_snapshot_id": result.snapshot_id,
                "reconciliation_passed": result.reconciliation_passed,
                "last_reconciliation_passed": result.reconciliation_passed,
                "blocking_count": result.blocking_count,
                "last_blocking_count": result.blocking_count,
                "warning_count": result.warning_count,
                "last_warning_count": result.warning_count,
                "consecutive_passes": consecutive_passes,
                "consecutive_failures": consecutive_failures,
                "duration_ms": result.duration_ms,
                "error_code": result.error_code,
                "safe_error_message": result.safe_error_message,
            }
            _atomic_write_json(self.root / "shadow_status.json", payload)
        except Exception as exc:
            logger.warning(
                "inventory_snapshot_shadow_status_write_failed error_code=%s message=%s",
                _error_code(exc),
                _safe_error_message(exc),
            )

    def _mark_sync_run_started(self, sync_run_id: str, *, attempt_at: str) -> bool:
        run_dir = self.root / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        marker = run_dir / f"{hashlib.sha256(sync_run_id.encode('utf-8')).hexdigest()}.json"
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": "inventory_snapshot_shadow_run.v1",
                    "sync_run_id": sync_run_id,
                    "started_at": attempt_at,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        return True


def parse_inventory_snapshot_mode(value: str | None = None) -> InventorySnapshotMode:
    raw = settings.inventory_snapshot_mode if value is None else value
    text = str(raw or "disabled").strip().lower()
    if text == InventorySnapshotMode.DISABLED.value:
        return InventorySnapshotMode.DISABLED
    if text == InventorySnapshotMode.SHADOW.value:
        return InventorySnapshotMode.SHADOW
    raise InventorySnapshotShadowConfigError(
        "INVENTORY_SNAPSHOT_MODE must be one of: disabled, shadow"
    )


def build_shadow_source_metadata(
    *,
    source_kind: str,
    source_version: str = "",
    cache_meta: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> InventorySourceMetadata:
    cache_meta = dict(cache_meta or {})
    merged_extra = {
        "adapter": "LegacyInventoryToSnapshotAdapter",
        "adapter_removal_milestone": ADAPTER_REMOVAL_MILESTONE,
        "shadow_mode_removal_milestone": "M1D",
    }
    merged_extra.update(extra or {})
    return InventorySourceMetadata(
        source_kind=source_kind,
        source_version=source_version or str(cache_meta.get("hash") or cache_meta.get("signature") or ""),
        source_modified_at=str(cache_meta.get("cache_mtime_iso") or cache_meta.get("synced_at_iso") or ""),
        revision=str(cache_meta.get("revision") or ""),
        range_ref=str(cache_meta.get("range") or ""),
        extra=merged_extra,
    )


def run_inventory_snapshot_shadow(
    *,
    legacy_rows: list[Any],
    source_kind: str,
    source_version: str = "",
    cache_meta: dict[str, Any] | None = None,
    legacy_rewrite_index_path: Path | None = None,
    legacy_rewrite_index: dict[str, Any] | None = None,
    source_payload: Any | None = None,
    root: Path | None = None,
    timeout_seconds: float = DEFAULT_SHADOW_TIMEOUT_SECONDS,
    sync_run_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    metadata = build_shadow_source_metadata(
        source_kind=source_kind,
        source_version=source_version,
        cache_meta=cache_meta,
    )
    return InventorySnapshotShadowCoordinator(
        mode=mode,
        root=root,
        timeout_seconds=timeout_seconds,
    ).run(
        legacy_rows=legacy_rows,
        source_metadata=metadata,
        legacy_rewrite_index_path=legacy_rewrite_index_path,
        legacy_rewrite_index=legacy_rewrite_index,
        source_payload=source_payload,
        sync_run_id=sync_run_id,
    )


def get_inventory_snapshot_shadow_health(
    *,
    root: Path | None = None,
    mode: str | None = None,
    stale_seconds: int | None = None,
    required_consecutive_passes: int | None = None,
) -> InventorySnapshotShadowHealth:
    root_path = Path(root) if root is not None else Path(settings.inventory_snapshot_shadow_root)
    stale_after = int(
        settings.inventory_snapshot_shadow_stale_seconds
        if stale_seconds is None
        else stale_seconds
    )
    required_passes = int(
        settings.inventory_snapshot_shadow_required_passes
        if required_consecutive_passes is None
        else required_consecutive_passes
    )
    try:
        parsed_mode = parse_inventory_snapshot_mode(mode)
    except InventorySnapshotShadowConfigError as exc:
        return InventorySnapshotShadowHealth(
            schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
            mode=str(mode if mode is not None else settings.inventory_snapshot_mode),
            status="error",
            stale=True,
            stale_reason="invalid_mode",
            ready_for_cutover_evaluation=False,
            not_ready_reasons=["invalid_mode"],
            safe_error_code="invalid_inventory_snapshot_mode",
            safe_error_message=_safe_error_message(exc),
        )
    if parsed_mode is InventorySnapshotMode.DISABLED:
        return InventorySnapshotShadowHealth(
            schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
            mode=parsed_mode.value,
            status="disabled",
            stale=False,
            ready_for_cutover_evaluation=False,
            not_ready_reasons=["mode_disabled"],
        )

    status_path = root_path / "shadow_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InventorySnapshotShadowHealth(
            schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
            mode=parsed_mode.value,
            status="never_run",
            stale=True,
            stale_reason="no_shadow_status",
            ready_for_cutover_evaluation=False,
            not_ready_reasons=["never_run"],
        )
    except (OSError, json.JSONDecodeError) as exc:
        return InventorySnapshotShadowHealth(
            schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
            mode=parsed_mode.value,
            status="error",
            stale=True,
            stale_reason="shadow_status_unreadable",
            ready_for_cutover_evaluation=False,
            not_ready_reasons=["shadow_status_unreadable"],
            safe_error_code=_error_code(exc),
            safe_error_message=_safe_error_message(exc),
        )
    if not isinstance(status, dict):
        return InventorySnapshotShadowHealth(
            schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
            mode=parsed_mode.value,
            status="error",
            stale=True,
            stale_reason="shadow_status_invalid",
            ready_for_cutover_evaluation=False,
            not_ready_reasons=["shadow_status_invalid"],
            safe_error_code="shadow_status_invalid",
            safe_error_message="shadow status is not an object",
        )

    scan_ok, scan_reason = _public_artifact_secret_scan(root_path, str(status.get("last_snapshot_id") or status.get("snapshot_id") or ""))
    last_attempt_at = str(status.get("last_attempt_at") or "")
    last_success_at = str(status.get("last_success_at") or "")
    stale, stale_reason = _stale_status(last_attempt_at, stale_after)
    blocking_count = int(status.get("last_blocking_count") or status.get("blocking_count") or 0)
    warning_count = int(status.get("last_warning_count") or status.get("warning_count") or 0)
    passed = bool(status.get("last_reconciliation_passed") or status.get("reconciliation_passed"))
    consecutive_passes = int(status.get("consecutive_passes") or 0)
    consecutive_failures = int(status.get("consecutive_failures") or 0)
    error_code = str(status.get("error_code") or "")
    not_ready: list[str] = []
    if parsed_mode is not InventorySnapshotMode.SHADOW:
        not_ready.append("mode_not_shadow")
    if not passed:
        not_ready.append("last_reconciliation_not_passed")
    if blocking_count:
        not_ready.append("blocking_mismatches_present")
    if not scan_ok:
        not_ready.append(scan_reason or "public_artifact_secret_scan_failed")
    if stale:
        not_ready.append(stale_reason or "stale")
    if consecutive_passes < required_passes:
        not_ready.append("insufficient_consecutive_passes")
    if error_code:
        not_ready.append("last_shadow_error")
    ready = not not_ready
    health_status = _health_status(
        stale=stale,
        error_code=error_code,
        blocking_count=blocking_count,
        warning_count=warning_count,
        passed=passed,
    )
    return InventorySnapshotShadowHealth(
        schema_version=SHADOW_HEALTH_SCHEMA_VERSION,
        mode=parsed_mode.value,
        status=health_status,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
        last_source_hash=str(status.get("last_source_hash") or status.get("source_hash") or ""),
        last_snapshot_id=str(status.get("last_snapshot_id") or status.get("snapshot_id") or ""),
        last_reconciliation_passed=passed,
        last_blocking_count=blocking_count,
        last_warning_count=warning_count,
        consecutive_passes=consecutive_passes,
        consecutive_failures=consecutive_failures,
        stale=stale,
        stale_reason=stale_reason,
        ready_for_cutover_evaluation=ready,
        not_ready_reasons=not_ready,
        duration_ms=int(status.get("duration_ms") or 0),
        safe_error_code=error_code,
        safe_error_message=str(status.get("safe_error_message") or ""),
        public_artifact_secret_scan_passed=scan_ok,
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{time.monotonic_ns()}.tmp")
    tmp_path.write_text(json.dumps(sanitize_for_log(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _read_json_safely(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sync_run_id(value: str | None) -> str:
    text = str(value or "").strip()
    if text:
        return re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)[:160]
    return f"shadow-{time.time_ns()}"


def _duration_ms(started_at: float) -> int:
    return int(round((time.monotonic() - started_at) * 1000))


def _stale_status(last_attempt_at: str, stale_seconds: int) -> tuple[bool, str]:
    if not last_attempt_at:
        return True, "no_last_attempt"
    parsed = parse_iso_datetime(last_attempt_at)
    if parsed is None:
        return True, "last_attempt_at_invalid"
    age = max(time.time() - parsed.timestamp(), 0)
    if age > stale_seconds:
        return True, "last_attempt_stale"
    return False, ""


def _health_status(
    *,
    stale: bool,
    error_code: str,
    blocking_count: int,
    warning_count: int,
    passed: bool,
) -> str:
    if stale:
        return "stale"
    if error_code:
        return "error"
    if blocking_count:
        return "blocking"
    if not passed or warning_count:
        return "warning"
    return "healthy"


def _public_artifact_secret_scan(root: Path, snapshot_id: str) -> tuple[bool, str]:
    if not snapshot_id:
        return False, "missing_snapshot_id"
    snapshot_dir = root / "snapshots" / snapshot_id
    if not snapshot_dir.exists():
        return False, "snapshot_dir_missing"
    for path in snapshot_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.relative_to(snapshot_dir / "private")
            continue
        except ValueError:
            pass
        if path.suffix.lower() not in {".json", ".csv", ".txt", ".md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig")
        if _contains_sensitive_artifact_text(text):
            return False, "public_artifact_secret_scan_failed"
    return True, ""


def _contains_sensitive_artifact_text(text: str) -> bool:
    if re.search(r"canary", text, flags=re.IGNORECASE):
        return True
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        return True
    if re.search(r"[A-Za-z]:\\Users\\", text):
        return True
    return False


def _safe_error_message(exc: BaseException) -> str:
    text = redact_sensitive_text(str(exc))[:500]
    text = re.sub(r"\b[\w-]*canary[\w#-]*", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsecret[\w#-]*", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", text)
    text = re.sub(r"/(?:[^\s/]+/)+[^\s\"']+", "<path>", text)
    return text


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, SnapshotStoreError):
        return "snapshot_store_error"
    return name or "shadow_error"


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name
