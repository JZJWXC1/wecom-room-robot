from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any

from app.services.kf_contracts import (
    ActionCaption,
    Claim,
    EvidenceItem,
    PreparedOutboundPackage,
    ResponseStrategy,
    SendAction,
    StructuredTaskPacket,
    ToolEvidenceBundle,
    safe_artifact_payload,
)


LLM2_OUTBOUND_PROMPT_VERSION = "kf_llm2_outbound.shadow.v1"
LLM2_OUTBOUND_SELFCHECK_PROFILE = "kf_llm2_outbound.guard.v1"

_URL_RE = re.compile(r"https?://[^\s，。；；、)）]+", re.I)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PASSWORD_RE = re.compile(r"(?:密码|门锁|开门码)[^\n，。；;]{0,12}\d{3,8}#?|\b\d{3,8}#\b")
_MONEY_RE = re.compile(
    r"(?:押一付一|押二付一|月租|租金|价格|房租|预算)[^\d]{0,10}([1-9]\d{2,5})"
    r"|([1-9]\d{2,5})\s*(?:元|块|/月|每月)"
)

_PRICE_FIELDS = {
    "rent",
    "rent_pay1",
    "rent_pay2",
    "price",
    "budget",
    "押一付一",
    "押一付一月租金",
    "押二付一",
    "押二付一月租金",
    "租金",
    "月租",
    "价格",
    "预算",
}
_STATUS_FIELDS = {
    "status",
    "availability",
    "availability_status",
    "room_status",
    "房态",
    "状态",
    "是否在租",
    "空出时间",
}
_LINK_FIELDS = {"url", "link", "链接", "下载链接", "素材页", "material_page_url", "original_video_url"}
_PLAIN_FACT_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "south_orientation",
        ("朝南", "南向"),
        ("朝南", "南向", '"朝向":"南"', '"orientation":"south"', '"orientation":"south-facing"'),
    ),
    (
        "elevator",
        ("有电梯", "电梯房"),
        ("有电梯", "电梯房", '"电梯":"有"', '"电梯":true', '"has_elevator":true', '"elevator":true'),
    ),
    (
        "vacant_now",
        ("已空出", "已经空出", "已空", "现在空", "空着", "随时入住"),
        ("已空出", "已经空出", "已空", "空置", "空房", "随时入住", '"房态":"已空"', '"状态":"已空"', '"availability":"vacant"'),
    ),
    (
        "cat_allowed",
        ("可养猫", "可以养猫", "能养猫", "允许养猫"),
        ("可养猫", "可以养猫", "能养猫", "允许养猫", '"可养猫":true', '"宠物":"可"', '"cat_allowed":true', '"pets_allowed":true'),
    ),
    (
        "near_subway",
        ("近地铁", "地铁近", "离地铁近", "地铁口", "地铁附近"),
        ("近地铁", "地铁近", "离地铁近", "地铁口", "地铁附近", '"近地铁":true', '"地铁":"近"', '"near_subway":true'),
    ),
)


