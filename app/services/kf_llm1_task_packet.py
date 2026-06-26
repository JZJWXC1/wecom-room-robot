from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from pydantic import ValidationError

from app.services.kf_contracts import (
    ConstraintOperation,
    ResponseStrategy,
    StructuredTaskPacket,
    TaskAtom,
    safe_artifact_payload,
)


LLM1_TASK_PACKET_PROMPT_VERSION = "dual_llm_shadow.llm1_task_packet.v1"

ACTION_TO_TASK_TYPE = {
    "search_inventory": "inventory_search",
    "compact_listing": "summarize_candidates",
    "context_tools": "context_lookup",
    "send_inventory_sheet": "send_inventory_sheet",
    "send_image": "send_image",
    "send_video": "send_video",
    "explain_missing_media": "explain_missing_media",
    "explain_unavailable_viewing": "viewing_guidance",
    "send_contract_contact": "contract_contact",
    "send_deposit_policy": "deposit_policy",
    "clarification": "clarification",
    "continue_search": "continue_search",
    "generate_reply": "reply_text",
}

ACTION_TO_TOOL = {
    "search_inventory": "inventory.search",
    "compact_listing": "inventory.compact",
    "context_tools": "context.memory",
    "send_inventory_sheet": "inventory.sheet_artifact",
    "send_image": "media.image",
    "send_video": "media.video",
    "explain_missing_media": "media.availability",
    "explain_unavailable_viewing": "viewing.policy",
    "send_contract_contact": "contact.contract",
    "send_deposit_policy": "deposit.policy",
    "continue_search": "inventory.search",
    "generate_reply": "reply.compose",
}

TASK_TYPE_TO_ACTION = {task_type: action for action, task_type in ACTION_TO_TASK_TYPE.items()}
TASK_TYPE_TO_TOOL = {
    ACTION_TO_TASK_TYPE[action]: tool_name
    for action, tool_name in ACTION_TO_TOOL.items()
    if action in ACTION_TO_TASK_TYPE
}

REPLY_OUTPUT_KEYS = {
    "reply",
    "reply_text",
    "draft_reply",
    "final_reply",
    "customer_reply",
    "pre_tool_reply_text",
    "clarification_text",
    "fallback_reply",
}
OPERATED_CONSTRAINT_KEYS = {
    "inherit",
    "inherited",
    "replace",
    "replaced",
    "exclude",
    "excluded",
    "clear",
    "cleared",
    "clear_keys",
    "cleared_keys",
}
CANDIDATE_NUMBER_KEYS = {
    "candidate_numbers",
    "candidate_indices",
    "selected_indices",
    "selected_candidate_numbers",
}


@dataclass(frozen=True)
class KfTaskPacketShadowBuild:
    packet: StructuredTaskPacket
    tool_plan: dict[str, Any]
    candidate_binding: dict[str, Any]
    legacy_diff: dict[str, Any]
    prompt_artifact: dict[str, Any]
    raw_llm1_output: dict[str, Any]
    source: str

    def to_safe_dict(self) -> dict[str, Any]:
        return safe_artifact_payload(
            {
                "source": self.source,
                "packet": self.packet.to_safe_dict(),
                "tool_plan": self.tool_plan,
                "candidate_binding": self.candidate_binding,
                "legacy_diff": self.legacy_diff,
                "prompt_artifact": self.prompt_artifact,
                "raw_llm1_output": self.raw_llm1_output,
            }
        )


