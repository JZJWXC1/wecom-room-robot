from __future__ import annotations

import json

from app.services.kf_contracts import (
    CandidateItem,
    CandidateSet,
    EvidenceItem,
    ResponseStrategy,
    SendAction,
    StructuredTaskPacket,
    TaskAtom,
    ToolEvidenceBundle,
)
from app.services.kf_llm2_outbound import compose_kf_outbound
from app.services.kf_outbound_validation import ValidationStatus, validate_prepared_outbound_package


def _task_packet() -> StructuredTaskPacket:
    return StructuredTaskPacket(
        conversation_id="conv-llm2",
        turn_id="turn-llm2",
        case_id="case-llm2",
        inventory_snapshot_id="snap-llm2",
        candidate_set_id="cand-llm2",
        response_strategy=ResponseStrategy.SEND_MEDIA,
        tasks=[
            TaskAtom(
                task_id="task-video",
                task_type="send_video",
                user_text="这套视频发我",
                required_tools=["media.video"],
                constraints={"candidate_numbers": [1]},
            )
        ],
    )


def _evidence_bundle() -> ToolEvidenceBundle:
    candidate_set = CandidateSet(
        conversation_id="conv-llm2",
        turn_id="turn-llm2",
        case_id="case-llm2",
        inventory_snapshot_id="snap-llm2",
        candidate_set_id="cand-llm2",
        candidates=[
            CandidateItem(
                candidate_number=1,
                listing_id="lst-801b",
                evidence_id="evd-listing-1",
                community="棠润府",
                room_no="15-2-801B",
                rent_pay1=3800,
            )
        ],
    )
    return ToolEvidenceBundle(
        conversation_id="conv-llm2",
        turn_id="turn-llm2",
        case_id="case-llm2",
        inventory_snapshot_id="snap-llm2",
        candidate_set_id="cand-llm2",
        tool_name="llm2.test.evidence",
        evidence=[
            EvidenceItem(
                evidence_id="evd-listing-1",
                listing_id="lst-801b",
                inventory_snapshot_id="snap-llm2",
                evidence_type="inventory_listing",
                summary="棠润府 15-2-801B 押一付一 3800",
                field_values={"rent_pay1": 3800, "community": "棠润府", "room_no": "15-2-801B"},
                metadata={"candidate_number": 1},
            ),
            EvidenceItem(
                evidence_id="evd-video-1",
                listing_id="lst-801b",
                inventory_snapshot_id="snap-llm2",
                evidence_type="video",
                summary="棠润府 15-2-801B 视频素材",
                source_record_id="room_database/video/hashed-demo.mp4",
                metadata={"candidate_number": 1},
            ),
        ],
        candidate_set=candidate_set,
        raw_tool_result={"password": "9999#", "token": "abc123"},
    )


def test_compose_kf_outbound_accepts_supported_llm2_text_and_preserves_bindings() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "有的，这是棠润府 15-2-801B 的视频，押一付一 3800。",
            "answered_task_ids": ["task-video", "task-not-real"],
            "claims": [
                {
                    "claim_id": "claim-rent",
                    "task_id": "task-video",
                    "field": "rent_pay1",
                    "value": 3800,
                    "evidence_ref": "evd-listing-1",
                    "listing_id": "lst-801b",
                    "candidate_number": 1,
                    "text": "棠润府 15-2-801B 押一付一 3800",
                }
            ],
            "action_captions": [
                {
                    "action_id": "send-video-1",
                    "text": "这是棠润府 15-2-801B 的视频。",
                }
            ],
            "self_review": {"status": "pass", "reason": ""},
        },
    )

    assert package.reply_text.startswith("有的")
    assert package.answered_task_ids == ["task-video"]
    assert package.candidate_set.candidates[0].candidate_number == 1
    assert package.candidate_set.candidates[0].listing_id == "lst-801b"
    assert [action.action_id for action in package.send_actions] == ["send-video-1"]
    assert package.send_actions[0].evidence_id == "evd-video-1"
    assert package.claims[0].listing_id == "lst-801b"
    assert package.action_captions[0].action_id == "send-video-1"
    assert package.self_review["llm2_decides_media_targets"] is False