def compose_kf_outbound(
    task_packet: StructuredTaskPacket | dict[str, Any],
    evidence_bundle: ToolEvidenceBundle | dict[str, Any],
    response_strategy: ResponseStrategy | dict[str, Any] | str | None = None,
    *,
    llm_output: dict[str, Any] | None = None,
    send_actions: list[SendAction | dict[str, Any]] | None = None,
    prompt_version: str = LLM2_OUTBOUND_PROMPT_VERSION,
    selfcheck_profile: str = LLM2_OUTBOUND_SELFCHECK_PROFILE,
    reply_source: str = "kf_llm2_outbound_shadow",
    allow_deterministic_fallback: bool = True,
) -> PreparedOutboundPackage:
    """Validate LLM2 shadow wording into a safe PreparedOutboundPackage.

    LLM2 is allowed to phrase text, claims and captions only. Candidate binding,
    listing ids and send actions always come from tool evidence/program input.
    """
    packet = _coerce_task_packet(task_packet)
    bundle = _coerce_evidence_bundle(evidence_bundle)
    strategy = _coerce_strategy(response_strategy or packet.response_strategy)
    trusted_actions = _coerce_send_actions(send_actions) if send_actions is not None else _send_actions_from_bundle(packet, bundle)
    output = dict(llm_output or {})
    if not output:
        if allow_deterministic_fallback:
            output = _deterministic_llm2_shadow_output(packet, bundle, trusted_actions)
        else:
            output = {
                "reply_text": "",
                "self_review": {
                    "status": "retry",
                    "reason": "LLM2 production returned empty output.",
                    "retry_reason": "llm2_production_empty_output",
                    "rewrite_retry_reason": "llm2_production_empty_output",
                    "llm2_decides_media_targets": False,
                },
                "source": "llm2_production_empty_output",
            }

    ignored_llm_send_actions = bool(output.get("send_actions"))
    reply_text = str(output.get("reply_text") or "")
    output_review = output.get("self_review") if isinstance(output.get("self_review"), dict) else {}
    explicit_status = str(output_review.get("status") or output.get("status") or "pass").strip().lower()
    retry_reason = str(
        output.get("retry_reason")
        or output.get("rewrite_retry_reason")
        or output.get("missing_evidence")
        or output_review.get("retry_reason")
        or output_review.get("reason")
        or ""
    ).strip()
    guard_reasons = []
    if explicit_status in {"retry", "fallback"} or bool(output.get("need_rewrite_clarification")):
        guard_reasons.append(retry_reason or f"llm2_shadow_status_{explicit_status}")

    guard_reasons.extend(_unsupported_high_risk_values(output, bundle))
    evidence_by_id = _evidence_by_id(bundle)
    claim_result = _claims_from_output(packet, bundle, output.get("claims"), evidence_by_id)
    caption_result = _action_captions_from_output(packet, trusted_actions, output.get("action_captions"), evidence_by_id)
    guard_reasons.extend(claim_result.errors)
    guard_reasons.extend(caption_result.errors)
    guard_reasons.extend(_unsupported_plain_facts_in_reply(reply_text, bundle, claim_result.items))

    if guard_reasons:
        return _failure_package(
            packet=packet,
            bundle=bundle,
            send_actions=trusted_actions,
            prompt_version=prompt_version,
            selfcheck_profile=selfcheck_profile,
            reply_source=reply_source,
            reasons=guard_reasons,
            requested_strategy=strategy,
            ignored_llm_send_actions=ignored_llm_send_actions,
        )

    claims = claim_result.items or _default_claims(packet, bundle)
    action_captions = caption_result.items or _default_action_captions(packet, trusted_actions, evidence_by_id, _candidate_labels_by_listing(bundle))
    answered_task_ids = _answered_task_ids(packet, output.get("answered_task_ids"), claims, trusted_actions, bool(reply_text))
    self_review = _success_review(
        output_review,
        requested_strategy=strategy,
        ignored_llm_send_actions=ignored_llm_send_actions,
        claim_count=len(claims),
        action_count=len(trusted_actions),
    )

    return PreparedOutboundPackage(
        prompt_version=prompt_version,
        conversation_id=packet.conversation_id,
        turn_id=packet.turn_id,
        case_id=packet.case_id,
        audience=packet.audience,
        inventory_snapshot_id=packet.inventory_snapshot_id,
        candidate_set_id=packet.candidate_set_id,
        reply_text=reply_text,
        response_strategy=strategy,
        answered_task_ids=answered_task_ids,
        candidate_set=bundle.candidate_set,
        evidence_bundle=bundle,
        claims=claims,
        action_captions=action_captions,
        send_actions=trusted_actions,
        missing_items=_string_list(output.get("missing_items")),
        self_review=self_review,
        selfcheck_profile=selfcheck_profile,
        reply_source=reply_source,
    )


class _BuildResult:
    def __init__(self, items: list[Any] | None = None, errors: list[str] | None = None) -> None:
        self.items = items or []
        self.errors = errors or []


