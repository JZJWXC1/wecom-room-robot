from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from app.services import release_preflight_graph
from app.services.release_preflight_graph import (
    ReleasePreflightGraphDeps,
    build_local_release_preflight_deps,
    run_release_preflight_graph,
)


def run(coro):
    return asyncio.run(coro)


def test_release_preflight_graph_runs_all_local_gates(tmp_path: Path) -> None:
    async def run_case() -> None:
        calls: list[str] = []
        project_dir = tmp_path / "project"
        rehearsal_root = tmp_path / "rehearsal"
        project_dir.mkdir()

        async def stage(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            assert kwargs["project_dir"] == project_dir
            assert kwargs["rehearsal_root"] == rehearsal_root
            return {"ok": True, "stage": name}

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "passed"
            assert kwargs["results"]["local_tests"]["ok"] is True
            assert kwargs["results"]["random_guard"]["ok"] is True
            assert kwargs["results"]["config_status"]["ok"] is True
            assert kwargs["results"]["release_rehearsal"]["ok"] is True
            return {"ok": True, "status": kwargs["status"]}

        state = await run_release_preflight_graph(
            ReleasePreflightGraphDeps(
                run_local_tests=lambda **kwargs: stage("local_tests", **kwargs),
                run_random_guard=lambda **kwargs: stage("random_guard", **kwargs),
                check_config=lambda **kwargs: stage("config_check", **kwargs),
                rehearse_release=lambda **kwargs: stage("release_rehearsal", **kwargs),
                write_report=write_report,
            ),
            project_dir=project_dir,
            rehearsal_root=rehearsal_root,
            version="test-version",
        )

        assert calls == [
            "local_tests",
            "random_guard",
            "config_check",
            "release_rehearsal",
            "report",
        ]
        assert state["status"] == "passed"
        assert state["trace"] == [
            "release_preflight:local_tests",
            "release_preflight:random_guard",
            "release_preflight:config_check",
            "release_preflight:release_rehearsal",
            "release_preflight:write_report",
        ]

    run(run_case())


def test_release_preflight_graph_fail_fast_stops_after_random_guard(tmp_path: Path) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def local_tests(**_kwargs: Any) -> dict[str, Any]:
            calls.append("local_tests")
            return {"ok": True}

        async def random_guard(**_kwargs: Any) -> dict[str, Any]:
            calls.append("random_guard")
            return {"passed": False, "reason": "medium_failure"}

        async def should_not_run(**_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("fail-fast should skip later preflight stages")

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "blocked"
            assert kwargs["blocked_stage"] == "random_guard"
            return {"ok": False, "status": "blocked"}

        state = await run_release_preflight_graph(
            ReleasePreflightGraphDeps(
                run_local_tests=local_tests,
                run_random_guard=random_guard,
                check_config=should_not_run,
                rehearse_release=should_not_run,
                write_report=write_report,
            ),
            project_dir=tmp_path,
            rehearsal_root=tmp_path / "rehearsal",
        )

        assert calls == ["local_tests", "random_guard", "report"]
        assert state["status"] == "blocked"
        assert state["failures"] == [
            {"stage": "random_guard", "reason": "medium_failure", "exit_code": None}
        ]

    run(run_case())


def test_local_release_preflight_deps_execute_commands_and_write_report(tmp_path: Path) -> None:
    async def run_case() -> None:
        rehearsal_root = tmp_path / "rehearsal"
        deps = build_local_release_preflight_deps(
            local_tests_command=[sys.executable, "-c", "print('local-ok')"],
            random_guard_command=[sys.executable, "-c", "import sys; print('random-fail'); sys.exit(2)"],
            timeout_seconds=30,
        )

        state = await run_release_preflight_graph(
            deps,
            project_dir=Path.cwd(),
            rehearsal_root=rehearsal_root,
            version="local-wrapper-test",
        )

        report_path = rehearsal_root / "release_preflight_graph_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert state["status"] == "blocked"
        assert state["blocked_stage"] == "random_guard"
        assert report["status"] == "blocked"
        assert report["results"]["local_tests"]["stdout_tail"].strip() == "local-ok"
        assert report["results"]["random_guard"]["exit_code"] == 2

    run(run_case())


def test_local_release_preflight_default_random_guard_uses_qa_graph(monkeypatch, tmp_path: Path) -> None:
    async def run_case() -> None:
        commands: list[tuple[str, list[str]]] = []

        def fake_run_local_command(command, *, cwd, timeout_seconds, stage):
            commands.append((stage, [str(part) for part in command]))
            return {"ok": True, "stage": stage, "exit_code": 0}

        monkeypatch.setattr(release_preflight_graph, "_run_local_command", fake_run_local_command)
        deps = build_local_release_preflight_deps(timeout_seconds=30)

        result = await deps.run_random_guard(
            project_dir=Path.cwd(),
            rehearsal_root=tmp_path,
            version="default-random-guard",
            previous_results={},
        )

        assert result["ok"] is True
        assert commands == [
            (
                "random_guard",
                [sys.executable, "qa_artifacts/run_kf_qa_gate_graph_utf8.py", "--seed", "0"],
            )
        ]
        assert "run_rag_random_guard_utf8.py" not in " ".join(commands[0][1])

    run(run_case())
