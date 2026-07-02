from __future__ import annotations

import operator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except ModuleNotFoundError as exc:  # pragma: no cover - covered by release-gate import tests
    END = START = None
    StateGraph = None
    _LANGGRAPH_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


AsyncNodeCallback = Callable[..., Awaitable[dict[str, Any]]]
ToolEvidenceSummary = Callable[[dict[str, Any]], dict[str, Any]]
ToolEvidencePredicate = Callable[[dict[str, Any]], bool]
ToolEvidenceMerger = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class KfProductionFlowState(TypedDict, total=False):
    content: str
    context: dict[str, Any]
    signals: dict[str, Any]
    planner_feedback: dict[str, Any]
    inventory_read_context: Any
    timer: Any
    retry_reason: str
    attempt: int
    max_attempts: int
    understanding: dict[str, Any]
    route: str
    route_reason: str
    planner_result: dict[str, Any]
    actions: list[str]
    tool_evidence: dict[str, Any]
    business_knowledge: dict[str, Any]
    preserved_sendable_evidence: dict[str, Any]
    reply_result: dict[str, Any]
    final_reply: str
    final_draft_reply: str
    status: str
    send_blocked: bool
    planner_rewrite_requested: bool
    planner_retry_requested: bool
    trace: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class KfProductionFlowDeps:
    understand_message: AsyncNodeCallback
    plan_actions: AsyncNodeCallback
    execute_tools: AsyncNodeCallback
    generate_reply_result: AsyncNodeCallback
    record_understanding: AsyncNodeCallback | None = None
    retrieve_business_knowledge: AsyncNodeCallback | None = None
    tool_evidence_summary: ToolEvidenceSummary | None = None
    has_sendable_actions: ToolEvidencePredicate | None = None
    merge_preserved_sendable_evidence: ToolEvidenceMerger | None = None