def _coerce_task_packet(value: StructuredTaskPacket | dict[str, Any]) -> StructuredTaskPacket:
    if isinstance(value, StructuredTaskPacket):
        return value
    return StructuredTaskPacket.from_legacy_dict(value)


def _coerce_evidence_bundle(value: ToolEvidenceBundle | dict[str, Any]) -> ToolEvidenceBundle:
    if isinstance(value, ToolEvidenceBundle):
        return value
    return ToolEvidenceBundle.from_legacy_dict(value)


def _coerce_strategy(value: ResponseStrategy | dict[str, Any] | str | None) -> ResponseStrategy:
    try:
        return ResponseStrategy.from_legacy_value(value or ResponseStrategy.ANSWER)
    except (TypeError, ValueError):
        return ResponseStrategy.ANSWER


def _coerce_send_actions(values: list[SendAction | dict[str, Any]]) -> list[SendAction]:
    result: list[SendAction] = []
    for value in values:
        if isinstance(value, SendAction):
            result.append(value)
        elif isinstance(value, dict):
            result.append(SendAction.from_legacy_dict(value))
    return result


def _send_actions_from_bundle(packet: StructuredTaskPacket, bundle: ToolEvidenceBundle) -> list[SendAction]:
    result: list[SendAction] = []
    counters: dict[str, int] = {}
    for item in bundle.evidence:
        action_type = _action_type_for_evidence(item.evidence_type)
        if not action_type:
            continue
        counters[action_type] = counters.get(action_type, 0) + 1
        action_id = f"send-{action_type}-{counters[action_type]}"
        result.append(
            SendAction(
                conversation_id=packet.conversation_id,
                turn_id=packet.turn_id,
                case_id=packet.case_id,
                audience=packet.audience,
                inventory_snapshot_id=packet.inventory_snapshot_id,
                candidate_set_id=packet.candidate_set_id,
                listing_id=item.listing_id,
                evidence_id=item.evidence_id,
                action_id=action_id,
                action_type=action_type,
                payload={"evidence_ref": item.evidence_id, "source_record_hash": _stable_hash(item.source_record_id)},
                metadata={"source": "tool_evidence", "evidence_type": item.evidence_type},
            )
        )
    return result


def _action_type_for_evidence(evidence_type: str) -> str:
    text = str(evidence_type or "").strip().lower()
    if text == "video":
        return "video"
    if text in {"image", "inventory_sheet"}:
        return "image"
    if text in {"contract_contact", "viewing_password", "viewing_contact"}:
        return text
    return ""


def _deterministic_llm2_shadow_output(
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
    send_actions: list[SendAction],
) -> dict[str, Any]:
    captions = _default_action_captions(packet, send_actions, _evidence_by_id(bundle), _candidate_labels_by_listing(bundle))
    claims = _default_claims(packet, bundle)
    return {
        "reply_text": _default_reply_text(bundle, send_actions),
        "answered_task_ids": _answered_task_ids(packet, [], claims, send_actions, bool(claims or send_actions)),
        "claims": [claim.to_legacy_dict() for claim in claims],
        "action_captions": [caption.to_legacy_dict() for caption in captions],
        "self_review": {
            "status": "pass",
            "source": "deterministic_llm2_shadow_fallback",
            "llm2_decides_media_targets": False,
        },
    }


def _default_reply_text(bundle: ToolEvidenceBundle, send_actions: list[SendAction]) -> str:
    if send_actions:
        evidence_by_id = _evidence_by_id(bundle)
        candidate_labels = _candidate_labels_by_listing(bundle)
        video_count = sum(1 for action in send_actions if action.action_type == "video")
        image_count = sum(1 for action in send_actions if action.action_type == "image")
        if len(send_actions) == 1:
            action = send_actions[0]
            evidence = evidence_by_id.get(action.evidence_id)
            return _oralized_action_text(action, evidence, 1, _candidate_label_for_action(action, evidence, candidate_labels))
        if video_count and not image_count:
            return f"这是这{_listing_count_label(video_count)}对应的视频。"
        if image_count and not video_count:
            return f"这是这{_listing_count_label(image_count)}对应的图片。"
        return "这是这批对应的素材。"
    for item in bundle.evidence:
        if item.summary:
            return item.summary
    return ""


