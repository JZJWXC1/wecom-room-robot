from __future__ import annotations

import asyncio
import inspect
import json
import operator
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
CutoverStageCallback = Callable[..., MaybeAwaitableDict]


class InventoryCutoverGraphState(TypedDict, total=False):
    root: str
    min_parity_cases: int
    fail_fast: bool
    replay_report: dict[str, Any]
    readiness_report: dict[str, Any]
    rollback_report: dict[str, Any]
    report: dict[str, Any]
    status: str
    blocked_stage: str
    failures: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class InventoryCutoverGraphDeps:
    run_primary_replay: CutoverStageCallback
    evaluate_readiness: CutoverStageCallback
    rehearse_rollback: CutoverStageCallback
    write_report: CutoverStageCallback


def build_inventory_cutover_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for inventory cutover graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(InventoryCutoverGraphState)
    graph.add_node("primary_replay", _make_primary_replay_node())
    graph.add_node("evaluate_readiness", _make_readiness_node())
    graph.add_node("rollback_rehearsal", _make_rollback_node())
    graph.add_node("write_report", _make_report_node())

    graph.add_edge(START, "primary_replay")
    graph.add_conditional_edges(
        "primary_replay",
        _route_after("primary_replay", "evaluate_readiness"),
        {"next": "evaluate_readiness", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "evaluate_readiness",
        _route_after("evaluate_readiness", "rollback_rehearsal"),
        {"next": "rollback_rehearsal", "report": "write_report"},
    )
    graph.add_edge("rollback_rehearsal", "write_report")
    graph.add_edge("write_report", END)
    return graph.compile(checkpointer=checkpointer)


def build_local_inventory_cutover_deps(
    *,
    report_name: str = "inventory_cutover_graph_report.json",
) -> InventoryCutoverGraphDeps:
    from app.services import inventory_snapshot_cutover

    async def run_primary_replay(**kwargs: Any) -> dict[str, Any]:
        root = Path(kwargs["root"])
        min_cases = int(kwargs.get("min_parity_cases") or 20)
        cases = inventory_snapshot_cutover.stability_replay_cases(min_cases=min_cases)
        return await asyncio.to_thread(
            inventory_snapshot_cutover.run_primary_replay,
            root,
            cases=cases,
        )

    async def evaluate_readiness(**kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            inventory_snapshot_cutover.evaluate_cutover_readiness,
            Path(kwargs["root"]),
            replay_report=dict(kwargs.get("replay_report") or {}),
            min_parity_cases=int(kwargs.get("min_parity_cases") or 20),
        )

    async def rehearse_rollback(**kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            inventory_snapshot_cutover.rehearse_rollback,
            Path(kwargs["root"]),
        )

    async def write_report(**kwargs: Any) -> dict[str, Any]:
        root = Path(kwargs["root"])
        root.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "inventory_cutover_graph_report.v1",
            "status": kwargs.get("status") or "",
            "blocked_stage": kwargs.get("blocked_stage") or "",
            "failures": list(kwargs.get("failures") or []),
            "replay_report": dict(kwargs.get("replay_report") or {}),
            "readiness_report": dict(kwargs.get("readiness_report") or {}),
            "rollback_report": dict(kwargs.get("rollback_report") or {}),
            "trace": list(kwargs.get("trace") or []),
        }
        path = root / report_name
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return {"ok": kwargs.get("status") == "passed", "path": str(path), **report}

    return InventoryCutoverGraphDeps(
        run_primary_replay=run_primary_replay,
        evaluate_readiness=evaluate_readiness,
        rehearse_rollback=rehearse_rollback,
        write_report=write_report,
    )


