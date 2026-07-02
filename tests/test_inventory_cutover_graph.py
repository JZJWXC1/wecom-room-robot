from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts import rehearse_inventory_cutover_graph
from app.services.inventory_cutover_graph import (
    InventoryCutoverGraphDeps,
    run_inventory_cutover_graph,
)


def run(coro):
    return asyncio.run(coro)


def test_inventory_cutover_graph_passes_replay_readiness_and_rollback(tmp_path: Path) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def run_primary_replay(**kwargs: Any) -> dict[str, Any]:
            calls.append("replay")
            assert kwargs["root"] == tmp_path
            return {
                "ok": True,
                "cases": [{"name": "case-1", "parity_passed": True}],
            }

        async def evaluate_readiness(**kwargs: Any) -> dict[str, Any]:
            calls.append("readiness")
            assert kwargs["replay_report"]["ok"] is True
            return {"ready": True, "not_ready_reasons": []}

        async def rehearse_rollback(**kwargs: Any) -> dict[str, Any]:
            calls.append("rollback")
            assert kwargs["root"] == tmp_path
            return {"ok": True}

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "passed"
            return {"ok": True, "status": kwargs["status"], "trace": kwargs["trace"]}

        state = await run_inventory_cutover_graph(
            InventoryCutoverGraphDeps(
                run_primary_replay=run_primary_replay,
                evaluate_readiness=evaluate_readiness,
                rehearse_rollback=rehearse_rollback,
                write_report=write_report,
            ),
            root=tmp_path,
        )

        assert calls == ["replay", "readiness", "rollback", "report"]
        assert state["status"] == "passed"
        assert state["trace"] == [
            "inventory_cutover:primary_replay",
            "inventory_cutover:evaluate_readiness",
            "inventory_cutover:rollback_rehearsal",
            "inventory_cutover:write_report",
        ]

    run(run_case())


def test_inventory_cutover_graph_fail_fast_blocks_before_readiness(tmp_path: Path) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def run_primary_replay(**_kwargs: Any) -> dict[str, Any]:
            calls.append("replay")
            return {
                "ok": True,
                "cases": [{"name": "case-1", "parity_passed": False}],
            }

        async def should_not_run(**_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("fail-fast should skip later cutover stages")

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "blocked"
            assert kwargs["blocked_stage"] == "primary_replay"
            return {"ok": False, "status": "blocked"}

        state = await run_inventory_cutover_graph(
            InventoryCutoverGraphDeps(
                run_primary_replay=run_primary_replay,
                evaluate_readiness=should_not_run,
                rehearse_rollback=should_not_run,
                write_report=write_report,
            ),
            root=tmp_path,
        )

        assert calls == ["replay", "report"]
        assert state["status"] == "blocked"
        assert state["failures"][0]["reason"] == "parity_case_failed"

    run(run_case())


def test_inventory_cutover_graph_cli_blocks_non_qa_root_without_override(tmp_path: Path) -> None:
    result = rehearse_inventory_cutover_graph.run_cutover_graph_rehearsal(
        root=tmp_path,
        min_parity_cases=1,
    )

    assert result["ok"] is False
    assert result["blocked_stage"] == "root_guard"
    assert result["failures"][0]["reason"].startswith("cutover graph rehearsal root")


def test_inventory_cutover_graph_cli_runs_local_rehearsal() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/rehearse_inventory_cutover_graph.py",
            "--min-parity-cases",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = completed.stdout + completed.stderr

    assert completed.returncode == 0, combined
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "passed"
    assert payload["trace"] == [
        "inventory_cutover:primary_replay",
        "inventory_cutover:evaluate_readiness",
        "inventory_cutover:rollback_rehearsal",
        "inventory_cutover:write_report",
    ]
    assert payload["replay_report"]["case_count"] >= 1
    assert payload["report"]["ok"] is True
