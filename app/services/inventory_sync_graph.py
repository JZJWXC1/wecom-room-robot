from __future__ import annotations

import inspect
import operator
from dataclasses import dataclass
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
SyncStageCallback = Callable[..., MaybeAwaitableDict]


class InventorySyncGraphState(TypedDict, total=False):
    dry_run: bool
    sync_media: bool
    fail_fast: bool
    cache_result: dict[str, Any]
    region_result: dict[str, Any]
    image_result: dict[str, Any]
    media_manifest_result: dict[str, Any]
    snapshot_result: dict[str, Any]
    report: dict[str, Any]
    status: str
    blocked_stage: str
    failures: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class InventorySyncGraphDeps:
    refresh_inventory_cache: SyncStageCallback
    sync_region_inventory: SyncStageCallback
    render_inventory_sheet_image: SyncStageCallback
    build_media_manifest: SyncStageCallback
    publish_snapshot: SyncStageCallback
    write_report: SyncStageCallback


def build_inventory_sync_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for inventory sync graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(InventorySyncGraphState)
    graph.add_node("refresh_inventory_cache", _make_stage_node("refresh_inventory_cache", "cache_result"))
    graph.add_node("sync_region_inventory", _make_stage_node("sync_region_inventory", "region_result"))
    graph.add_node("render_inventory_sheet_image", _make_stage_node("render_inventory_sheet_image", "image_result"))
    graph.add_node("build_media_manifest", _make_stage_node("build_media_manifest", "media_manifest_result"))
    graph.add_node("publish_snapshot", _make_stage_node("publish_snapshot", "snapshot_result"))
    graph.add_node("write_report", _make_report_node())

    graph.add_edge(START, "sync_region_inventory")
    graph.add_conditional_edges(
        "sync_region_inventory",
        _route_after_stage("sync_region_inventory", "refresh_inventory_cache"),
        {"next": "refresh_inventory_cache", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "refresh_inventory_cache",
        _route_after_stage("refresh_inventory_cache", "render_inventory_sheet_image"),
        {"next": "render_inventory_sheet_image", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "render_inventory_sheet_image",
        _route_after_stage("render_inventory_sheet_image", "build_media_manifest"),
        {"next": "build_media_manifest", "report": "write_report"},
    )
    graph.add_conditional_edges(
        "build_media_manifest",
        _route_after_stage("build_media_manifest", "publish_snapshot"),
        {"next": "publish_snapshot", "report": "write_report"},
    )
    graph.add_edge("publish_snapshot", "write_report")
    graph.add_edge("write_report", END)
    return graph.compile(checkpointer=checkpointer)


async def run_inventory_sync_graph(
    deps: InventorySyncGraphDeps,
    *,
    dry_run: bool = False,
    sync_media: bool = True,
    fail_fast: bool = True,
    conversation_id: str = "inventory-sync-graph",
    checkpointer: Any | None = None,
) -> InventorySyncGraphState:
    app = build_inventory_sync_graph_app(checkpointer=checkpointer)
    state: InventorySyncGraphState = {
        "dry_run": dry_run,
        "sync_media": sync_media,
        "fail_fast": fail_fast,
        "failures": [],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_stage_node(stage_name: str, result_key: str) -> Callable[[InventorySyncGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventorySyncGraphState) -> dict[str, Any]:
        callback = getattr(_deps(state), stage_name)
        result = await _maybe_await(
            callback(
                dry_run=bool(state.get("dry_run", False)),
                sync_media=bool(state.get("sync_media", True)),
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
            "trace": [f"inventory_sync:{stage_name}"],
        }

    return node


def _make_report_node() -> Callable[[InventorySyncGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: InventorySyncGraphState) -> dict[str, Any]:
        status = "blocked" if state.get("failures") else "passed"
        report = await _maybe_await(
            _deps(state).write_report(
                dry_run=bool(state.get("dry_run", False)),
                sync_media=bool(state.get("sync_media", True)),
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
            "trace": ["inventory_sync:write_report"],
        }

    return node


def _route_after_stage(stage_name: str, next_stage: str) -> Callable[[InventorySyncGraphState], str]:
    def route(state: InventorySyncGraphState) -> str:
        if state.get("fail_fast", True) and state.get("blocked_stage") == stage_name:
            return "report"
        return "next"

    return route


def _previous_results(state: InventorySyncGraphState) -> dict[str, Any]:
    return {
        "cache_result": dict(state.get("cache_result") or {}),
        "region_result": dict(state.get("region_result") or {}),
        "image_result": dict(state.get("image_result") or {}),
        "media_manifest_result": dict(state.get("media_manifest_result") or {}),
        "snapshot_result": dict(state.get("snapshot_result") or {}),
    }


def _failures_for_stage(stage: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    ok = result.get("ok")
    ready = result.get("ready")
    errors = result.get("errors") or result.get("failures") or result.get("media_failed") or []
    failed = ok is False or ready is False or bool(errors)
    if not failed:
        return []
    return [
        {
            "stage": stage,
            "ok": bool(ok) if ok is not None else ready is not False,
            "error_count": len(errors) if isinstance(errors, list) else 1,
            "reason": str(
                result.get("reason")
                or result.get("error")
                or ",".join(str(item) for item in result.get("not_ready_reasons") or [])
                or ""
            ),
        }
    ]


async def _maybe_await(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def _deps(state: InventorySyncGraphState) -> InventorySyncGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, InventorySyncGraphDeps):
        raise RuntimeError("InventorySyncGraphDeps missing from state")
    return deps
