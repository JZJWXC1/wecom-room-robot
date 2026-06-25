from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.inventory_snapshot_shadow_observer import (  # noqa: E402
    ShadowObservationOptions,
    collect_shadow_observation,
    format_shadow_observation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read InventorySnapshot Shadow health safely.")
    parser.add_argument("--json", action="store_true", help="Print safe JSON output.")
    parser.add_argument("--root", type=Path, default=None, help="Override Shadow root directory.")
    parser.add_argument("--mode", default=None, help="Override mode for observation, usually disabled or shadow.")
    parser.add_argument("--stale-seconds", type=int, default=None, help="Override stale threshold.")
    parser.add_argument("--required-passes", type=int, default=None, help="Override readiness pass threshold.")
    return parser


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect_shadow_observation(
        ShadowObservationOptions(
            root=args.root,
            mode=args.mode,
            stale_seconds=args.stale_seconds,
            required_consecutive_passes=args.required_passes,
        )
    )
    if args.json:
        print_json(payload)
    else:
        text = format_shadow_observation(payload)
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