def _unsupported_high_risk_values(output: dict[str, Any], bundle: ToolEvidenceBundle) -> list[str]:
    text = json.dumps(safe_artifact_payload(output), ensure_ascii=False, default=str)
    reasons: list[str] = []
    if _URL_RE.search(text):
        reasons.append("llm2_output_contains_link_use_evidence_slot")
    if _PHONE_RE.search(text):
        reasons.append("llm2_output_contains_full_phone")
    if _PASSWORD_RE.search(text):
        reasons.append("llm2_output_contains_password_like_value")
    allowed_prices = _allowed_price_tokens(bundle)
    for token in _money_tokens(text):
        if token not in allowed_prices:
            reasons.append(f"unsupported_price_or_budget:{token}")
    return _dedupe(reasons)


def _unsupported_plain_facts_in_reply(
    reply_text: str,
    bundle: ToolEvidenceBundle,
    claims: list[Claim],
) -> list[str]:
    if not str(reply_text or "").strip():
        return []
    support_payload = {
        "evidence_bundle": bundle.to_safe_dict(),
        "claims": [claim.to_safe_dict() for claim in claims],
    }
    return _unsupported_plain_facts(reply_text, support_payload, prefix="reply_text")


def _unsupported_plain_facts(text: str, support_payload: Any, *, prefix: str) -> list[str]:
    compact_text = _compact_support_text(text)
    if not compact_text:
        return []
    compact_support = _compact_support_text(support_payload)
    reasons: list[str] = []
    for fact_key, triggers, support_tokens in _PLAIN_FACT_SPECS:
        if not any(_compact_support_text(trigger) in compact_text for trigger in triggers):
            continue
        if any(_compact_support_text(token) in compact_support for token in support_tokens):
            continue
        reasons.append(f"{prefix}_unsupported_plain_fact:{fact_key}")
    return reasons


def _allowed_price_tokens(bundle: ToolEvidenceBundle) -> set[str]:
    text_parts: list[str] = []
    for item in bundle.evidence:
        text_parts.append(item.summary)
        for key, value in item.field_values.items():
            if _is_price_field(key):
                text_parts.append(str(value))
    text_parts.append(json.dumps(bundle.field_values, ensure_ascii=False, default=str))
    return set(_money_tokens("\n".join(text_parts))) | {
        str(value).strip()
        for item in bundle.evidence
        for key, value in item.field_values.items()
        if _is_price_field(key) and str(value).strip().isdigit()
    }


def _money_tokens(text: str) -> list[str]:
    result: list[str] = []
    for match in _MONEY_RE.finditer(text):
        token = match.group(1) or match.group(2) or ""
        if token:
            result.append(token)
    return result


