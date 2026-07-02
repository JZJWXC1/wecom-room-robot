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
MessagePredicate = Callable[[dict[str, Any]], MaybeAwaitableAny]
MessageToText = Callable[[dict[str, Any]], MaybeAwaitableAny]
ProcessedPredicate = Callable[[str], MaybeAwaitableAny]


class KfEntryGraphState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    enter_session_messages: list[dict[str, Any]]
    text_messages: list[dict[str, Any]]
    ignored_messages: list[dict[str, Any]]
    text_groups: list[dict[str, Any]]
    dispatch_plan: dict[str, Any]
    status: str
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class KfEntryGraphDeps:
    is_enter_session_event: MessagePredicate
    should_auto_reply_message: MessagePredicate
    message_id: MessageToText
    is_processed: ProcessedPredicate
    open_kfid: MessageToText
    external_userid: MessageToText
    pending_item: Callable[[dict[str, Any]], MaybeAwaitableAny]


def build_kf_entry_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for KF entry graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfEntryGraphState)
    graph.add_node("classify_messages", _make_classify_node())
    graph.add_node("group_text_messages", _make_group_node())
    graph.add_node("build_dispatch_plan", _make_plan_node())

    graph.add_edge(START, "classify_messages")
    graph.add_edge("classify_messages", "group_text_messages")
    graph.add_edge("group_text_messages", "build_dispatch_plan")
    graph.add_edge("build_dispatch_plan", END)
    return graph.compile(checkpointer=checkpointer)


async def run_kf_entry_graph(
    deps: KfEntryGraphDeps,
    *,
    messages: list[dict[str, Any]],
    conversation_id: str = "kf-entry-graph",
    checkpointer: Any | None = None,
) -> KfEntryGraphState:
    app = build_kf_entry_graph_app(checkpointer=checkpointer)
    state: KfEntryGraphState = {
        "messages": [dict(message or {}) for message in messages],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def text_groups_from_dispatch_plan(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    groups = plan.get("text_groups")
    if not isinstance(groups, list):
        return []
    return [dict(group) for group in groups if isinstance(group, dict)]


def enter_session_messages_from_dispatch_plan(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    messages = plan.get("enter_session_messages")
    if not isinstance(messages, list):
        return []
    return [dict(message) for message in messages if isinstance(message, dict)]


def _make_classify_node() -> Callable[[KfEntryGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfEntryGraphState) -> dict[str, Any]:
        deps = _deps(state)
        enter_session_messages: list[dict[str, Any]] = []
        text_messages: list[dict[str, Any]] = []
        ignored_messages: list[dict[str, Any]] = []
        for raw_message in state.get("messages") or []:
            message = dict(raw_message or {})
            try:
                is_enter_session = await _truthy(deps.is_enter_session_event(message))
                should_auto_reply = (
                    False if is_enter_session else await _truthy(deps.should_auto_reply_message(message))
                )
                msgid = str(await _maybe_await(deps.message_id(message)) or "").strip()
            except Exception as exc:
                ignored_messages.append(_ignored(message, "classification_error", exc))
                continue
            if is_enter_session:
                enter_session_messages.append(message)
                continue
            if not should_auto_reply:
                ignored_messages.append(_ignored(message, "not_auto_reply"))
                continue
            try:
                already_processed = bool(msgid and await _truthy(deps.is_processed(msgid)))
            except Exception as exc:
                ignored_messages.append(_ignored(message, "processed_check_error", exc))
                continue
            if already_processed:
                ignored_messages.append(_ignored(message, "processed"))
                continue
            text_messages.append(message)
        return {
            "enter_session_messages": enter_session_messages,
            "text_messages": text_messages,
            "ignored_messages": ignored_messages,
            "status": "classified",
            "trace": ["entry_graph:classify_messages"],
        }

    return node


def _make_group_node() -> Callable[[KfEntryGraphState], Awaitable[dict[str, Any]]]:
    async def node(state: KfEntryGraphState) -> dict[str, Any]:
        deps = _deps(state)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        ignored_messages = list(state.get("ignored_messages") or [])
        for message in state.get("text_messages") or []:
            try:
                open_kfid = str(await _maybe_await(deps.open_kfid(message)) or "").strip()
                external_userid = str(await _maybe_await(deps.external_userid(message)) or "").strip()
                item = await _maybe_await(deps.pending_item(message))
            except Exception as exc:
                ignored_messages.append(_ignored(message, "grouping_error", exc))
                continue
            if not isinstance(item, dict):
                item = {}
            if not open_kfid or not external_userid:
                ignored_messages.append(_ignored(message, "missing_conversation_target"))
                continue
            if not str(item.get("content") or "").strip():
                ignored_messages.append(_ignored(message, "empty_text_content"))
                continue
            grouped.setdefault((open_kfid, external_userid), []).append(dict(item))
        groups = [
            {
                "open_kfid": open_kfid,
                "external_userid": external_userid,
                "items": items,
            }
            for (open_kfid, external_userid), items in sorted(grouped.items())
        ]
        return {
            "text_groups": groups,
            "ignored_messages": ignored_messages,
            "status": "grouped",
            "trace": ["entry_graph:group_text_messages"],
        }

    return node


def _make_plan_node() -> Callable[[KfEntryGraphState], dict[str, Any]]:
    def node(state: KfEntryGraphState) -> dict[str, Any]:
        enter_session_messages = [dict(message) for message in state.get("enter_session_messages") or []]
        text_groups = [dict(group) for group in state.get("text_groups") or []]
        ignored_messages = [dict(message) for message in state.get("ignored_messages") or []]
        dispatch_plan = {
            "schema_version": "kf_entry_graph.v1",
            "enter_session_messages": enter_session_messages,
            "text_groups": text_groups,
            "ignored_messages": ignored_messages,
            "enter_session_count": len(enter_session_messages),
            "text_group_count": len(text_groups),
            "text_message_count": sum(len(group.get("items") or []) for group in text_groups),
            "ignored_count": len(ignored_messages),
        }
        return {
            "dispatch_plan": dispatch_plan,
            "status": "planned",
            "trace": ["entry_graph:build_dispatch_plan"],
        }

    return node


def _ignored(message: dict[str, Any], reason: str, error: BaseException | None = None) -> dict[str, Any]:
    payload = {
        "reason": reason,
        "msgid": str(message.get("msgid") or "").strip(),
        "msgtype": str(message.get("msgtype") or message.get("MsgType") or "").strip(),
    }
    if error is not None:
        payload["error_type"] = error.__class__.__name__
    return payload


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _truthy(value: Any) -> bool:
    return bool(await _maybe_await(value))


def _deps(state: KfEntryGraphState) -> KfEntryGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, KfEntryGraphDeps):
        raise RuntimeError("KfEntryGraphDeps missing from state")
    return deps
