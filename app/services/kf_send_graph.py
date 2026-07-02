from __future__ import annotations

import inspect
import operator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except Exception as exc:  # pragma: no cover - exercised only when dependency is absent
    StateGraph = None  # type: ignore[assignment]
    START = "__start__"  # type: ignore[assignment]
    END = "__end__"  # type: ignore[assignment]
    _LANGGRAPH_IMPORT_ERROR = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


MaybeAwaitableDict = dict[str, Any] | Awaitable[dict[str, Any]]
AsyncDictCallback = Callable[..., MaybeAwaitableDict]
AsyncVoidCallback = Callable[..., Any]
ReduceContextCallback = Callable[..., dict[str, Any]]
SaveContextCallback = Callable[[str, str, dict[str, Any]], Any]
MarkProcessedCallback = Callable[[str], Any]
StaleGuardCallback = Callable[[], Any]


class KfSendGraphState(TypedDict, total=False):
    open_kfid: str
    external_userid: str
    conversation_key: str
    content: str
    msgids: list[str]
    generation: int
    context: dict[str, Any]
    understanding: dict[str, Any]
    planner_result: dict[str, Any]
    tool_evidence: dict[str, Any]
    reply_result: dict[str, Any]
    final_reply: str
    final_draft_reply: str
    outbound_package: dict[str, Any]
    inventory_read_context: Any
    graph_state: dict[str, Any]
    timer: Any
    send_result: dict[str, Any]
    status: str
    send_blocked: bool
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class KfSendGraphDeps:
    build_audit_artifact: AsyncVoidCallback
    send_final_actions: AsyncDictCallback
    reduce_turn_context: ReduceContextCallback
    save_context: SaveContextCallback
    mark_processed: MarkProcessedCallback
    stale_guard: StaleGuardCallback | None = None


def build_kf_send_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError(
            "LangGraph is required for KF send graph. Install requirements.txt."
        ) from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfSendGraphState)
    graph.add_node("audit_artifact", _make_audit_node())
    graph.add_node("send_actions", _make_send_node())
    graph.add_node("reduce_sent_context", _make_reduce_sent_node())
    graph.add_node("reduce_blocked_context", _make_reduce_blocked_node())
    graph.add_node("persist_context", _make_persist_node())
    graph.add_node("mark_processed", _make_mark_processed_node())

    graph.add_edge(START, "audit_artifact")
    graph.add_conditional_edges(
        "audit_artifact",
        _route_after_audit,
        {"send_actions": "send_actions", "reduce_blocked_context": "reduce_blocked_context"},
    )
    graph.add_edge("send_actions", "reduce_sent_context")
    graph.add_edge("reduce_sent_context", "persist_context")
    graph.add_edge("reduce_blocked_context", "persist_context")
    graph.add_edge("persist_context", "mark_processed")
    graph.add_edge("mark_processed", END)
    return graph.compile(checkpointer=checkpointer)