def _claims_from_output(
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
    raw_claims: Any,
    evidence_by_id: dict[str, EvidenceItem],
) -> _BuildResult:
    if not isinstance(raw_claims, list):
        return _BuildResult()
    result: list[Claim] = []
    errors: list[str] = []
    valid_tasks = {task.task_id for task in packet.tasks}
    for index, raw in enumerate(raw_claims, start=1):
        if not isinstance(raw, dict):
            continue
        evidence_ref = str(raw.get("evidence_ref") or raw.get("evidence_id") or "").strip()
        evidence = evidence_by_id.get(evidence_ref)
        if not evidence:
            errors.append(f"claim_{index}_missing_valid_evidence_ref")
            continue
        if _claim_changes_listing_or_candidate(raw, evidence, bundle):
            errors.append(f"claim_{index}_changes_listing_or_candidate")
            continue
        field = str(raw.get("field") or "").strip()
        if _is_high_risk_field(field) and not _value_supported_by_evidence(raw.get("value"), evidence):
            errors.append(f"claim_{index}_unsupported_high_risk_field:{field or 'unknown'}")
            continue
        if not _value_supported_by_evidence(raw.get("value"), evidence):
            errors.append(f"claim_{index}_unsupported_by_evidence:{field or 'unknown'}")
            continue
        task_id = str(raw.get("task_id") or "").strip()
        if task_id and task_id not in valid_tasks:
            errors.append(f"claim_{index}_unknown_task_id")
            continue
        result.append(
            Claim(
                prompt_version=LLM2_OUTBOUND_PROMPT_VERSION,
                conversation_id=packet.conversation_id,
                turn_id=packet.turn_id,
                case_id=packet.case_id,
                audience=packet.audience,
                inventory_snapshot_id=evidence.inventory_snapshot_id or packet.inventory_snapshot_id,
                candidate_set_id=packet.candidate_set_id,
                listing_id=evidence.listing_id,
                evidence_id=evidence.evidence_id,
                claim_id=str(raw.get("claim_id") or f"claim-llm2-{index}"),
                task_id=task_id or _first_task_id(packet),
                field=field,
                value=safe_artifact_payload(raw.get("value")),
                evidence_ref=evidence.evidence_id,
                text_span=raw.get("text_span") or {},
                sensitivity=str(raw.get("sensitivity") or evidence.sensitivity or "public"),
                text=str(raw.get("text") or evidence.summary or field or f"claim {index}"),
                status=str(raw.get("status") or "supported"),
                support=_string_list(raw.get("support")) or [evidence.evidence_id],
                risk=str(raw.get("risk") or "low"),
            )
        )
    return _BuildResult(result, errors)


def _claim_changes_listing_or_candidate(
    raw: dict[str, Any],
    evidence: EvidenceItem,
    bundle: ToolEvidenceBundle,
) -> bool:
    raw_listing_id = str(raw.get("listing_id") or "").strip()
    if raw_listing_id and evidence.listing_id and raw_listing_id != evidence.listing_id:
        return True
    raw_candidate_number = raw.get("candidate_number")
    if raw_candidate_number is None:
        return False
    expected = _candidate_number_for_listing(bundle, evidence.listing_id)
    try:
        return bool(expected and int(raw_candidate_number) != expected)
    except (TypeError, ValueError):
        return True


def _candidate_number_for_listing(bundle: ToolEvidenceBundle, listing_id: str) -> int | None:
    if not bundle.candidate_set or not listing_id:
        return None
    for candidate in bundle.candidate_set.candidates:
        if candidate.listing_id == listing_id:
            return candidate.candidate_number
    return None


def _value_supported_by_evidence(value: Any, evidence: EvidenceItem) -> bool:
    safe_value = safe_artifact_payload(value)
    if safe_value in (None, "", {}, []):
        return True
    if isinstance(safe_value, dict):
        leaf_values = [item for item in safe_value.values() if item not in (None, "", {}, [])]
        return all(_value_supported_by_evidence(item, evidence) for item in leaf_values)
    if isinstance(safe_value, list):
        leaf_values = [item for item in safe_value if item not in (None, "", {}, [])]
        return all(_value_supported_by_evidence(item, evidence) for item in leaf_values)
    evidence_text = _normalized_text(
        {
            "summary": evidence.summary,
            "field_values": evidence.field_values,
            "metadata": evidence.metadata,
            "source_record_id": evidence.source_record_id,
        }
    )
    compact_value = _compact_support_text(safe_value)
    compact_evidence = _compact_support_text(evidence_text)
    if compact_value and (len(compact_value) >= 2 or compact_value.isdigit()) and compact_value in compact_evidence:
        return True
    value_text = _normalized_text(safe_value)
    if not value_text:
        return True
    return value_text in evidence_text


def _compact_support_text(value: Any) -> str:
    text = value if isinstance(value, str) else _normalized_text(value)
    return re.sub(r"\s+", "", str(text)).lower()


