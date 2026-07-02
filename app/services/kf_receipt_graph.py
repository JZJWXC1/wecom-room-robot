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


MaybeAwaitableAny = Any | Awaitable[Any]


class KfReceiptGraphState(TypedDict, total=False):
    context: dict[str, Any]
    action: Any
    idempotency_key: str
    receipt_metadata: dict[str, Any]
    outbox_decision: Any
    existing_receipt: Any
    receipt: Any
    receipt_payload: dict[str, Any]
    provider_result: dict[str, Any]
    sent: bool
    should_send: bool
    status: str
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class KfReceiptGraphDeps:
    find_blocking_receipt: Callable[[dict[str, Any], str], MaybeAwaitableAny]
    reserve_outbox: Callable[[Any, str], MaybeAwaitableAny]
    send_call: Callable[[], MaybeAwaitableAny]
    append_receipt: Callable[[dict[str, Any], Any], dict[str, Any]]
    record_persistent_receipt: Callable[..., Any]
    build_duplicate_receipt: Callable[..., Any]
    build_duplicate_from_outbox_decision: Callable[..., Any]
    build_error_receipt: Callable[..., Any]
    build_sent_receipt: Callable[..., Any]
    stale_guard: Callable[[], Any] | None = None


def build_kf_receipt_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for KF receipt graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfReceiptGraphState)
    graph.add_node("check_context_receipt", _make_context_guard_node())
    graph.add_node("reserve_outbox", _make_reserve_node())
    graph.add_node("execute_send", _make_execute_node())

    graph.add_edge(START, "check_context_receipt")
    graph.add_conditional_edges(
        "check_context_receipt",
        _route_after_context_guard,
        {"reserve_outbox": "reserve_outbox", "end": END},
    )
    graph.add_conditional_edges(
        "reserve_outbox",
        _route_after_reserve,
        {"execute_send": "execute_send", "end": END},
    )
    graph.add_edge("execute_send", END)
    return graph.compile(checkpointer=checkpointer)


async def run_kf_receipt_graph(
    deps: KfReceiptGraphDeps,
    *,
    context: dict[str, Any],
    action: Any,
    idempotency_key: str,
    receipt_metadata: dict[str, Any] | None = None,
    conversation_id: str = "kf-receipt-graph",
    checkpointer: Any | None = None,
) -> KfReceiptGraphState:
    app = build_kf_receipt_graph_app(checkpointer=checkpointer)
    state: KfReceiptGraphState = {
        "context": dict(context or {}),
        "action": action,
        "idempotency_key": str(idempotency_key or ""),
        "receipt_metadata": dict(receipt_metadata or {}),
        "sent": False,
        "should_send": False,
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_context_guard_node() -> Callable[[KfReceiptGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfReceiptGraphState) -> dict[str, Any]:
        deps = _deps(state)
        context = dict(state.get("context") or {})
        action = state.get("action")
        key = str(state.get("idempotency_key") or "")
        existing = await _maybe_await(deps.find_blocking_receipt(context, key))
        if existing:
            await _record_receipt(
                deps,
                existing,
                action=action,
                idempotency_key=key,
            )
            duplicate = deps.build_duplicate_receipt(
                action,
                existing,
                idempotency_key=key,
                metadata={"duplicate_reason": "context_receipt_blocks_duplicate"},
            )
            context = deps.append_receipt(context, duplicate)
            await _record_receipt(deps, duplicate, action=action, idempotency_key=key)
            return {
                "context": context,
                "existing_receipt": existing,
                "receipt": duplicate,
                "receipt_payload": _receipt_payload(duplicate),
                "sent": False,
                "should_send": False,
                "status": "context_duplicate_blocked",
                "trace": ["receipt_graph:check_context_receipt"],
            }
        return {
            "should_send": True,
            "status": "context_clear",
            "trace": ["receipt_graph:check_context_receipt"],
        }

    return node


def _make_reserve_node() -> Callable[[KfReceiptGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfReceiptGraphState) -> dict[str, Any]:
        deps = _deps(state)
        context = dict(state.get("context") or {})
        action = state.get("action")
        key = str(state.get("idempotency_key") or "")
        decision = await _maybe_await(deps.reserve_outbox(action, key))
        if not bool(getattr(decision, "should_send", False)):
            duplicate = deps.build_duplicate_from_outbox_decision(action, decision, idempotency_key=key)
            context = deps.append_receipt(context, duplicate)
            await _record_receipt(deps, duplicate, action=action, idempotency_key=key)
            return {
                "context": context,
                "outbox_decision": decision,
                "receipt": duplicate,
                "receipt_payload": _receipt_payload(duplicate),
                "sent": False,
                "should_send": False,
                "status": "outbox_duplicate_blocked",
                "trace": ["receipt_graph:reserve_outbox"],
            }
        return {
            "outbox_decision": decision,
            "should_send": True,
            "status": "outbox_reserved",
            "trace": ["receipt_graph:reserve_outbox"],
        }

    return node


def _make_execute_node() -> Callable[[KfReceiptGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfReceiptGraphState) -> dict[str, Any]:
        deps = _deps(state)
        context = dict(state.get("context") or {})
        action = state.get("action")
        key = str(state.get("idempotency_key") or "")
        decision = state.get("outbox_decision")
        outbox_id = str(getattr(decision, "outbox_id", "") or "")
        metadata = dict(state.get("receipt_metadata") or {})
        try:
            if deps.stale_guard is not None:
                deps.stale_guard()
            provider_result = await _maybe_await(deps.send_call())
        except Exception as exc:
            failed = deps.build_error_receipt(
                action,
                idempotency_key=key,
                error=exc,
                metadata=metadata,
            )
            context = deps.append_receipt(context, failed)
            await _record_receipt(
                deps,
                failed,
                action=action,
                idempotency_key=key,
                outbox_id=outbox_id,
            )
            raise
        if not isinstance(provider_result, dict):
            provider_result = {}
        sent = deps.build_sent_receipt(
            action,
            idempotency_key=key,
            provider_result=provider_result,
            metadata=metadata,
        )
        context = deps.append_receipt(context, sent)
        await _record_receipt(
            deps,
            sent,
            action=action,
            idempotency_key=key,
            outbox_id=outbox_id,
        )
        return {
            "context": context,
            "provider_result": provider_result,
            "receipt": sent,
            "receipt_payload": _receipt_payload(sent),
            "sent": True,
            "should_send": True,
            "status": "sent",
            "trace": ["receipt_graph:execute_send"],
        }

    return node


def _route_after_context_guard(state: KfReceiptGraphState) -> str:
    return "reserve_outbox" if state.get("should_send") else "end"


def _route_after_reserve(state: KfReceiptGraphState) -> str:
    return "execute_send" if state.get("should_send") else "end"


async def _record_receipt(
    deps: KfReceiptGraphDeps,
    receipt: Any,
    *,
    action: Any,
    idempotency_key: str,
    outbox_id: str = "",
) -> None:
    await _maybe_await(
        deps.record_persistent_receipt(
            receipt,
            action=action,
            idempotency_key=idempotency_key,
            outbox_id=outbox_id,
        )
    )


def _receipt_payload(receipt: Any) -> dict[str, Any]:
    if hasattr(receipt, "to_safe_dict"):
        payload = receipt.to_safe_dict()
        return dict(payload or {}) if isinstance(payload, dict) else {}
    if isinstance(receipt, dict):
        return dict(receipt)
    return {}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deps(state: KfReceiptGraphState) -> KfReceiptGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, KfReceiptGraphDeps):
        raise RuntimeError("KfReceiptGraphDeps missing from state")
    return deps
