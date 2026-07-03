from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.kf_qa_gate_graph import KfQaGateDeps, run_kf_qa_gate
from qa_artifacts.run_rag_10windows_10turns_utf8 import (
    ArtifactWriteError,
    load_fixture_windows,
    run_all,
)
from qa_artifacts.run_rag_random_guard_utf8 import run_random_guard


ARTIFACT_DIR = PROJECT_ROOT / "qa_artifacts"


def _passed_skipped(stage: str, reason: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "skipped": True,
        "reason": reason,
        "quality_status": {
            "passed": True,
            "high_count": 0,
            "medium_count": 0,
            "infrastructure_error": False,
            "exit_code": 0,
        },
    }


def _artifact_result(path: Path, *, stage: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "stage": stage,
        "artifact_path": str(path),
        "summary": dict(payload.get("summary") or {}),
        "quality_status": dict(payload.get("quality_status") or {}),
        "completed": bool(payload.get("completed")),
        "full_suite_completed": bool(payload.get("full_suite_completed")),
        "expected_case_count": payload.get("expected_case_count"),
        "actual_window_count": payload.get("actual_window_count"),
        "actual_case_count": payload.get("actual_case_count"),
    }


def _stage_quality(result: dict[str, Any]) -> dict[str, Any]:
    return dict(result.get("quality_status") or {})


def _stage_passed(result: dict[str, Any]) -> bool:
    quality = _stage_quality(result)
    return (
        bool(quality.get("passed"))
        and int(quality.get("high_count") or 0) == 0
        and int(quality.get("medium_count") or 0) == 0
        and not bool(quality.get("infrastructure_error") or quality.get("infrastructure_errors"))
    )


def _required_stage_full_suite_completed(result: dict[str, Any]) -> bool:
    return _stage_passed(result) and bool(result.get("full_suite_completed"))


def _optional_stage_accepted(result: dict[str, Any]) -> bool:
    return bool(result.get("skipped")) or _stage_passed(result)


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _qa_gate_release_summary(payload: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(payload.get("fixed_result") or {})
    random = dict(payload.get("random_result") or {})
    historical = dict(payload.get("historical_result") or {})
    required_results = [fixed, random]
    full_suite_completed = (
        payload.get("status") == "passed"
        and all(_required_stage_full_suite_completed(result) for result in required_results)
        and _optional_stage_accepted(historical)
    )
    actual_case_count = sum(_int_value(result.get("actual_case_count")) for result in required_results)
    expected_case_count = sum(_int_value(result.get("expected_case_count")) for result in required_results)
    actual_window_count = sum(_int_value(result.get("actual_window_count")) for result in required_results)
    return {
        "schema": "kf_qa_gate_graph_summary.v1",
        "artifact_role": "pass_transcript" if payload.get("status") == "passed" else "failure_log",
        "usable_for_release": bool(full_suite_completed and expected_case_count > 0 and actual_case_count == expected_case_count),
        "passed": payload.get("status") == "passed",
        "actual_case_count": actual_case_count,
        "expected_case_count": expected_case_count,
        "actual_window_count": actual_window_count,
        "full_suite_completed": bool(full_suite_completed),
        "fixed_full_suite_completed": _required_stage_full_suite_completed(fixed),
        "random_full_suite_completed": _required_stage_full_suite_completed(random),
        "historical_accepted": _optional_stage_accepted(historical),
    }


def _write_gate_artifact(**kwargs: Any) -> dict[str, Any]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().isoformat()
    path = ARTIFACT_DIR / f"kf_qa_gate_graph_utf8_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    payload = {
        "schema": "kf_qa_gate_graph_utf8.v1",
        "created_at": created_at,
        "seed": kwargs.get("seed"),
        "status": kwargs.get("status") or "",
        "blocked_stage": kwargs.get("blocked_stage") or "",
        "failures": list(kwargs.get("failures") or []),
        "trace": list(kwargs.get("trace") or []),
        "fixed_result": dict(kwargs.get("fixed_result") or {}),
        "random_result": dict(kwargs.get("random_result") or {}),
            "historical_result": dict(kwargs.get("historical_result") or {}),
    }
    release_summary = _qa_gate_release_summary(payload)
    payload["full_suite_completed"] = release_summary["full_suite_completed"]
    payload["actual_case_count"] = release_summary["actual_case_count"]
    payload["expected_case_count"] = release_summary["expected_case_count"]
    payload["actual_window_count"] = release_summary["actual_window_count"]
    payload["summary"] = release_summary
    payload["quality_status"] = {
        "passed": payload["status"] == "passed",
        "high_count": sum(int(item.get("high_count") or 0) for item in payload["failures"]),
        "medium_count": sum(int(item.get("medium_count") or 0) for item in payload["failures"]),
        "infrastructure_error": any(bool(item.get("infrastructure_error")) for item in payload["failures"]),
        "exit_code": 0 if payload["status"] == "passed" else 4,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "path": str(path),
        "passed": payload["quality_status"]["passed"],
        "quality_status": payload["quality_status"],
    }