def _action_captions_from_output(
    packet: StructuredTaskPacket,
    send_actions: list[SendAction],
    raw_captions: Any,
    evidence_by_id: dict[str, EvidenceItem],
) -> _BuildResult:
    if not isinstance(raw_captions, list):
        return _BuildResult()
    actions_by_id = {action.action_id: action for action in send_actions}
    result: list[ActionCaption] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_captions, start=1):
        if not isinstance(raw, dict):
            continue
        action_id = str(raw.get("action_id") or "").strip()
        action = actions_by_id.get(action_id)
        if not action:
            errors.append(f"caption_{index}_unknown_action_id")
            continue
        evidence = evidence_by_id.get(action.evidence_id)
        caption_text = str(raw.get("text") or (evidence.summary if evidence else action.action_type))
        caption_errors = _unsupported_plain_facts(
            caption_text,
            {"action": action.to_safe_dict(), "evidence": evidence.to_safe_dict() if evidence else {}},
            prefix=f"caption_{index}",
        )
        if caption_errors:
            errors.extend(caption_errors)
            continue
        result.append(
            ActionCaption(
                prompt_version=LLM2_OUTBOUND_PROMPT_VERSION,
                conversation_id=packet.conversation_id,
                turn_id=packet.turn_id,
                case_id=packet.case_id,
                audience=packet.audience,
                inventory_snapshot_id=action.inventory_snapshot_id or packet.inventory_snapshot_id,
                candidate_set_id=packet.candidate_set_id,
                listing_id=evidence.listing_id if evidence else action.listing_id,
                evidence_id=action.evidence_id,
                caption_id=str(raw.get("caption_id") or f"caption-{action_id}"),
                action_id=action.action_id,
                action_type=action.action_type,
                text=caption_text,
                display_order=_int_value(raw.get("display_order"), index),
                metadata={
                    "source": "llm2_shadow",
                    "evidence_type": evidence.evidence_type if evidence else "",
                },
            )
        )
    return _BuildResult(result, errors)


def _default_claims(packet: StructuredTaskPacket, bundle: ToolEvidenceBundle) -> list[Claim]:
    claims: list[Claim] = []
    for index, evidence in enumerate(bundle.evidence, start=1):
        if not evidence.evidence_id or not evidence.summary:
            continue
        if evidence.evidence_type in {"video", "image", "inventory_sheet"}:
            continue
        claims.append(
            Claim(
                prompt_version=LLM2_OUTBOUND_PROMPT_VERSION,
                conversation_id=packet.conversation_id,
                turn_id=packet.turn_id,
                case_id=packet.case_id,
                audience=packet.audience,
                inventory_snapshot_id=evidence.inventory_snapshot_id or packet.inventory_snapshot_id,
                candidate_set_id=packet.candidate_set_id,
                listing_id=evidence.listing_id,
                evidence_id=evidence.evidence_id,
                claim_id=f"claim-evidence-{index}",
                task_id=_first_task_id(packet),
                field=evidence.evidence_type,
                value=evidence.field_values or evidence.summary,
                evidence_ref=evidence.evidence_id,
                text=evidence.summary,
                support=[evidence.evidence_id],
                risk="low",
            )
        )
    return claims


def _default_action_captions(
    packet: StructuredTaskPacket,
    send_actions: list[SendAction],
    evidence_by_id: dict[str, EvidenceItem],
    candidate_labels: dict[str, str],
) -> list[ActionCaption]:
    captions: list[ActionCaption] = []
    for index, action in enumerate(send_actions, start=1):
        evidence = evidence_by_id.get(action.evidence_id)
        if action.action_type == "text":
            continue
        label = _candidate_label_for_action(action, evidence, candidate_labels)
        captions.append(
            ActionCaption(
                prompt_version=LLM2_OUTBOUND_PROMPT_VERSION,
                conversation_id=packet.conversation_id,
                turn_id=packet.turn_id,
                case_id=packet.case_id,
                audience=packet.audience,
                inventory_snapshot_id=action.inventory_snapshot_id or packet.inventory_snapshot_id,
                candidate_set_id=packet.candidate_set_id,
                listing_id=evidence.listing_id if evidence else action.listing_id,
                evidence_id=action.evidence_id,
                caption_id=f"caption-{action.action_id}",
                action_id=action.action_id,
                action_type=action.action_type,
                text=_oralized_action_text(action, evidence, index, label),
                display_order=index,
                metadata={
                    "source": "tool_evidence",
                    "evidence_type": evidence.evidence_type if evidence else "",
                },
            )
        )
    return captions


