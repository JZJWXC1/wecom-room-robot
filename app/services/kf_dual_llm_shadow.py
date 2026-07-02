from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from pydantic import ValidationError

from app.services.kf_contracts import (
    ActionCaption,
    CandidateItem,
    CandidateSet,
    Claim,
    ConstraintOperation,
    EvidenceItem,
    PreparedOutboundPackage,
    ResponseStrategy,
    SendAction,
    StructuredTaskPacket,
    TaskAtom,
    ToolEvidenceBundle,
    safe_artifact_payload,
)
from app.services.kf_llm1_task_packet import (
    LLM1_TASK_PACKET_PROMPT_VERSION,
    build_kf_task_packet_shadow,
)
from app.services.kf_llm2_outbound import compose_kf_outbound


DUAL_LLM_SHADOW_SCHEMA_VERSION = "rag_v2_dual_llm_shadow.v1"

ACTION_TO_TASK_TYPE = {
    "search_inventory": "inventory_search",
    "compact_listing": "summarize_candidates",
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

ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "listing_id": ("listing_id", "listingId", "房源ID", "房源编号"),
    "community": ("community", "community_name", "小区", "小区名称"),
    "room_no": ("room_no", "room", "房号", "房间号"),
    "title": ("title", "标题"),
    "area": ("area", "区域", "商圈", "板块", "位置"),
    "layout": ("layout", "户型分类", "户型", "房型"),
    "layout_description": ("layout_description", "户型描述", "户型详情", "户型介绍"),
    "rent_pay1": ("rent_pay1", "押一付一", "押一付一月租金"),
    "rent_pay2": ("rent_pay2", "押二付一", "押二付一月租金"),
    "utilities": ("utilities", "备注", "水电", "水电费", "水电备注", "说明"),
    "viewing": ("viewing", "看房方式密码", "看房方式", "看房密码", "密码"),
    "source_kind": ("source_kind", "inventory_source_kind"),
    "source_hash": ("source_hash", "inventory_source_hash"),
    "snapshot_id": ("inventory_snapshot_id", "snapshot_id"),
    "candidate_number": ("candidate_number", "candidate_no", "candidate_index", "selection_number"),
}


