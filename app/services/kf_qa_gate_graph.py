from __future__ import annotations

import inspect
import operator
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except Exception as exc:  # pragma: no cover - only hit when dependency is missing
    StateGraph = None  # type: ignore[assignment]
    START = "__start__"  # type: ignore[assignment]
    END = "__end__"  # type: ignore[assignment]
    _LANGGRAPH_IMPORT_ERROR = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


MaybeAwaitableDict = dict[str, Any] | Awaitable[dict[str, Any]]
QaStageCallback = Callable[..., MaybeAwaitableDict]


class KfQaGateState(TypedDict, total=False):
    seed: int
    fail_fast: bool
    fixed_result: dict[str, Any]
    random_result: dict[str, Any]
    historical_result: dict[str, Any]
    artifact: dict[str, Any]
    status: str
    blocked_stage: str
    failures: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class KfQaGateDeps:
    run_fixed_windows: QaStageCallback
    run_random_windows: QaStageCallback
    run_historical_failures: QaStageCallback
    write_artifact: QaStageCallback


def build_kf_qa_gate_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for QA gate graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfQaGateState)
    graph.add_node("fixed_windows", _make_fixed_node())
    graph.add_node("random_windows", _make_random_node())
    graph.add_node("historical_failures", _make_historical_node())
    graph.add_node("write_artifact", _make_artifact_node())

    graph.add_edge(START, "fixed_windows")
    graph.add_conditional_edges(
        "fixed_windows",
        _route_after_fixed,
        {"random_windows": "random_windows", "write_artifact": "write_artifact"},
    )
    graph.add_conditional_edges(
        "random_windows",
        _route_after_random,
        {"historical_failures": "historical_failures", "write_artifact": "write_artifact"},
    )
    graph.add_edge("historical_failures", "write_artifact")
    graph.add_edge("write_artifact", END)
    return graph.compile(checkpointer=checkpointer)


async def run_kf_qa_gate(
    deps: KfQaGateDeps,
    *,
    seed: int = 0,
    fail_fast: bool = True,
    conversation_id: str = "kf-qa-gate",
    checkpointer: Any | None = None,
) -> KfQaGateState:
    app = build_kf_qa_gate_graph_app(checkpointer=checkpointer)
    state: KfQaGateState = {
        "seed": seed,
        "fail_fast": fail_fast,
        "failures": [],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_fixed_node() -> Callable[[KfQaGateState], Awaitable[dict[str, Any]]]:
    async def node(state: KfQaGateState) -> dict[str, Any]:
        result = await _maybe_await(_deps(state).run_fixed_windows(seed=state.get("seed")))
        failures = _failures_for_stage("fixed_windows", result)
        return {
            "fixed_result": result,
            "failures": failures,
            "blocked_stage": "fixed_windows" if failures and state.get("fail_fast", True) else "",
            "status": "blocked" if failures and state.get("fail_fast", True) else "fixed_passed",
            "trace": ["qa_gate:fixed_windows"],
        }

    return node


def _make_random_node() -> Callable[[KfQaGateState], Awaitable[dict[str, Any]]]:
    async def node(state: KfQaGateState) -> dict[str, Any]:
        result = await _maybe_await(_deps(state).run_random_windows(seed=state.get("seed")))
        failures = _failures_for_stage("random_windows", result)
        return {
            "random_result": result,
            "failures": failures,
            "blocked_stage": "random_windows" if failures and state.get("fail_fast", True) else "",
            "status": "blocked" if failures and state.get("fail_fast", True) else "random_passed",
            "trace": ["qa_gate:random_windows"],
        }

    return node


def _make_historical_node() -> Callable[[KfQaGateState], Awaitable[dict[str, Any]]]:
    async def node(state: KfQaGateState) -> dict[str, Any]:
        result = await _maybe_await(_deps(state).run_historical_failures(seed=state.get("seed")))
        failures = _failures_for_stage("historical_failures", result)
        return {
            "historical_result": result,
            "failures": failures,
            "blocked_stage": "historical_failures" if failures else "",
            "status": "blocked" if failures else "passed",
            "trace": ["qa_gate:historical_failures"],
        }

    return node


def _make_artifact_node() -> Callable[[KfQaGateState], Awaitable[dict[str, Any]]]:
    async def node(state: KfQaGateState) -> dict[str, Any]:
        status = "blocked" if state.get("failures") else "passed"
        artifact = await _maybe_await(
            _deps(state).write_artifact(
                seed=state.get("seed"),
                status=status,
                blocked_stage=state.get("blocked_stage") or "",
                failures=list(state.get("failures") or []),
                fixed_result=dict(state.get("fixed_result") or {}),
                random_result=dict(state.get("random_result") or {}),
                historical_result=dict(state.get("historical_result") or {}),
                trace=list(state.get("trace") or []),
            )
        )
        return {
            "artifact": artifact,
            "status": status,
            "trace": ["qa_gate:write_artifact"],
        }

    return node


def _route_after_fixed(state: KfQaGateState) -> str:
    if state.get("fail_fast", True) and state.get("blocked_stage"):
        return "write_artifact"
    return "random_windows"


def _route_after_random(state: KfQaGateState) -> str:
    if state.get("fail_fast", True) and state.get("blocked_stage"):
        return "write_artifact"
    return "historical_failures"


def _failures_for_stage(stage: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    quality = result.get("quality_status") if isinstance(result.get("quality_status"), dict) else result
    high = int(quality.get("high_count") or quality.get("high") or 0)
    medium = int(quality.get("medium_count") or quality.get("medium") or 0)
    infra = bool(quality.get("infrastructure_error") or quality.get("infrastructure_errors"))
    passed = quality.get("passed")
    failed = high > 0 or medium > 0 or infra or passed is False
    if not failed:
        return []
    return [
        {
            "stage": stage,
            "high_count": high,
            "medium_count": medium,
            "infrastructure_error": infra,
            "passed": bool(passed) if passed is not None else False,
        }
    ]


async def _maybe_await(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def _deps(state: KfQaGateState) -> KfQaGateDeps:
    deps = state.get("_deps")
    if not isinstance(deps, KfQaGateDeps):
        raise RuntimeError("KfQaGateDeps missing from state")
    return deps