def _default_caption_text(action: SendAction, index: int) -> str:
    if action.action_type == "contract_contact":
        return "合同、定金和订房联系方式已由受控通道绑定。"
    if action.action_type == "viewing_password":
        return "看房密码已由受控通道绑定。"
    if action.action_type == "viewing_contact":
        return "看房联系号码已由受控通道绑定。"
    if action.action_type == "video":
        return f"这是第 {index} 个房间的视频。"
    if action.action_type == "image":
        return f"这是第 {index} 个房间的图片。"
    return f"发送动作 {index}"


def _oralized_action_text(action: SendAction, evidence: EvidenceItem | None, index: int, candidate_label: str = "") -> str:
    label = _evidence_room_label(evidence) or _action_room_label(action) or candidate_label
    if action.action_type in {"contract_contact", "viewing_password", "viewing_contact"}:
        return evidence.summary if evidence and evidence.summary else _default_caption_text(action, index)
    if action.action_type == "video":
        return f"这是{label}房间的视频。" if label else _default_caption_text(action, index)
    if action.action_type == "image":
        if evidence and evidence.evidence_type == "inventory_sheet":
            return "这是房源表。"
        return f"这是{label}房间的图片。" if label else _default_caption_text(action, index)
    return _default_caption_text(action, index)


def _evidence_room_label(evidence: EvidenceItem | None) -> str:
    if not evidence:
        return ""
    field_values = dict(evidence.field_values or {})
    metadata = dict(evidence.metadata or {})
    community = _first_text(field_values, metadata, keys=("community", "小区", "community_name"))
    room_no = _first_text(field_values, metadata, keys=("room_no", "房号", "room"))
    if community and room_no:
        return f"{community}{room_no}"
    if community:
        return community
    if room_no:
        return room_no
    return ""


def _action_room_label(action: SendAction) -> str:
    payload = dict(action.payload or {})
    metadata = dict(action.metadata or {})
    community = _first_text(payload, metadata, keys=("community", "小区", "community_name"))
    room_no = _first_text(payload, metadata, keys=("room_no", "房号", "room"))
    if community and room_no:
        return f"{community}{room_no}"
    return community or room_no


def _candidate_labels_by_listing(bundle: ToolEvidenceBundle) -> dict[str, str]:
    if not bundle.candidate_set:
        return {}
    result: dict[str, str] = {}
    for candidate in bundle.candidate_set.candidates:
        listing_id = str(candidate.listing_id or "").strip()
        if not listing_id:
            continue
        label = _candidate_room_label(candidate)
        if label:
            result[listing_id] = label
    return result


def _candidate_label_for_action(
    action: SendAction,
    evidence: EvidenceItem | None,
    candidate_labels: dict[str, str],
) -> str:
    listing_id = str(action.listing_id or (evidence.listing_id if evidence else "") or "").strip()
    return candidate_labels.get(listing_id, "")


def _candidate_room_label(candidate: Any) -> str:
    community = str(getattr(candidate, "community", "") or "").strip()
    room_no = str(getattr(candidate, "room_no", "") or "").strip()
    if community and room_no:
        return f"{community}{room_no}"
    return community or room_no


def _first_text(*payloads: dict[str, Any], keys: tuple[str, ...]) -> str:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _listing_count_label(count: int) -> str:
    return "几套" if count >= 2 else "套"