def build_shadow_task_packet(
    legacy_rewrite: dict[str, Any] | None = None,
    legacy_planner: dict[str, Any] | None = None,
    *,
    llm1_shadow_output: dict[str, Any] | None = None,
    content: str = "",
    raw_dialog_context: list[dict[str, Any]] | None = None,
    structured_memory: dict[str, Any] | None = None,
    inventory_index: dict[str, Any] | None = None,
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
    conversation_id: str = "",
    turn_id: str = "",
    case_id: str = "",
    prompt_version: str = LLM1_TASK_PACKET_PROMPT_VERSION,
    inventory_snapshot_id: str = "",
    candidate_set_id: str = "",
) -> StructuredTaskPacket:
    """构建未来 LLM1 shadow 任务包；无 LLM1 输出时仅作为 legacy baseline fallback。"""
    return build_kf_task_packet_shadow(
        llm1_shadow_output,
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


def compose_shadow_outbound(
    task_packet: StructuredTaskPacket,
    tool_evidence: dict[str, Any] | None = None,
    legacy_reply_text: str = "",
    *,
    legacy_planner: dict[str, Any] | None = None,
    legacy_reply_result: dict[str, Any] | None = None,
    prompt_version: str = "dual_llm_shadow.llm2.v1",
) -> PreparedOutboundPackage:
    """把工具证据和 legacy 文本适配成未来 LLM2 的 shadow 待发送包。"""
    evidence = dict(tool_evidence or {})
    planner = dict(legacy_planner or {})
    reply_result = dict(legacy_reply_result or {})
    candidate_set = _candidate_set_from_evidence(task_packet, evidence)
    evidence_bundle = _evidence_bundle_from(task_packet, evidence, candidate_set)
    send_actions = _send_actions_from(
        task_packet=task_packet,
        evidence=evidence,
        reply_text=legacy_reply_text,
    )
    action_captions = _action_captions_from(
        task_packet=task_packet,
        evidence_bundle=evidence_bundle,
        send_actions=send_actions,
    )
    claims = _claims_from(
        task_packet=task_packet,
        evidence=evidence,
        evidence_bundle=evidence_bundle,
        legacy_reply_text=legacy_reply_text,
    )
    response_strategy = _strategy_from(
        _actions_from(planner, evidence),
        planner,
        evidence,
        fallback=task_packet.response_strategy,
    )
    llm2_output = {
        "reply_text": str(reply_result.get("reply") or legacy_reply_text or ""),
        "answered_task_ids": _answered_task_ids(task_packet, claims, send_actions),
        "claims": [claim.to_legacy_dict() for claim in claims if claim.evidence_ref or claim.evidence_id],
        "action_captions": [caption.to_legacy_dict() for caption in action_captions],
        "missing_items": _string_list(evidence.get("missing_items") or reply_result.get("missing_items")),
        "self_review": _self_review_payload(claims=claims, send_actions=send_actions, reply_result=reply_result),
    }
    return compose_kf_outbound(
        task_packet,
        evidence_bundle,
        response_strategy,
        llm_output=llm2_output,
        send_actions=send_actions,
        prompt_version=prompt_version,
        selfcheck_profile=str(
            reply_result.get("selfcheck_profile")
            or planner.get("selfcheck_profile")
            or evidence.get("selfcheck_profile")
            or "dual_llm_shadow.selfcheck.v1"
        ),
        reply_source=str(evidence.get("deterministic_reply_source") or reply_result.get("reply_source") or "dual_llm_shadow"),
    )


def build_program_outbound_contract_inputs(
    *,
    task_packet: StructuredTaskPacket,
    tool_evidence: dict[str, Any] | None = None,
    planner_result: dict[str, Any] | None = None,
) -> tuple[ToolEvidenceBundle, ResponseStrategy, list[SendAction]]:
    """Build production-safe inputs: evidence, strategy and program-owned send actions only."""
    evidence = dict(tool_evidence or {})
    planner = dict(planner_result or {})
    candidate_set = _candidate_set_from_evidence(task_packet, evidence)
    evidence_bundle = _evidence_bundle_from(task_packet, evidence, candidate_set)
    response_strategy = _strategy_from(
        _actions_from(planner, evidence),
        planner,
        evidence,
        fallback=task_packet.response_strategy,
    )
    send_actions = _program_send_actions_from(task_packet=task_packet, evidence=evidence)
    return evidence_bundle, response_strategy, send_actions


def build_dual_llm_shadow_record(
    *,
    llm1_shadow_output: dict[str, Any] | None = None,
    legacy_rewrite: dict[str, Any] | None = None,
    legacy_planner: dict[str, Any] | None = None,
    tool_evidence: dict[str, Any] | None = None,
    legacy_reply_text: str = "",
    legacy_reply_result: dict[str, Any] | None = None,
    content: str = "",
    raw_dialog_context: list[dict[str, Any]] | None = None,
    structured_memory: dict[str, Any] | None = None,
    inventory_index: dict[str, Any] | None = None,
    candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
    conversation_id: str = "",
    turn_id: str = "",
    case_id: str = "",
) -> dict[str, Any]:
    llm1_build = build_kf_task_packet_shadow(
        llm1_shadow_output,
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
    )
    packet = llm1_build.packet
    package = compose_shadow_outbound(
        packet,
        tool_evidence,
        legacy_reply_text,
        legacy_planner=legacy_planner,
        legacy_reply_result=legacy_reply_result,
    )
    evidence = dict(tool_evidence or {})
    planner = dict(legacy_planner or {})
    reply_result = dict(legacy_reply_result or {})
    record = {
        "schema_version": DUAL_LLM_SHADOW_SCHEMA_VERSION,
        "mode": "shadow",
        "llm1": {
            "source": llm1_build.source,
            "task_atoms": [task.to_safe_dict() for task in packet.tasks],
            "constraints": _packet_constraints(packet),
            "candidate_binding": llm1_build.candidate_binding,
            "response_strategy": packet.response_strategy.to_safe_dict(),
            "tool_plan": _tool_plan(packet, planner, evidence, llm1_tool_plan=llm1_build.tool_plan),
            "legacy_diff": llm1_build.legacy_diff,
            "prompt_artifact": llm1_build.prompt_artifact,
        },
        "llm2": {
            "response_strategy": package.response_strategy.to_safe_dict(),
            "candidate_binding": _candidate_binding(packet, evidence),
            "claims": [claim.to_safe_dict() for claim in package.claims],
            "action_captions": [caption.to_safe_dict() for caption in package.action_captions],
            "send_actions": [action.to_safe_dict() for action in package.send_actions],
            "self_review": _self_review(package, reply_result),
        },
        "legacy_roundtrip": {
            "reply_hash": _stable_hash(package.reply_text),
            "task_count": len(packet.tasks),
            "claim_count": len(package.claims),
            "send_action_count": len(package.send_actions),
        },
    }
    return safe_artifact_payload(record)


def _direct_packet_payload(*payloads: dict[str, Any]) -> dict[str, Any]:
    for payload in payloads:
        for key in ("tasks", "task_atoms", "structured_tasks"):
            if isinstance(payload.get(key), list) and payload.get(key):
                return dict(payload)
    return {}


def _normalize_direct_packet_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    tasks = result.get("tasks") or result.get("task_atoms") or result.get("structured_tasks") or []
    if isinstance(tasks, list):
        result["tasks"] = [
            TaskAtom.from_legacy_dict(task).to_legacy_dict() if isinstance(task, dict) else task
            for task in tasks
        ]
        result.pop("task_atoms", None)
        result.pop("structured_tasks", None)
    return result


def _task_from_action(
    *,
    action: str,
    index: int,
    content: str,
    constraints: dict[str, Any],
    candidate_numbers: list[int],
) -> TaskAtom:
    operation = ConstraintOperation.REPLACE if candidate_numbers and action in {"send_video", "send_image"} else ConstraintOperation.INHERIT
    return TaskAtom(
        task_id=f"task-{index}-{_slug(action)}",
        task_type=ACTION_TO_TASK_TYPE.get(action, action or "reply_text"),
        user_text=content,
        constraint_operation=operation,
        constraints=constraints,
        response_strategy=_strategy_for_action(action),
        required_tools=[ACTION_TO_TOOL[action]] if action in ACTION_TO_TOOL else [],
        depends_on_task_ids=["task-1-search_inventory"] if index > 1 and action in {"send_video", "send_image"} else [],
    )


def _strategy_for_action(action: str) -> ResponseStrategy:
    if action == "clarification":
        return ResponseStrategy.ASK_CLARIFICATION
    if action in {"send_video", "send_image", "send_inventory_sheet"}:
        return ResponseStrategy.SEND_MEDIA
    if action in {"search_inventory", "compact_listing", "continue_search"}:
        return ResponseStrategy.TOOL_FIRST
    if action in {"send_contract_contact"}:
        return ResponseStrategy.HANDOFF
    return ResponseStrategy.ANSWER


def _strategy_from(
    actions: list[str],
    *payloads: dict[str, Any],
    fallback: str | ResponseStrategy = ResponseStrategy.ANSWER,
) -> ResponseStrategy:
    for payload in payloads:
        raw = payload.get("response_strategy") or payload.get("strategy")
        if raw:
            try:
                return ResponseStrategy.from_legacy_value(raw)
            except (TypeError, ValueError, ValidationError):
                pass
    action_set = set(actions)
    if "clarification" in action_set:
        return ResponseStrategy.ASK_CLARIFICATION
    if action_set & {"send_video", "send_image", "send_inventory_sheet"}:
        return ResponseStrategy.SEND_MEDIA
    if action_set & {"search_inventory", "compact_listing", "continue_search"}:
        return ResponseStrategy.TOOL_FIRST
    if action_set & {"send_contract_contact"}:
        return ResponseStrategy.HANDOFF
    try:
        return ResponseStrategy.from_legacy_value(fallback)
    except (TypeError, ValueError, ValidationError):
        return ResponseStrategy.ANSWER


def _actions_from(*payloads: dict[str, Any]) -> list[str]:
    for payload in payloads:
        actions = _string_list(payload.get("actions") or payload.get("tool_actions"))
        if actions:
            return _dedupe(actions)
        plan = payload.get("tool_plan")
        if isinstance(plan, dict):
            planned = _string_list(plan.get("actions"))
            if planned:
                return _dedupe(planned)
    return []


def _constraints_from(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        for key in ("constraints", "query", "filters", "slots"):
            value = payload.get(key)
            if isinstance(value, dict):
                merged.update(value)
    return safe_artifact_payload(merged)


def _candidate_numbers_from(*payloads: dict[str, Any]) -> list[int]:
    numbers: list[int] = []
    for payload in payloads:
        for key in ("candidate_numbers", "candidate_indices", "selected_candidate_numbers"):
            numbers.extend(_int_list(payload.get(key)))
        selection = payload.get("candidate_selection")
        if isinstance(selection, dict):
            numbers.extend(_int_list(selection.get("candidate_numbers") or selection.get("indices")))
    return _dedupe_ints([number for number in numbers if number > 0])


def _candidate_set_from_evidence(task_packet: StructuredTaskPacket, evidence: dict[str, Any]) -> CandidateSet | None:
    rows = _rows_from_evidence(evidence)
    if not rows:
        return None
    candidates: list[CandidateItem] = []
    for index, row in enumerate(rows, start=1):
        candidates.append(
            CandidateItem(
                candidate_number=index,
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=_row_text(row, "snapshot_id") or task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                listing_id=_row_text(row, "listing_id"),
                evidence_id=f"evd-candidate-{index}",
                community=_row_text(row, "community"),
                room_no=_row_text(row, "room_no"),
                title=_row_text(row, "title"),
                rent_pay1=_row_int(row, "rent_pay1"),
                rent_pay2=_row_int(row, "rent_pay2"),
                source_kind=_row_text(row, "source_kind"),
            )
        )
    return CandidateSet(
        conversation_id=task_packet.conversation_id,
        turn_id=task_packet.turn_id,
        case_id=task_packet.case_id,
        inventory_snapshot_id=task_packet.inventory_snapshot_id,
        candidate_set_id=task_packet.candidate_set_id,
        candidates=candidates,
        query_state=safe_artifact_payload(evidence.get("query_state") or {}),
    )


def _evidence_bundle_from(
    task_packet: StructuredTaskPacket,
    evidence: dict[str, Any],
    candidate_set: CandidateSet | None,
) -> ToolEvidenceBundle:
    evidence_items: list[EvidenceItem] = []
    rows = _rows_from_evidence(evidence)
    rows_by_listing_id = {
        _row_text(row, "listing_id"): row
        for row in rows
        if _row_text(row, "listing_id")
    }
    for index, raw in enumerate(evidence.get("inventory_listing_evidence") or [], start=1):
        if not isinstance(raw, dict):
            continue
        listing_id = str(raw.get("listing_id") or "")
        row_field_values = _row_field_values(rows_by_listing_id[listing_id]) if listing_id in rows_by_listing_id else {}
        evidence_items.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=str(raw.get("inventory_snapshot_id") or task_packet.inventory_snapshot_id),
                listing_id=listing_id,
                evidence_id=str(raw.get("evidence_id") or f"evd-listing-{index}"),
                evidence_type=str(raw.get("evidence_type") or "inventory_listing"),
                summary=str(raw.get("summary") or ""),
                source_kind=str(raw.get("source_kind") or ""),
                source_record_id=str(raw.get("source_record_id") or raw.get("source_hash") or ""),
                field_values=safe_artifact_payload(raw.get("field_values") or raw.get("fields") or row_field_values),
                sensitivity=str(raw.get("sensitivity") or "public"),
                fetched_at=str(raw.get("fetched_at") or evidence.get("fetched_at") or ""),
                confidence=raw.get("confidence"),
                metadata=safe_artifact_payload(raw.get("metadata") or {}),
                sensitive_metadata=raw.get("sensitive_metadata") or {},
            )
        )
    for index, row in enumerate(rows, start=1):
        evidence_items.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=_row_text(row, "snapshot_id") or task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                listing_id=_row_text(row, "listing_id"),
                evidence_id=f"evd-candidate-{index}",
                evidence_type="inventory_candidate",
                summary=_row_summary(row, index),
                source_kind=_row_text(row, "source_kind"),
                source_record_id=_row_text(row, "source_hash"),
                field_values=_row_field_values(row),
                sensitivity=str(evidence.get("sensitivity") or "public"),
                fetched_at=str(evidence.get("fetched_at") or ""),
                metadata={
                    "candidate_number": index,
                    "source_hash": _row_text(row, "source_hash"),
                },
            )
        )
    for kind, key in (("video", "video_paths"), ("image", "image_paths"), ("inventory_sheet", "inventory_image_paths")):
        for index, path in enumerate(_string_list(evidence.get(key)), start=1):
            row = _media_row_for_index(evidence, kind, index)
            listing_id = _row_text(row, "listing_id") if row else ""
            binding_number = _media_binding_number(kind, index, row)
            candidate_number = _media_candidate_number(kind, row)
            metadata = {
                "media_number": index,
                "binding_number": binding_number,
                "path_hash": _stable_hash(path),
            }
            if candidate_number is not None:
                metadata["candidate_number"] = candidate_number
            evidence_items.append(
                EvidenceItem(
                    conversation_id=task_packet.conversation_id,
                    turn_id=task_packet.turn_id,
                    case_id=task_packet.case_id,
                    inventory_snapshot_id=task_packet.inventory_snapshot_id,
                    candidate_set_id=task_packet.candidate_set_id,
                    listing_id=listing_id,
                    evidence_id=f"evd-{kind}-{binding_number}",
                    evidence_type=kind,
                    summary=_media_evidence_summary(kind, binding_number, row),
                    source_kind=str(evidence.get("source_kind") or ""),
                    source_record_id=_stable_hash(path),
                    field_values=_media_field_values(kind, index, row),
                    sensitivity="public",
                    fetched_at=str(evidence.get("fetched_at") or ""),
                    metadata=metadata,
                )
            )
    evidence_items.extend(_rule_evidence_items(task_packet, evidence))
    evidence_items.extend(_target_error_evidence_items(task_packet, evidence))
    evidence_items.extend(_original_video_evidence_items(task_packet, evidence))
    evidence_items.extend(_missing_media_evidence_items(task_packet, evidence))
    return ToolEvidenceBundle(
        conversation_id=task_packet.conversation_id,
        turn_id=task_packet.turn_id,
        case_id=task_packet.case_id,
        inventory_snapshot_id=task_packet.inventory_snapshot_id,
        candidate_set_id=task_packet.candidate_set_id,
        tool_name="dual_llm_shadow.evidence_adapter",
        source_record_id=str(evidence.get("source_record_id") or evidence.get("source_hash") or ""),
        field_values=safe_artifact_payload(evidence.get("field_values") or {}),
        sensitivity=str(evidence.get("sensitivity") or "public"),
        fetched_at=str(evidence.get("fetched_at") or ""),
        evidence=evidence_items,
        candidate_set=candidate_set,
        raw_tool_result=evidence,
    )


