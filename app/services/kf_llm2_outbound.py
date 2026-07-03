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
CONTROLLED_SLOT_ACTION_TYPES = {"contract_contact", "viewing_password", "viewing_contact"}

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

    guard_reasons.extend(_unsupported_high_risk_values(output, packet, bundle))
    evidence_by_id = _evidence_by_id(bundle)
    claim_result = _claims_from_output(packet, bundle, output.get("claims"), evidence_by_id)
    caption_result = _action_captions_from_output(packet, trusted_actions, output.get("action_captions"), evidence_by_id)
    controlled_slot_only = _only_controlled_slot_actions(trusted_actions)
    claim_errors = [] if controlled_slot_only else list(claim_result.errors)
    claim_items = [] if controlled_slot_only else list(claim_result.items)
    guard_reasons.extend(claim_errors)
    guard_reasons.extend(caption_result.errors)
    if not allow_deterministic_fallback:
        guard_reasons.extend(
            _missing_production_media_action_captions(
                trusted_actions,
                caption_result.items,
            )
        )
    guard_reasons.extend(_unsupported_plain_facts_in_reply(reply_text, bundle, claim_items))
    if (
        not allow_deterministic_fallback
        and not reply_text.strip()
        and (trusted_actions or claim_result.items or bundle.evidence)
    ):
        guard_reasons.append("llm2_output_missing_visible_reply")

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

    claims = claim_items or _default_claims(packet, bundle)
    action_captions = caption_result.items or _default_action_captions(packet, trusted_actions, evidence_by_id, _candidate_labels_by_listing(bundle))
    reply_text = _normalize_inventory_sheet_only_reply_text(reply_text, trusted_actions, evidence_by_id)
    reply_text, price_reply_template = _normalize_requested_payment_options_reply_text(reply_text, packet, bundle)
    reply_text, original_video_reply_template = _normalize_original_video_reply_text(
        reply_text,
        packet,
        bundle,
        trusted_actions,
    )
    reply_text, controlled_reply_template = _normalize_controlled_slot_reply_text(reply_text, trusted_actions)
    answered_task_ids = _answered_task_ids(packet, output.get("answered_task_ids"), claims, trusted_actions, bool(reply_text))
    self_review = _success_review(
        output_review,
        requested_strategy=strategy,
        ignored_llm_send_actions=ignored_llm_send_actions,
        claim_count=len(claims),
        action_count=len(trusted_actions),
        controlled_reply_template=controlled_reply_template or original_video_reply_template or price_reply_template,
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


def _only_controlled_slot_actions(actions: list[SendAction]) -> bool:
    return bool(actions) and all(action.action_type in CONTROLLED_SLOT_ACTION_TYPES for action in actions)


def _is_inventory_sheet_action(action: SendAction, evidence_by_id: dict[str, EvidenceItem]) -> bool:
    evidence = evidence_by_id.get(action.evidence_id)
    metadata = dict(action.metadata or {})
    return (
        action.action_type in {"inventory_sheet", "image", "send_inventory_sheet"}
        and (
            (evidence is not None and evidence.evidence_type == "inventory_sheet")
            or metadata.get("evidence_type") == "inventory_sheet"
        )
    )


def _only_inventory_sheet_actions(actions: list[SendAction], evidence_by_id: dict[str, EvidenceItem]) -> bool:
    return bool(actions) and all(_is_inventory_sheet_action(action, evidence_by_id) for action in actions)


def _normalize_inventory_sheet_only_reply_text(
    reply_text: str,
    actions: list[SendAction],
    evidence_by_id: dict[str, EvidenceItem],
) -> str:
    text = str(reply_text or "").strip()
    if not text or not _only_inventory_sheet_actions(actions, evidence_by_id):
        return reply_text
    if "房源表" in text or "空房表" in text or "库存表" in text:
        return reply_text
    return "房源表图片发你了，你可以让客户先整体看一下。"


def _normalize_controlled_slot_reply_text(reply_text: str, actions: list[SendAction]) -> tuple[str, str]:
    controlled_types = {str(action.action_type or "").strip() for action in actions}
    controlled_types &= CONTROLLED_SLOT_ACTION_TYPES
    if not controlled_types:
        return reply_text, ""

    parts: list[str] = []
    if "contract_contact" in controlled_types:
        parts.append("合同、定金和订房联系方式如下。")

    has_viewing_password = "viewing_password" in controlled_types
    has_viewing_contact = "viewing_contact" in controlled_types
    if has_viewing_password and has_viewing_contact:
        parts.append("看房密码和联系方式如下。")
    elif has_viewing_password:
        parts.append("看房密码如下。")
    elif has_viewing_contact:
        parts.append("看房需要联系确认，联系方式如下。")

    if not parts:
        return reply_text, ""
    template = "controlled_slot_reply"
    return "".join(parts), template


def _normalize_requested_payment_options_reply_text(
    reply_text: str,
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
) -> tuple[str, str]:
    task_text = _packet_task_search_text(packet)
    wants_pay1 = any(marker in task_text for marker in ("押一", "押1", "pay1", "rent_pay1"))
    wants_pay2 = any(marker in task_text for marker in ("押二", "押2", "pay2", "rent_pay2"))
    if not (wants_pay1 and wants_pay2):
        return reply_text, ""
    rows = _payment_option_rows(bundle)
    if not rows:
        return reply_text, ""
    lines: list[str] = []
    for index, row in enumerate(rows[:8], start=1):
        label = str(row.get("label") or "").strip() or f"第{index}套"
        rent_pay1 = str(row.get("rent_pay1") or "").strip()
        rent_pay2 = str(row.get("rent_pay2") or "").strip()
        parts: list[str] = []
        if rent_pay1:
            parts.append(f"押一付一{rent_pay1}元/月")
        if rent_pay2:
            parts.append(f"押二付一{rent_pay2}元/月")
        if not parts:
            continue
        lines.append(f"{index}. {label}：{'，'.join(parts)}")
    if not lines:
        return reply_text, ""
    return "这几套价格如下：\n" + "\n".join(lines), "payment_options_reply"


def _normalize_original_video_reply_text(
    reply_text: str,
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
    actions: list[SendAction],
) -> tuple[str, str]:
    task_text = _packet_task_search_text(packet)
    if not any(marker in task_text for marker in ("原视频", "原片", "高清", "源文件", "下载链接", "太糊", "模糊", "保存", "转发")):
        return reply_text, ""
    if _bundle_has_original_video_source(bundle):
        return reply_text, ""
    has_video_action = any(str(action.action_type or "").strip() == "video" for action in actions)
    if has_video_action:
        return "目前工具证据里没有原视频/高清下载链接；这套企业微信可发送视频如下。", "original_video_no_source_reply"
    return "目前工具证据里没有原视频/高清下载链接。你回房源序号或小区+房号后，我再按素材库查。", "original_video_no_source_reply"


def _bundle_has_original_video_source(bundle: ToolEvidenceBundle) -> bool:
    raw = bundle.raw_tool_result if isinstance(bundle.raw_tool_result, dict) else {}
    for key in ("original_video_paths", "original_video_urls", "material_page_urls"):
        if raw.get(key):
            return True
    for item in bundle.evidence:
        evidence_type = str(item.evidence_type or "").strip().lower()
        if evidence_type in {"original_video", "original_video_url", "material_page", "material_page_url"}:
            return True
        values = dict(item.field_values or {})
        metadata = dict(item.metadata or {})
        for payload in (values, metadata):
            if payload.get("original_video_url") or payload.get("material_page_url") or payload.get("original_video_path"):
                return True
    return False


def _payment_option_rows(bundle: ToolEvidenceBundle) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def append_row(*, key: str, label: str, rent_pay1: Any, rent_pay2: Any) -> None:
        safe_label = str(label or "").strip()
        safe_pay1 = _digits_text(rent_pay1)
        safe_pay2 = _digits_text(rent_pay2)
        if not safe_label or (not safe_pay1 and not safe_pay2):
            return
        row_key = key or safe_label
        if row_key in seen:
            return
        seen.add(row_key)
        rows.append({"label": safe_label, "rent_pay1": safe_pay1, "rent_pay2": safe_pay2})

    for item in bundle.evidence:
        evidence_type = str(item.evidence_type or "").strip()
        if evidence_type not in {"inventory_listing", "inventory_candidate"}:
            continue
        values = dict(item.field_values or {})
        label = _evidence_room_label(item)
        append_row(
            key=str(item.listing_id or item.evidence_id or "").strip(),
            label=label,
            rent_pay1=values.get("rent_pay1") or values.get("rent_yayi") or values.get("押一付一"),
            rent_pay2=values.get("rent_pay2") or values.get("rent_yaer") or values.get("押二付一"),
        )
    if rows or not bundle.candidate_set:
        return rows
    for candidate in bundle.candidate_set.candidates:
        append_row(
            key=str(candidate.listing_id or candidate.candidate_number or "").strip(),
            label=_candidate_room_label(candidate),
            rent_pay1=getattr(candidate, "rent_pay1", None),
            rent_pay2=getattr(candidate, "rent_pay2", None),
        )
    return rows


def _digits_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"[1-9]\d{2,5}", text)
    return match.group(0) if match else text


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
    reply_text = _default_reply_text(packet, bundle, send_actions)
    return {
        "reply_text": reply_text,
        "answered_task_ids": _answered_task_ids(packet, [], claims, send_actions, bool(reply_text or claims or send_actions)),
        "claims": [claim.to_legacy_dict() for claim in claims],
        "action_captions": [caption.to_legacy_dict() for caption in captions],
        "self_review": {
            "status": "pass",
            "source": "deterministic_llm2_shadow_fallback",
            "llm2_decides_media_targets": False,
        },
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


def _is_short_acknowledgement_text(text: str) -> bool:
    normalized = re.sub(r"[\s,，。.!！?？~～、]+", "", str(text or "").strip().lower())
    normalized = re.sub(r"(啦|哈|呀|喔|哦)+$", "", normalized)
    if not normalized:
        return False
    tokens = ("okay", "ok", "好的", "好滴", "嗯嗯", "谢谢", "辛苦", "收到", "可以", "好", "嗯", "行")
    if normalized in set(tokens):
        return True
    if len(normalized) > 12:
        return False

    def can_segment(offset: int) -> bool:
        if offset == len(normalized):
            return True
        return any(
            normalized.startswith(token, offset) and can_segment(offset + len(token))
            for token in tokens
        )

    return can_segment(0)


def _has_greeting_reply_compose_signal(packet: StructuredTaskPacket) -> bool:
    for task in packet.tasks or []:
        task_type = str(getattr(task, "task_type", "") or "").strip()
        if _is_literal_greeting_text(getattr(task, "user_text", "")):
            return True
        if task_type != "reply_compose_signal":
            continue
    return _is_literal_greeting_text(getattr(packet, "rewritten_query", ""))


def _has_acknowledgement_reply_compose_signal(packet: StructuredTaskPacket) -> bool:
    for task in packet.tasks or []:
        task_type = str(getattr(task, "task_type", "") or "").strip()
        if task_type != "reply_compose_signal":
            continue
        if _is_short_acknowledgement_text(getattr(task, "user_text", "")):
            return True
    return _is_short_acknowledgement_text(getattr(packet, "rewritten_query", ""))


def _default_reply_text(
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
    send_actions: list[SendAction],
) -> str:
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
    if _has_greeting_reply_compose_signal(packet):
        return "你好，在的。你直接发小区、房号、预算、房源表、图片或视频需求，我马上帮你查。"
    if _has_acknowledgement_reply_compose_signal(packet):
        return "好的，有需要你直接发小区、房号、预算、图片、视频或房源表，我继续帮你查。"
    prioritized = _priority_evidence_reply_text(packet, bundle)
    if prioritized:
        return prioritized
    target_error_reply = _target_error_reply_text(packet, bundle)
    if target_error_reply:
        return target_error_reply
    inventory_candidate_reply = _inventory_candidate_reply_text(bundle)
    if inventory_candidate_reply:
        return inventory_candidate_reply
    for item in bundle.evidence:
        if item.summary:
            return item.summary
    raw_tool_result = bundle.raw_tool_result if isinstance(bundle.raw_tool_result, dict) else {}
    actions = set(_string_list(raw_tool_result.get("actions")))
    if (
        "search_inventory" in actions
        and not raw_tool_result.get("inventory_rows")
        and not raw_tool_result.get("target_rows")
        and not raw_tool_result.get("inventory_read_error")
    ):
        return "我按这个条件查了，最新房源表里暂时没有匹配的房源。你可以放宽预算、区域或户型，我再帮你筛。"
    return ""


def _inventory_candidate_reply_text(bundle: ToolEvidenceBundle) -> str:
    items = [
        item
        for item in bundle.evidence
        if str(item.evidence_type or "").strip() == "inventory_candidate"
    ]
    if not items:
        return ""
    lines: list[str] = []
    for index, item in enumerate(items[:8], start=1):
        values = item.field_values if isinstance(item.field_values, dict) else {}
        label = _evidence_room_label(item)
        layout = str(values.get("layout_description") or values.get("layout") or "").strip()
        rent_pay1 = str(values.get("rent_pay1") or "").strip()
        rent_pay2 = str(values.get("rent_pay2") or "").strip()
        parts = [part for part in (label, layout) if part]
        if rent_pay1:
            parts.append(f"押一付一{rent_pay1}")
        if rent_pay2:
            parts.append(f"押二付一{rent_pay2}")
        if not parts:
            parts.append(str(item.summary or "").strip() or "这套房源")
        lines.append(f"{index}. {'，'.join(parts)}")
    return "我按房源表查到这几套：" + "；".join(lines) + "。"


def _priority_evidence_reply_text(packet: StructuredTaskPacket, bundle: ToolEvidenceBundle) -> str:
    task_text = _packet_task_search_text(packet)
    preferred_types: list[str] = []
    if any(marker in task_text for marker in ("send_image", "image", "图片", "照片")):
        preferred_types.append("missing_media")
    if any(marker in task_text for marker in ("send_video", "video", "视频", "原视频", "高清")):
        preferred_types.append("missing_media")
    if any(marker in task_text for marker in ("viewing_guidance", "viewing", "看房", "密码", "门锁", "门禁")):
        preferred_types.extend(["viewing_password", "viewing_contact", "viewing_guidance"])
    if any(marker in task_text for marker in ("deposit_policy", "send_deposit_policy", "免押", "无忧住", "芝麻", "押金", "服务费")):
        preferred_types.append("deposit_policy")
    for evidence_type in preferred_types:
        for item in bundle.evidence:
            if str(item.evidence_type or "").strip() == evidence_type and str(item.summary or "").strip():
                summary = str(item.summary or "").strip()
                if evidence_type == "deposit_policy" and "信用额度" not in summary and "租房板块" not in summary:
                    summary += "客户可以打开支付宝：我的 - 芝麻信用 - 我的 - 信用额度 - 租房板块申请额度，有额度再继续走免押流程。"
                return summary
    return ""


def _target_error_reply_text(packet: StructuredTaskPacket, bundle: ToolEvidenceBundle) -> str:
    blocked_action = _unbound_target_action_phrase(packet)
    for item in bundle.evidence:
        evidence_type = str(item.evidence_type or "").strip()
        values = item.field_values if isinstance(item.field_values, dict) else {}
        if evidence_type == "selection_error":
            indices = [
                int(index)
                for index in values.get("requested_indices") or []
                if str(index).isdigit() and int(index) > 0
            ]
            selected_text = "、".join(f"第{index}套" for index in indices) or "这个序号"
            return (
                f"我这边没法按{selected_text}准确定位，先不{blocked_action}。"
                "你发小区+房号，或者让我重新列一遍后再按序号发。"
            )
        if evidence_type == "missing_target":
            return (
                f"我这边还没定位到具体房源，先不{blocked_action}。"
                "你发小区+房号，或者让我重新列一遍后再按序号发。"
            )
        if evidence_type == "field_target_error":
            field = str(values.get("field") or _requested_media_name(packet) or "这个信息").strip()
            if str(values.get("reason") or "") == "original_video_followup_missing_stable_video_target":
                return "我这边还没定位到要继续追的那套视频，先不发原视频。你回房源序号或小区+房号后，我再按素材库查。"
            return f"我这边还没定位到具体房源，先不发{field}。你发小区+房号，或者让我重新列一遍后再按序号查。"
    return ""


def _unbound_target_action_phrase(packet: StructuredTaskPacket) -> str:
    task_text = _packet_task_search_text(packet)
    if any(marker in task_text for marker in ("send_image", "image", "图片", "照片")):
        return "发图片"
    if any(marker in task_text for marker in ("send_video", "video", "视频", "原视频", "高清")):
        return "发视频"
    if any(marker in task_text for marker in ("水电", "水费", "电费", "utility")):
        return "核对水电"
    if any(marker in task_text for marker in ("价格", "租金", "多少钱", "哪个低", "哪套低", "更低", "便宜", "押一付一", "押二付一", "price", "rent")):
        return "比较价格"
    if any(marker in task_text for marker in ("看房", "密码", "门锁", "viewing")):
        return "说看房方式"
    return "查这个信息"


def _requested_media_name(packet: StructuredTaskPacket) -> str:
    task_text = _packet_task_search_text(packet)
    if any(marker in task_text for marker in ("send_image", "image", "图片", "照片")):
        return "图片"
    if any(marker in task_text for marker in ("send_video", "video", "视频", "原视频", "高清")):
        return "视频"
    return "素材"


def _packet_task_search_text(packet: StructuredTaskPacket) -> str:
    parts: list[str] = []
    for task in packet.tasks:
        parts.extend(
            [
                str(getattr(task, "task_type", "") or ""),
                str(getattr(task, "user_text", "") or ""),
                " ".join(str(tool) for tool in getattr(task, "required_tools", []) or []),
            ]
        )
    parts.append(str(packet.rewritten_query or ""))
    return " ".join(part for part in parts if part).lower()


def _unsupported_high_risk_values(
    output: dict[str, Any],
    packet: StructuredTaskPacket,
    bundle: ToolEvidenceBundle,
) -> list[str]:
    text = json.dumps(safe_artifact_payload(output), ensure_ascii=False, default=str)
    reasons: list[str] = []
    if _URL_RE.search(text):
        reasons.append("llm2_output_contains_link_use_evidence_slot")
    if _PHONE_RE.search(text):
        reasons.append("llm2_output_contains_full_phone")
    if _PASSWORD_RE.search(text):
        reasons.append("llm2_output_contains_password_like_value")
    allowed_prices = _allowed_price_tokens(packet, bundle)
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


def _allowed_price_tokens(packet: StructuredTaskPacket, bundle: ToolEvidenceBundle) -> set[str]:
    text_parts: list[str] = []
    for item in bundle.evidence:
        text_parts.append(item.summary)
        for key, value in item.field_values.items():
            if _is_price_field(key):
                text_parts.append(str(value))
    text_parts.append(json.dumps(bundle.field_values, ensure_ascii=False, default=str))
    return set(_money_tokens("\n".join(text_parts))) | _constraint_price_tokens(packet) | {
        str(value).strip()
        for item in bundle.evidence
        for key, value in item.field_values.items()
        if _is_price_field(key) and str(value).strip().isdigit()
    }


def _constraint_price_tokens(packet: StructuredTaskPacket) -> set[str]:
    payload = {
        "rewritten_query": packet.rewritten_query,
        "inherited_constraints": packet.inherited_constraints,
        "replaced_constraints": packet.replaced_constraints,
        "tasks": [
            {
                "user_text": task.user_text,
                "constraints": task.constraints,
            }
            for task in packet.tasks
        ],
    }
    tokens: set[str] = set(_money_tokens(json.dumps(payload, ensure_ascii=False, default=str)))

    def visit(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*path, str(key)))
            return
        if isinstance(value, list):
            for item in value:
                visit(item, path)
            return
        key_text = " ".join(path).lower()
        if not any(marker in key_text for marker in ("budget", "price", "rent", "预算", "价格", "租金", "押一", "押二")):
            return
        for token in re.findall(r"(?<!\d)[1-9]\d{2,5}(?!\d)", str(value)):
            tokens.add(token)

    visit(payload)
    return tokens


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
    single_task_id = next(iter(valid_tasks), "") if len(valid_tasks) == 1 else ""
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
            if single_task_id:
                task_id = single_task_id
            else:
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