def build_kf_task_packet(
    llm1_output: dict[str, Any] | None = None,
    *,
    content: str = "",
    raw_dialog_context: list[dict[str, Any]] | None = None,
    structured_memory: dict[str, Any] | None = None,
    inventory_index: dict[str, Any] | None = None,
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
    legacy_rewrite: dict[str, Any] | None = None,
    legacy_planner: dict[str, Any] | None = None,
    conversation_id: str = "",
    turn_id: str = "",
    case_id: str = "",
    prompt_version: str = LLM1_TASK_PACKET_PROMPT_VERSION,
    inventory_snapshot_id: str = "",
    candidate_set_id: str = "",
) -> StructuredTaskPacket:
    """构建 LLM1 shadow 的结构化任务包；只返回契约对象，不生成客户回复。"""

    return build_kf_task_packet_shadow(
        llm1_output,
        content=content,
        raw_dialog_context=raw_dialog_context,
        structured_memory=structured_memory,
        inventory_index=inventory_index,
        candidate_set=candidate_set,
        legacy_rewrite=legacy_rewrite,
        legacy_planner=legacy_planner,
        conversation_id=conversation_id,
        turn_id=turn_id,
        case_id=case_id,
        prompt_version=prompt_version,
        inventory_snapshot_id=inventory_snapshot_id,
        candidate_set_id=candidate_set_id,
    ).packet


def build_kf_task_packet_shadow(
    llm1_output: dict[str, Any] | None = None,
    *,
    content: str = "",
    raw_dialog_context: list[dict[str, Any]] | None = None,
    structured_memory: dict[str, Any] | None = None,
    inventory_index: dict[str, Any] | None = None,
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
    legacy_rewrite: dict[str, Any] | None = None,
    legacy_planner: dict[str, Any] | None = None,
    conversation_id: str = "",
    turn_id: str = "",
    case_id: str = "",
    prompt_version: str = LLM1_TASK_PACKET_PROMPT_VERSION,
    inventory_snapshot_id: str = "",
    candidate_set_id: str = "",
) -> KfTaskPacketShadowBuild:
    raw_output = _sanitize_llm1_output(llm1_output)
    rewrite = _safe_dict(legacy_rewrite)
    planner = _safe_dict(legacy_planner)
    candidate_context = _candidate_context(candidate_set, rewrite, planner, candidate_set_id=candidate_set_id)
    prompt_artifact = build_kf_task_packet_prompt_artifact(
        content=content,
        raw_dialog_context=raw_dialog_context,
        structured_memory=structured_memory,
        inventory_index=inventory_index,
        candidate_context=candidate_context,
        legacy_rewrite=rewrite,
        legacy_planner=planner,
        prompt_version=prompt_version,
    )
    source = "llm1_shadow" if raw_output else "legacy_shadow_fallback"

    payload, tool_plan, candidate_binding = _packet_payload_from_llm1(
        raw_output,
        content=content,
        legacy_rewrite=rewrite,
        legacy_planner=planner,
        candidate_context=candidate_context,
        prompt_version=prompt_version,
        conversation_id=conversation_id,
        turn_id=turn_id,
        case_id=case_id,
        inventory_snapshot_id=inventory_snapshot_id,
    )
    try:
        packet = StructuredTaskPacket(**payload)
    except (TypeError, ValueError, ValidationError):
        source = "legacy_shadow_fallback_after_invalid_llm1"
        payload, tool_plan, candidate_binding = _packet_payload_from_llm1(
            {},
            content=content,
            legacy_rewrite=rewrite,
            legacy_planner=planner,
            candidate_context=candidate_context,
            prompt_version=prompt_version,
            conversation_id=conversation_id,
            turn_id=turn_id,
            case_id=case_id,
            inventory_snapshot_id=inventory_snapshot_id,
        )
        packet = StructuredTaskPacket(**payload)

    legacy_diff = _legacy_diff(
        packet=packet,
        tool_plan=tool_plan,
        candidate_binding=candidate_binding,
        legacy_rewrite=rewrite,
        legacy_planner=planner,
    )
    packet.legacy_unknown_fields = safe_artifact_payload(
        {
            "llm1_shadow": {
                "source": source,
                "tool_plan": tool_plan,
                "candidate_binding": candidate_binding,
                "legacy_diff": legacy_diff,
                "prompt_artifact": prompt_artifact,
            }
        }
    )
    return KfTaskPacketShadowBuild(
        packet=packet,
        tool_plan=safe_artifact_payload(tool_plan),
        candidate_binding=safe_artifact_payload(candidate_binding),
        legacy_diff=safe_artifact_payload(legacy_diff),
        prompt_artifact=prompt_artifact,
        raw_llm1_output=raw_output,
        source=source,
    )