def _claims_from(
    *,
    task_packet: StructuredTaskPacket,
    evidence: dict[str, Any],
    evidence_bundle: ToolEvidenceBundle,
    legacy_reply_text: str,
) -> list[Claim]:
    explicit_claims = evidence.get("claims")
    if isinstance(explicit_claims, list) and explicit_claims:
        claims: list[Claim] = []
        for index, raw in enumerate(explicit_claims, start=1):
            if not isinstance(raw, dict):
                continue
            claims.append(
                Claim(
                    conversation_id=task_packet.conversation_id,
                    turn_id=task_packet.turn_id,
                    case_id=task_packet.case_id,
                    inventory_snapshot_id=task_packet.inventory_snapshot_id,
                    candidate_set_id=task_packet.candidate_set_id,
                    listing_id=str(raw.get("listing_id") or ""),
                    evidence_id=str(raw.get("evidence_id") or ""),
                    claim_id=str(raw.get("claim_id") or f"claim-{index}"),
                    task_id=str(raw.get("task_id") or ""),
                    field=str(raw.get("field") or ""),
                    value=safe_artifact_payload(raw.get("value")),
                    evidence_ref=str(raw.get("evidence_ref") or raw.get("evidence_id") or ""),
                    text_span=raw.get("text_span") or {},
                    sensitivity=str(raw.get("sensitivity") or "public"),
                    text=str(raw.get("text") or ""),
                    status=str(raw.get("status") or "supported"),
                    support=_string_list(raw.get("support")),
                    risk=str(raw.get("risk") or "low"),
                )
            )
        if claims:
            return claims
    support_ids = [item.evidence_id for item in evidence_bundle.evidence if item.evidence_id]
    claims = []
    for index, row in enumerate(_rows_from_evidence(evidence), start=1):
        claims.append(
            Claim(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                listing_id=_row_text(row, "listing_id"),
                evidence_id=f"evd-candidate-{index}",
                claim_id=f"claim-candidate-{index}",
                task_id=task_packet.tasks[0].task_id if task_packet.tasks else "",
                field="candidate_summary",
                value=_row_field_values(row),
                evidence_ref=f"evd-candidate-{index}",
                text_span={},
                sensitivity="public",
                text=_row_summary(row, index),
                support=[f"evd-candidate-{index}"],
                risk="low",
            )
        )
    if legacy_reply_text and not claims:
        claims.append(
            Claim(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                claim_id="claim-legacy-reply",
                task_id=task_packet.tasks[-1].task_id if task_packet.tasks else "",
                field="reply_text",
                value=legacy_reply_text,
                text=legacy_reply_text,
                support=support_ids[:5],
                evidence_ref=support_ids[0] if support_ids else "",
                sensitivity="public",
                risk="medium" if not support_ids else "low",
            )
        )
    return claims