def _missing_production_media_action_captions(
    send_actions: list[SendAction],
    captions: list[ActionCaption],
) -> list[str]:
    media_action_types = {"video", "image", "inventory_sheet", "send_video", "send_image", "send_inventory_sheet"}
    captioned_action_ids = {caption.action_id for caption in captions}
    return [
        f"production_missing_action_caption:{action.action_id}"
        for action in send_actions
        if str(action.action_type or "").strip() in media_action_types
        and action.action_id not in captioned_action_ids
    ]


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
    has_caption_required_action = any(action.action_type in {"video", "image", "inventory_sheet"} for action in send_actions)
    result: list[ActionCaption] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_captions, start=1):
        if not isinstance(raw, dict):
            continue
        action_id = str(raw.get("action_id") or "").strip()
        action = actions_by_id.get(action_id)
        if not action:
            if has_caption_required_action:
                errors.append(f"caption_{index}_unknown_action_id")
            continue
        if action.action_type in CONTROLLED_SLOT_ACTION_TYPES:
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
        evidence_type = str(evidence.evidence_type or "").strip()
        if evidence_type in {"video", "image", "inventory_sheet"}:
            continue
        summary_backed_types = {
            "missing_media",
            "viewing_guidance",
            "viewing_contact",
            "viewing_password",
            "target_error",
            "candidate_selection_error",
            "field_target_error",
        }
        if evidence_type in summary_backed_types:
            continue
        claim_value = evidence.field_values or evidence.summary
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
                field=evidence_type,
                value=claim_value,
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
        if action.action_type == "text" or action.action_type in CONTROLLED_SLOT_ACTION_TYPES:
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
        return "合同、定金和订房联系方式已确认。"
    if action.action_type == "viewing_password":
        return "看房密码已确认。"
    if action.action_type == "viewing_contact":
        return "看房联系号码已确认。"
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
    controlled_reply_template: str = "",
) -> dict[str, Any]:
    review = safe_artifact_payload(dict(output_review or {}))
    review["status"] = "pass"
    review["source"] = str(review.get("source") or LLM2_OUTBOUND_SELFCHECK_PROFILE)
    review["requested_response_strategy"] = requested_strategy.to_safe_dict()
    review["llm2_decides_media_targets"] = False
    review["ignored_llm_send_actions"] = ignored_llm_send_actions
    review["claim_count"] = claim_count
    review["send_action_count"] = action_count
    if controlled_reply_template:
        review["controlled_reply_template"] = controlled_reply_template
        review["reply_text_owner"] = "controlled_template"
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