def test_production_contract_retries_when_llm2_omits_visible_reply_for_evidence() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={"reply_text": "", "self_review": {"status": "pass", "reason": ""}},
        allow_deterministic_fallback=False,
    )

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.reply_text == ""
    assert "llm2_output_missing_visible_reply" in package.self_review["retry_reason"]
    assert [action.action_id for action in package.send_actions] == ["send-video-1"]


def test_deterministic_fallback_replies_to_short_acknowledgement_signal() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-ack",
        turn_id="turn-ack",
        case_id="case-ack",
        inventory_snapshot_id="snap-ack",
        candidate_set_id="",
        response_strategy=ResponseStrategy.ANSWER,
        rewritten_query="好的",
        tasks=[
            TaskAtom(
                task_id="task-ack",
                task_type="reply_compose_signal",
                user_text="好的",
                required_tools=["reply.compose"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-ack",
        turn_id="turn-ack",
        case_id="case-ack",
        inventory_snapshot_id="snap-ack",
        candidate_set_id="",
        tool_name="llm2.test.ack",
        evidence=[],
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.ANSWER,
        llm_output={},
        allow_deterministic_fallback=True,
    )

    assert package.reply_text.startswith("好的")
    assert "房源表" in package.reply_text
    assert package.response_strategy == ResponseStrategy.ANSWER
    assert package.self_review["status"] == "pass"


def test_inventory_sheet_only_reply_text_is_normalized_when_llm2_calls_it_image() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-sheet",
        turn_id="turn-sheet",
        case_id="case-sheet",
        inventory_snapshot_id="snap-sheet",
        candidate_set_id="",
        response_strategy=ResponseStrategy.SEND_MEDIA,
        tasks=[
            TaskAtom(
                task_id="task-sheet",
                task_type="send_inventory_sheet",
                user_text="发最新房源表图片，不要文字列表",
                required_tools=["inventory.sheet_artifact"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-sheet",
        turn_id="turn-sheet",
        case_id="case-sheet",
        inventory_snapshot_id="snap-sheet",
        candidate_set_id="",
        tool_name="llm2.test.sheet",
        evidence=[
            EvidenceItem(
                evidence_id="evd-sheet-1",
                inventory_snapshot_id="snap-sheet",
                evidence_type="inventory_sheet",
                summary="最新房源表 PNG 第 1 页",
                source_record_id="room_database/inventory-1.png",
            )
        ],
    )
    action = SendAction(
        conversation_id="conv-sheet",
        turn_id="turn-sheet",
        case_id="case-sheet",
        inventory_snapshot_id="snap-sheet",
        evidence_id="evd-sheet-1",
        action_id="send-inventory-sheet-1",
        action_type="image",
        metadata={"evidence_type": "inventory_sheet"},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "这是这几套对应的图片。",
            "action_captions": [
                {"action_id": "send-inventory-sheet-1", "text": "这是房源表。"}
            ],
            "self_review": {"status": "pass", "reason": ""},
        },
        send_actions=[action],
    )

    assert package.reply_text == "房源表图片发你了，你可以让客户先整体看一下。"
    assert package.action_captions[0].text == "这是房源表。"


def test_compose_kf_outbound_repairs_single_task_claim_id_without_changing_evidence() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "有的，这是棠润府 15-2-801B 的视频，押一付一 3800。",
            "claims": [
                {
                    "claim_id": "claim-rent",
                    "task_id": "task-1",
                    "field": "rent_pay1",
                    "value": 3800,
                    "evidence_ref": "evd-listing-1",
                    "listing_id": "lst-801b",
                    "candidate_number": 1,
                    "text": "棠润府 15-2-801B 押一付一 3800",
                }
            ],
            "action_captions": [
                {
                    "action_id": "send-video-1",
                    "text": "这是棠润府 15-2-801B 的视频。",
                }
            ],
            "self_review": {"status": "pass", "reason": ""},
        },
    )

    assert package.response_strategy != ResponseStrategy.RETRY
    assert package.claims[0].task_id == "task-video"
    assert package.claims[0].evidence_ref == "evd-listing-1"
    assert package.answered_task_ids == ["task-video"]


def test_compose_kf_outbound_allows_budget_tokens_from_task_constraints() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-budget",
        turn_id="turn-budget",
        case_id="case-budget",
        inventory_snapshot_id="snap-budget",
        response_strategy=ResponseStrategy.ANSWER,
        rewritten_query="1500到2200的一室",
        tasks=[
            TaskAtom(
                task_id="task-search",
                task_type="inventory_search",
                user_text="1500到2200的一室",
                constraints={"budget_range": {"min": 1500, "max": 2200}, "layout": "一室"},
                required_tools=["inventory.search"],
            )
        ],
    )

    package = compose_kf_outbound(
        packet,
        _evidence_bundle(),
        ResponseStrategy.ANSWER,
        llm_output={
            "reply_text": "按你1500到2200的预算，有匹配的一室可以看。",
            "claims": [],
            "action_captions": [
                {
                    "action_id": "send-video-1",
                    "text": "这是棠润府 15-2-801B 的视频。",
                }
            ],
            "self_review": {"status": "pass", "reason": ""},
        },
    )

    assert package.response_strategy != ResponseStrategy.RETRY
    assert package.reply_text.startswith("按你1500到2200")
    assert package.answered_task_ids == ["task-search"]