def _send_actions_from(
    *,
    task_packet: StructuredTaskPacket,
    evidence: dict[str, Any],
    reply_text: str,
) -> list[SendAction]:
    actions = _actions_from(evidence)
    result: list[SendAction] = []
    if reply_text and ("generate_reply" in actions or not actions or any(task.task_type == "reply_text" for task in task_packet.tasks)):
        result.append(
            SendAction(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                action_id="send-text-1",
                action_type="text",
                payload={"reply_hash": _stable_hash(reply_text)},
                metadata={"source": "legacy_reply_text"},
            )
        )
    for kind, key, action_type in (
        ("video", "video_paths", "video"),
        ("image", "image_paths", "image"),
        ("inventory_sheet", "inventory_image_paths", "image"),
    ):
        for index, path in enumerate(_string_list(evidence.get(key)), start=1):
            row = _media_row_for_index(evidence, kind, index)
            listing_id = _row_text(row, "listing_id") if row else ""
            room_label = _room_label(row) if row else ""
            binding_number = _media_binding_number(kind, index, row)
            candidate_number = _media_candidate_number(kind, row)
            payload = {
                "media_number": index,
                "binding_number": binding_number,
                "path_hash": _stable_hash(path),
                "community": _row_text(row, "community") if row else "",
                "room_no": _row_text(row, "room_no") if row else "",
            }
            metadata = {
                "source": "program_evidence",
                "kind": kind,
                "room_label": room_label,
                "media_number": index,
                "binding_number": binding_number,
            }
            if candidate_number is not None:
                payload["candidate_number"] = candidate_number
                metadata["candidate_number"] = candidate_number
            result.append(
                SendAction(
                    conversation_id=task_packet.conversation_id,
                    turn_id=task_packet.turn_id,
                    case_id=task_packet.case_id,
                    inventory_snapshot_id=task_packet.inventory_snapshot_id,
                    candidate_set_id=task_packet.candidate_set_id,
                    listing_id=listing_id,
                    evidence_id=f"evd-{kind}-{binding_number}",
                    action_id=f"send-{kind}-{binding_number}",
                    action_type=action_type,
                    payload=payload,
                    metadata=metadata,
                )
            )
    return result


