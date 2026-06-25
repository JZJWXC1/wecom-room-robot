from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.inventory_snapshot_shadow_preflight import (  # noqa: E402
    ShadowPreflightOptions,
    format_shadow_preflight,
    run_shadow_preflight,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only InventorySnapshot Shadow deployment preflight.")
    parser.add_argument("--json", action="store_true", help="Print safe JSON output.")
    parser.add_argument("--mode", default=None, help="Override INVENTORY_SNAPSHOT_MODE for preflight.")
    parser.add_argument("--shadow-root", type=Path, default=None, help="Override Shadow root.")
    parser.add_argument("--production-snapshot-root", type=Path, default=None, help="Override production Snapshot root.")
    parser.add_argument("--inventory-cache-path", type=Path, default=None, help="Override legacy inventory cache path.")
    parser.add_argument("--rewrite-inventory-index-path", type=Path, default=None, help="Override legacy rewrite index path.")
    parser.add_argument("--inventory-image-glob", default=None, help="Override legacy inventory PNG glob.")
    parser.add_argument("--room-database-path", type=Path, default=None, help="Override room database directory.")
    parser.add_argument("--min-free-bytes", type=int, default=256 * 1024 * 1024, help="Minimum free disk bytes.")
    return parser


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_shadow_preflight(
        ShadowPreflightOptions(
            mode=args.mode,
            shadow_root=args.shadow_root,
            production_snapshot_root=args.production_snapshot_root,
            inventory_cache_path=args.inventory_cache_path,
            rewrite_inventory_index_path=args.rewrite_inventory_index_path,
            inventory_image_glob=args.inventory_image_glob,
            room_database_path=args.room_database_path,
            min_free_bytes=args.min_free_bytes,
            project_root=PROJECT_ROOT,
        )
    )
    if args.json:
        print_json(payload)
    else:
        text = format_shadow_preflight(payload)
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(text.encode("utf-8"))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
