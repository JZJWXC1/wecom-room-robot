from __future__ import annotations

import re
from typing import Any

from app.services import kf_dual_llm_shadow
from app.services.kf_contracts import PreparedOutboundPackage, StructuredTaskPacket, safe_artifact_payload
from app.services.kf_llm1_task_packet import ACTION_TO_TOOL
from app.services.kf_llm2_outbound import compose_kf_outbound
from app.services.kf_outbound_validation import (
    OutboundValidationContext,
    OutboundValidationResult,
    validate_prepared_outbound_package,
)


DUAL_LLM_PRODUCTION_LLM1_PROMPT_VERSION = "dual_llm_production.llm1_task_packet.v1"
DUAL_LLM_PRODUCTION_LLM2_PROMPT_VERSION = "kf_llm2_outbound.production.v1"
DUAL_LLM_PRODUCTION_SELFCHECK_PROFILE = "kf_llm2_outbound.production_guard.v1"
DUAL_LLM_PRODUCTION_REPLY_SOURCE = "kf_llm2_outbound_production"
DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE = "kf_llm2_controlled_evidence_renderer"
SUPPORTED_DUAL_LLM_MODES = {"shadow", "production"}
LLM1_PRODUCTION_ALLOWED_ACTIONS = frozenset(ACTION_TO_TOOL)
LLM1_PRODUCTION_TOOL_ALIASES = {
    **{tool_name: action for action, tool_name in ACTION_TO_TOOL.items()},
    "context_tools.get_candidate_context": "context_tools",
    "context.memory.get_candidate_context": "context_tools",
    "context.get_candidate_context": "context_tools",
    "context.lookup": "context_tools",
    "media.fetch": "context_tools",
    "media.search": "context_tools",
    "media.lookup": "context_tools",
    "media.video.fetch": "send_video",
    "media.video.search": "send_video",
    "video.fetch": "send_video",
    "video.search": "send_video",
    "room_video.fetch": "send_video",
    "media.image.fetch": "send_image",
    "inventory.image.fetch": "send_image",
    "image.fetch": "send_image",
    "photo.fetch": "send_image",
    "picture.fetch": "send_image",
    "reply_compose_signal": "generate_reply",
    "reply_text": "generate_reply",
}


def _is_literal_greeting_text(text: str) -> bool:
    normalized = re.sub(r"[\s,，。.!！?？~～、]+", "", str(text or "").strip())
    return normalized in {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "有人吗",
        "你好在吗",
        "您好在吗",
        "在吗你好",
        "在吗您好",
    }


def _has_greeting_reply_compose_signal(packet: StructuredTaskPacket) -> bool:
    has_reply_signal = False
    for task in packet.tasks or []:
        task_type = str(getattr(task, "task_type", "") or "").strip()
        if task_type != "reply_compose_signal":
            continue
        has_reply_signal = True
        if _is_literal_greeting_text(getattr(task, "user_text", "")):
            return True
    return has_reply_signal and _is_literal_greeting_text(getattr(packet, "rewritten_query", ""))


def normalize_mode(value: Any) -> str:
    mode = str(value or "shadow").strip().lower()
    return mode if mode in SUPPORTED_DUAL_LLM_MODES else "shadow"


def production_enabled(value: Any) -> bool:
    return normalize_mode(value) == "production"


def tool_plan_from_task_packet(task_packet: StructuredTaskPacket | dict[str, Any]) -> dict[str, Any]:
    packet = _coerce_task_packet(task_packet)
    metadata = _llm1_metadata(packet)
    raw_plan = metadata.get("tool_plan") if isinstance(metadata, dict) else {}
    plan = dict(raw_plan) if isinstance(raw_plan, dict) else {}
    actions = _normalize_llm1_actions(_string_list(plan.get("actions")))
    invalid_actions = [action for action in actions if action not in LLM1_PRODUCTION_ALLOWED_ACTIONS]
    retry_reason = ""
    if not isinstance(raw_plan, dict):
        retry_reason = "LLM1 production task packet missing tool_plan; retry rewrite before tool planning."
    elif invalid_actions:
        retry_reason = "LLM1 production tool_plan contains unsupported action; retry rewrite before tool execution."
    elif not actions:
        retry_reason = "LLM1 production tool_plan.actions is empty; retry rewrite before tool execution."
    if str(packet.response_strategy.mode) == "retry" or str(metadata.get("status") or "") == "retry_required":
        retry_reason = str(
            metadata.get("retry_reason")
            or plan.get("missing_evidence")
            or retry_reason
            or "LLM1 production task packet requires retry."
        )
    if retry_reason:
        plan["retry_required"] = True
        plan["need_rewrite_clarification"] = True
        plan["actions"] = []
        plan["missing_evidence"] = retry_reason
        if invalid_actions:
            plan["invalid_actions"] = invalid_actions
    else:
        plan["actions"] = actions
    plan["source"] = _source_from_llm1_metadata(metadata)
    plan.pop("reply", None)
    plan.pop("final_reply", None)
    plan.pop("pre_tool_reply_text", None)
    plan.pop("planner_missing_reply", None)
    plan.pop("clarification_text", None)
    plan["reply_text"] = ""
    return safe_artifact_payload(plan)