def _answered_task_ids(
    packet: StructuredTaskPacket,
    raw_ids: Any,
    claims: list[Claim],
    send_actions: list[SendAction],
    has_reply_text: bool,
) -> list[str]:
    valid_ids = [task.task_id for task in packet.tasks if task.task_id]
    valid_set = set(valid_ids)
    explicit = [task_id for task_id in _string_list(raw_ids) if task_id in valid_set]
    if explicit:
        return _dedupe(explicit)
    from_claims = [claim.task_id for claim in claims if claim.task_id in valid_set]
    if from_claims:
        return _dedupe(from_claims)
    if send_actions or has_reply_text:
        return valid_ids
    return []


def _failure_package(
    *,
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
    send_actions: list[SendAction],
    prompt_version: str,
    selfcheck_profile: str,
    reply_source: str,
    reasons: list[str],
    requested_strategy: ResponseStrategy,
    ignored_llm_send_actions: bool,
) -> PreparedOutboundPackage:
    safe_reasons = _dedupe([str(reason).strip() for reason in reasons if str(reason).strip()])
    retry_reason = "；".join(safe_reasons)
    return PreparedOutboundPackage(
        prompt_version=prompt_version,
        conversation_id=packet.conversation_id,
        turn_id=packet.turn_id,
        case_id=packet.case_id,
        audience=packet.audience,
        inventory_snapshot_id=packet.inventory_snapshot_id,
        candidate_set_id=packet.candidate_set_id,
        reply_text="",
        response_strategy=ResponseStrategy.RETRY,
        answered_task_ids=[],
        candidate_set=bundle.candidate_set,
        evidence_bundle=bundle,
        claims=[],
        action_captions=[],
        send_actions=send_actions,
        self_review=safe_artifact_payload(
            {
                "status": "retry",
                "source": LLM2_OUTBOUND_SELFCHECK_PROFILE,
                "retry_reason": retry_reason,
                "rewrite_retry_reason": retry_reason,
                "requested_response_strategy": requested_strategy.to_safe_dict(),
                "llm2_decides_media_targets": False,
                "send_actions_preserved": True,
                "ignored_llm_send_actions": ignored_llm_send_actions,
            }
        ),
        selfcheck_profile=selfcheck_profile,
        reply_source=reply_source,
    )


def _success_review(
    output_review: dict[str, Any],
    *,
    requested_strategy: ResponseStrategy,
    ignored_llm_send_actions: bool,
    claim_count: int,
    action_count: int,
) -> dict[str, Any]:
    review = safe_artifact_payload(dict(output_review or {}))
    review["status"] = "pass"
    review["source"] = str(review.get("source") or LLM2_OUTBOUND_SELFCHECK_PROFILE)
    review["requested_response_strategy"] = requested_strategy.to_safe_dict()
    review["llm2_decides_media_targets"] = False
    review["ignored_llm_send_actions"] = ignored_llm_send_actions
    review["claim_count"] = claim_count
    review["send_action_count"] = action_count
    return review


def _evidence_by_id(bundle: ToolEvidenceBundle) -> dict[str, EvidenceItem]:
    return {item.evidence_id: item for item in bundle.evidence if item.evidence_id}


def _is_high_risk_field(field: Any) -> bool:
    text = str(field or "").strip().lower()
    return _is_price_field(text) or text in _STATUS_FIELDS or text in _LINK_FIELDS or "密码" in text


def _is_price_field(field: Any) -> bool:
    text = str(field or "").strip().lower()
    return text in {item.lower() for item in _PRICE_FIELDS}


def _normalized_text(value: Any) -> str:
    return json.dumps(safe_artifact_payload(value), ensure_ascii=False, sort_keys=True, default=str)


def _first_task_id(packet: StructuredTaskPacket) -> str:
    return packet.tasks[0].task_id if packet.tasks else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _stable_hash(value: Any) -> str:
    payload = json.dumps(safe_artifact_payload(value), ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()
