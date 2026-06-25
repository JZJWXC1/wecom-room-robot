from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any

import pandas as pd

from app.services.inventory import InventoryService
from app.services.inventory_read_models import (
    FALLBACK_LEGACY_WHOLE_REQUEST,
    FALLBACK_STRICT,
    READ_MODE_DISABLED,
    READ_MODE_PRIMARY,
    InventoryListingEvidence,
)
from app.services.inventory_read_provider import (
    LegacyInventoryReadProvider,
    SnapshotInventoryReadProvider,
)
from app.services.inventory_read_router import InventoryReadRouter
from app.services.inventory_sensitive_access import (
    InventorySheetArtifactResult,
    sheet_artifacts_for_context,
)
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_models import (
    CurrentSnapshotPointer,
    InventorySnapshot,
    InventorySourceMetadata,
    InventorySyncReport,
    now_utc_iso,
    sanitize_for_log,
)
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.inventory_snapshot_shadow import scan_public_artifacts_for_sensitive_text
from app.services.inventory_snapshot_store import SnapshotStore
from app.services.rewrite_inventory_index import build_rewrite_inventory_index


@dataclass(frozen=True)
class PrimaryReplayCase:
    name: str
    query: Any
    limit: int = 8


@dataclass(frozen=True)
class LocalSnapshotBuildResult:
    root: Path
    snapshot: InventorySnapshot
    report: InventorySyncReport
    pointer: CurrentSnapshotPointer | None
    sheet_png_path: Path | None = None


@dataclass(frozen=True)
class PreparedOutboundPackage:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    send_actions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_log(
            {
                "schema_version": "prepared_outbound_package.local_replay.v1",
                "text": self.text,
                "metadata": self.metadata,
                "send_actions": list(self.send_actions),
            }
        )


def synthetic_inventory_rows(*, version: str = "v1") -> list[dict[str, Any]]:
    rows = [
        {
            "区域": "东新园 杭氧 新天地",
            "小区": "晨星花园",
            "房号": "1-101A",
            "户型描述": "朝南一室，民用水电",
            "户型分类": "一室",
            "押一付一": "1800",
            "押二付一": "1700",
            "看房方式密码": "0007#",
            "备注": "民水民电",
            "图片": "有",
            "视频": "有",
        },
        {
            "区域": "东新园 杭氧 新天地",
            "小区": "晨星花园",
            "房号": "1-102",
            "户型描述": "一室一厅带厅",
            "户型分类": "一室一厅",
            "押一付一": "3200",
            "押二付一": "3000",
            "看房方式密码": "提前联系管家",
            "备注": "水30，电1元/度",
            "图片": "有",
            "视频": "无",
        },
        {
            "区域": "东新园 杭氧 新天地",
            "小区": "星河公寓",
            "房号": "2-201",
            "户型描述": "两室一厅，中文全角符号",
            "户型分类": "两室一厅",
            "押一付一": "3500",
            "押二付一": "3300",
            "看房方式密码": "",
            "备注": "商水商电",
            "图片": "无",
            "视频": "有",
        },
        {
            "区域": "拱墅万达 北部软件园 城北万象城",
            "小区": "云杉苑",
            "房号": "A-302",
            "户型描述": "开间，独立厨卫",
            "户型分类": "单间",
            "押一付一": "1600",
            "押二付一": "1500",
            "看房方式密码": "",
            "备注": "民水民电",
            "图片": "有",
            "视频": "有",
        },
    ]
    if version != "v1":
        rows = [dict(row) for row in rows]
        rows[0]["押一付一"] = "1900"
        rows[0]["备注"] = "民水民电，第二版本地演练"
    return rows


def default_replay_cases() -> tuple[PrimaryReplayCase, ...]:
    return (
        PrimaryReplayCase("area_layout", {"query": "新填地一室", "area": "东新园 杭氧 新天地"}),
        PrimaryReplayCase("area_budget", {"query": "东新园3000-3500两室", "area": "东新园 杭氧 新天地"}),
        PrimaryReplayCase("exact_room", "晨星花园1-101A"),
        PrimaryReplayCase("community_layout", "晨星花园一室带厅"),
        PrimaryReplayCase("media_utility", "云杉苑民水民电视频图片"),
    )