async def compose_production_outbound_package(
    *,
    reply_generator: Any,
    task_packet: StructuredTaskPacket | dict[str, Any],
    tool_evidence: dict[str, Any],
    draft_reply: str,
    planner_result: dict[str, Any] | None = None,
    reply_result: dict[str, Any] | None = None,
    retry_reason: str = "",
) -> PreparedOutboundPackage:
    packet = _coerce_task_packet(task_packet)
    llm1_metadata = _llm1_metadata(packet)
    contract_tool_evidence = _contract_tool_evidence(tool_evidence)
    evidence_bundle, response_strategy, send_actions = kf_dual_llm_shadow.build_program_outbound_contract_inputs(
        task_packet=packet,
        tool_evidence=contract_tool_evidence,
        planner_result=planner_result or {},
    )
    if str(packet.response_strategy.mode) == "retry" or str(llm1_metadata.get("status") or "") == "retry_required":
        reason = str(
            llm1_metadata.get("retry_reason")
            or llm1_metadata.get("missing_evidence")
            or "LLM1 production task packet requires retry; LLM2 production is gated."
        )
        return compose_kf_outbound(
            packet,
            evidence_bundle,
            response_strategy,
            llm_output={
                "reply_text": "",
                "self_review": {
                    "status": "retry",
                    "reason": reason,
                    "retry_reason": reason,
                    "rewrite_retry_reason": reason,
                    "llm2_decides_media_targets": False,
                },
                "source": "llm1_production_retry_gate",
            },
            send_actions=send_actions,
            prompt_version=DUAL_LLM_PRODUCTION_LLM2_PROMPT_VERSION,
            selfcheck_profile=DUAL_LLM_PRODUCTION_SELFCHECK_PROFILE,
            reply_source=DUAL_LLM_PRODUCTION_REPLY_SOURCE,
            allow_deterministic_fallback=False,
        )
    if _has_greeting_reply_compose_signal(packet):
        return compose_controlled_evidence_outbound_package(
            task_packet=packet,
            tool_evidence=tool_evidence,
            planner_result=planner_result or {},
            reason="literal_greeting_reply_compose_signal",
        )
    compose_llm2 = getattr(reply_generator, "compose_kf_outbound_production", None)
    if not callable(compose_llm2):
        llm2_output = {
            "reply_text": "",
            "self_review": {
                "status": "retry",
                "reason": "LLM2 production composer is unavailable.",
                "llm2_decides_media_targets": False,
            },
            "source": "missing_llm2_composer",
        }
    else:
        evidence_payload = evidence_bundle.to_safe_dict()
        if send_actions:
            evidence_payload["send_actions"] = [action.to_safe_dict() for action in send_actions]
        kwargs = {
            "task_packet": packet.to_safe_dict(),
            "evidence_bundle": evidence_payload,
            "response_strategy": response_strategy.to_safe_dict(),
            "retry_reason": retry_reason,
        }
        llm2_output = await compose_llm2(**kwargs)
    if not isinstance(llm2_output, dict):
        llm2_output = {}
    if not str(llm2_output.get("reply_text") or "").strip():
        return compose_controlled_evidence_outbound_package(
            task_packet=packet,
            tool_evidence=tool_evidence,
            planner_result=planner_result or {},
            reason=str(
                (llm2_output.get("self_review") or {}).get("retry_reason")
                if isinstance(llm2_output.get("self_review"), dict)
                else ""
            )
            or str(llm2_output.get("retry_reason") or "")
            or "llm2_output_missing_visible_reply",
        )
    return compose_kf_outbound(
        packet,
        evidence_bundle,
        response_strategy,
        llm_output=llm2_output,
        send_actions=send_actions,
        prompt_version=DUAL_LLM_PRODUCTION_LLM2_PROMPT_VERSION,
        selfcheck_profile=DUAL_LLM_PRODUCTION_SELFCHECK_PROFILE,
        reply_source=DUAL_LLM_PRODUCTION_REPLY_SOURCE,
        allow_deterministic_fallback=False,
    )


def compose_controlled_evidence_outbound_package(
    *,
    task_packet: StructuredTaskPacket | dict[str, Any],
    tool_evidence: dict[str, Any],
    planner_result: dict[str, Any] | None = None,
    reason: str = "",
) -> PreparedOutboundPackage:
    packet = _coerce_task_packet(task_packet)
    evidence_bundle, response_strategy, send_actions = kf_dual_llm_shadow.build_program_outbound_contract_inputs(
        task_packet=packet,
        tool_evidence=_contract_tool_evidence(tool_evidence),
        planner_result=planner_result or {},
    )
    package = compose_kf_outbound(
        packet,
        evidence_bundle,
        response_strategy,
        llm_output={},
        send_actions=send_actions,
        prompt_version=DUAL_LLM_PRODUCTION_LLM2_PROMPT_VERSION,
        selfcheck_profile=DUAL_LLM_PRODUCTION_SELFCHECK_PROFILE,
        reply_source=DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE,
        allow_deterministic_fallback=True,
    )
    review = dict(package.self_review or {})
    review["source"] = DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
    review["controlled_renderer_reason"] = str(reason or "llm2_production_retry_exhausted")
    review["llm2_decides_media_targets"] = False
    package.self_review = safe_artifact_payload(review)
    package.reply_source = DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
    return package