def test_deterministic_llm2_shadow_fallback_is_oralized_and_l3_clean() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
    )
    validation = validate_prepared_outbound_package(package, task_packet=_task_packet())

    assert package.reply_text == "这是棠润府15-2-801B房间的视频。"
    assert package.action_captions[0].text == "这是棠润府15-2-801B房间的视频。"
    assert "准备好" not in package.reply_text
    assert validation.status == ValidationStatus.PASS
    assert not validation.l3_rewrite_reasons


def test_deterministic_llm2_fallback_replies_for_empty_inventory_search() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-empty-search",
        turn_id="turn-empty-search",
        case_id="case-empty-search",
        inventory_snapshot_id="snap-empty-search",
        response_strategy=ResponseStrategy.TOOL_FIRST,
        tasks=[
            TaskAtom(
                task_id="task-search",
                task_type="inventory_search",
                user_text="独卫优先",
                required_tools=["inventory.search"],
                constraints={"feature": "独卫"},
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-empty-search",
        turn_id="turn-empty-search",
        case_id="case-empty-search",
        inventory_snapshot_id="snap-empty-search",
        tool_name="llm2.test.empty_search",
        raw_tool_result={
            "actions": ["search_inventory", "generate_reply"],
            "inventory_rows": [],
            "target_rows": [],
        },
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.TOOL_FIRST,
        llm_output={},
        send_actions=[],
        allow_deterministic_fallback=True,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert validation.status == ValidationStatus.PASS
    assert "暂时没有匹配" in package.reply_text


def test_deterministic_llm2_fallback_prioritizes_missing_image_media_over_listing_summary() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-missing-image",
        turn_id="turn-missing-image",
        case_id="case-missing-image",
        inventory_snapshot_id="snap-missing-image",
        response_strategy=ResponseStrategy.SEND_MEDIA,
        tasks=[
            TaskAtom(
                task_id="task-image",
                task_type="send_image",
                user_text="图片也发",
                required_tools=["media.image"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-missing-image",
        turn_id="turn-missing-image",
        case_id="case-missing-image",
        inventory_snapshot_id="snap-missing-image",
        tool_name="llm2.test.missing_image",
        evidence=[
            EvidenceItem(
                evidence_id="evd-candidate-1",
                listing_id="lst-801b",
                evidence_type="inventory_candidate",
                summary="棠润府15-2-801B还在租。",
            ),
            EvidenceItem(
                evidence_id="evd-missing-media-1",
                listing_id="lst-801b",
                evidence_type="missing_media",
                summary="棠润府15-2-801B 暂未找到可发送图片。",
                field_values={"media_kind": "image", "label": "棠润府15-2-801B"},
            ),
        ],
        raw_tool_result={"actions": ["search_inventory", "context_tools", "send_image", "explain_missing_media"]},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.SEND_MEDIA,
        llm_output={},
        send_actions=[],
        allow_deterministic_fallback=True,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert validation.status == ValidationStatus.PASS
    assert "暂未找到" in package.reply_text
    assert "图片" in package.reply_text


def test_deterministic_llm2_fallback_prioritizes_viewing_guidance_over_listing_summary() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-viewing",
        turn_id="turn-viewing",
        case_id="case-viewing",
        inventory_snapshot_id="snap-viewing",
        response_strategy=ResponseStrategy.TOOL_FIRST,
        tasks=[
            TaskAtom(
                task_id="task-viewing",
                task_type="viewing_guidance",
                user_text="3号看房方式是什么",
                required_tools=["viewing.policy"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-viewing",
        turn_id="turn-viewing",
        case_id="case-viewing",
        inventory_snapshot_id="snap-viewing",
        tool_name="llm2.test.viewing",
        evidence=[
            EvidenceItem(
                evidence_id="evd-candidate-1",
                listing_id="lst-1102",
                evidence_type="inventory_candidate",
                summary="石桥铭苑6-1102押一付一4800。",
            ),
            EvidenceItem(
                evidence_id="evd-controlled-viewing-guidance-1",
                listing_id="lst-1102",
                evidence_type="viewing_guidance",
                summary="石桥铭苑6-1102 可以按看房方式自助查看；具体开门信息按受控通道单独确认。",
                field_values={"room": "石桥铭苑6-1102", "has_password": True, "needs_contact": False},
                metadata={"controlled_channel": "viewing_guidance", "evidence_bound": True},
            ),
        ],
        raw_tool_result={"actions": ["search_inventory", "context_tools", "explain_unavailable_viewing"]},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.TOOL_FIRST,
        llm_output={},
        send_actions=[],
        allow_deterministic_fallback=True,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert validation.status == ValidationStatus.PASS
    assert "看房方式" in package.reply_text
    assert "押一付一4800" not in package.reply_text


def test_non_media_controlled_action_ignores_unknown_llm2_caption_id() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-viewing-contact",
        turn_id="turn-viewing-contact",
        case_id="case-viewing-contact",
        inventory_snapshot_id="snap-viewing-contact",
        response_strategy=ResponseStrategy.TOOL_FIRST,
        tasks=[
            TaskAtom(
                task_id="task-password-contact",
                task_type="viewing_guidance",
                user_text="密码多少",
                required_tools=["viewing.policy"],
            )
        ],
    )
    evidence_id = "evd-controlled-viewing-contact-general-1"
    action_id = "send-controlled-viewing-contact-general-1"
    bundle = ToolEvidenceBundle(
        conversation_id="conv-viewing-contact",
        turn_id="turn-viewing-contact",
        case_id="case-viewing-contact",
        inventory_snapshot_id="snap-viewing-contact",
        tool_name="llm2.test.viewing_contact",
        evidence=[
            EvidenceItem(
                evidence_id=evidence_id,
                evidence_type="viewing_contact",
                summary="看房或密码异常需要联系确认。",
                field_values={"room": "看房/密码异常", "needs_contact": True},
                metadata={"controlled_channel": "viewing_contact", "evidence_bound": True},
            )
        ],
        raw_tool_result={"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.TOOL_FIRST,
        llm_output={
            "reply_text": "密码没有或者不对的话，联系管家确认一下。",
            "claims": [
                {
                    "claim_id": "claim-bad-viewing-contact",
                    "evidence_ref": "viewing_contact",
                    "field": "viewing_contact",
                    "value": "联系管家确认",
                    "text": "密码没有或者不对的话，联系管家确认一下。",
                }
            ],
            "action_captions": [{"action_id": "viewing_contact", "text": "联系管家确认。"}],
        },
        send_actions=[
            SendAction(
                action_id=action_id,
                action_type="viewing_contact",
                evidence_id=evidence_id,
                metadata={"controlled_channel": "viewing_contact", "evidence_bound": True},
            )
        ],
        allow_deterministic_fallback=False,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert package.self_review["status"] == "pass"
    assert package.claims == []
    assert package.action_captions == []
    assert validation.status == ValidationStatus.PASS


def test_deterministic_llm2_fallback_prioritizes_deposit_policy_over_listing_summary() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-deposit",
        turn_id="turn-deposit",
        case_id="case-deposit",
        inventory_snapshot_id="snap-deposit",
        response_strategy=ResponseStrategy.TOOL_FIRST,
        tasks=[
            TaskAtom(
                task_id="task-deposit",
                task_type="deposit_policy",
                user_text="能免押吗",
                required_tools=["policy.deposit"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-deposit",
        turn_id="turn-deposit",
        case_id="case-deposit",
        inventory_snapshot_id="snap-deposit",
        tool_name="llm2.test.deposit",
        evidence=[
            EvidenceItem(
                evidence_id="evd-candidate-1",
                listing_id="lst-1606b",
                evidence_type="inventory_candidate",
                summary="候选1 星桥锦绣嘉苑 20-1606B 租金2000",
            ),
            EvidenceItem(
                evidence_id="evd-rule-deposit-policy-1",
                evidence_type="deposit_policy",
                summary="免押是支付宝无忧住芝麻信用评估，不是免费免押；符合风控后需支付押金金额 5.5%-8% 的免押服务费。",
                field_values={"platform": "支付宝无忧住", "fee_rate": "5.5%-8%"},
            ),
        ],
        raw_tool_result={"actions": ["search_inventory", "send_deposit_policy", "generate_reply"]},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.TOOL_FIRST,
        llm_output={},
        send_actions=[],
        allow_deterministic_fallback=True,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert validation.status == ValidationStatus.PASS
    assert "免押是支付宝无忧住" in package.reply_text
    assert "候选1" not in package.reply_text


def test_deterministic_llm2_fallback_replies_for_greeting_compose_signal() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-greeting",
        turn_id="turn-greeting",
        case_id="case-greeting",
        inventory_snapshot_id="snap-greeting",
        response_strategy=ResponseStrategy.ANSWER,
        rewritten_query="你好，在吗",
        tasks=[
            TaskAtom(
                task_id="task-greeting",
                task_type="reply_compose_signal",
                user_text="你好，在吗",
                required_tools=["reply.compose"],
            )
        ],
    )
    bundle = ToolEvidenceBundle(
        conversation_id="conv-greeting",
        turn_id="turn-greeting",
        case_id="case-greeting",
        inventory_snapshot_id="snap-greeting",
        tool_name="llm2.test.greeting",
        raw_tool_result={"actions": ["generate_reply"]},
    )

    package = compose_kf_outbound(
        packet,
        bundle,
        ResponseStrategy.ANSWER,
        llm_output={},
        send_actions=[],
        allow_deterministic_fallback=True,
    )
    validation = validate_prepared_outbound_package(package, task_packet=packet)

    assert validation.status == ValidationStatus.PASS
    assert package.reply_text
    assert package.answered_task_ids == ["task-greeting"]
    assert "小区" in package.reply_text
    assert "视频" in package.reply_text


def test_production_requires_llm2_action_captions_for_media_actions() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "有的，这是棠润府 15-2-801B 的视频。",
            "claims": [],
            "action_captions": [],
            "self_review": {"status": "pass"},
        },
        reply_source="kf_llm2_outbound_production",
        allow_deterministic_fallback=False,
    )

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.reply_text == ""
    assert package.action_captions == []
    assert "production_missing_action_caption:send-video-1" in package.self_review["retry_reason"]


def test_compose_kf_outbound_retries_when_llm2_invents_price() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "有的，这套押一付一 9999 元。",
            "claims": [
                {
                    "claim_id": "claim-bad-rent",
                    "task_id": "task-video",
                    "field": "rent_pay1",
                    "value": 9999,
                    "evidence_ref": "evd-listing-1",
                    "text": "押一付一 9999 元",
                }
            ],
            "action_captions": [{"action_id": "send-video-1", "text": "视频发你了。"}],
            "self_review": {"status": "pass"},
        },
    )

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.reply_text == ""
    assert package.claims == []
    assert package.action_captions == []
    assert [action.action_id for action in package.send_actions] == ["send-video-1"]
    assert "unsupported_price_or_budget" in package.self_review["retry_reason"]


def test_compose_kf_outbound_ignores_llm2_send_action_mutation() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "有的，这是棠润府 15-2-801B 的视频。",
            "send_actions": [{"action_id": "send-evil", "action_type": "video"}],
            "action_captions": [{"action_id": "send-video-1", "text": "这是棠润府 15-2-801B 的视频。"}],
            "self_review": {"status": "pass"},
        },
    )
    dumped = json.dumps(package.to_legacy_dict(), ensure_ascii=False)

    assert [action.action_id for action in package.send_actions] == ["send-video-1"]
    assert "send-evil" not in dumped
    assert package.self_review["ignored_llm_send_actions"] is True


def test_compose_kf_outbound_blocks_high_risk_password_link_and_phone_from_artifact() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "密码 9999#，链接 https://example.invalid/raw.mp4，电话 19900009999",
            "self_review": {"status": "pass"},
        },
    )
    dumped = json.dumps(package.to_legacy_dict(), ensure_ascii=False)

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.reply_text == ""
    assert "9999#" not in dumped
    assert "19900009999" not in dumped
    assert "https://example.invalid" not in dumped
    assert "raw_tool_result" not in dumped


def test_compose_kf_outbound_retries_when_llm2_invents_plain_facts() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "这套朝南，有电梯，已空出，可以养猫，离地铁近。",
            "claims": [],
            "action_captions": [],
            "self_review": {"status": "pass"},
        },
    )
    reason = package.self_review["retry_reason"]

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.reply_text == ""
    assert "reply_text_unsupported_plain_fact:south_orientation" in reason
    assert "reply_text_unsupported_plain_fact:elevator" in reason
    assert "reply_text_unsupported_plain_fact:vacant_now" in reason
    assert "reply_text_unsupported_plain_fact:cat_allowed" in reason
    assert "reply_text_unsupported_plain_fact:near_subway" in reason


def test_compose_kf_outbound_retries_when_caption_invents_fact_outside_action_evidence() -> None:
    package = compose_kf_outbound(
        _task_packet(),
        _evidence_bundle(),
        ResponseStrategy.SEND_MEDIA,
        llm_output={
            "reply_text": "这是棠润府15-2-801B房间的视频。",
            "claims": [],
            "action_captions": [
                {
                    "action_id": "send-video-1",
                    "text": "这是朝南有电梯的房间视频。",
                }
            ],
            "self_review": {"status": "pass"},
        },
    )

    assert package.response_strategy == ResponseStrategy.RETRY
    assert package.action_captions == []
    assert "caption_1_unsupported_plain_fact:south_orientation" in package.self_review["retry_reason"]
    assert "caption_1_unsupported_plain_fact:elevator" in package.self_review["retry_reason"]
