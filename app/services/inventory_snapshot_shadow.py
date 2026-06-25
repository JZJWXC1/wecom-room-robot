from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from enum import Enum
import json
import logging
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
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        attempt_at = now_utc_iso()
        try:
            mode = parse_inventory_snapshot_mode(self.mode_value)
        except InventorySnapshotShadowConfigError as exc:
            result = ShadowExecutionResult(
                ok=False,
                mode=str(self.mode_value if self.mode_value is not None else settings.inventory_snapshot_mode),
                status="config_error",
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
            }

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inventory-snapshot-shadow")
        future = executor.submit(
            self._execute_shadow,
            legacy_rows,
            source_metadata,
            legacy_rewrite_index_path,
            legacy_rewrite_index,
            source_payload,
        )
        try:
            result = future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError:
            result = ShadowExecutionResult(
                ok=False,
                mode=mode.value,
                status="timeout",
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
            if result.ok:
                last_success_at = now_utc_iso()
            payload = {
                "schema_version": "inventory_snapshot_shadow_status.v1",
                "last_attempt_at": attempt_at,
                "last_success_at": last_success_at,
                "source_version": result.source_version,
                "source_hash": result.source_hash,
                "snapshot_id": result.snapshot_id,
                "reconciliation_passed": result.reconciliation_passed,
                "blocking_count": result.blocking_count,
                "warning_count": result.warning_count,
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
) -> dict[str, Any]:
    metadata = build_shadow_source_metadata(
        source_kind=source_kind,
        source_version=source_version,
        cache_meta=cache_meta,
    )
    return InventorySnapshotShadowCoordinator(
        root=root,
        timeout_seconds=timeout_seconds,
    ).run(
        legacy_rows=legacy_rows,
        source_metadata=metadata,
        legacy_rewrite_index_path=legacy_rewrite_index_path,
        legacy_rewrite_index=legacy_rewrite_index,
        source_payload=source_payload,
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


def _duration_ms(started_at: float) -> int:
    return int(round((time.monotonic() - started_at) * 1000))


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