def _program_send_actions_from(
    *,
    task_packet: StructuredTaskPacket,
    evidence: dict[str, Any],
) -> list[SendAction]:
    explicit_actions = evidence.get("send_actions")
    if isinstance(explicit_actions, list):
        result: list[SendAction] = []
        for raw in explicit_actions:
            if isinstance(raw, SendAction):
                result.append(raw)
            elif isinstance(raw, dict):
                result.append(SendAction.from_legacy_dict(raw))
        return result
    return _send_actions_from(task_packet=task_packet, evidence=evidence, reply_text="")


def _action_captions_from(
    *,
    task_packet: StructuredTaskPacket,
    evidence_bundle: ToolEvidenceBundle,
    send_actions: list[SendAction],
) -> list[ActionCaption]:
    captions: list[ActionCaption] = []
    evidence_by_id = {item.evidence_id: item for item in evidence_bundle.evidence if item.evidence_id}
    for index, action in enumerate(send_actions, start=1):
        if action.action_type == "text":
            continue
        evidence_item = evidence_by_id.get(action.evidence_id)
        captions.append(
            ActionCaption(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                listing_id=evidence_item.listing_id if evidence_item else "",
                evidence_id=action.evidence_id,
                caption_id=f"caption-{action.action_id}",
                action_id=action.action_id,
                action_type=action.action_type,
                text=_caption_text(action, evidence_item, index),
                display_order=index,
                metadata={
                    "source": "program_evidence",
                    "evidence_type": evidence_item.evidence_type if evidence_item else "",
                },
            )
        )
    return captions


def _caption_text(action: SendAction, evidence_item: EvidenceItem | None, index: int) -> str:
    if evidence_item and evidence_item.summary:
        return evidence_item.summary
    if action.action_type == "video":
        return f"视频素材 {index}"
    if action.action_type == "image":
        return f"图片素材 {index}"
    return f"发送动作 {index}"


def _answered_task_ids(task_packet: StructuredTaskPacket, claims: list[Claim], send_actions: list[SendAction]) -> list[str]:
    if claims:
        claimed = _dedupe([claim.task_id for claim in claims if claim.task_id])
        if claimed:
            return claimed
    if send_actions:
        return [task.task_id for task in task_packet.tasks if task.task_id]
    return []


def _packet_constraints(packet: StructuredTaskPacket) -> dict[str, Any]:
    task_constraints: dict[str, Any] = {}
    for task in packet.tasks:
        if task.constraints:
            task_constraints[task.task_id] = task.constraints
    return safe_artifact_payload(
        {
            "inherited": packet.inherited_constraints,
            "replaced": packet.replaced_constraints,
            "excluded": packet.excluded_constraints,
            "cleared_keys": packet.cleared_constraint_keys,
            "task_constraints": task_constraints,
        }
    )