async def run_inventory_cutover_graph(
    deps: InventoryCutoverGraphDeps,
    *,
    root: Path | str,
    min_parity_cases: int = 20,
    fail_fast: bool = True,
    conversation_id: str = "inventory-cutover-graph",
    checkpointer: Any | None = None,
) -> InventoryCutoverGraphState:
    app = build_inventory_cutover_graph_app(checkpointer=checkpointer)
    state: InventoryCutoverGraphState = {
        "root": str(root),
        "min_parity_cases": int(min_parity_cases),
        "fail_fast": bool(fail_fast),
        "failures": [],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_primary_replay_node() -> Callable[[InventoryCutoverGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventoryCutoverGraphState) -> dict[str, Any]:
        result = await _maybe_await(
            _deps(state).run_primary_replay(
                root=Path(str(state.get("root") or "")),
                min_parity_cases=int(state.get("min_parity_cases") or 20),
            )
        )
        failures = _failures_for_replay(result)
        blocked = bool(failures and state.get("fail_fast", True))
        return {
            "replay_report": result,
            "failures": failures,
            "blocked_stage": "primary_replay" if blocked else "",
            "status": "blocked" if blocked else "primary_replay:passed",
            "trace": ["inventory_cutover:primary_replay"],
        }

    return node


def _make_readiness_node() -> Callable[[InventoryCutoverGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventoryCutoverGraphState) -> dict[str, Any]:
        result = await _maybe_await(
            _deps(state).evaluate_readiness(
                root=Path(str(state.get("root") or "")),
                replay_report=dict(state.get("replay_report") or {}),
                min_parity_cases=int(state.get("min_parity_cases") or 20),
            )
        )
        failures = _failures_for_readiness(result)
        blocked = bool(failures and state.get("fail_fast", True))
        return {
            "readiness_report": result,
            "failures": failures,
            "blocked_stage": "evaluate_readiness" if blocked else "",
            "status": "blocked" if blocked else "evaluate_readiness:passed",
            "trace": ["inventory_cutover:evaluate_readiness"],
        }

    return node


def _make_rollback_node() -> Callable[[InventoryCutoverGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventoryCutoverGraphState) -> dict[str, Any]:
        result = await _maybe_await(
            _deps(state).rehearse_rollback(root=Path(str(state.get("root") or "")))
        )
        failures = _failures_for_ok_stage("rollback_rehearsal", result)
        return {
            "rollback_report": result,
            "failures": failures,
            "blocked_stage": "rollback_rehearsal" if failures else "",
            "status": "blocked" if failures else "passed",
            "trace": ["inventory_cutover:rollback_rehearsal"],
        }

    return node


def _make_report_node() -> Callable[[InventoryCutoverGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventoryCutoverGraphState) -> dict[str, Any]:
        status = "blocked" if state.get("failures") else "passed"
        report = await _maybe_await(
            _deps(state).write_report(
                root=Path(str(state.get("root") or "")),
                status=status,
                blocked_stage=state.get("blocked_stage") or "",
                failures=list(state.get("failures") or []),
                replay_report=dict(state.get("replay_report") or {}),
                readiness_report=dict(state.get("readiness_report") or {}),
                rollback_report=dict(state.get("rollback_report") or {}),
                trace=list(state.get("trace") or []),
            )
        )
        return {
            "report": report,
            "status": status,
            "trace": ["inventory_cutover:write_report"],
        }

    return node


def _route_after(stage_name: str, next_stage: str) -> Callable[[InventoryCutoverGraphState], str]:
    def route(state: InventoryCutoverGraphState) -> str:
        if state.get("fail_fast", True) and state.get("blocked_stage") == stage_name:
            return "report"
        return "next"

    return route


def _failures_for_replay(result: dict[str, Any]) -> list[dict[str, Any]]:
    failures = _failures_for_ok_stage("primary_replay", result)
    failed_cases = [case for case in result.get("cases") or [] if not case.get("parity_passed")]
    if failed_cases:
        failures.append(
            {
                "stage": "primary_replay",
                "reason": "parity_case_failed",
                "failed_case_count": len(failed_cases),
            }
        )
    return failures


def _failures_for_readiness(result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("ready") is True:
        return []
    return [
        {
            "stage": "evaluate_readiness",
            "reason": ",".join(str(item) for item in result.get("not_ready_reasons") or [])
            or str(result.get("reason") or "not_ready"),
        }
    ]


def _failures_for_ok_stage(stage: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("ok") is not False:
        return []
    return [
        {
            "stage": stage,
            "reason": str(result.get("reason") or result.get("error") or "not_ok"),
        }
    ]


async def _maybe_await(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def _deps(state: InventoryCutoverGraphState) -> InventoryCutoverGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, InventoryCutoverGraphDeps):
        raise RuntimeError("InventoryCutoverGraphDeps missing from state")
    return deps