async def run_gate(
    *,
    seed: int = 0,
    turn_timeout: float = 90,
    fail_fast: bool = True,
    skip_fixed: bool = False,
    skip_random: bool = False,
    historical_fixture: Path | None = None,
    require_historical: bool = False,
) -> dict[str, Any]:
    async def fixed_windows(**_kwargs: Any) -> dict[str, Any]:
        if skip_fixed:
            return _passed_skipped("fixed_windows", "--skip-fixed")
        artifact = await run_all(turn_timeout=turn_timeout, fail_fast_on_problem=fail_fast)
        return _artifact_result(artifact, stage="fixed_windows")

    async def random_windows(**kwargs: Any) -> dict[str, Any]:
        if skip_random:
            return _passed_skipped("random_windows", "--skip-random")
        stage_seed = int(kwargs.get("seed") or seed or 0)
        artifact = await run_random_guard(
            seed=stage_seed or None,
            turn_timeout=turn_timeout,
            fail_fast_on_problem=fail_fast,
        )
        return _artifact_result(artifact, stage="random_windows")

    async def historical_failures(**_kwargs: Any) -> dict[str, Any]:
        fixture = historical_fixture
        if not fixture or not fixture.exists():
            if require_historical:
                return {
                    "stage": "historical_failures",
                    "quality_status": {
                        "passed": False,
                        "high_count": 0,
                        "medium_count": 0,
                        "infrastructure_error": True,
                        "exit_code": 4,
                    },
                    "reason": f"historical fixture missing: {fixture or ''}",
                }
            return _passed_skipped("historical_failures", "historical fixture not configured")
        windows = load_fixture_windows(fixture)
        artifact = await run_all(
            turn_timeout=turn_timeout,
            windows=windows,
            artifact_prefix="rag_historical_failure_graph_utf8",
            conversation_prefix="conv_historical_failure_graph",
            required_tokens=(),
            expected_window_count=None,
            min_window_count=1,
            min_turn_count=1,
            fail_fast_on_problem=fail_fast,
        )
        return _artifact_result(artifact, stage="historical_failures")

    state = await run_kf_qa_gate(
        KfQaGateDeps(
            run_fixed_windows=fixed_windows,
            run_random_windows=random_windows,
            run_historical_failures=historical_failures,
            write_artifact=lambda **kwargs: _write_gate_artifact(**kwargs),
        ),
        seed=seed,
        fail_fast=fail_fast,
        conversation_id=f"kf-qa-gate-graph:{seed}",
    )
    return dict(state)


def print_summary(state: dict[str, Any]) -> None:
    artifact = dict(state.get("artifact") or {})
    quality = dict(artifact.get("quality_status") or {})
    summary = {
        "schema": "kf_qa_gate_graph_summary.v1",
        "status": state.get("status") or "",
        "blocked_stage": state.get("blocked_stage") or "",
        "passed": bool(quality.get("passed")),
        "exit_code": int(quality.get("exit_code") or 0),
        "trace": list(state.get("trace") or []),
        "artifact_path": artifact.get("path") or "",
    }
    print("SUMMARY_JSON " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if artifact.get("path"):
        print(f"ARTIFACT {artifact['path']}")
    print("QA_GRAPH_TRACE " + " -> ".join(summary["trace"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--turn-timeout", type=float, default=90)
    parser.add_argument("--no-fail-fast", action="store_true")
    parser.add_argument("--skip-fixed", action="store_true")
    parser.add_argument("--skip-random", action="store_true")
    parser.add_argument("--historical-fixture", type=Path)
    parser.add_argument("--require-historical", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        gate_state = asyncio.run(
            run_gate(
                seed=args.seed,
                turn_timeout=args.turn_timeout,
                fail_fast=not args.no_fail_fast,
                skip_fixed=args.skip_fixed,
                skip_random=args.skip_random,
                historical_fixture=args.historical_fixture,
                require_historical=args.require_historical,
            )
        )
    except ArtifactWriteError as error:
        print(f"ARTIFACT_WRITE_ERROR {error.artifact_path}")
        raise SystemExit(2) from error
    print_summary(gate_state)
    artifact = dict(gate_state.get("artifact") or {})
    quality = dict(artifact.get("quality_status") or {})
    raise SystemExit(int(quality.get("exit_code") or 0))