def _candidate_binding(packet: StructuredTaskPacket, evidence: dict[str, Any]) -> dict[str, Any]:
    rows = _rows_from_evidence(evidence)
    selected_numbers: list[int] = []
    for task in packet.tasks:
        selected_numbers.extend(_int_list(task.constraints.get("candidate_numbers")))
    candidates = [
        {
            "candidate_number": index,
            "listing_id": _row_text(row, "listing_id"),
            "row_hash": _stable_hash(_row_identity(row)),
            "community": _row_text(row, "community"),
            "room_no": _row_text(row, "room_no"),
        }
        for index, row in enumerate(rows, start=1)
    ]
    media = []
    for kind, key in (("video", "video_paths"), ("image", "image_paths"), ("inventory_sheet", "inventory_image_paths")):
        for index, path in enumerate(_string_list(evidence.get(key)), start=1):
            candidate_number = selected_numbers[index - 1] if index <= len(selected_numbers) else (index if index <= len(candidates) else None)
            media.append(
                {
                    "kind": kind,
                    "media_number": index,
                    "candidate_number": candidate_number,
                    "path_hash": _stable_hash(path),
                    "bound_by": "task_candidate_numbers" if selected_numbers else "evidence_order",
                }
            )
    return safe_artifact_payload(
        {
            "selected_candidate_numbers": _dedupe_ints(selected_numbers),
            "candidates": candidates,
            "media": media,
        }
    )


def _tool_plan(
    packet: StructuredTaskPacket,
    planner: dict[str, Any],
    evidence: dict[str, Any],
    *,
    llm1_tool_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    llm1_plan = dict(llm1_tool_plan or {})
    planned = _actions_from(planner)
    observed = _actions_from(evidence)
    actions = _string_list(llm1_plan.get("actions")) or observed or planned
    required_tools = _string_list(llm1_plan.get("required_tools")) or _dedupe([tool for task in packet.tasks for tool in task.required_tools])
    return safe_artifact_payload(
        {
            "actions": actions,
            "required_tools": required_tools,
            "continue_search": "continue_search" in set(actions),
            "source": str(
                llm1_plan.get("source")
                or planner.get("source")
                or planner.get("reply_source")
                or evidence.get("deterministic_reply_source")
                or ""
            ),
            "legacy_planner_actions": planned,
            "observed_tool_actions": observed,
        }
    )


def _self_review(package: PreparedOutboundPackage, reply_result: dict[str, Any]) -> dict[str, Any]:
    if package.self_review:
        return safe_artifact_payload(package.self_review)
    return _self_review_payload(claims=package.claims, send_actions=package.send_actions, reply_result=reply_result)


def _self_review_payload(
    *,
    claims: list[Claim],
    send_actions: list[SendAction],
    reply_result: dict[str, Any],
) -> dict[str, Any]:
    selfcheck = reply_result.get("selfcheck") if isinstance(reply_result.get("selfcheck"), dict) else {}
    return safe_artifact_payload(
        {
            "status": str(selfcheck.get("status") or "shadow_only"),
            "source": str(selfcheck.get("source") or "dual_llm_shadow"),
            "needs_planner_retry": bool(reply_result.get("needs_planner_retry")),
            "has_claims": bool(claims),
            "send_action_count": len(send_actions),
            "llm2_decides_media_targets": False,
        }
    )


def _rows_from_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("target_rows", "inventory_rows", "candidate_rows"):
        rows = [dict(row) for row in evidence.get(key) or [] if isinstance(row, dict)]
        if rows:
            return rows
    candidate_set = evidence.get("candidate_set")
    if isinstance(candidate_set, dict):
        return [dict(row) for row in candidate_set.get("candidates") or [] if isinstance(row, dict)]
    return []


def _media_row_for_index(evidence: dict[str, Any], kind: str, index: int) -> dict[str, Any]:
    key = "video_rows" if kind == "video" else ("image_rows" if kind == "image" else "")
    if key:
        rows = [dict(row) for row in evidence.get(key) or [] if isinstance(row, dict)]
        if 0 <= index - 1 < len(rows):
            return rows[index - 1]
    rows = _rows_from_evidence(evidence)
    if kind in {"video", "image"} and 0 <= index - 1 < len(rows):
        return rows[index - 1]
    return {}


def _room_label(row: dict[str, Any]) -> str:
    community = _row_text(row, "community")
    room_no = _row_text(row, "room_no")
    if community and room_no:
        return f"{community}{room_no}"
    return community or room_no


def _media_evidence_summary(kind: str, index: int, row: dict[str, Any]) -> str:
    if kind == "inventory_sheet":
        return "房源表 PNG 图片已由工具生成，可通过受控图片动作发送。"
    label = _room_label(row)
    media_name = "视频" if kind == "video" else "图片"
    if label:
        return f"{label}{media_name}素材已匹配，可通过受控{media_name}动作发送。"
    return f"第 {index} 个{media_name}素材已匹配，可通过受控{media_name}动作发送。"


def _media_field_values(kind: str, index: int, row: dict[str, Any]) -> dict[str, Any]:
    binding_number = _media_binding_number(kind, index, row)
    candidate_number = _media_candidate_number(kind, row)
    values = {
        "kind": kind,
        "media_number": index,
        "binding_number": binding_number,
        "send_channel": "image" if kind in {"image", "inventory_sheet"} else "video",
    }
    if candidate_number is not None:
        values["candidate_number"] = candidate_number
    if kind == "inventory_sheet":
        values["artifact"] = "inventory_sheet_png"
    if row:
        values.update(_row_field_values(row))
        values["room_label"] = _room_label(row)
    return safe_artifact_payload(values)


def _rule_evidence_items(task_packet: StructuredTaskPacket, evidence: dict[str, Any]) -> list[EvidenceItem]:
    rule_evidence = evidence.get("rule_evidence")
    if not isinstance(rule_evidence, dict):
        return []
    result: list[EvidenceItem] = []
    deposit_policy = rule_evidence.get("deposit_policy")
    if isinstance(deposit_policy, dict):
        result.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                evidence_id="evd-rule-deposit-policy-1",
                evidence_type="deposit_policy",
                summary=(
                    "免押是支付宝无忧住芝麻信用评估，不是免费免押；"
                    "符合风控后需支付押金金额 5.5%-8% 的免押服务费。"
                ),
                source_kind="rule_evidence",
                source_record_id=_stable_hash(deposit_policy),
                field_values=safe_artifact_payload(deposit_policy),
                sensitivity="public",
                metadata={"controlled_fact_source": "deposit_policy"},
            )
        )
    return result


