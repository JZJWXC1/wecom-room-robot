from __future__ import annotations

import inspect
import json
import operator
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except Exception as exc:  # pragma: no cover - dependency guard
    StateGraph = None  # type: ignore[assignment]
    START = "__start__"  # type: ignore[assignment]
    END = "__end__"  # type: ignore[assignment]
    _LANGGRAPH_IMPORT_ERROR = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


MaybeAwaitableDict = dict[str, Any] | Awaitable[dict[str, Any]]
PreflightStageCallback = Callable[..., MaybeAwaitableDict]


class ReleasePreflightGraphState(TypedDict, total=False):
    project_dir: str
    rehearsal_root: str
    version: str
    fail_fast: bool
    local_tests: dict[str, Any]
    random_guard: dict[str, Any]
    config_status: dict[str, Any]
    release_rehearsal: dict[str, Any]
    report: dict[str, Any]
    status: str
    blocked_stage: str
    failures: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class ReleasePreflightGraphDeps:
    run_local_tests: PreflightStageCallback
    run_random_guard: PreflightStageCallback
    check_config: PreflightStageCallback
    rehearse_release: PreflightStageCallback
    write_report: PreflightStageCallback


def build_release_preflight_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for release preflight graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(ReleasePreflightGraphState)
    graph.add_node("local_tests", _make_stage_node("local_tests", "run_local_tests", "local_tests"))
    graph.add_node("random_guard", _make_stage_node("random_guard", "run_random_guard", "random_guard"))
    graph.add_node("config_check", _make_stage_node("config_check", "check_config", "config_status"))
    graph.add_node("release_rehearsal", _make_stage_node("release_rehearsal", "rehearse_release", "release_rehearsal"))
    graph.add_node("write_report", _make_report_node())

    graph.add_edge(START, "local_tests")
    graph.add_conditional_edges(
        "local_tests",
        _route_after("local_tests"),
        {"next": "random_guard", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "random_guard",
        _route_after("random_guard"),
        {"next": "config_check", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "config_check",
        _route_after("config_check"),
        {"next": "release_rehearsal", "report": "write_report"},
    )
    graph.add_edge("release_rehearsal", "write_report")
    graph.add_edge("write_report", END)
    return graph.compile(checkpointer=checkpointer)


def build_local_release_preflight_deps(
    *,
    local_tests_command: list[str] | None = None,
    random_guard_command: list[str] | None = None,
    report_name: str = "release_preflight_graph_report.json",
    timeout_seconds: int = 600,
) -> ReleasePreflightGraphDeps:
    from app.services.config_check import get_config_status
    from scripts import rehearse_release_pipeline

    local_tests_command = local_tests_command or [sys.executable, "-m", "pytest", "-q"]
    random_guard_command = random_guard_command or [
        sys.executable,
        "qa_artifacts/run_kf_qa_gate_graph_utf8.py",
        "--seed",
        "0",
    ]

    async def run_local_tests(**kwargs: Any) -> dict[str, Any]:
        return _run_local_command(
            local_tests_command or [],
            cwd=Path(kwargs["project_dir"]),
            timeout_seconds=timeout_seconds,
            stage="local_tests",
        )

    async def run_random_guard(**kwargs: Any) -> dict[str, Any]:
        return _run_local_command(
            random_guard_command or [],
            cwd=Path(kwargs["project_dir"]),
            timeout_seconds=timeout_seconds,
            stage="random_guard",
        )

    async def check_config(**_kwargs: Any) -> dict[str, Any]:
        return get_config_status()

    async def rehearse_release(**kwargs: Any) -> dict[str, Any]:
        return rehearse_release_pipeline.rehearse_release_pipeline(
            Path(kwargs["project_dir"]),
            Path(kwargs["rehearsal_root"]),
            version=str(kwargs.get("version") or ""),
        )

    async def write_report(**kwargs: Any) -> dict[str, Any]:
        rehearsal_root = Path(kwargs["rehearsal_root"])
        rehearsal_root.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "release_preflight_graph_report.v1",
            "version": kwargs.get("version") or "",
            "status": kwargs.get("status") or "",
            "blocked_stage": kwargs.get("blocked_stage") or "",
            "failures": list(kwargs.get("failures") or []),
            "results": dict(kwargs.get("results") or {}),
            "trace": list(kwargs.get("trace") or []),
        }
        path = rehearsal_root / report_name
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return {"ok": kwargs.get("status") == "passed", "path": str(path), **report}

    return ReleasePreflightGraphDeps(
        run_local_tests=run_local_tests,
        run_random_guard=run_random_guard,
        check_config=check_config,
        rehearse_release=rehearse_release,
        write_report=write_report,
    )


async def run_release_preflight_graph(
    deps: ReleasePreflightGraphDeps,
    *,
    project_dir: Path | str,
    rehearsal_root: Path | str,
    version: str = "",
    fail_fast: bool = True,
    conversation_id: str = "release-preflight-graph",
    checkpointer: Any | None = None,
) -> ReleasePreflightGraphState:
    app = build_release_preflight_graph_app(checkpointer=checkpointer)
    state: ReleasePreflightGraphState = {
        "project_dir": str(project_dir),
        "rehearsal_root": str(rehearsal_root),
        "version": str(version or ""),
        "fail_fast": bool(fail_fast),
        "failures": [],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_stage_node(
    stage_name: str,
    callback_name: str,
    result_key: str,
) -> Callable[[ReleasePreflightGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: ReleasePreflightGraphState) -> dict[str, Any]:
        callback = getattr(_deps(state), callback_name)
        result = await _maybe_await(
            callback(
                project_dir=Path(str(state.get("project_dir") or "")),
                rehearsal_root=Path(str(state.get("rehearsal_root") or "")),
                version=str(state.get("version") or ""),
                previous_results=_previous_results(state),
            )
        )
        failures = _failures_for_stage(stage_name, result)
        blocked = bool(failures and state.get("fail_fast", True))
        return {
            result_key: result,
            "failures": failures,
            "blocked_stage": stage_name if blocked else "",
            "status": "blocked" if blocked else f"{stage_name}:passed",
            "trace": [f"release_preflight:{stage_name}"],
        }

    return node


def _make_report_node() -> Callable[[ReleasePreflightGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: ReleasePreflightGraphState) -> dict[str, Any]:
        status = "blocked" if state.get("failures") else "passed"
        report = await _maybe_await(
            _deps(state).write_report(
                project_dir=Path(str(state.get("project_dir") or "")),
                rehearsal_root=Path(str(state.get("rehearsal_root") or "")),
                version=str(state.get("version") or ""),
                status=status,
                blocked_stage=state.get("blocked_stage") or "",
                failures=list(state.get("failures") or []),
                results=_previous_results(state),
                trace=list(state.get("trace") or []),
            )
        )
        return {
            "report": report,
            "status": status,
            "trace": ["release_preflight:write_report"],
        }

    return node


def _route_after(stage_name: str) -> Callable[[ReleasePreflightGraphState], str]:
    def route(state: ReleasePreflightGraphState) -> str:
        if state.get("fail_fast", True) and state.get("blocked_stage") == stage_name:
            return "report"
        return "next"

    return route


def _previous_results(state: ReleasePreflightGraphState) -> dict[str, Any]:
    return {
        "local_tests": dict(state.get("local_tests") or {}),
        "random_guard": dict(state.get("random_guard") or {}),
        "config_status": dict(state.get("config_status") or {}),
        "release_rehearsal": dict(state.get("release_rehearsal") or {}),
    }


def _failures_for_stage(stage: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    ok = result.get("ok")
    passed = result.get("passed")
    exit_code = result.get("exit_code")
    failed = ok is False or passed is False or (isinstance(exit_code, int) and exit_code != 0)
    if not failed:
        return []
    return [
        {
            "stage": stage,
            "reason": str(result.get("reason") or result.get("error") or "not_ok"),
            "exit_code": exit_code if isinstance(exit_code, int) else None,
        }
    ]


def _run_local_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    stage: str,
) -> dict[str, Any]:
    if not command:
        return {"ok": False, "stage": stage, "reason": "missing_command", "exit_code": -1}
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "stage": stage,
            "reason": "timeout",
            "exit_code": -1,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
        }
    except Exception as exc:
        return {
            "ok": False,
            "stage": stage,
            "reason": exc.__class__.__name__,
            "exit_code": -1,
        }
    return {
        "ok": completed.returncode == 0,
        "stage": stage,
        "command": [str(part) for part in command],
        "exit_code": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _tail(value: Any, *, limit: int = 4000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    return text[-limit:]


async def _maybe_await(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def _deps(state: ReleasePreflightGraphState) -> ReleasePreflightGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, ReleasePreflightGraphDeps):
        raise RuntimeError("ReleasePreflightGraphDeps missing from state")
    return deps