def build_kf_task_packet_prompt_artifact(
    *,
    content: str = "",
    raw_dialog_context: list[dict[str, Any]] | None = None,
    structured_memory: dict[str, Any] | None = None,
    inventory_index: dict[str, Any] | None = None,
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
    candidate_context: dict[str, Any] | None = None,
    legacy_rewrite: dict[str, Any] | None = None,
    legacy_planner: dict[str, Any] | None = None,
    prompt_version: str = LLM1_TASK_PACKET_PROMPT_VERSION,
) -> dict[str, Any]:
    if candidate_context is None:
        candidate_context = _candidate_context(candidate_set, legacy_rewrite or {}, legacy_planner or {})
    return safe_artifact_payload(
        {
            "prompt_version": prompt_version,
            "content": _clip(safe_artifact_payload(content), 1000),
            "raw_dialog_context": _clip_json(safe_artifact_payload(raw_dialog_context or []), 2500),
            "structured_memory": _clip_json(safe_artifact_payload(structured_memory or {}), 2500),
            "inventory_index": _clip_json(safe_artifact_payload(inventory_index or {}), 2500),
            "candidate_set": {
                "candidate_set_id": str((candidate_context or {}).get("candidate_set_id") or ""),
                "candidate_count": int((candidate_context or {}).get("candidate_count") or 0),
                "candidates": (candidate_context or {}).get("candidates", [])[:10],
            },
            "legacy_rewrite_summary": _legacy_summary(legacy_rewrite or {}),
            "legacy_planner_summary": _legacy_summary(legacy_planner or {}),
        }
    )


