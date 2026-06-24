from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from app.services.inventory_snapshot_models import (
    CurrentSnapshotPointer,
    InventorySnapshot,
    InventorySnapshotHealth,
    InventorySyncReport,
    now_utc_iso,
)
from app.services.inventory_snapshot_validator import SnapshotValidator


DEFAULT_SNAPSHOT_ROOT = Path("data/inventory_snapshots")


class SnapshotStoreError(RuntimeError):
    pass


class SnapshotStore:
    def __init__(
        self,
        root: Path | str = DEFAULT_SNAPSHOT_ROOT,
        *,
        validator: SnapshotValidator | None = None,
    ) -> None:
        self.root = Path(root)
        self.validator = validator or SnapshotValidator()

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def current_pointer_path(self) -> Path:
        return self.root / "current_snapshot.json"

    def write_snapshot(
        self,
        snapshot: InventorySnapshot,
        report: InventorySyncReport,
        *,
        activate: bool = True,
        simulate_write_failure_after: str | None = None,
        simulate_pointer_failure: bool = False,
    ) -> CurrentSnapshotPointer | None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        staging = self.tmp_dir / f"{snapshot.snapshot_id}.tmp"
        final_path = self.snapshots_dir / snapshot.snapshot_id
        if final_path.exists():
            raise SnapshotStoreError(f"snapshot already exists: {snapshot.snapshot_id}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        try:
            self._write_snapshot_files(
                staging,
                snapshot,
                report,
                simulate_write_failure_after=simulate_write_failure_after,
            )
            validation_result = self.validator.validate_directory(staging)
            validation_result.extend(report.validation_result)
            if not validation_result.ok:
                raise SnapshotStoreError(
                    "snapshot validation failed: "
                    + "; ".join(issue.code for issue in validation_result.errors)
                )
            staging.replace(final_path)
            if not activate:
                return None
            return self._activate_snapshot(
                snapshot,
                simulate_pointer_failure=simulate_pointer_failure,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def _write_snapshot_files(
        self,
        staging: Path,
        snapshot: InventorySnapshot,
        report: InventorySyncReport,
        *,
        simulate_write_failure_after: str | None,
    ) -> None:
        paths = {
            "inventory_json": staging / "inventory.json",
            "inventory_csv": staging / "inventory.csv",
            "rewrite_inventory_index": staging / "rewrite_inventory_index.json",
            "sync_report": staging / "sync_report.json",
            "private_viewing_secrets": staging / "private" / "viewing_secrets.json",
        }
        _write_json(paths["inventory_json"], snapshot.inventory_payload(redact_sensitive=True))
        self._maybe_fail(simulate_write_failure_after, "inventory_json")
        self._write_inventory_csv(paths["inventory_csv"], snapshot)
        self._maybe_fail(simulate_write_failure_after, "inventory_csv")
        _write_json(paths["rewrite_inventory_index"], snapshot.rewrite_index)
        self._maybe_fail(simulate_write_failure_after, "rewrite_inventory_index")
        _write_json(paths["private_viewing_secrets"], snapshot.private_viewing_secrets)
        self._maybe_fail(simulate_write_failure_after, "private_viewing_secrets")
        _write_json(paths["sync_report"], report.to_dict(redact_sensitive=True))
        self._maybe_fail(simulate_write_failure_after, "sync_report")

        snapshot.manifest.files.update(
            {
                "inventory_json": _file_entry(paths["inventory_json"], staging),
                "inventory_csv": _file_entry(paths["inventory_csv"], staging),
                "rewrite_inventory_index": _file_entry(paths["rewrite_inventory_index"], staging),
                "sync_report": _file_entry(paths["sync_report"], staging),
                "private_viewing_secrets": _file_entry(paths["private_viewing_secrets"], staging),
                "manifest": {"path": "manifest.json"},
                "png": {"path": "png/", "status": "reserved"},
            }
        )
        _write_json(staging / "manifest.json", snapshot.manifest.to_dict(redact_sensitive=False))
        self._maybe_fail(simulate_write_failure_after, "manifest")

    def _write_inventory_csv(self, path: Path, snapshot: InventorySnapshot) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "listing_id",
            "source_record_id",
            "source_row_number",
            "area",
            "community",
            "room_no",
            "layout_desc",
            "layout_type",
            "rent_monthly_pay1",
            "rent_monthly_pay2",
            "viewing_secret_ref",
            "has_password",
            "viewing_mode",
            "availability_status",
            "remark",
            "has_image",
            "has_video",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for listing in snapshot.listings:
                writer.writerow(
                    {
                        "listing_id": listing.listing_id,
                        "source_record_id": listing.source_record_id or "",
                        "source_row_number": str(listing.source_row_number),
                        "area": listing.area,
                        "community": listing.community,
                        "room_no": listing.room_no,
                        "layout_desc": listing.layout_desc,
                        "layout_type": listing.layout_type,
                        "rent_monthly_pay1": "" if listing.rent_monthly_pay1 is None else str(listing.rent_monthly_pay1),
                        "rent_monthly_pay2": "" if listing.rent_monthly_pay2 is None else str(listing.rent_monthly_pay2),
                        "viewing_secret_ref": listing.viewing_secret_ref,
                        "has_password": str(bool(listing.viewing_summary.get("has_password"))).lower(),
                        "viewing_mode": str(listing.viewing_summary.get("viewing_mode") or ""),
                        "availability_status": str(listing.availability_summary.get("status") or ""),
                        "remark": listing.remark,
                        "has_image": str(listing.has_image).lower(),
                        "has_video": str(listing.has_video).lower(),
                    }
                )

    def _activate_snapshot(
        self,
        snapshot: InventorySnapshot,
        *,
        simulate_pointer_failure: bool = False,
    ) -> CurrentSnapshotPointer:
        activated_at = now_utc_iso()
        pointer = CurrentSnapshotPointer(
            snapshot_id=snapshot.snapshot_id,
            source_hash=snapshot.source_hash,
            snapshot_path=(Path("snapshots") / snapshot.snapshot_id).as_posix(),
            created_at=snapshot.generated_at,
            activated_at=activated_at,
            row_count=len(snapshot.listings),
            health=InventorySnapshotHealth(
                status="ok",
                snapshot_id=snapshot.snapshot_id,
                age_seconds=0,
                message="snapshot activated",
                checked_at=activated_at,
            ),
        )
        pointer_tmp = self.current_pointer_path.with_name(f"{self.current_pointer_path.name}.tmp")
        _write_json(pointer_tmp, pointer.to_dict())
        reloaded = json.loads(pointer_tmp.read_text(encoding="utf-8"))
        validation_result = self.validator.validate_pointer(reloaded, self.root)
        if not validation_result.ok:
            pointer_tmp.unlink(missing_ok=True)
            raise SnapshotStoreError(
                "current pointer validation failed: "
                + "; ".join(issue.code for issue in validation_result.errors)
            )
        if simulate_pointer_failure:
            pointer_tmp.unlink(missing_ok=True)
            raise SnapshotStoreError("simulated current pointer replace failure")
        pointer_tmp.replace(self.current_pointer_path)
        return pointer

    def _maybe_fail(self, simulate_write_failure_after: str | None, logical_name: str) -> None:
        if simulate_write_failure_after == logical_name:
            raise SnapshotStoreError(f"simulated write failure after {logical_name}")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