def build_kf_production_flow_app(
    deps: KfProductionFlowDeps,
    *,
    checkpointer: Any | None = None,
) -> Any:
    if StateGraph is None:
        raise RuntimeError(
            "LangGraph is required for KF production flow. "
            "Install requirements.txt or disable KF_LANGGRAPH_ENABLED."
        ) from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfProductionFlowState)
    graph.add_node("understand_message", _make_understand_node(deps))
    graph.add_node("record_understanding", _make_record_understanding_node(deps))
    graph.add_node("plan_actions", _make_plan_node(deps))
    graph.add_node("execute_tools", _make_tools_node(deps))
    graph.add_node("business_knowledge", _make_business_knowledge_node(deps))
    graph.add_node("generate_reply", _make_reply_node(deps))

    graph.add_edge(START, "understand_message")
    graph.add_edge("understand_message", "record_understanding")
    graph.add_conditional_edges(
        "record_understanding",
        _route_after_record_understanding,
        {"plan_actions": "plan_actions", "business_knowledge": "business_knowledge", "end": END},
    )
    graph.add_conditional_edges(
        "plan_actions",
        _route_after_plan,
        {"understand_message": "understand_message", "execute_tools": "execute_tools", "end": END},
    )
    graph.add_edge("execute_tools", "generate_reply")
    graph.add_edge("business_knowledge", "generate_reply")
    graph.add_conditional_edges(
        "generate_reply",
        _route_after_reply,
        {"understand_message": "understand_message", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)


async def run_kf_production_flow(
    deps: KfProductionFlowDeps,
    *,
    content: str,
    context: dict[str, Any] | None = None,
    signals: dict[str, Any] | None = None,
    planner_feedback: dict[str, Any] | None = None,
    inventory_read_context: Any | None = None,
    timer: Any | None = None,
    retry_reason: str = "",
    max_attempts: int = 2,
    conversation_id: str = "kf-production-flow",
    checkpointer: Any | None = None,
) -> KfProductionFlowState:
    app = build_kf_production_flow_app(deps, checkpointer=checkpointer)
    state: KfProductionFlowState = {
        "content": content,
        "context": dict(context or {}),
        "signals": dict(signals or {}),
        "planner_feedback": dict(planner_feedback or {}),
        "inventory_read_context": inventory_read_context,
        "timer": timer,
        "retry_reason": retry_reason,
        "attempt": 0,
        "max_attempts": max(1, int(max_attempts or 1)),
        "trace": [],
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


def _make_understand_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        with _timer_stage(state.get("timer"), "rewrite_intent"):
            understanding = await deps.understand_message(
                content=str(state.get("content") or ""),
                context=dict(state.get("context") or {}),
                signals=dict(state.get("signals") or {}),
                planner_feedback=dict(state.get("planner_feedback") or {}),
                inventory_read_context=state.get("inventory_read_context"),
            )
        understanding = _require_dict(understanding, "understand_message")
        status = "needs_clarification" if understanding.get("needs_clarification") else "understood"
        route = ""
        route_reason = ""
        trace = ["understand_message"]
        if status != "needs_clarification":
            route, route_reason = _classify_intent_route(
                content=str(state.get("content") or ""),
                signals=dict(state.get("signals") or {}),
                understanding=understanding,
            )
            trace.append(f"intent_route:{route}")
        return {
            "understanding": understanding,
            "route": route,
            "route_reason": route_reason,
            "status": status,
            "planner_rewrite_requested": False,
            "planner_retry_requested": False,
            "trace": trace,
        }

    return node


def _make_record_understanding_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        context = dict(state.get("context") or {})
        if deps.record_understanding:
            recorded = await deps.record_understanding(
                content=str(state.get("content") or ""),
                context=context,
                understanding=dict(state.get("understanding") or {}),
                signals=dict(state.get("signals") or {}),
                inventory_read_context=state.get("inventory_read_context"),
                attempt=_current_attempt(state),
            )
            recorded = _require_dict(recorded, "record_understanding")
            context = dict(recorded.get("context") or context)
        return {
            "context": context,
            "trace": ["record_understanding"],
        }

    return node


def _make_plan_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        with _timer_stage(state.get("timer"), "planner_tools"):
            planner_result = await deps.plan_actions(
                content=str(state.get("content") or ""),
                context=dict(state.get("context") or {}),
                understanding=dict(state.get("understanding") or {}),
                signals=dict(state.get("signals") or {}),
                retry_reason=str(state.get("retry_reason") or ""),
            )
        planner_result = _require_dict(planner_result, "plan_actions")
        actions = _safe_action_list(planner_result)
        if planner_result.get("need_rewrite_clarification"):
            retry_reason = str(
                planner_result.get("missing_evidence")
                or planner_result.get("planner_retry_reason")
                or "planner_missing_evidence"
            )
            if _can_retry(state):
                return {
                    "planner_result": planner_result,
                    "actions": actions,
                    "retry_reason": retry_reason,
                    "planner_feedback": {
                        "need_rewrite_clarification": True,
                        "missing_evidence": retry_reason,
                        "planner_result": planner_result,
                    },
                    "attempt": _next_attempt(state),
                    "planner_rewrite_requested": True,
                    "status": "planner_rewrite_requested",
                    "trace": ["plan_actions"],
                }
            return {
                "planner_result": planner_result,
                "actions": actions,
                "tool_evidence": {
                    "actions": [],
                    "planner_missing_evidence": retry_reason,
                    "suppress_actions": True,
                },
                "retry_reason": retry_reason,
                "planner_rewrite_requested": False,
                "send_blocked": True,
                "status": "planner_rewrite_exhausted",
                "trace": ["plan_actions"],
            }
        return {
            "planner_result": planner_result,
            "actions": actions,
            "planner_rewrite_requested": False,
            "status": "planned",
            "trace": ["plan_actions"],
        }

    return node


def _make_business_knowledge_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        if deps.retrieve_business_knowledge:
            business_knowledge = await deps.retrieve_business_knowledge(
                content=str(state.get("content") or ""),
                context=dict(state.get("context") or {}),
                understanding=dict(state.get("understanding") or {}),
                signals=dict(state.get("signals") or {}),
                inventory_read_context=state.get("inventory_read_context"),
                retry_reason=str(state.get("retry_reason") or ""),
            )
            business_knowledge = _require_dict(business_knowledge, "retrieve_business_knowledge")
        else:
            business_knowledge = _default_business_knowledge(
                content=str(state.get("content") or ""),
                signals=dict(state.get("signals") or {}),
                understanding=dict(state.get("understanding") or {}),
            )
        planner_result = {
            "actions": ["generate_reply"],
            "reply_text": "",
            "source": "langgraph_business_knowledge",
        }
        tool_evidence = {
            "actions": ["generate_reply"],
            "business_knowledge": business_knowledge,
            "rule_evidence": business_knowledge.get("rule_evidence") or {},
            "deterministic_reply_source": "business_knowledge",
        }
        return {
            "business_knowledge": business_knowledge,
            "planner_result": planner_result,
            "actions": ["generate_reply"],
            "tool_evidence": tool_evidence,
            "status": "business_knowledge_retrieved",
            "trace": ["business_knowledge"],
        }

    return node


def _make_tools_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        with _timer_stage(state.get("timer"), "tool_execution"):
            tool_evidence = await deps.execute_tools(
                actions=list(state.get("actions") or []),
                content=str(state.get("content") or ""),
                context=dict(state.get("context") or {}),
                understanding=dict(state.get("understanding") or {}),
                inventory_read_context=state.get("inventory_read_context"),
            )
        tool_evidence = _require_dict(tool_evidence, "execute_tools")
        preserved = dict(state.get("preserved_sendable_evidence") or {})
        if preserved and deps.merge_preserved_sendable_evidence:
            tool_evidence = deps.merge_preserved_sendable_evidence(tool_evidence, preserved)
        if deps.has_sendable_actions and deps.has_sendable_actions(tool_evidence):
            preserved = dict(tool_evidence)
        return {
            "tool_evidence": tool_evidence,
            "preserved_sendable_evidence": preserved,
            "status": "tools_executed",
            "trace": ["execute_tools"],
        }

    return node


def _make_reply_node(deps: KfProductionFlowDeps) -> AsyncNodeCallback:
    async def node(state: KfProductionFlowState) -> dict[str, Any]:
        tool_evidence = dict(state.get("tool_evidence") or {})
        reply_result = await deps.generate_reply_result(
            content=str(state.get("content") or ""),
            context=dict(state.get("context") or {}),
            understanding=dict(state.get("understanding") or {}),
            tool_evidence=tool_evidence,
            planner_result=dict(state.get("planner_result") or {}),
            retry_reason=str(state.get("retry_reason") or ""),
            timer=state.get("timer"),
            inventory_read_context=state.get("inventory_read_context"),
        )
        reply_result = _require_dict(reply_result, "generate_reply")
        context = _reply_context(reply_result, state)
        if reply_result.get("needs_planner_retry"):
            retry_reason = str(reply_result.get("planner_retry_reason") or "final_selfcheck_retry")
            if _can_retry(state):
                tool_evidence = dict(state.get("tool_evidence") or {})
                summary_builder = deps.tool_evidence_summary or _default_tool_evidence_summary
                return {
                    "reply_result": reply_result,
                    "context": context,
                    "tool_evidence": tool_evidence,
                    "retry_reason": retry_reason,
                    "planner_feedback": {
                        "planner_retry_reason": retry_reason,
                        "selfcheck_result": reply_result.get("selfcheck") or {},
                        "planner_result": dict(state.get("planner_result") or {}),
                        "tool_evidence_summary": summary_builder(tool_evidence),
                    },
                    "attempt": _next_attempt(state),
                    "planner_retry_requested": True,
                    "status": "planner_retry_requested",
                    "trace": ["generate_reply"],
                }
        final_reply = str(reply_result.get("reply") or "")
        final_draft_reply = str(reply_result.get("draft_reply") or final_reply)
        send_blocked = bool(reply_result.get("send_blocked") and not final_reply.strip())
        if send_blocked:
            status = "send_blocked"
        elif final_reply.strip():
            status = "ready_to_send"
        else:
            status = "empty_reply"
        return {
            "reply_result": reply_result,
            "context": context,
            "tool_evidence": tool_evidence,
            "final_reply": final_reply,
            "final_draft_reply": final_draft_reply,
            "planner_retry_requested": False,
            "send_blocked": send_blocked,
            "status": status,
            "trace": ["generate_reply"],
        }

    return node


def _route_after_record_understanding(state: KfProductionFlowState) -> str:
    if state.get("status") == "needs_clarification":
        return "end"
    route = str(state.get("route") or "")
    if route == "business_qa":
        return "business_knowledge"
    if route == "housing_tools":
        return "plan_actions"
    return "plan_actions"


def _route_after_plan(state: KfProductionFlowState) -> str:
    if state.get("planner_rewrite_requested"):
        return "understand_message"
    if state.get("status") == "planner_rewrite_exhausted":
        return "end"
    return "execute_tools"


def _route_after_reply(state: KfProductionFlowState) -> str:
    if state.get("planner_retry_requested"):
        return "understand_message"
    return "end"


HOUSING_TOOL_ACTIONS = {
    "search_inventory",
    "context_tools",
    "send_inventory_sheet",
    "send_image",
    "send_video",
    "explain_missing_media",
    "explain_unavailable_viewing",
    "compact_listing",
    "showing_selection",
    "missing_inventory",
}

BUSINESS_ACTIONS = {
    "send_deposit_policy",
    "send_contract_contact",
    "send_price_negotiation_contact",
    "generate_reply",
}

BUSINESS_INTENTS = {
    "deposit",
    "contract",
    "greeting",
    "price_negotiation",
    "business_qa",
}


def _classify_intent_route(
    *,
    content: str,
    signals: dict[str, Any],
    understanding: dict[str, Any],
) -> tuple[str, str]:
    actions = set(_understanding_tool_actions(understanding))
    intent = str(understanding.get("intent") or "").strip().lower()
    query_state = dict(understanding.get("query_state") or {})
    state_intent = str(query_state.get("intent") or "").strip().lower()
    has_business_signal = bool(
        intent in BUSINESS_INTENTS
        or state_intent in BUSINESS_INTENTS
        or signals.get("wants_deposit")
        or signals.get("wants_contract_contact")
        or signals.get("wants_price_negotiation")
        or _looks_like_greeting_or_ack(content)
    )
    has_housing_signal = bool(
        signals.get("wants_inventory_sheet")
        or signals.get("wants_video")
        or signals.get("wants_original_video")
        or signals.get("wants_image")
        or signals.get("wants_viewing")
        or signals.get("wants_password")
        or signals.get("wants_access")
        or signals.get("wants_price")
        or signals.get("wants_utilities")
        or signals.get("wants_inventory_field")
    )
    if has_business_signal and not has_housing_signal:
        return "business_qa", "business_intent_or_signal"
    if actions & HOUSING_TOOL_ACTIONS:
        return "housing_tools", "llm1_tool_plan_requires_housing_tools"
    if actions and actions <= BUSINESS_ACTIONS:
        return "business_qa", "llm1_tool_plan_business_only"
    return "housing_tools", "default_to_tools_for_customer_fact_safety"


def _understanding_tool_actions(understanding: dict[str, Any]) -> list[str]:
    tool_plan = understanding.get("tool_plan")
    if not isinstance(tool_plan, dict):
        structured_task = understanding.get("structured_task")
        if isinstance(structured_task, dict):
            tool_plan = structured_task.get("tool_plan")
    if not isinstance(tool_plan, dict):
        packet = understanding.get("llm1_task_packet")
        if isinstance(packet, dict):
            tool_plan = packet.get("tool_plan")
    if not isinstance(tool_plan, dict):
        return []
    return _safe_action_list(tool_plan)


def _looks_like_greeting_or_ack(content: str) -> bool:
    text = "".join(str(content or "").split()).lower()
    text = text.strip("，,。.!！?？~～")
    return text in {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "有人吗",
        "ok",
        "okay",
        "好的",
        "好",
        "嗯",
        "嗯嗯",
        "收到",
        "谢谢",
    }


def _default_business_knowledge(
    *,
    content: str,
    signals: dict[str, Any],
    understanding: dict[str, Any],
) -> dict[str, Any]:
    rule_evidence: dict[str, Any] = {}
    topics: list[str] = []
    if signals.get("wants_deposit") or str(understanding.get("intent") or "") == "deposit":
        topics.append("deposit")
        rule_evidence["deposit_policy"] = {
            "service": "支付宝芝麻信用无忧住",
            "is_free": False,
            "service_fee": "押金金额 5.5%-8%",
            "notes": [
                "免押不是免费",
                "是否通过以支付宝无忧住风控结果为准",
            ],
        }
    if signals.get("wants_contract_contact") or str(understanding.get("intent") or "") == "contract":
        topics.append("contract")
        rule_evidence["contract_contact"] = ["18758141785", "13282125992", "19941091943"]
    if _looks_like_greeting_or_ack(content):
        topics.append("greeting")
    return {
        "source": "langgraph_default_business_knowledge",
        "topics": topics or ["general_business_qa"],
        "rule_evidence": rule_evidence,
    }


def _timer_stage(timer: Any, name: str) -> Any:
    stage = getattr(timer, "stage", None)
    if callable(stage):
        return stage(name)
    return nullcontext()


def _require_dict(value: Any, node_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"{node_name} must return a dict")


def _safe_action_list(planner_result: dict[str, Any]) -> list[str]:
    return [str(action).strip() for action in planner_result.get("actions") or [] if str(action).strip()]


def _max_attempts(state: KfProductionFlowState) -> int:
    return max(1, int(state.get("max_attempts") or 1))


def _current_attempt(state: KfProductionFlowState) -> int:
    return max(0, int(state.get("attempt") or 0))


def _next_attempt(state: KfProductionFlowState) -> int:
    return _current_attempt(state) + 1


def _can_retry(state: KfProductionFlowState) -> bool:
    return _next_attempt(state) < _max_attempts(state)


def _reply_context(reply_result: dict[str, Any], state: KfProductionFlowState) -> dict[str, Any]:
    context = reply_result.get("context")
    if isinstance(context, dict):
        return dict(context)
    return dict(state.get("context") or {})


def _default_tool_evidence_summary(tool_evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "actions": _safe_action_list(tool_evidence),
        "inventory_rows": len(tool_evidence.get("inventory_rows") or []),
        "target_rows": len(tool_evidence.get("target_rows") or []),
        "image_paths": len(tool_evidence.get("image_paths") or []),
        "video_paths": len(tool_evidence.get("video_paths") or []),
        "missing_media": len(tool_evidence.get("missing_media") or []),
    }