def build_local_snapshot(
    root: Path,
    rows: list[dict[str, Any]],
    *,
    version: str,
    activate: bool = True,
    include_sheet_png: bool = False,
) -> LocalSnapshotBuildResult:
    snapshot, report = SnapshotBuilder().build(
        rows,
        InventorySourceMetadata(
            source_kind="m1d2b2_local_fixture",
            source_version=version,
            extra={"offline": True, "fictional": True},
        ),
        generated_at="2026-06-25T00:00:00Z",
    )
    if not report.ok:
        raise ValueError("synthetic snapshot build failed")
    pointer = SnapshotStore(root).write_snapshot(snapshot, report, activate=activate)
    sheet_png_path = _attach_sheet_png(root, snapshot.snapshot_id) if include_sheet_png else None
    if sheet_png_path is not None:
        reader = SnapshotReader(root)
        validation = reader.get_snapshot(snapshot.snapshot_id)
        if not validation.ok:
            raise ValueError(f"snapshot with sheet png failed validation: {validation.code}")
    return LocalSnapshotBuildResult(
        root=root,
        snapshot=snapshot,
        report=report,
        pointer=pointer,
        sheet_png_path=sheet_png_path,
    )


def run_primary_replay(
    root: Path,
    rows: list[dict[str, Any]] | None = None,
    *,
    cases: tuple[PrimaryReplayCase, ...] | None = None,
    include_sheet_png: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    fixture_rows = rows or synthetic_inventory_rows()
    build = build_local_snapshot(
        root,
        fixture_rows,
        version="v1",
        include_sheet_png=include_sheet_png,
    )
    legacy_provider = _legacy_provider(fixture_rows)
    snapshot_provider = SnapshotInventoryReadProvider(SnapshotReader(root))
    primary_router = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=legacy_provider,
        snapshot_provider=snapshot_provider,
        readiness_state=ready_readiness_state(),
    )
    legacy_router = InventoryReadRouter(
        mode=READ_MODE_DISABLED,
        legacy_provider=legacy_provider,
        snapshot_provider=snapshot_provider,
    )

    case_results: list[dict[str, Any]] = []
    for case in cases or default_replay_cases():
        legacy_session = legacy_router.start_turn(request_id=f"legacy-{case.name}", turn_id="turn-1")
        primary_session = primary_router.start_turn(request_id=f"primary-{case.name}", turn_id="turn-1")
        legacy_evidence = _run(legacy_session.search_inventory(case.query, limit=case.limit))
        primary_evidence = _run(primary_session.search_inventory(case.query, limit=case.limit))
        case_results.append(
            {
                "name": case.name,
                "query": case.query,
                "legacy": _evidence_signature(legacy_evidence),
                "snapshot": _evidence_signature(primary_evidence),
                "parity_passed": _evidence_signature(legacy_evidence) == _evidence_signature(primary_evidence),
                "decision_id": primary_session.context.decision_id,
                "snapshot_id": primary_session.context.snapshot_id,
                "source_hash": primary_session.context.source_hash,
            }
        )

    primary_session = primary_router.start_turn(request_id="primary-sheet", turn_id="turn-1")
    sheet_result = _run(
        sheet_artifacts_for_context(
            context=primary_session.context,
            refresh_func=_noop_async,
            list_paths_func=lambda: [],
            snapshot_reader=SnapshotReader(root),
        )
    )
    package = _prepared_package(primary_session.context.to_log_dict(), sheet_result)
    scan = scan_public_artifacts_for_sensitive_text(root, snapshot_id=build.snapshot.snapshot_id)
    duration_ms = int(round((time.perf_counter() - started) * 1000))
    report = {
        "schema_version": "inventory_snapshot_primary_replay.v1",
        "ok": all(item["parity_passed"] for item in case_results) and bool(scan["passed"]),
        "snapshot_id": build.snapshot.snapshot_id,
        "source_hash": build.snapshot.source_hash,
        "case_count": len(case_results),
        "cases": case_results,
        "sheet_evidence": [item.to_dict() for item in sheet_result.evidence],
        "prepared_outbound_package": package.to_dict(),
        "public_artifact_scan": scan,
        "performance": {
            "duration_ms": duration_ms,
            "cases_per_second": round((len(case_results) / max(duration_ms, 1)) * 1000, 2),
        },
    }
    return sanitize_for_log(report)


