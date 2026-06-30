from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import sys
import tempfile
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.services.feishu import FeishuClient
from app.services.inventory import InventoryService
from app.services.inventory_sensitive_access import legacy_listing_id_for_row
from app.services.inventory_snapshot_builder import (
    IMAGE_FIELD_ALIASES,
    VIDEO_FIELD_ALIASES,
    parse_media_bool,
)
from app.services.inventory_snapshot_shadow import run_inventory_snapshot_shadow
from app.services.media_manifest import (
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    FeishuDriveMediaManifestAdapter,
    MediaBindingReport,
)
from app.services.region_inventory_sync import RegionInventorySyncService
from app.services.region_inventory_utils import safe_name
from app.services.rewrite_inventory_index import (
    DEFAULT_AREA_ALIASES,
    write_rewrite_inventory_index,
)


LOCK_STALE_SECONDS = 6 * 60 * 60


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def config_template() -> list[dict[str, Any]]:
    return [
        {
            "name": "四区汇总房源表",
            "app_token": "JefQbkmgCatbUEsXBZBcXSpWnFj",
            "table_id": "",
            "view_id": "",
            "split_by_area": "true",
            "area_field": "区域",
            "area_title_map": {
                "万达": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
                "石桥": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
                "东新": "东新园 杭氧 新天地 成交全部全佣🧧",
                "东站": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
            },
        },
    ]


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                self._remove()
                return self.acquire()
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(
                {"pid": os.getpid(), "created_at": now_iso()},
                output,
                ensure_ascii=False,
            )
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self._remove()
            self.acquired = False

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS
        except FileNotFoundError:
            return True

    def _remove(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def print_json(payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def write_state(result: dict[str, Any]) -> None:
    state_path = settings.feishu_region_sync_state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_run": result}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def run_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
    service = RegionInventorySyncService()
    return await service.sync(dry_run=dry_run, sync_media=sync_media)


def rows_from_frame(frame: Any) -> list[dict[str, Any]]:
    return frame.fillna("").to_dict(orient="records") if hasattr(frame, "fillna") else []


def listing_label_for_row(row: dict[str, Any]) -> str:
    community = str(row.get("小区") or row.get("小区名") or row.get("community") or "").strip()
    room_no = str(row.get("房号") or row.get("房间号") or row.get("room_no") or "").strip()
    return safe_name(f"{community}{room_no}") if community and room_no else ""


def media_manifest_context_for_rows(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    labels: dict[str, str] = {}
    for row in rows:
        listing_id = legacy_listing_id_for_row(row)
        label = listing_label_for_row(row)
        if listing_id and label and listing_id not in labels:
            labels[listing_id] = label
    return list(labels), labels


def expected_media_kinds_for_row(row: dict[str, Any]) -> list[str]:
    expected: list[str] = []
    if parse_media_bool(row, IMAGE_FIELD_ALIASES):
        expected.append(MEDIA_KIND_IMAGE)
    if parse_media_bool(row, VIDEO_FIELD_ALIASES):
        expected.append(MEDIA_KIND_VIDEO)
    return expected


def expected_media_kinds_by_listing(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    expected: dict[str, list[str]] = {}
    for row in rows:
        listing_id = legacy_listing_id_for_row(row)
        if not listing_id:
            continue
        kinds = expected_media_kinds_for_row(row)
        if kinds:
            expected.setdefault(listing_id, [])
            for kind in kinds:
                if kind not in expected[listing_id]:
                    expected[listing_id].append(kind)
    return expected


def missing_media_for_expected_rows(
    manifest: Any,
    expected_by_listing: dict[str, list[str]],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for listing_id, expected_kinds in expected_by_listing.items():
        missing_kinds = [
            kind
            for kind in expected_kinds
            if not manifest.items_for_listing(listing_id, kind=kind)
        ]
        if missing_kinds:
            missing.append({"listing_id": listing_id, "missing_kinds": missing_kinds})
    return missing


def media_manifest_ready(report: MediaBindingReport) -> bool:
    return report.ready


def media_manifest_blocking_count(report: MediaBindingReport) -> int:
    return (
        len(report.failed)
        + len(report.missing)
        + len(report.ambiguous_items)
        + len(report.orphan_items)
        + len(report.fuzzy_candidates)
    )


def compact_media_binding_report(report: MediaBindingReport) -> dict[str, Any]:
    payload = report.to_dict()
    for key in (
        "bound_items",
        "missing",
        "ambiguous_items",
        "orphan_items",
        "fuzzy_candidates",
        "isolated_items",
        "downloaded",
        "reused",
        "skipped",
        "failed",
    ):
        values = payload.get(key) or []
        payload[f"{key}_sample"] = values[:20]
        payload.pop(key, None)
    return payload


def _safe_manifest_relative_path(value: str) -> str:
    raw_path = str(value or "").strip().replace("\\", "/")
    if not raw_path:
        return ""
    normalized = PurePosixPath(raw_path).as_posix()
    if raw_path != normalized or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"unsafe media manifest path: {value}")
    return normalized


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_has_same_hash(target_path: Path, sha256: str) -> bool:
    return bool(sha256) and target_path.is_file() and _file_sha256(target_path) == sha256


def _unique_publish_relative_path(output_root: Path, relative_path: str, sha256: str) -> str:
    target_path = output_root / Path(relative_path)
    if not target_path.exists() or _target_has_same_hash(target_path, sha256):
        return relative_path

    path = PurePosixPath(relative_path)
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    digest = sha256[:12] or str(time.time_ns())
    parent = path.parent
    for index in range(100):
        marker = digest if index == 0 else f"{digest}-{index}"
        candidate_name = f"{stem}.{marker}{suffix}"
        candidate = parent / candidate_name
        candidate_relative = candidate.as_posix()
        candidate_path = output_root / Path(candidate_relative)
        if not candidate_path.exists() or _target_has_same_hash(candidate_path, sha256):
            return candidate_relative
    raise RuntimeError(f"cannot allocate collision-free media path: {relative_path}")


def _copy_file_atomically(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"staged media file is missing: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        shutil.copy2(source_path, temp_path)
        temp_path.replace(target_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_manifest_atomically(manifest: Any, manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_name(
        f".{manifest_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        manifest.write_json(temp_path)
        temp_path.replace(manifest_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def publish_media_manifest_snapshot(
    *,
    manifest: Any,
    staging_root: Path,
    output_root: Path,
    manifest_path: Path,
    relative_prefix: str = "",
) -> Any:
    output_root.mkdir(parents=True, exist_ok=True)
    published_items: list[Any] = []
    pending_copies: list[tuple[Path, Path]] = []
    for item in manifest.items:
        staging_relative_path = _safe_manifest_relative_path(item.local_path or item.relative_path)
        if not staging_relative_path:
            published_items.append(item)
            continue
        output_relative_path = _safe_manifest_relative_path(
            PurePosixPath(relative_prefix, staging_relative_path).as_posix()
        )
        source_path = staging_root / Path(staging_relative_path)
        if not source_path.is_file():
            raise FileNotFoundError(f"staged media file is missing: {source_path}")
        output_relative_path = _unique_publish_relative_path(
            output_root,
            output_relative_path,
            item.sha256,
        )
        pending_copies.append((source_path, output_root / Path(output_relative_path)))
        if output_relative_path != item.local_path or output_relative_path != item.relative_path:
            published_items.append(
                replace(
                    item,
                    media_id="",
                    relative_path=output_relative_path,
                    local_path=output_relative_path,
                )
            )
        else:
            published_items.append(item)

    published_manifest = type(manifest).build(
        listing_ids=manifest.listing_ids,
        items=published_items,
        generated_at=manifest.generated_at,
        snapshot_id=manifest.snapshot_id,
        manifest_version=manifest.manifest_version,
    )
    for source_path, target_path in pending_copies:
        _copy_file_atomically(source_path, target_path)
    _write_manifest_atomically(published_manifest, manifest_path)
    return published_manifest


async def refresh_media_manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    listing_ids, listing_labels = media_manifest_context_for_rows(rows)
    expected_by_listing = expected_media_kinds_by_listing(rows)
    manifest_path = settings.room_database_path / "media_manifest.json"
    candidate_path = settings.room_database_path / "_manual_review" / "media_manifest_candidate.json"
    candidate_root = settings.room_database_path / "_manual_review" / "media_manifest_candidate_files"
    if not settings.feishu_region_sync_target_drive_folder_token:
        return {
            "ok": False,
            "reason": "FEISHU_REGION_SYNC_TARGET_DRIVE_FOLDER_TOKEN is empty",
            "path": str(manifest_path),
        }
    if not listing_ids:
        return {
            "ok": False,
            "reason": "no listing_id context built from inventory rows",
            "path": str(manifest_path),
        }

    settings.room_database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".media_manifest_staging_",
        dir=str(settings.room_database_path.parent),
    ) as staging_directory:
        staging_root = Path(staging_directory) / "room_database"
        adapter = FeishuDriveMediaManifestAdapter(
            client=FeishuClient(),
            listing_ids=listing_ids,
            listing_labels=listing_labels,
            target_root=staging_root,
        )
        manifest, report = await adapter.sync_from_drive(
            root_folder_token=settings.feishu_region_sync_target_drive_folder_token,
            expected_kinds=[],
        )
        report.missing = missing_media_for_expected_rows(manifest, expected_by_listing)
        ready = media_manifest_ready(report)
        if ready:
            manifest = publish_media_manifest_snapshot(
                manifest=manifest,
                staging_root=staging_root,
                output_root=settings.room_database_path,
                manifest_path=manifest_path,
            )
            report.manifest_path = str(manifest_path)
            candidate_output = ""
        else:
            manifest = publish_media_manifest_snapshot(
                manifest=manifest,
                staging_root=staging_root,
                output_root=candidate_path.parent,
                manifest_path=candidate_path,
                relative_prefix=candidate_root.name,
            )
            report.manifest_path = str(candidate_path)
            candidate_output = str(candidate_path)
        report_summary = compact_media_binding_report(report)
    return {
        "ok": ready,
        "generated": report.ok,
        "ready": ready,
        "status": "ready" if ready else ("failed" if report.failed else "degraded"),
        "blocking_count": media_manifest_blocking_count(report),
        "path": str(manifest_path),
        "candidate_path": candidate_output,
        "candidate_files_path": str(candidate_root) if candidate_output else "",
        "listing_id_count": len(listing_ids),
        "expected_listing_count": len(expected_by_listing),
        "item_count": len(manifest.items),
        "image_count": sum(1 for item in manifest.items if item.kind == MEDIA_KIND_IMAGE),
        "video_count": sum(1 for item in manifest.items if item.kind == MEDIA_KIND_VIDEO),
        "source_hash": manifest.source_hash,
        "report": report_summary,
    }


def build_rewrite_inventory_payload(
    *,
    rows: list[dict[str, Any]],
    cache_meta: dict[str, Any],
) -> dict[str, Any]:
    index = write_rewrite_inventory_index(
        rows,
        area_aliases=DEFAULT_AREA_ALIASES,
        cache_meta=cache_meta,
    )
    shadow = run_inventory_snapshot_shadow(
        legacy_rows=rows,
        source_kind="feishu_region_inventory_sync",
        source_version=str(index.get("signature") or cache_meta.get("hash") or ""),
        cache_meta=cache_meta,
        legacy_rewrite_index_path=settings.rewrite_inventory_index_path,
        legacy_rewrite_index=index,
        sync_run_id=f"feishu_region_inventory_sync:{time.time_ns()}",
    )
    return {
        "ok": True,
        "path": str(settings.rewrite_inventory_index_path),
        "row_count": index.get("row_count", 0),
        "signature": index.get("signature", ""),
        "inventory_snapshot_shadow": shadow,
    }


async def refresh_rewrite_inventory_index() -> dict[str, Any]:
    service = InventoryService()
    frame = await service.refresh()
    rows = rows_from_frame(frame)
    return build_rewrite_inventory_payload(rows=rows, cache_meta=service.cache_meta)


async def refresh_runtime_artifacts(*, sync_media_manifest: bool) -> dict[str, Any]:
    service = InventoryService()
    frame = await service.refresh()
    rows = rows_from_frame(frame)
    result = {
        "rewrite_index": build_rewrite_inventory_payload(rows=rows, cache_meta=service.cache_meta),
    }
    if sync_media_manifest:
        result["media_manifest"] = await refresh_media_manifest(rows)
    else:
        result["media_manifest"] = {
            "ok": True,
            "skipped": True,
            "reason": "skip_media_requested",
            "path": str(settings.room_database_path / "media_manifest.json"),
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Feishu region inventory and media.")
    parser.add_argument("--dry-run", action="store_true", help="Only print planned changes.")
    parser.add_argument("--skip-media", action="store_true", help="Only sync the target sheet, not media files.")
    parser.add_argument("--no-lock", action="store_true", help="Run without the overlap lock.")
    parser.add_argument(
        "--config-template",
        action="store_true",
        help="Print FEISHU_REGION_SYNC_SOURCES JSON template.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.config_template:
        print_json(config_template())
        return 0

    started_at = now_iso()
    start_time = time.monotonic()
    lock = FileLock(settings.feishu_region_sync_state_path.with_suffix(".lock"))
    if not args.no_lock and not lock.acquire():
        result = {
            "ok": False,
            "dry_run": args.dry_run,
            "reason": "locked",
            "started_at": started_at,
            "finished_at": now_iso(),
        }
        print_json(result)
        return 0

    try:
        if not settings.feishu_region_sync_sources.strip():
            result = {
                "ok": False,
                "dry_run": args.dry_run,
                "reason": "FEISHU_REGION_SYNC_SOURCES is empty",
                "started_at": started_at,
                "finished_at": now_iso(),
            }
            write_state(result)
            print_json(result)
            return 2

        result = asyncio.run(run_sync(dry_run=args.dry_run, sync_media=not args.skip_media))
        if result.get("ok") and not args.dry_run:
            try:
                runtime_artifacts = asyncio.run(
                    refresh_runtime_artifacts(sync_media_manifest=not args.skip_media)
                )
                result.update(runtime_artifacts)
                media_manifest = result.get("media_manifest")
                if isinstance(media_manifest, dict) and not media_manifest.get("ok", False):
                    result["ok"] = False
            except Exception as exc:
                result["runtime_artifacts"] = {"ok": False, "error": str(exc)}
                result["ok"] = False
        result["started_at"] = started_at
        result["finished_at"] = now_iso()
        result["duration_seconds"] = round(time.monotonic() - start_time, 3)
        write_state(result)
        print_json(result)
        return 0 if result.get("ok") else 1
    except Exception as exc:
        result = {
            "ok": False,
            "dry_run": args.dry_run,
            "error": str(exc),
            "started_at": started_at,
            "finished_at": now_iso(),
            "duration_seconds": round(time.monotonic() - start_time, 3),
        }
        write_state(result)
        print_json(result)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