def _packet_payload_from_llm1(
    raw_output: dict[str, Any],
    *,
    content: str,
    legacy_rewrite: dict[str, Any],
    legacy_planner: dict[str, Any],
    candidate_context: dict[str, Any],
    prompt_version: str,
    conversation_id: str,
    turn_id: str,
    case_id: str,
    inventory_snapshot_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    constraints_payload = _constraint_payload(raw_output, legacy_rewrite, legacy_planner)
    raw_tasks = _raw_tasks(raw_output)
    raw_actions = _actions_from_payloads(raw_output)
    legacy_actions = _actions_from_payloads(legacy_rewrite, legacy_planner)
    actions = raw_actions or _actions_from_tasks(raw_tasks) or legacy_actions
    tool_plan = _tool_plan_from(raw_output, actions=actions, legacy_planner=legacy_planner)
    actions = _string_list(tool_plan.get("actions")) or actions
    candidate_binding = _candidate_binding_from(raw_output, raw_tasks, legacy_rewrite, legacy_planner, candidate_context)
    base_constraints = _base_task_constraints(raw_output, legacy_rewrite, legacy_planner)
    tasks = _tasks_from_raw(
        raw_tasks,
        content=content,
        candidate_binding=candidate_binding,
    )
    if not tasks:
        tasks = _tasks_from_actions(
            actions,
            content=content,
            constraints=base_constraints,
            candidate_binding=candidate_binding,
        )
    if not tasks:
        tasks = _tasks_from_actions(
            ["clarification" if tool_plan.get("need_rewrite_clarification") else "generate_reply"],
            content=content,
            constraints=base_constraints,
            candidate_binding=candidate_binding,
        )

    candidate_set_id = str(candidate_context.get("candidate_set_id") or "")
    return (
        {
            "prompt_version": prompt_version,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "case_id": case_id,
            "inventory_snapshot_id": inventory_snapshot_id,
            "candidate_set_id": candidate_set_id,
            "response_strategy": _strategy_from(raw_output, actions=actions, fallback=legacy_planner),
            "tasks": tasks,
            "inherited_constraints": constraints_payload["inherited_constraints"],
            "replaced_constraints": constraints_payload["replaced_constraints"],
            "excluded_constraints": constraints_payload["excluded_constraints"],
            "cleared_constraint_keys": constraints_payload["cleared_constraint_keys"],
            "rewritten_query": str(
                raw_output.get("rewritten_query")
                or raw_output.get("rewrite")
                or legacy_rewrite.get("rewritten_query")
                or legacy_rewrite.get("rewrite")
                or legacy_rewrite.get("query")
                or content
                or ""
            ),
        },
        tool_plan,
        candidate_binding,
    )


def _tasks_from_raw(
    raw_tasks: list[dict[str, Any]],
    *,
    content: str,
    candidate_binding: dict[str, Any],
) -> list[TaskAtom]:
    tasks: list[TaskAtom] = []
    for index, raw_task in enumerate(raw_tasks, start=1):
        task = _safe_dict(raw_task)
        action = str(task.get("action") or "").strip()
        task_type = str(task.get("task_type") or task.get("type") or task.get("intent") or "").strip()
        if not task_type and action:
            task_type = ACTION_TO_TASK_TYPE.get(action, action)
        task_type = task_type or "reply_text"
        action = action or TASK_TYPE_TO_ACTION.get(task_type, "")
        constraints = _sanitize_task_constraints(task.get("constraints"), candidate_binding)
        operation = _constraint_operation(
            task.get("constraint_operation") or task.get("operation") or task.get("constraint_mode"),
            default=ConstraintOperation.REPLACE if constraints.get("candidate_numbers") else ConstraintOperation.INHERIT,
        )
        required_tools = _string_list(task.get("required_tools") or task.get("tools"))
        if not required_tools:
            tool_name = ACTION_TO_TOOL.get(action) or TASK_TYPE_TO_TOOL.get(task_type)
            required_tools = [tool_name] if tool_name else []
        try:
            tasks.append(
                TaskAtom(
                    task_id=str(task.get("task_id") or task.get("id") or f"task-{index}-{_slug(task_type)}"),
                    task_type=task_type,
                    user_text=str(task.get("user_text") or task.get("text") or safe_artifact_payload(content) or ""),
                    constraint_operation=operation,
                    constraints=constraints,
                    response_strategy=_strategy_for_action(action, task_type=task_type),
                    depends_on_task_ids=_string_list(task.get("depends_on_task_ids") or task.get("depends_on")),
                    required_tools=required_tools,
                )
            )
        except (TypeError, ValueError, ValidationError):
            continue
    return tasks


def _tasks_from_actions(
    actions: list[str],
    *,
    content: str,
    constraints: dict[str, Any],
    candidate_binding: dict[str, Any],
) -> list[TaskAtom]:
    tasks: list[TaskAtom] = []
    selected_numbers = _int_list(candidate_binding.get("selected_candidate_numbers"))
    safe_content = str(safe_artifact_payload(content) or "")
    for index, action in enumerate(_dedupe(actions), start=1):
        task_constraints = dict(constraints)
        if selected_numbers and action in {"send_video", "send_image", "explain_missing_media"}:
            task_constraints["candidate_numbers"] = selected_numbers
        task_constraints = _sanitize_task_constraints(task_constraints, candidate_binding)
        task_type = ACTION_TO_TASK_TYPE.get(action, action or "reply_text")
        tasks.append(
            TaskAtom(
                task_id=f"task-{index}-{_slug(action or task_type)}",
                task_type=task_type,
                user_text=safe_content,
                constraint_operation=(
                    ConstraintOperation.REPLACE
                    if task_constraints.get("candidate_numbers") and action in {"send_video", "send_image"}
                    else ConstraintOperation.INHERIT
                ),
                constraints=task_constraints,
                response_strategy=_strategy_for_action(action, task_type=task_type),
                required_tools=[ACTION_TO_TOOL[action]] if action in ACTION_TO_TOOL else [],
                depends_on_task_ids=["task-1-search_inventory"] if index > 1 and action in {"send_video", "send_image"} else [],
            )
        )
    return tasks


def _tool_plan_from(raw_output: dict[str, Any], *, actions: list[str], legacy_planner: dict[str, Any]) -> dict[str, Any]:
    raw_plan = raw_output.get("tool_plan") if isinstance(raw_output.get("tool_plan"), dict) else {}
    plan_actions = _string_list(raw_plan.get("actions")) or actions
    plan = _drop_reply_output(dict(raw_plan))
    plan["actions"] = _dedupe(plan_actions)
    plan["required_tools"] = _dedupe(
        _string_list(plan.get("required_tools"))
        or [ACTION_TO_TOOL[action] for action in plan["actions"] if action in ACTION_TO_TOOL]
    )
    plan["need_rewrite_clarification"] = bool(
        plan.get("need_rewrite_clarification")
        or plan.get("needs_clarification")
        or "clarification" in set(plan["actions"])
    )
    plan["continue_search"] = "continue_search" in set(plan["actions"])
    plan["source"] = str(plan.get("source") or raw_output.get("source") or legacy_planner.get("source") or "llm1_shadow")
    return safe_artifact_payload(plan)


def _candidate_binding_from(
    raw_output: dict[str, Any],
    raw_tasks: list[dict[str, Any]],
    legacy_rewrite: dict[str, Any],
    legacy_planner: dict[str, Any],
    candidate_context: dict[str, Any],
) -> dict[str, Any]:
    selected = _candidate_numbers_from(raw_output)
    for task in raw_tasks:
        selected.extend(_candidate_numbers_from(task))
    if not selected:
        selected.extend(_candidate_numbers_from(legacy_rewrite, legacy_planner))
    selected = _dedupe_ints(number for number in selected if number > 0)
    valid_numbers = set(_int_list(candidate_context.get("candidate_numbers")))
    candidate_count = int(candidate_context.get("candidate_count") or 0)
    if not candidate_count:
        return safe_artifact_payload(
            {
                "status": "no_candidate_set",
                "selected_candidate_numbers": [],
                "dropped_candidate_numbers": selected,
                "candidate_set_id": "",
                "candidate_count": 0,
                "bound_by": "not_bound_without_candidate_set",
            }
        )
    valid_selected = [number for number in selected if number in valid_numbers]
    dropped = [number for number in selected if number not in valid_numbers]
    if valid_selected and dropped:
        status = "partial"
    elif valid_selected:
        status = "bound"
    elif dropped:
        status = "invalid_candidate_number"
    else:
        status = "not_requested"
    return safe_artifact_payload(
        {
            "status": status,
            "selected_candidate_numbers": valid_selected,
            "dropped_candidate_numbers": dropped,
            "candidate_set_id": str(candidate_context.get("candidate_set_id") or ""),
            "candidate_count": candidate_count,
            "bound_by": "candidate_set",
        }
    )


def _candidate_context(
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None,
    *payloads: dict[str, Any],
    candidate_set_id: str = "",
) -> dict[str, Any]:
    raw_candidate_set: Any = candidate_set
    if raw_candidate_set is None:
        for payload in payloads:
            if isinstance(payload.get("candidate_set"), (dict, list)):
                raw_candidate_set = payload.get("candidate_set")
                break
    raw_candidates: list[Any] = []
    raw_id = candidate_set_id
    if isinstance(raw_candidate_set, dict):
        raw_id = raw_id or str(raw_candidate_set.get("candidate_set_id") or "")
        for key in ("candidates", "candidate_rows", "target_rows", "inventory_rows"):
            value = raw_candidate_set.get(key)
            if isinstance(value, list):
                raw_candidates = value
                break
    elif isinstance(raw_candidate_set, list):
        raw_candidates = raw_candidate_set
    candidates: list[dict[str, Any]] = []
    numbers: list[int] = []
    for index, item in enumerate(raw_candidates, start=1):
        if not isinstance(item, dict):
            continue
        number = _first_int(item, "candidate_number", "number", "candidate_no") or index
        if number <= 0:
            continue
        numbers.append(number)
        candidates.append(
            safe_artifact_payload(
                {
                    "candidate_number": number,
                    "listing_id": item.get("listing_id") or item.get("房源ID") or "",
                    "community": item.get("community") or item.get("小区") or item.get("小区名称") or "",
                    "room_no": item.get("room_no") or item.get("房号") or item.get("房间号") or "",
                    "title": item.get("title") or item.get("标题") or "",
                }
            )
        )
    return safe_artifact_payload(
        {
            "candidate_set_id": raw_id,
            "candidate_count": len(candidates),
            "candidate_numbers": _dedupe_ints(numbers),
            "candidates": candidates,
        }
    )


def _constraint_payload(
    raw_output: dict[str, Any],
    legacy_rewrite: dict[str, Any],
    legacy_planner: dict[str, Any],
) -> dict[str, Any]:
    raw_constraints = raw_output.get("constraints") if isinstance(raw_output.get("constraints"), dict) else {}
    has_operations = bool(set(raw_constraints) & OPERATED_CONSTRAINT_KEYS)
    inherited = raw_output.get("inherited_constraints") or (raw_constraints.get("inherit") if has_operations else None)
    replaced = raw_output.get("replaced_constraints") or (raw_constraints.get("replace") if has_operations else None)
    excluded = raw_output.get("excluded_constraints") or (raw_constraints.get("exclude") if has_operations else None)
    cleared = (
        raw_output.get("cleared_constraint_keys")
        or raw_output.get("clear_constraint_keys")
        or raw_constraints.get("clear")
        or raw_constraints.get("cleared_keys")
    )
    if not has_operations and raw_constraints:
        inherited = inherited or raw_constraints
    return {
        "inherited_constraints": _safe_dict(inherited or legacy_rewrite.get("inherited_constraints") or legacy_planner.get("inherited_constraints")),
        "replaced_constraints": _safe_dict(replaced or legacy_rewrite.get("replaced_constraints") or legacy_planner.get("replaced_constraints")),
        "excluded_constraints": _safe_dict(excluded or legacy_rewrite.get("excluded_constraints") or legacy_planner.get("excluded_constraints")),
        "cleared_constraint_keys": _string_list(cleared or legacy_rewrite.get("cleared_constraint_keys") or legacy_planner.get("cleared_constraint_keys")),
    }


def _base_task_constraints(
    raw_output: dict[str, Any],
    legacy_rewrite: dict[str, Any],
    legacy_planner: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in (legacy_rewrite, legacy_planner, raw_output):
        for key in ("query_state", "query", "filters", "slots"):
            value = payload.get(key)
            if isinstance(value, dict):
                merged.update(value)
        constraints = payload.get("constraints")
        if isinstance(constraints, dict) and not (set(constraints) & OPERATED_CONSTRAINT_KEYS):
            merged.update(constraints)
    return _safe_dict(merged)


def _legacy_diff(
    *,
    packet: StructuredTaskPacket,
    tool_plan: dict[str, Any],
    candidate_binding: dict[str, Any],
    legacy_rewrite: dict[str, Any],
    legacy_planner: dict[str, Any],
) -> dict[str, Any]:
    legacy_actions = _actions_from_payloads(legacy_rewrite, legacy_planner)
    llm1_actions = _string_list(tool_plan.get("actions"))
    legacy_task_types = _task_types_from_legacy(legacy_rewrite, legacy_planner)
    if not legacy_task_types:
        legacy_task_types = [ACTION_TO_TASK_TYPE.get(action, action) for action in legacy_actions]
    llm1_task_types = [task.task_type for task in packet.tasks]
    legacy_candidate_numbers = _dedupe_ints(_candidate_numbers_from(legacy_rewrite, legacy_planner))
    llm1_candidate_numbers = _int_list(candidate_binding.get("selected_candidate_numbers"))
    comparisons = {
        "task_types": {"llm1": llm1_task_types, "legacy": legacy_task_types},
        "tool_actions": {"llm1": llm1_actions, "legacy": legacy_actions},
        "candidate_numbers": {"llm1": llm1_candidate_numbers, "legacy": legacy_candidate_numbers},
        "constraint_operations": {
            "llm1": [str(task.constraint_operation) for task in packet.tasks],
            "legacy": _legacy_constraint_operations(legacy_rewrite, legacy_planner),
        },
    }
    if not legacy_rewrite and not legacy_planner:
        status = "no_legacy_baseline"
        changed_fields: list[str] = []
    else:
        changed_fields = [
            key
            for key, value in comparisons.items()
            if value["legacy"] and value["llm1"] != value["legacy"]
        ]
        status = "diff" if changed_fields else "match"
    return safe_artifact_payload({"status": status, "changed_fields": changed_fields, **comparisons})


def _sanitize_task_constraints(value: Any, candidate_binding: dict[str, Any]) -> dict[str, Any]:
    constraints = _safe_dict(value)
    selected = _int_list(candidate_binding.get("selected_candidate_numbers"))
    for key in list(constraints):
        if key in CANDIDATE_NUMBER_KEYS:
            constraints.pop(key, None)
    if selected:
        constraints["candidate_numbers"] = selected
    return constraints


def _sanitize_llm1_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _safe_dict(_drop_reply_output(value))


def _drop_reply_output(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in REPLY_OUTPUT_KEYS:
                continue
            result[key_text] = _drop_reply_output(item)
        return result
    if isinstance(value, list):
        return [_drop_reply_output(item) for item in value]
    if isinstance(value, tuple):
        return [_drop_reply_output(item) for item in value]
    return value


def _raw_tasks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("tasks", "task_atoms", "structured_tasks"):
        value = payload.get(key)
        if isinstance(value, list):
            return [_safe_dict(item) for item in value if isinstance(item, dict)]
    return []


def _actions_from_tasks(raw_tasks: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for task in raw_tasks:
        action = str(task.get("action") or "").strip()
        task_type = str(task.get("task_type") or task.get("type") or task.get("intent") or "").strip()
        if action:
            actions.append(action)
        elif task_type in TASK_TYPE_TO_ACTION:
            actions.append(TASK_TYPE_TO_ACTION[task_type])
    return _dedupe(actions)


def _actions_from_payloads(*payloads: dict[str, Any]) -> list[str]:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        actions = _string_list(payload.get("actions") or payload.get("tool_actions"))
        if actions:
            return _dedupe(actions)
        plan = payload.get("tool_plan")
        if isinstance(plan, dict):
            planned = _string_list(plan.get("actions"))
            if planned:
                return _dedupe(planned)
    return []


def _candidate_numbers_from(*payloads: dict[str, Any]) -> list[int]:
    numbers: list[int] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in CANDIDATE_NUMBER_KEYS:
            numbers.extend(_int_list(payload.get(key)))
        selection = payload.get("candidate_selection")
        if isinstance(selection, dict):
            numbers.extend(_int_list(selection.get("candidate_numbers") or selection.get("indices")))
        binding = payload.get("candidate_binding")
        if isinstance(binding, dict):
            numbers.extend(
                _int_list(
                    binding.get("selected_candidate_numbers")
                    or binding.get("candidate_numbers")
                    or binding.get("selected_indices")
                )
            )
    return _dedupe_ints(numbers)


def _strategy_from(
    raw_output: dict[str, Any],
    *,
    actions: list[str],
    fallback: dict[str, Any],
) -> ResponseStrategy:
    raw_strategy = raw_output.get("response_strategy") or raw_output.get("strategy") or fallback.get("response_strategy") or fallback.get("strategy")
    if raw_strategy:
        try:
            return ResponseStrategy.from_legacy_value(raw_strategy)
        except (TypeError, ValueError, ValidationError):
            pass
    action_set = set(actions)
    if "clarification" in action_set:
        return ResponseStrategy.ASK_CLARIFICATION
    if action_set & {"send_video", "send_image", "send_inventory_sheet"}:
        return ResponseStrategy.SEND_MEDIA
    if action_set & {"search_inventory", "compact_listing", "continue_search"}:
        return ResponseStrategy.TOOL_FIRST
    if "send_contract_contact" in action_set:
        return ResponseStrategy.HANDOFF
    return ResponseStrategy.ANSWER


def _strategy_for_action(action: str, *, task_type: str = "") -> ResponseStrategy:
    if action == "clarification" or task_type == "clarification":
        return ResponseStrategy.ASK_CLARIFICATION
    if action in {"send_video", "send_image", "send_inventory_sheet"} or task_type in {
        "send_video",
        "send_image",
        "send_inventory_sheet",
    }:
        return ResponseStrategy.SEND_MEDIA
    if action in {"search_inventory", "compact_listing", "continue_search"} or task_type in {
        "inventory_search",
        "summarize_candidates",
        "continue_search",
    }:
        return ResponseStrategy.TOOL_FIRST
    if action == "send_contract_contact" or task_type == "contract_contact":
        return ResponseStrategy.HANDOFF
    return ResponseStrategy.ANSWER


def _constraint_operation(value: Any, *, default: ConstraintOperation) -> ConstraintOperation:
    text = str(getattr(value, "value", value) or getattr(default, "value", default)).strip().lower()
    for operation in ConstraintOperation:
        if text == operation.value:
            return operation
    return default


def _task_types_from_legacy(*payloads: dict[str, Any]) -> list[str]:
    task_types: list[str] = []
    for payload in payloads:
        for task in _raw_tasks(payload):
            task_type = str(task.get("task_type") or task.get("type") or task.get("intent") or "").strip()
            if task_type:
                task_types.append(task_type)
    return _dedupe(task_types)


def _legacy_constraint_operations(*payloads: dict[str, Any]) -> list[str]:
    operations: list[str] = []
    for payload in payloads:
        for task in _raw_tasks(payload):
            operation = task.get("constraint_operation") or task.get("operation")
            if operation:
                operations.append(str(getattr(operation, "value", operation)))
    return _dedupe(operations)


def _legacy_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return safe_artifact_payload(
        {
            "keys": sorted(str(key) for key in payload.keys()),
            "actions": _actions_from_payloads(payload),
            "candidate_numbers": _candidate_numbers_from(payload),
            "task_types": _task_types_from_legacy(payload),
            "has_tool_plan": isinstance(payload.get("tool_plan"), dict),
        }
    )


def _safe_dict(value: Any) -> dict[str, Any]:
    return safe_artifact_payload(dict(value or {})) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _int_list(value: Any) -> list[int]:
    raw_values: list[Any]
    if isinstance(value, int):
        raw_values = [value]
    elif isinstance(value, str):
        raw_values = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = []
    result: list[int] = []
    for item in raw_values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _first_int(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        numbers = _int_list(payload.get(key))
        if numbers:
            return numbers[0]
    return 0


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _dedupe_ints(values: Any) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in seen:
            result.append(number)
            seen.add(number)
    return result


def _slug(value: str) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "task"))
    text = "-".join(part for part in text.split("-") if part)
    return text[:40] or "task"


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _clip_json(value: Any, limit: int) -> str:
    return _clip(json.dumps(value, ensure_ascii=False, default=str), limit)
