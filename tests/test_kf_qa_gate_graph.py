from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from qa_artifacts import run_kf_qa_gate_graph_utf8 as qa_gate_cli
from app.services.kf_qa_gate_graph import KfQaGateDeps, run_kf_qa_gate


def run(coro):
    return asyncio.run(coro)


def passing_result() -> dict[str, Any]:
    return {
        "quality_status": {
            "passed": True,
            "high_count": 0,
            "medium_count": 0,
            "infrastructure_error": False,
        }
    }


def complete_artifact_result(stage: str, *, windows: int, cases: int) -> dict[str, Any]:
    return {
        "stage": stage,
        "quality_status": {
            "passed": True,
            "high_count": 0,
            "medium_count": 0,
            "infrastructure_error": False,
        },
        "completed": True,
        "full_suite_completed": True,
        "actual_window_count": windows,
        "actual_case_count": cases,
        "expected_case_count": cases,
    }


def test_qa_gate_runs_all_stages_and_writes_pass_artifact() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def stage(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            assert kwargs["seed"] == 7
            return passing_result()

        async def write_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("artifact")
            assert kwargs["status"] == "passed"
            assert kwargs["failures"] == []
            assert kwargs["fixed_result"]["quality_status"]["passed"] is True
            assert kwargs["random_result"]["quality_status"]["passed"] is True
            assert kwargs["historical_result"]["quality_status"]["passed"] is True
            return {"path": "qa_artifacts/pass.json", "passed": True}

        result = await run_kf_qa_gate(
            KfQaGateDeps(
                run_fixed_windows=lambda **kwargs: stage("fixed", **kwargs),
                run_random_windows=lambda **kwargs: stage("random", **kwargs),
                run_historical_failures=lambda **kwargs: stage("historical", **kwargs),
                write_artifact=write_artifact,
            ),
            seed=7,
        )

        assert calls == ["fixed", "random", "historical", "artifact"]
        assert result["status"] == "passed"
        assert result["trace"] == [
            "qa_gate:fixed_windows",
            "qa_gate:random_windows",
            "qa_gate:historical_failures",
            "qa_gate:write_artifact",
        ]

    run(run_case())


def test_qa_gate_cli_artifact_is_release_usable_when_required_windows_complete() -> None:
    artifact_path = Path(
        qa_gate_cli._write_gate_artifact(
            seed=7,
            status="passed",
            blocked_stage="",
            failures=[],
            trace=["qa_gate:fixed_windows", "qa_gate:random_windows", "qa_gate:historical_failures"],
            fixed_result=complete_artifact_result("fixed_windows", windows=10, cases=100),
            random_result=complete_artifact_result("random_windows", windows=20, cases=200),
            historical_result={
                "stage": "historical_failures",
                "skipped": True,
                "quality_status": {
                    "passed": True,
                    "high_count": 0,
                    "medium_count": 0,
                    "infrastructure_error": False,
                },
            },
        )["path"]
    )
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["full_suite_completed"] is True
        assert payload["actual_case_count"] == 300
        assert payload["expected_case_count"] == 300
        assert payload["summary"]["usable_for_release"] is True
        assert payload["summary"]["fixed_full_suite_completed"] is True
        assert payload["summary"]["random_full_suite_completed"] is True
    finally:
        artifact_path.unlink(missing_ok=True)


def test_qa_gate_fail_fast_stops_after_fixed_failure() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def fixed(**kwargs: Any) -> dict[str, Any]:
            calls.append("fixed")
            return {
                "quality_status": {
                    "passed": False,
                    "high_count": 1,
                    "medium_count": 0,
                    "infrastructure_error": False,
                }
            }

        async def should_not_run(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("fail-fast must stop before later QA stages")

        async def write_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("artifact")
            assert kwargs["status"] == "blocked"
            assert kwargs["blocked_stage"] == "fixed_windows"
            assert kwargs["failures"][0]["high_count"] == 1
            return {"path": "qa_artifacts/fail.json", "passed": False}

        result = await run_kf_qa_gate(
            KfQaGateDeps(
                run_fixed_windows=fixed,
                run_random_windows=should_not_run,
                run_historical_failures=should_not_run,
                write_artifact=write_artifact,
            )
        )

        assert calls == ["fixed", "artifact"]
        assert result["status"] == "blocked"
        assert result["blocked_stage"] == "fixed_windows"
        assert result["trace"] == ["qa_gate:fixed_windows", "qa_gate:write_artifact"]

    run(run_case())


def test_qa_gate_can_continue_when_fail_fast_disabled_but_final_status_blocks() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def fixed(**kwargs: Any) -> dict[str, Any]:
            calls.append("fixed")
            return {"quality_status": {"passed": False, "high_count": 0, "medium_count": 1}}

        async def random_stage(**kwargs: Any) -> dict[str, Any]:
            calls.append("random")
            return passing_result()

        async def historical(**kwargs: Any) -> dict[str, Any]:
            calls.append("historical")
            return passing_result()

        async def write_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("artifact")
            assert kwargs["status"] == "blocked"
            assert kwargs["failures"][0]["stage"] == "fixed_windows"
            return {"path": "qa_artifacts/review.json", "passed": False}

        result = await run_kf_qa_gate(
            KfQaGateDeps(
                run_fixed_windows=fixed,
                run_random_windows=random_stage,
                run_historical_failures=historical,
                write_artifact=write_artifact,
            ),
            fail_fast=False,
        )

        assert calls == ["fixed", "random", "historical", "artifact"]
        assert result["status"] == "blocked"
        assert result["failures"][0]["medium_count"] == 1

    run(run_case())


def test_qa_gate_graph_cli_writes_artifact_in_skip_mode() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "qa_artifacts/run_kf_qa_gate_graph_utf8.py",
            "--skip-fixed",
            "--skip-random",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = completed.stdout + completed.stderr

    assert completed.returncode == 0, combined
    assert "SUMMARY_JSON" in completed.stdout
    assert "ARTIFACT " in completed.stdout
    artifact_path = max(
        Path("qa_artifacts").glob("kf_qa_gate_graph_utf8_*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["fixed_result"]["skipped"] is True
    assert payload["random_result"]["skipped"] is True
    assert payload["full_suite_completed"] is False
    assert payload["summary"]["usable_for_release"] is False
    assert payload["trace"] == [
        "qa_gate:fixed_windows",
        "qa_gate:random_windows",
        "qa_gate:historical_failures",
    ]
