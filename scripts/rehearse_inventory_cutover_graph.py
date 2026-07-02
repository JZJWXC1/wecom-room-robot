from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services import inventory_cutover_graph


def _default_root() -> Path:
    return PROJECT_ROOT / "qa_artifacts" / f"inventory_cutover_graph_{int(time.time())}"


def _is_qa_root(root: Path) -> bool:
    try:
        root.resolve().relative_to((PROJECT_ROOT / "qa_artifacts").resolve())
        return True
    except ValueError:
        return False


def _guarded_root_result(root: Path) -> dict[str, Any]:
    return {
        "schema_version": "inventory_cutover_graph_cli.v1",
        "ok": False,
        "status": "blocked",
        "blocked_stage": "root_guard",
        "failures": [
            {
                "stage": "root_guard",
                "reason": "cutover graph rehearsal root must stay under qa_artifacts unless --allow-non-qa-root is set",
                "root": str(root),
            }
        ],
        "trace": [],
        "root": str(root),
        "report": {},
    }


def run_cutover_graph_rehearsal(
    *,
    root: Path | None = None,
    min_parity_cases: int = 20,
    fail_fast: bool = True,
    allow_non_qa_root: bool = False,
) -> dict[str, Any]:
    rehearsal_root = root or _default_root()
    if not allow_non_qa_root and not _is_qa_root(rehearsal_root):
        return _guarded_root_result(rehearsal_root)
    state = asyncio.run(
        inventory_cutover_graph.run_inventory_cutover_graph(
            inventory_cutover_graph.build_local_inventory_cutover_deps(),
            root=rehearsal_root,
            min_parity_cases=min_parity_cases,
            fail_fast=fail_fast,
            conversation_id=f"inventory-cutover-cli:{int(time.time())}",
        )
    )
    return {
        "schema_version": "inventory_cutover_graph_cli.v1",
        "ok": state.get("status") == "passed",
        "status": state.get("status") or "",
        "blocked_stage": state.get("blocked_stage") or "",
        "failures": list(state.get("failures") or []),
        "trace": list(state.get("trace") or []),
        "root": str(rehearsal_root),
        "replay_report": dict(state.get("replay_report") or {}),
        "readiness_report": dict(state.get("readiness_report") or {}),
        "rollback_report": dict(state.get("rollback_report") or {}),
        "report": dict(state.get("report") or {}),
    }


def _print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local LangGraph inventory cutover rehearsal.")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--min-parity-cases", type=int, default=20)
    parser.add_argument("--no-fail-fast", action="store_true")
    parser.add_argument(
        "--allow-non-qa-root",
        action="store_true",
        help="Allow a custom rehearsal root outside qa_artifacts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_cutover_graph_rehearsal(
        root=args.root,
        min_parity_cases=args.min_parity_cases,
        fail_fast=not args.no_fail_fast,
        allow_non_qa_root=args.allow_non_qa_root,
    )
    _print_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
