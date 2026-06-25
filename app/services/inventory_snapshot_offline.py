from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from app.services.inventory_legacy_parser import spreadsheet_values_to_inventory_rows
from app.services.inventory_snapshot_models import sanitize_for_log
from app.services.inventory_snapshot_shadow import (
    get_inventory_snapshot_shadow_health,
    run_inventory_snapshot_shadow,
)
from app.services.rewrite_inventory_index import build_rewrite_inventory_index


@dataclass(frozen=True)
class OfflineComparisonResult:
    ok: bool
    artifact_root: Path
    legacy_row_count: int
    shadow_result: dict[str, Any]
    health: dict[str, Any]
    artifact_scan_passed: bool
    artifact_scan_issues: list[str]
    paths: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "ok": self.ok,
                "artifact_root": self.artifact_root.name,
                "legacy_row_count": self.legacy_row_count,
                "shadow_result": self.shadow_result,
                "health": self.health,
                "artifact_scan_passed": self.artifact_scan_passed,
                "artifact_scan_issues": self.artifact_scan_issues,
                "paths": self.paths,
            }
        )


class InventorySnapshotOfflineComparisonRunner:
    """Run the legacy parser and Snapshot Shadow pipeline with offline values only."""

    def run(
        self,
        *,
        values: list[list[Any]],
        artifact_root: Path,
        fixture_name: str = "offline_fixture",
        sync_run_id: str | None = None,
    ) -> OfflineComparisonResult:
        artifact_root.mkdir(parents=True, exist_ok=True)
        legacy_rows = spreadsheet_values_to_inventory_rows(values)
        source_version = _source_version(fixture_name, values)
        legacy_index = build_rewrite_inventory_index(
            legacy_rows,
            cache_meta={
                "source": "offline_fixture",
                "status": "success",
                "hash": source_version,
                "row_count": len(legacy_rows),
            },
        )
        shadow_root = artifact_root / "shadow"
        run_id = sync_run_id or f"offline:{fixture_name}:{time.time_ns()}"
        shadow_result = run_inventory_snapshot_shadow(
            legacy_rows=legacy_rows,
            source_kind="offline_fixture",
            source_version=source_version,
            cache_meta={"hash": source_version, "row_count": len(legacy_rows)},
            legacy_rewrite_index=legacy_index,
            root=shadow_root,
            sync_run_id=run_id,
            mode="shadow",
        )
        health = get_inventory_snapshot_shadow_health(
            root=shadow_root,
            mode="shadow",
            stale_seconds=24 * 60 * 60,
            required_consecutive_passes=1,
        ).to_dict()

        paths = {
            "legacy_summary": "safe_legacy_summary.json",
            "snapshot_summary": "safe_snapshot_summary.json",
            "shadow_health": "shadow_health.json",
            "execution_summary": "test_execution_summary.json",
        }
        self._write_json(artifact_root / paths["legacy_summary"], _legacy_summary(legacy_rows))
        self._write_json(
            artifact_root / paths["snapshot_summary"],
            _snapshot_summary(shadow_root, str(shadow_result.get("snapshot_id") or "")),
        )
        self._write_json(artifact_root / paths["shadow_health"], health)
        execution_summary = {
            "ok": bool(shadow_result.get("ok")),
            "fixture_name": fixture_name,
            "source_version": source_version,
            "sync_run_id": run_id,
            "legacy_row_count": len(legacy_rows),
            "shadow": shadow_result,
        }
        self._write_json(artifact_root / paths["execution_summary"], execution_summary)
        scan_passed, issues = scan_safe_artifacts_for_canaries(artifact_root)
        return OfflineComparisonResult(
            ok=bool(shadow_result.get("ok")) and health.get("status") in {"healthy", "warning"},
            artifact_root=artifact_root,
            legacy_row_count=len(legacy_rows),
            shadow_result=shadow_result,
            health=health,
            artifact_scan_passed=scan_passed,
            artifact_scan_issues=issues,
            paths=paths,
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(sanitize_for_log(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def scan_safe_artifacts_for_canaries(root: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".json", ".csv", ".txt", ".md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig")
        lowered = text.lower()
        if "canary" in lowered or "phone_canary" in lowered:
            issues.append(path.relative_to(root).as_posix())
        if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
            issues.append(path.relative_to(root).as_posix())
        if "c:\\users\\" in lowered:
            issues.append(path.relative_to(root).as_posix())
    unique_issues = sorted(set(issues))
    return not unique_issues, unique_issues


def _source_version(fixture_name: str, values: list[list[Any]]) -> str:
    payload = {
        "fixture_name": fixture_name,
        "values": values,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _legacy_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(rows),
        "areas": sorted({str(row.get("区域") or "") for row in rows if row.get("区域")}),
        "communities": sorted({str(row.get("小区") or "") for row in rows if row.get("小区")}),
        "rooms": [
            {
                "area": row.get("区域") or "",
                "community": row.get("小区") or "",
                "room_no": row.get("房号") or "",
                "has_image": bool(row.get("图片") or row.get("has_image")),
                "has_video": bool(row.get("视频") or row.get("has_video")),
            }
            for row in rows
        ],
    }


def _snapshot_summary(shadow_root: Path, snapshot_id: str) -> dict[str, Any]:
    if not snapshot_id:
        return {"snapshot_id": "", "row_count": 0, "rooms": []}
    inventory_path = shadow_root / "snapshots" / snapshot_id / "inventory.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"snapshot_id": snapshot_id, "row_count": 0, "rooms": []}
    listings = inventory.get("listings") or []
    return {
        "snapshot_id": snapshot_id,
        "source_hash": inventory.get("source_hash") or "",
        "row_count": len(listings),
        "rooms": [
            {
                "listing_id": item.get("listing_id") or "",
                "area": item.get("area") or "",
                "community": item.get("community") or "",
                "room_no": item.get("room_no") or "",
                "has_image": bool(item.get("has_image")),
                "has_video": bool(item.get("has_video")),
                "has_password": bool((item.get("viewing_summary") or {}).get("has_password")),
            }
            for item in listings
            if isinstance(item, dict)
        ],
    }