async def run_kf_send_graph(
    deps: KfSendGraphDeps,
    *,
    open_kfid: str,
    external_userid: str,
    conversation_key: str,
    content: str,
    msgids: list[str],
    generation: int,
    context: dict[str, Any],
    understanding: dict[str, Any],
    planner_result: dict[str, Any],
    tool_evidence: dict[str, Any],
    reply_result: dict[str, Any],
    final_reply: str,
    final_draft_reply: str,
    inventory_read_context: Any | None = None,
    graph_state: dict[str, Any] | None = None,
    timer: Any | None = None,
    conversation_id: str = "kf-send-graph",
    checkpointer: Any | None = None,
) -> KfSendGraphState:
    app = build_kf_send_graph_app(checkpointer=checkpointer)
    state: KfSendGraphState = {
        "open_kfid": open_kfid,
        "external_userid": external_userid,
        "conversation_key": conversation_key,
        "content": content,
        "msgids": list(msgids),
        "generation": generation,
        "context": dict(context or {}),
        "understanding": dict(understanding or {}),
        "planner_result": dict(planner_result or {}),
        "tool_evidence": dict(tool_evidence or {}),
        "reply_result": dict(reply_result or {}),
        "final_reply": str(final_reply or ""),
        "final_draft_reply": str(final_draft_reply or ""),
        "inventory_read_context": inventory_read_context,
        "graph_state": dict(graph_state or {}),
        "timer": timer,
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_audit_node() -> Callable[[KfSendGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        await _maybe_await(
            deps.build_audit_artifact(
                content=str(state.get("content") or ""),
                open_kfid=str(state.get("open_kfid") or ""),
                external_userid=str(state.get("external_userid") or ""),
                msgids=list(state.get("msgids") or []),
                generation=state.get("generation"),
                inventory_read_context=state.get("inventory_read_context"),
                understanding=dict(state.get("understanding") or {}),
                planner_result=dict(state.get("planner_result") or {}),
                tool_evidence=dict(state.get("tool_evidence") or {}),
                reply_result=dict(state.get("reply_result") or {}),
                final_reply=str(state.get("final_reply") or ""),
                graph_state=dict(state.get("graph_state") or {}),
            )
        )
        send_blocked = _is_send_blocked(state)
        return {
            "send_blocked": send_blocked,
            "status": "send_blocked" if send_blocked else "send_ready",
            "trace": ["send_graph:audit_artifact"],
        }

    return node


def _make_send_node() -> Callable[[KfSendGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        _run_stale_guard(deps)
        with _timer_stage(state.get("timer"), "send"):
            send_result = await _maybe_await(
                deps.send_final_actions(
                    open_kfid=str(state.get("open_kfid") or ""),
                    external_userid=str(state.get("external_userid") or ""),
                    context=dict(state.get("context") or {}),
                    final_reply=str(state.get("final_reply") or ""),
                    tool_evidence=dict(state.get("tool_evidence") or {}),
                    msgids=list(state.get("msgids") or []),
                    stale_guard=deps.stale_guard,
                )
            )
        return {
            "send_result": dict(send_result or {}),
            "status": "sent",
            "trace": ["send_graph:send_actions"],
        }

    return node


def _make_reduce_sent_node() -> Callable[[KfSendGraphState], dict[str, Any]]:
    def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        send_result = dict(state.get("send_result") or {})
        context = dict(send_result.get("context") or state.get("context") or {})
        reduced = deps.reduce_turn_context(
            context,
            understanding=dict(state.get("understanding") or {}),
            tool_evidence=dict(state.get("tool_evidence") or {}),
            send_result=send_result,
            final_package=_final_package(state),
        )
        return {
            "context": dict(reduced or context),
            "send_result": send_result,
            "status": "context_reduced",
            "trace": ["send_graph:reduce_sent_context"],
        }

    return node


def _make_reduce_blocked_node() -> Callable[[KfSendGraphState], dict[str, Any]]:
    def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        _run_stale_guard(deps)
        context = dict(state.get("context") or {})
        send_result = {"sent_actions": [], "context": context, "send_blocked": True}
        reduced = deps.reduce_turn_context(
            context,
            understanding=dict(state.get("understanding") or {}),
            tool_evidence=dict(state.get("tool_evidence") or {}),
            send_result=send_result,
            final_package=_final_package(state, blocked=True),
        )
        return {
            "context": dict(reduced or context),
            "send_result": send_result,
            "status": "send_blocked",
            "send_blocked": True,
            "trace": ["send_graph:reduce_blocked_context"],
        }

    return node


def _make_persist_node() -> Callable[[KfSendGraphState], dict[str, Any]]:
    def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        deps.save_context(
            str(state.get("open_kfid") or ""),
            str(state.get("external_userid") or ""),
            dict(state.get("context") or {}),
        )
        return {
            "status": "context_saved",
            "trace": ["send_graph:persist_context"],
        }

    return node


def _make_mark_processed_node() -> Callable[[KfSendGraphState], dict[str, Any]]:
    def node(state: KfSendGraphState) -> dict[str, Any]:
        deps = _deps(state)
        for msgid in list(state.get("msgids") or []):
            deps.mark_processed(str(msgid))
        status = "send_blocked" if state.get("send_blocked") else "sent"
        return {
            "status": status,
            "trace": ["send_graph:mark_processed"],
        }

    return node


def _route_after_audit(state: KfSendGraphState) -> str:
    if state.get("send_blocked") or _is_send_blocked(state):
        return "reduce_blocked_context"
    return "send_actions"


def _is_send_blocked(state: KfSendGraphState) -> bool:
    reply_result = dict(state.get("reply_result") or {})
    if bool(reply_result.get("send_blocked")):
        return True
    tool_evidence = dict(state.get("tool_evidence") or {})
    return bool(tool_evidence.get("suppress_actions") and not str(state.get("final_reply") or "").strip())


def _final_package(state: KfSendGraphState, *, blocked: bool = False) -> dict[str, Any]:
    final_reply = "" if blocked else str(state.get("final_reply") or "")
    return {
        "draft_reply": str(state.get("final_draft_reply") or ""),
        "final_reply": final_reply,
        "outbound_package": (state.get("tool_evidence") or {}).get("outbound_package") or {},
    }


def _run_stale_guard(deps: KfSendGraphDeps) -> None:
    if deps.stale_guard:
        deps.stale_guard()


async def _maybe_await(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def _timer_stage(timer: Any, name: str) -> Any:
    stage = getattr(timer, "stage", None)
    if callable(stage):
        return stage(name)
    return nullcontext()


def _deps(state: KfSendGraphState) -> KfSendGraphDeps:
    deps = state.get("_deps")  # type: ignore[typeddict-item]
    if not isinstance(deps, KfSendGraphDeps):
        raise RuntimeError("KfSendGraphDeps missing from state")
    return deps
