from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from app.config import settings
from app.services.inventory_snapshot_models import redact_sensitive_text, sanitize_for_log
from app.services.inventory_snapshot_shadow import get_inventory_snapshot_shadow_health


OBSERVATION_SCHEMA_VERSION = "inventory_snapshot_shadow_observation.v1"


@dataclass(frozen=True)
class ShadowObservationOptions:
    root: Path | None = None
    mode: str | None = None
    stale_seconds: int | None = None
    required_consecutive_passes: int | None = None


def collect_shadow_observation(options: ShadowObservationOptions | None = None) -> dict[str, Any]:
    options = options or ShadowObservationOptions()
    root = Path(options.root) if options.root is not None else Path(settings.inventory_snapshot_shadow_root)
    health = get_inventory_snapshot_shadow_health(
        root=root,
        mode=options.mode,
        stale_seconds=options.stale_seconds,
        required_consecutive_passes=options.required_consecutive_passes,
    ).to_dict()
    status = _read_json(root / "shadow_status.json")
    pointer = _read_json(root / "shadow_current_snapshot.json")
    report = _read_reconciliation_report(root, pointer, str(health.get("last_snapshot_id") or ""))
    payload = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "mode": health.get("mode") or "",
        "status": health.get("status") or "",
        "last_attempt_at": health.get("last_attempt_at") or "",
        "last_success_at": health.get("last_success_at") or "",
        "source_hash_prefix": str(health.get("last_source_hash") or "")[:12],
        "snapshot_id": health.get("last_snapshot_id") or "",
        "reconciliation_passed": bool(health.get("last_reconciliation_passed")),
        "blocking_count": int(health.get("last_blocking_count") or 0),
        "warning_count": int(health.get("last_warning_count") or 0),
        "consecutive_passes": int(health.get("consecutive_passes") or 0),
        "consecutive_failures": int(health.get("consecutive_failures") or 0),
        "stale": bool(health.get("stale")),
        "stale_reason": health.get("stale_reason") or "",
        "ready_for_cutover_evaluation": bool(health.get("ready_for_cutover_evaluation")),
        "not_ready_reasons": list(health.get("not_ready_reasons") or []),
        "public_artifact_secret_scan_passed": bool(health.get("public_artifact_secret_scan_passed")),
        "safe_error": {
            "code": health.get("safe_error_code") or "",
            "message": health.get("safe_error_message") or "",
        },
        "recent_sync_status": _sync_status(status),
        "recent_reconciliation": report,
    }
    return _sanitize_public_payload(payload)


def format_shadow_observation(payload: dict[str, Any]) -> str:
    recent = dict(payload.get("recent_reconciliation") or {})
    safe_error = dict(payload.get("safe_error") or {})
    lines = [
        "InventorySnapshot Shadow 观察",
        f"mode: {payload.get('mode') or ''}",
        f"status: {payload.get('status') or ''}",
        f"last_attempt_at: {payload.get('last_attempt_at') or ''}",
        f"last_success_at: {payload.get('last_success_at') or ''}",
        f"source_hash_prefix: {payload.get('source_hash_prefix') or ''}",
        f"snapshot_id: {payload.get('snapshot_id') or ''}",
        f"reconciliation_passed: {str(bool(payload.get('reconciliation_passed'))).lower()}",
        f"blocking_count: {int(payload.get('blocking_count') or 0)}",
        f"warning_count: {int(payload.get('warning_count') or 0)}",
        f"consecutive_passes: {int(payload.get('consecutive_passes') or 0)}",
        f"stale: {str(bool(payload.get('stale'))).lower()}",
        f"ready_for_cutover_evaluation: {str(bool(payload.get('ready_for_cutover_evaluation'))).lower()}",
        f"public_artifact_secret_scan_passed: {str(bool(payload.get('public_artifact_secret_scan_passed'))).lower()}",
        f"safe_error_code: {safe_error.get('code') or ''}",
        f"safe_error_message: {safe_error.get('message') or ''}",
        f"recent_reconciliation_status: {recent.get('status') or ''}",
        f"recent_reconciliation_matched_count: {recent.get('matched_count') if recent.get('matched_count') is not None else ''}",
    ]
    reasons = payload.get("not_ready_reasons") or []
    if reasons:
        lines.append("not_ready_reasons: " + ", ".join(str(item) for item in reasons))
    return "\n".join(lines) + "\n"


def _sync_status(status: dict[str, Any]) -> dict[str, Any]:
    if not status:
        return {"status": "missing"}
    return {
        "status": "present",
        "last_sync_run_id": str(status.get("last_sync_run_id") or ""),
        "source_version": str(status.get("source_version") or ""),
        "source_hash_prefix": str(status.get("last_source_hash") or status.get("source_hash") or "")[:12],
        "duration_ms": int(status.get("duration_ms") or 0),
        "error_code": str(status.get("error_code") or ""),
    }


def _read_reconciliation_report(root: Path, pointer: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    report_path_text = str(pointer.get("report_path") or "").strip()
    candidates: list[Path] = []
    if report_path_text and _is_safe_relative_path(report_path_text):
        candidates.append(root / report_path_text)
    if snapshot_id:
        candidates.append(root / "reports" / f"{snapshot_id}_reconciliation.json")
    if not candidates:
        return {"status": "missing"}
    report_path = candidates[0]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "missing", "report_path": report_path.name}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "corrupt",
            "report_path": report_path.name,
            "safe_error": redact_sensitive_text(str(exc))[:300],
        }
    if not isinstance(report, dict):
        return {"status": "corrupt", "report_path": report_path.name}
    severity = dict(report.get("severity_counts") or {})
    return {
        "status": "present",
        "report_path": report_path.name,
        "legacy_record_count": int(report.get("legacy_record_count") or 0),
        "snapshot_record_count": int(report.get("snapshot_record_count") or 0),
        "matched_count": int(report.get("matched_count") or 0),
        "passed": bool(report.get("passed")),
        "blocking_count": int(severity.get("blocking") or 0),
        "warning_count": int(severity.get("warning") or 0),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_safe_relative_path(value: str) -> bool:
    return bool(value) and not value.startswith(("/", "~")) and ":" not in value and ".." not in Path(value).parts


def _sanitize_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_for_log(payload)
    text = json.dumps(sanitized, ensure_ascii=False)
    text = re.sub(r"\b[\w-]*canary[\w#-]*", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(token|access_token|secret)\s+[^\s,\"}]+", r"\1 [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(token|access_token|secret)\s*[:=]\s*[^\s,\"}]+", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z]:\\\\Users\\\\[^\"}]+", "<path>", text)
    text = re.sub(r"[A-Za-z]:\\Users\\[^\"]+", "<path>", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED]", text)
    return json.loads(text)