def _target_error_evidence_items(task_packet: StructuredTaskPacket, evidence: dict[str, Any]) -> list[EvidenceItem]:
    result: list[EvidenceItem] = []
    selection_error = evidence.get("selection_error")
    if isinstance(selection_error, dict) and selection_error:
        requested_indices: list[int] = []
        for raw_index in selection_error.get("requested_indices") or []:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if index > 0:
                requested_indices.append(index)
        requested_indices = _dedupe_ints(requested_indices)
        try:
            candidate_count = int(selection_error.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
        candidate_labels = _string_list(selection_error.get("candidate_labels"))[:8]
        requested_text = "、".join(f"第{index}套" for index in requested_indices) or "客户所选序号"
        summary = f"客户选择了{requested_text}，但上一轮只有 {candidate_count} 套候选。"
        if candidate_labels:
            summary += " 上一轮候选：" + "、".join(candidate_labels[:5]) + "。"
        field_values = {
            "error_code": "selection_error",
            "requested_indices": requested_indices,
            "candidate_count": candidate_count,
            "candidate_labels": candidate_labels,
            "reason": str(selection_error.get("reason") or ""),
        }
        result.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                evidence_id="evd-target-selection-error-1",
                evidence_type="selection_error",
                summary=summary,
                source_kind="tool_resolver",
                source_record_id=_stable_hash(field_values),
                field_values=field_values,
                sensitivity="public",
                metadata={"controlled_error_code": "selection_error"},
            )
        )

    field_target_error = evidence.get("field_target_error")
    if isinstance(field_target_error, dict) and field_target_error:
        field = str(field_target_error.get("field") or "这个信息").strip()
        reason = str(field_target_error.get("reason") or "field_target_error").strip()
        candidate_labels = _string_list(field_target_error.get("candidate_labels"))[:8]
        if reason == "original_video_followup_missing_stable_video_target":
            summary = "客户追问原视频/高清源，但上一轮没有稳定绑定到可继续追问的视频房源。"
            error_code = "original_video_target_error"
        elif reason == "community_media_request_missing_room_ref":
            summary = f"客户按小区要{field}，但该小区有多套候选，工具未绑定到具体房源。"
            error_code = "field_target_error"
        else:
            summary = f"客户要查询{field}，但工具未绑定到具体房源。"
            error_code = "field_target_error"
        if candidate_labels:
            summary += " 可供客户选择的候选：" + "、".join(candidate_labels[:5]) + "。"
        try:
            candidate_count = int(field_target_error.get("candidate_count") or len(candidate_labels))
        except (TypeError, ValueError):
            candidate_count = len(candidate_labels)
        field_values = {
            "error_code": error_code,
            "field": field,
            "reason": reason,
            "candidate_count": candidate_count,
            "candidate_labels": candidate_labels,
            "requires_customer_room_ref": True,
        }
        result.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                evidence_id="evd-target-field-error-1",
                evidence_type="field_target_error",
                summary=summary,
                source_kind="tool_resolver",
                source_record_id=_stable_hash(field_values),
                field_values=field_values,
                sensitivity="public",
                metadata={"controlled_error_code": error_code},
            )
        )
    missing_target_reason = str(evidence.get("missing_target_reason") or "").strip()
    if missing_target_reason and not result:
        field_values = {
            "error_code": "missing_target",
            "missing_target": True,
            "reason": missing_target_reason,
            "resolution": "需要先确认具体房源",
            "requires_customer_room_ref": True,
        }
        result.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                evidence_id="evd-target-missing-1",
                evidence_type="missing_target",
                summary="客户请求发送素材或查询单套信息，但工具没有可绑定的目标房源。",
                source_kind="tool_resolver",
                source_record_id=_stable_hash(field_values),
                field_values=field_values,
                sensitivity="public",
                metadata={"controlled_error_code": "missing_target"},
            )
        )
    return result


def _original_video_evidence_items(task_packet: StructuredTaskPacket, evidence: dict[str, Any]) -> list[EvidenceItem]:
    request = evidence.get("original_video_request")
    request_data = dict(request) if isinstance(request, dict) else {}
    original_paths = _string_list(evidence.get("original_video_paths"))
    original_urls = _string_list(evidence.get("original_video_urls"))
    material_urls = _string_list(evidence.get("material_page_urls"))
    requested = bool(
        request_data.get("requested")
        or original_paths
        or original_urls
        or material_urls
    )
    if not requested:
        return []
    has_original_source = bool(
        request_data.get("has_original_source")
        or original_paths
        or original_urls
        or material_urls
    )
    has_sendable_video = bool(_string_list(evidence.get("video_paths")))
    if has_original_source:
        evidence_type = "original_video_source"
        summary = "客户要原视频/高清源，素材库已有原视频或素材页来源证据。"
    elif has_sendable_video:
        evidence_type = "original_video_unavailable"
        summary = "客户要原视频/高清源；当前只有企业微信可发送视频，没有原片或高清下载链接证据。"
    else:
        evidence_type = "original_video_unavailable"
        summary = "客户要原视频/高清源；工具未找到普通视频，也未找到原片或高清下载链接证据。"
    field_values = {
        "error_code": evidence_type,
        "requested": True,
        "has_original_source": has_original_source,
        "has_sendable_video": has_sendable_video,
        "original_video_path_count": len(original_paths),
        "original_video_urls": original_urls[:3],
        "material_page_urls": material_urls[:3],
    }
    return [
        EvidenceItem(
            conversation_id=task_packet.conversation_id,
            turn_id=task_packet.turn_id,
            case_id=task_packet.case_id,
            inventory_snapshot_id=task_packet.inventory_snapshot_id,
            candidate_set_id=task_packet.candidate_set_id,
            evidence_id="evd-original-video-1",
            evidence_type=evidence_type,
            summary=summary,
            source_kind="media_store",
            source_record_id=_stable_hash(field_values),
            field_values=field_values,
            sensitivity="public",
            metadata={"controlled_error_code": evidence_type},
        )
    ]


