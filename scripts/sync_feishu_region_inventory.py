from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.services.inventory import InventoryService
from app.services.inventory_snapshot_shadow import run_inventory_snapshot_shadow
from app.services.region_inventory_sync import RegionInventorySyncService
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


async def refresh_rewrite_inventory_index() -> dict[str, Any]:
    service = InventoryService()
    frame = await service.refresh()
    rows = frame.fillna("").to_dict(orient="records") if hasattr(frame, "fillna") else []
    index = write_rewrite_inventory_index(
        rows,
        area_aliases=DEFAULT_AREA_ALIASES,
        cache_meta=service.cache_meta,
    )
    shadow = run_inventory_snapshot_shadow(
        legacy_rows=rows,
        source_kind="feishu_region_inventory_sync",
        source_version=str(index.get("signature") or service.cache_meta.get("hash") or ""),
        cache_meta=service.cache_meta,
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
                result["rewrite_index"] = asyncio.run(refresh_rewrite_inventory_index())
            except Exception as exc:
                result["rewrite_index"] = {"ok": False, "error": str(exc)}
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
