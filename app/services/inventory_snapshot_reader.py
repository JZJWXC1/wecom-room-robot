from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from app.services.inventory_snapshot_models import (
    CurrentSnapshotPointer,
    InventorySnapshot,
    InventorySnapshotHealth,
    InventorySnapshotManifest,
    SnapshotReadResult,
    SnapshotValidationIssue,
    parse_iso_datetime,
)
from app.services.inventory_snapshot_store import DEFAULT_SNAPSHOT_ROOT
from app.services.inventory_snapshot_validator import SnapshotValidator


class SnapshotReader:
    def __init__(
        self,
        root: Path | str = DEFAULT_SNAPSHOT_ROOT,
        *,
        validator: SnapshotValidator | None = None,
        max_age_seconds: int | None = None,
    ) -> None:
        self.root = Path(root)
        self.validator = validator or SnapshotValidator()
        self.max_age_seconds = max_age_seconds

    @property
    def current_pointer_path(self) -> Path:
        return self.root / "current_snapshot.json"

    def get_current_pointer(self) -> SnapshotReadResult:
        path = self.current_pointer_path
        if not path.exists():
            return _error_result(
                "current_pointer_missing",
                "没有 current_snapshot.json，不能猜测最新 snapshot。",
                status="missing",
            )
        try:
            data = _read_json(path)
            validation = self.validator.validate_pointer(data, self.root)
            if not validation.ok:
                return SnapshotReadResult(
                    ok=False,
                    code="current_pointer_invalid",
                    message="current_snapshot.json 指向的快照不可用。",
                    status="corrupt",
                    issues=validation.errors,
                )
            return SnapshotReadResult(ok=True, value=CurrentSnapshotPointer.from_dict(data), status="ok")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _error_result(
                "current_pointer_unreadable",
                "current_snapshot.json 损坏或不可读。",
                status="corrupt",
                issues=[_issue("error", "current_pointer_unreadable", str(exc), path="current_snapshot.json")],
            )

    def get_current_snapshot(self) -> SnapshotReadResult:
        pointer_result = self.get_current_pointer()
        if not pointer_result.ok:
            return pointer_result
        pointer = pointer_result.value
        return self.get_snapshot(pointer.snapshot_id)

    def get_snapshot(self, snapshot_id: str) -> SnapshotReadResult:
        if not snapshot_id:
            return _error_result("missing_snapshot_id", "snapshot_id 不能为空。", status="invalid")
        snapshot_dir = self.root / "snapshots" / snapshot_id
        if not snapshot_dir.exists():
            return _error_result(
                "snapshot_missing",
                f"snapshot 目录不存在：{snapshot_id}",
                status="missing",
            )
        validation = self.validator.validate_directory(snapshot_dir)
        if not validation.ok:
            return SnapshotReadResult(
                ok=False,
                code="snapshot_invalid",
                message="snapshot 文件缺失或校验失败。",
                status="invalid",
                issues=validation.errors,
            )
        try:
            manifest = InventorySnapshotManifest.from_dict(_read_json(snapshot_dir / "manifest.json"))
            inventory_data = _read_json(snapshot_dir / "inventory.json")
            rewrite_index = _read_json(snapshot_dir / "rewrite_inventory_index.json")
            private_viewing_secrets = _read_optional_json(snapshot_dir / "private" / "viewing_secrets.json")
            snapshot = InventorySnapshot.from_inventory_payload(
                inventory_data,
                manifest=manifest,
                rewrite_index=rewrite_index,
                private_viewing_secrets=private_viewing_secrets,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _error_result(
                "snapshot_unreadable",
                "snapshot 文件不可读。",
                status="invalid",
                issues=[_issue("error", "snapshot_unreadable", str(exc), path=str(snapshot_dir))],
            )
        return SnapshotReadResult(ok=True, value=snapshot, status="ok")

    def get_listing(self, listing_id: str) -> SnapshotReadResult:
        snapshot_result = self.get_current_snapshot()
        if not snapshot_result.ok:
            return snapshot_result
        for listing in snapshot_result.value.listings:
            if listing.listing_id == listing_id:
                return SnapshotReadResult(ok=True, value=listing, status="ok")
        return _error_result(
            "listing_not_found",
            f"当前 snapshot 中没有 listing_id：{listing_id}",
            status="not_found",
        )

    def get_rewrite_index(self) -> SnapshotReadResult:
        snapshot_result = self.get_current_snapshot()
        if not snapshot_result.ok:
            return snapshot_result
        return SnapshotReadResult(ok=True, value=snapshot_result.value.rewrite_index, status="ok")

    def health(self) -> InventorySnapshotHealth:
        pointer_result = self.get_current_pointer()
        if not pointer_result.ok:
            return InventorySnapshotHealth(
                status=pointer_result.status or "missing",
                message=pointer_result.message,
                issues=pointer_result.issues,
            )
        pointer = pointer_result.value
        snapshot_result = self.get_snapshot(pointer.snapshot_id)
        if not snapshot_result.ok:
            return InventorySnapshotHealth(
                status="corrupt" if snapshot_result.status == "invalid" else snapshot_result.status,
                snapshot_id=pointer.snapshot_id,
                message=snapshot_result.message,
                issues=snapshot_result.issues,
            )
        age_seconds = _age_seconds(pointer.activated_at or pointer.created_at)
        status = "ok"
        message = "snapshot readable"
        if self.max_age_seconds is not None and age_seconds is not None and age_seconds > self.max_age_seconds:
            status = "stale"
            message = "snapshot age exceeds threshold"
        return InventorySnapshotHealth(
            status=status,
            snapshot_id=pointer.snapshot_id,
            age_seconds=age_seconds,
            message=message,
        )


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} is not a JSON object")
    return data


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_json(path)


def _age_seconds(value: str) -> int | None:
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))


def _issue(severity: str, code: str, message: str, *, path: str = "") -> SnapshotValidationIssue:
    return SnapshotValidationIssue(severity=severity, code=code, message=message, path=path)


def _error_result(
    code: str,
    message: str,
    *,
    status: str,
    issues: list[SnapshotValidationIssue] | None = None,
) -> SnapshotReadResult:
    return SnapshotReadResult(
        ok=False,
        code=code,
        message=message,
        status=status,
        issues=issues or [_issue("error", code, message)],
    )