def evaluate_cutover_readiness(
    root: Path,
    *,
    readiness_state: dict[str, Any] | None = None,
    replay_report: dict[str, Any] | None = None,
    min_parity_cases: int = 1,
) -> dict[str, Any]:
    reader = SnapshotReader(root)
    pointer = reader.get_current_pointer()
    health = reader.health()
    not_ready: list[str] = []
    scan = {"passed": False, "files_scanned": 0, "findings": []}
    if pointer.ok:
        scan = scan_public_artifacts_for_sensitive_text(root, snapshot_id=pointer.value.snapshot_id)
    else:
        not_ready.append(pointer.code)
    decision = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=_legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(reader),
        readiness_state=readiness_state or ready_readiness_state(),
    ).select_context(request_id="cutover-readiness", turn_id="turn-1")
    if not decision.ok and decision.error is not None:
        not_ready.append(decision.error.code)
    if not scan.get("passed"):
        not_ready.append("public_artifact_secret_scan_failed")
    if replay_report is not None:
        parity_cases = [item for item in replay_report.get("cases") or [] if item.get("parity_passed")]
        if not replay_report.get("ok") or len(parity_cases) < min_parity_cases:
            not_ready.append("primary_replay_parity_failed")
    ready = not not_ready
    return sanitize_for_log(
        {
            "schema_version": "inventory_snapshot_cutover_readiness.v1",
            "ready": ready,
            "not_ready_reasons": sorted(set(not_ready)),
            "mode": READ_MODE_PRIMARY,
            "snapshot_health": health.to_dict(),
            "decision": decision.to_dict(),
            "public_artifact_scan": scan,
            "replay_case_count": len(replay_report.get("cases") or []) if replay_report else 0,
        }
    )


def rehearse_rollback(root: Path) -> dict[str, Any]:
    first = build_local_snapshot(root, synthetic_inventory_rows(version="v1"), version="rollback-v1")
    first_snapshot_id = first.snapshot.snapshot_id
    second = build_local_snapshot(root, synthetic_inventory_rows(version="v2"), version="rollback-v2")
    second_snapshot_id = second.snapshot.snapshot_id
    before = SnapshotReader(root).get_current_pointer()
    rollback = switch_current_pointer(root, first_snapshot_id)
    after = SnapshotReader(root).get_current_pointer()
    return sanitize_for_log(
        {
            "schema_version": "inventory_snapshot_primary_rollback_rehearsal.v1",
            "ok": bool(rollback["ok"] and after.ok and after.value.snapshot_id == first_snapshot_id),
            "from_snapshot_id": second_snapshot_id,
            "to_snapshot_id": first_snapshot_id,
            "before_snapshot_id": before.value.snapshot_id if before.ok else "",
            "after_snapshot_id": after.value.snapshot_id if after.ok else "",
            "rollback": rollback,
        }
    )