def validate_production_outbound_package(
    package: PreparedOutboundPackage,
    *,
    task_packet: StructuredTaskPacket | dict[str, Any],
    user_asked_password: bool | None = None,
    known_constraints: dict[str, Any] | None = None,
) -> OutboundValidationResult:
    packet = _coerce_task_packet(task_packet)
    return validate_prepared_outbound_package(
        package,
        context=OutboundValidationContext(
            task_packet=packet,
            user_asked_password=user_asked_password,
            answered_task_ids=(),
            known_constraints=safe_artifact_payload(known_constraints or {}),
        ),
    )


def outbound_validation_retry_reason(result: OutboundValidationResult) -> str:
    blocking = result.blocking_issues
    if blocking:
        return "kf_outbound_validation L0-L2 blocked: " + "; ".join(
            f"{issue.code}:{issue.message}" for issue in blocking[:5]
        )
    if result.requires_rewrite:
        return "kf_outbound_validation L3 rewrite required: " + "; ".join(result.l3_rewrite_reasons[:5])
    return ""


def package_passed(package: PreparedOutboundPackage) -> bool:
    return str((package.self_review or {}).get("status") or "").strip().lower() == "pass"


def package_retry_reason(package: PreparedOutboundPackage) -> str:
    review = package.self_review or {}
    return str(
        review.get("rewrite_retry_reason")
        or review.get("retry_reason")
        or review.get("reason")
        or "LLM2 production outbound guard failed."
    )


def package_log_payload(package: PreparedOutboundPackage) -> dict[str, Any]:
    return safe_artifact_payload(
        {
            "reply_source": package.reply_source,
            "prompt_version": package.prompt_version,
            "selfcheck_profile": package.selfcheck_profile,
            "self_review": package.self_review,
            "claim_count": len(package.claims),
            "action_caption_count": len(package.action_captions),
            "send_action_count": len(package.send_actions),
            "reply_text_present": bool(str(package.reply_text or "").strip()),
        }
    )


def _coerce_task_packet(value: StructuredTaskPacket | dict[str, Any]) -> StructuredTaskPacket:
    if isinstance(value, StructuredTaskPacket):
        return value
    return StructuredTaskPacket.from_legacy_dict(value)


def _contract_tool_evidence(tool_evidence: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(tool_evidence or {})
    if "inventory_image_paths" not in evidence and evidence.get("inventory_images"):
        evidence["inventory_image_paths"] = evidence.get("inventory_images")
    return evidence


def _llm1_metadata(packet: StructuredTaskPacket) -> dict[str, Any]:
    unknown = packet.legacy_unknown_fields if isinstance(packet.legacy_unknown_fields, dict) else {}
    raw = unknown.get("llm1_production") or unknown.get("llm1_shadow") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _source_from_llm1_metadata(metadata: dict[str, Any]) -> str:
    source = str(metadata.get("source") or "").strip()
    if source == "llm1_shadow":
        return "llm1_production_task_packet"
    if source:
        return f"{source}+production_task_packet"
    return "llm1_production_task_packet"


def _actions_from_task_types(task_types: list[str]) -> list[str]:
    actions: list[str] = []
    for task_type in task_types:
        text = str(task_type or "").strip().lower()
        if text == "inventory_search":
            actions.extend(["search_inventory", "compact_listing", "generate_reply"])
        elif text == "send_video":
            actions.extend(["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"])
        elif text == "send_image":
            actions.extend(["search_inventory", "context_tools", "send_image", "explain_missing_media", "generate_reply"])
        elif text == "send_inventory_sheet":
            actions.extend(["send_inventory_sheet", "generate_reply"])
        elif text == "deposit_policy":
            actions.extend(["send_deposit_policy", "generate_reply"])
        elif text == "contract_contact":
            actions.extend(["send_contract_contact", "generate_reply"])
        elif text == "viewing_guidance":
            actions.extend(["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"])
        elif text == "reply_text":
            actions.append("generate_reply")
    return _dedupe(actions)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_llm1_actions(actions: list[str]) -> list[str]:
    normalized: list[str] = []
    for action in actions:
        value = str(action or "").strip()
        lower_value = value.lower()
        normalized.append(
            LLM1_PRODUCTION_TOOL_ALIASES.get(value)
            or LLM1_PRODUCTION_TOOL_ALIASES.get(lower_value)
            or lower_value
        )
    return _dedupe(normalized)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