def _missing_media_kind(evidence: dict[str, Any]) -> str:
    actions = set(_actions_from(evidence))
    if {"send_video", "send_image"} <= actions:
        return "video_and_image"
    if "send_video" in actions:
        return "video"
    if "send_image" in actions:
        return "image"
    status = evidence.get("media_status")
    if isinstance(status, dict):
        if "video" in status and "image" in status:
            return "video_and_image"
        if "video" in status:
            return "video"
        if "image" in status:
            return "image"
    return "media"


def _missing_media_label(kind: str) -> str:
    if kind == "video":
        return "视频"
    if kind == "image":
        return "图片"
    if kind == "video_and_image":
        return "视频或图片"
    return "素材"


def _missing_media_evidence_items(task_packet: StructuredTaskPacket, evidence: dict[str, Any]) -> list[EvidenceItem]:
    missing = _string_list(evidence.get("missing_media"))
    if not missing and not evidence.get("media_status"):
        return []
    media_kind = _missing_media_kind(evidence)
    labels = missing or ["未匹配到可发送素材"]
    result: list[EvidenceItem] = []
    for index, label in enumerate(labels, start=1):
        result.append(
            EvidenceItem(
                conversation_id=task_packet.conversation_id,
                turn_id=task_packet.turn_id,
                case_id=task_packet.case_id,
                inventory_snapshot_id=task_packet.inventory_snapshot_id,
                candidate_set_id=task_packet.candidate_set_id,
                evidence_id=f"evd-missing-media-{index}",
                evidence_type="missing_media",
                summary=f"{label} 暂未找到可发送{_missing_media_label(media_kind)}。",
                source_kind="media_store",
                source_record_id=_stable_hash({"missing": label, "kind": media_kind}),
                field_values={
                    "error_code": "missing_media",
                    "media_kind": media_kind,
                    "label": label,
                    "has_sendable_video": bool(evidence.get("video_paths")),
                    "has_sendable_image": bool(evidence.get("image_paths")),
                },
                sensitivity="public",
                metadata={"controlled_error_code": "missing_media"},
            )
        )
    return result


def _row_summary(row: dict[str, Any], index: int) -> str:
    community = _row_text(row, "community")
    room_no = _row_text(row, "room_no")
    rent = _row_int(row, "rent_pay1") or _row_int(row, "rent_pay2")
    parts = [f"候选{index}"]
    if community:
        parts.append(community)
    if room_no:
        parts.append(room_no)
    if rent:
        parts.append(f"租金{rent}")
    return " ".join(parts)


def _row_identity(row: dict[str, Any]) -> dict[str, str]:
    return {
        "listing_id": _row_text(row, "listing_id"),
        "community": _row_text(row, "community"),
        "room_no": _row_text(row, "room_no"),
    }


def _row_field_values(row: dict[str, Any]) -> dict[str, Any]:
    return safe_artifact_payload(
        {
            "listing_id": _row_text(row, "listing_id"),
            "community": _row_text(row, "community"),
            "room_no": _row_text(row, "room_no"),
            "area": _row_text(row, "area"),
            "layout": _row_text(row, "layout"),
            "layout_description": _row_text(row, "layout_description"),
            "rent_pay1": _row_int(row, "rent_pay1"),
            "rent_pay2": _row_int(row, "rent_pay2"),
            "utilities": _row_text(row, "utilities"),
            "has_viewing_text": bool(_row_text(row, "viewing")),
            "candidate_number": _row_int(row, "candidate_number"),
            "source_kind": _row_text(row, "source_kind"),
        }
    )


def _row_text(row: dict[str, Any], field: str) -> str:
    for name in ROW_ALIASES[field]:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _row_int(row: dict[str, Any], field: str) -> int | None:
    text = _row_text(row, field)
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _media_binding_number(kind: str, index: int, row: dict[str, Any] | None) -> int:
    candidate_number = _media_candidate_number(kind, row)
    if candidate_number is not None:
        return candidate_number
    return index


def _media_candidate_number(kind: str, row: dict[str, Any] | None) -> int | None:
    if kind in {"video", "image"} and isinstance(row, dict):
        candidate_number = _row_int(row, "candidate_number")
        if candidate_number and candidate_number > 0:
            return candidate_number
    return None


def _safe_dict(value: Any) -> dict[str, Any]:
    return safe_artifact_payload(dict(value or {})) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _int_list(value: Any) -> list[int]:
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        value = value.replace("，", ",")
        return [int(item) for item in value.split(",") if item.strip().isdigit()]
    if isinstance(value, (list, tuple, set)):
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    return []


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _dedupe_ints(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower()).strip("_") or "action"


def _stable_hash(value: Any) -> str:
    payload = json.dumps(safe_artifact_payload(value), ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()
