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
    "rent_pay1": ("rent_pay1", "押一付一", "押一付一月租金"),
    "rent_pay2": ("rent_pay2", "押二付一", "押二付一月租金"),
    "source_kind": ("source_kind", "inventory_source_kind"),
    "source_hash": ("source_hash", "inventory_source_hash"),
    "snapshot_id": ("inventory_snapshot_id", "snapshot_id"),
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
            evidence_items.append(
                EvidenceItem(
                    conversation_id=task_packet.conversation_id,
                    turn_id=task_packet.turn_id,
                    case_id=task_packet.case_id,
                    inventory_snapshot_id=task_packet.inventory_snapshot_id,
                    candidate_set_id=task_packet.candidate_set_id,
                    evidence_id=f"evd-{kind}-{index}",
                    evidence_type=kind,
                    summary=f"{kind} artifact {index}",
                    source_kind=str(evidence.get("source_kind") or ""),
                    source_record_id=_stable_hash(path),
                    field_values={
                        "kind": kind,
                        "media_number": index,
                    },
                    sensitivity="public",
                    fetched_at=str(evidence.get("fetched_at") or ""),
                    metadata={
                        "media_number": index,
                        "path_hash": _stable_hash(path),
                    },
                )
            )
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
            result.append(
                SendAction(
                    conversation_id=task_packet.conversation_id,
                    turn_id=task_packet.turn_id,
                    case_id=task_packet.case_id,
                    inventory_snapshot_id=task_packet.inventory_snapshot_id,
                    candidate_set_id=task_packet.candidate_set_id,
                    evidence_id=f"evd-{kind}-{index}",
                    action_id=f"send-{kind}-{index}",
                    action_type=action_type,
                    payload={
                        "media_number": index,
                        "path_hash": _stable_hash(path),
                    },
                    metadata={"source": "program_evidence", "kind": kind},
                )
            )
    return result


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
            "rent_pay1": _row_int(row, "rent_pay1"),
            "rent_pay2": _row_int(row, "rent_pay2"),
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