def switch_current_pointer(root: Path, snapshot_id: str) -> dict[str, Any]:
    reader = SnapshotReader(root)
    snapshot_result = reader.get_snapshot(snapshot_id)
    if not snapshot_result.ok:
        return {"ok": False, "code": snapshot_result.code, "message": snapshot_result.message}
    snapshot = snapshot_result.value
    pointer = CurrentSnapshotPointer(
        snapshot_id=snapshot.snapshot_id,
        source_hash=snapshot.source_hash,
        snapshot_path=(Path("snapshots") / snapshot.snapshot_id).as_posix(),
        created_at=snapshot.generated_at,
        activated_at=now_utc_iso(),
        row_count=len(snapshot.listings),
    )
    pointer_path = Path(root) / "current_snapshot.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pointer_path.with_name(f".{pointer_path.name}.rollback.tmp")
    tmp_path.write_text(json.dumps(pointer.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    validation = reader.validator.validate_pointer(json.loads(tmp_path.read_text(encoding="utf-8")), Path(root))
    if not validation.ok:
        tmp_path.unlink(missing_ok=True)
        return {
            "ok": False,
            "code": "rollback_pointer_invalid",
            "issues": [issue.to_dict() for issue in validation.errors],
        }
    tmp_path.replace(pointer_path)
    return {"ok": True, "snapshot_id": snapshot.snapshot_id, "source_hash": snapshot.source_hash}


def ready_readiness_state(**overrides: Any) -> dict[str, Any]:
    payload = {
        "reconciliation_passed": True,
        "blocking_count": 0,
        "public_artifact_secret_scan_passed": True,
        "missing_valid_aliases": 0,
        "unresolved_aliases": 0,
        "active_alias_conflicts": 0,
        "unknown_canonical_areas": 0,
        "ambiguous_direct_mappings": 0,
    }
    payload.update(overrides)
    return payload


def strict_and_fallback_probe(root: Path) -> dict[str, Any]:
    strict = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_STRICT,
        legacy_provider=_legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
    ).select_context(request_id="strict-missing", turn_id="turn-1")
    fallback = InventoryReadRouter(
        mode=READ_MODE_PRIMARY,
        fallback_strategy=FALLBACK_LEGACY_WHOLE_REQUEST,
        legacy_provider=_legacy_provider(synthetic_inventory_rows()),
        snapshot_provider=SnapshotInventoryReadProvider(SnapshotReader(root)),
    ).select_context(request_id="fallback-missing", turn_id="turn-1")
    return sanitize_for_log(
        {
            "strict": strict.to_dict(),
            "legacy_whole_request": fallback.to_dict(),
        }
    )


def legacy_removal_report() -> dict[str, Any]:
    return {
        "schema_version": "inventory_snapshot_legacy_removal_report.v1",
        "removed_this_milestone": [],
        "retained": [
            {
                "component": "InventoryService",
                "reason": "production customer reads remain disabled/shadow until approved cutover",
                "removal_milestone": "after approved primary cutover and rollback window",
            },
            {
                "component": "legacy CSV/rewrite index/PNG",
                "reason": "legacy_whole_request rollback and production safety",
                "removal_milestone": "post-primary stability review",
            },
            {
                "component": "LegacyInventoryReadProvider",
                "reason": "golden parity and whole-request fallback contract",
                "removal_milestone": "after primary becomes sole approved read source",
            },
        ],
    }


def _legacy_provider(rows: list[dict[str, Any]]) -> LegacyInventoryReadProvider:
    service = InventoryService()
    service._cache = pd.DataFrame(rows)
    service._cache_file_marker = None
    service._cache_meta = {
        "status": "success",
        "hash": _rows_hash(rows),
        "row_count": len(rows),
    }
    return LegacyInventoryReadProvider(
        service,
        rewrite_index_loader=lambda: build_rewrite_inventory_index(rows, cache_meta=service.cache_meta()),
    )


def _evidence_signature(items: list[InventoryListingEvidence]) -> list[dict[str, Any]]:
    return [
        {
            "listing_id": item.listing_id,
            "room_no": item.room_no,
            "rent_pay1": item.rent_pay1,
            "rent_pay2": item.rent_pay2,
            "utility_summary": dict(item.utility_summary),
            "availability_summary": dict(item.availability_summary),
            "has_image": item.has_image,
            "has_video": item.has_video,
        }
        for item in items
    ]


def _prepared_package(context: dict[str, Any], sheet_result: InventorySheetArtifactResult) -> PreparedOutboundPackage:
    evidence_ids = [item.evidence_id for item in sheet_result.evidence]
    send_actions = tuple(
        {
            "type": "image",
            "metadata": {
                "evidence_id": item.evidence_id,
                "decision_id": item.decision_id,
                "source_hash": item.source_hash,
                "snapshot_id": item.snapshot_id,
            },
        }
        for item in sheet_result.evidence
    )
    return PreparedOutboundPackage(
        text="本地 primary 演练已准备房源表发送动作。",
        metadata={
            "decision_id": context.get("decision_id", ""),
            "snapshot_id": context.get("snapshot_id", ""),
            "source_hash": context.get("source_hash", ""),
            "evidence_ids": evidence_ids,
        },
        send_actions=send_actions,
    )


async def _noop_async() -> None:
    return None


def _attach_sheet_png(root: Path, snapshot_id: str) -> Path:
    snapshot_dir = root / "snapshots" / snapshot_id
    png_path = snapshot_dir / "png" / "inventory.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(b"\x89PNG\r\n\x1a\nm1d2b2-local-fixture")
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})["inventory_sheet_png"] = {
        "path": "png/inventory.png",
        "sha256": _file_sha256(png_path),
        "bytes": png_path.stat().st_size,
        "mime_type": "image/png",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return png_path


def _rows_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)
