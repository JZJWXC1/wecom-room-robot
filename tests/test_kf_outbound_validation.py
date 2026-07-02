from __future__ import annotations

from app.services.kf_contracts import (
    ActionCaption,
    CandidateItem,
    CandidateSet,
    Claim,
    EvidenceItem,
    PreparedOutboundPackage,
    SendAction,
    StructuredTaskPacket,
    TaskAtom,
    ToolEvidenceBundle,
)
from app.services.kf_outbound_validation import (
    OutboundValidationContext,
    ValidationLevel,
    ValidationStatus,
    validate_prepared_outbound_package,
)


def _candidate_set() -> CandidateSet:
    return CandidateSet(
        candidate_set_id="cand-1",
        inventory_snapshot_id="snap-1",
        candidates=[
            CandidateItem(candidate_number=1, listing_id="lst-1", inventory_snapshot_id="snap-1", community="星河苑", room_no="1-101"),
        ],
    )


def _evidence_bundle(candidate_set: CandidateSet | None = None) -> ToolEvidenceBundle:
    return ToolEvidenceBundle(
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        tool_name="inventory.search",
        candidate_set=candidate_set,
        evidence=[
            EvidenceItem(
                evidence_id="evd-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="inventory_listing",
                summary="星河苑 1-101 押一付一 4200",
                metadata={"field_values": {"rent_pay1": "4200", "room_no": "1-101", "community": "星河苑"}},
            )
        ],
    )


def _base_package() -> PreparedOutboundPackage:
    candidate_set = _candidate_set()
    return PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="星河苑 1-101 押一付一 4200。",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-rent",
                listing_id="lst-1",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-1",
                text="星河苑 1-101 押一付一 4200",
                support=["evd-1"],
                legacy_unknown_fields={"field": "rent_pay1", "value": "4200", "evidence_ref": "evd-1"},
            )
        ],
        send_actions=[
            SendAction(
                action_id="send-text",
                action_type="text",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-1",
                payload={"reply_hash": "hash-only"},
            )
        ],
    )


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_valid_package_passes_l0_to_l3_without_touching_send_pipeline() -> None:
    result = validate_prepared_outbound_package(_base_package())

    assert result.status == ValidationStatus.PASS
    assert result.passed is True
    assert result.send_allowed is True
    assert result.to_dict()["l3_rewrite_reasons"] == []


def test_l0_flags_schema_refs_duplicate_actions_and_bad_candidate_ref_type() -> None:
    candidate_set = _candidate_set()
    package = PreparedOutboundPackage(
        schema_version="future-contract",
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="星河苑 1-101 有视频。",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-missing-evidence",
                listing_id="lst-1",
                evidence_id="evd-missing",
                text="星河苑 1-101 有视频",
                support=["evd-missing"],
            )
        ],
        send_actions=[
            SendAction(action_id="send-video", action_type="video", evidence_id="evd-1"),
            SendAction(action_id="send-video", action_type="video", evidence_id="evd-1"),
            SendAction(
                action_id="send-image",
                action_type="image",
                evidence_id="evd-1",
                payload={"candidate_number": "first"},
                metadata={"depends_on_action_ids": ["send-missing"]},
            ),
        ],
    )

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.BLOCKED
    assert result.facts_passed is False
    assert {
        "l0.schema_version",
        "l0.unknown_evidence_ref",
        "l0.duplicate_action",
        "l0.invalid_candidate_number",
        "l0.unknown_action_ref",
    }.issubset(_codes(result))


def test_l0_flags_action_caption_unknown_action_and_type_mismatch() -> None:
    package = _base_package()
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]
    package.action_captions = [
        ActionCaption(action_id="send-missing", action_type="video", text="这是星河苑1-101的视频。"),
        ActionCaption(action_id="send-video", action_type="image", text="这是星河苑1-101的视频。"),
    ]

    result = validate_prepared_outbound_package(package)

    assert {
        "l0.unknown_action_ref",
        "l0.action_caption_type_mismatch",
    }.issubset(_codes(result))


def test_l1_checks_claim_value_listing_snapshot_and_sensitive_slots() -> None:
    candidate_set = _candidate_set()
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        reply_text="详情链接：https://example.test/listing/1",
        candidate_set=candidate_set,
        evidence_bundle=_evidence_bundle(candidate_set),
        claims=[
            Claim(
                claim_id="claim-bad-value",
                listing_id="lst-other",
                evidence_id="evd-1",
                inventory_snapshot_id="snap-other",
                text="星河苑 1-101 押一付一 4500",
                support=["evd-1"],
                legacy_unknown_fields={"field": "rent_pay1", "value": "4500", "evidence_ref": "evd-1"},
            )
        ],
        send_actions=[
            SendAction(
                action_id="send-link",
                action_type="link",
                evidence_id="evd-1",
                payload={"url": "https://example.test/listing/1"},
            )
        ],
    )

    result = validate_prepared_outbound_package(package)

    assert {
        "l1.claim_value_not_in_evidence",
        "l1.listing_mismatch",
        "l1.snapshot_mismatch",
        "l1.sensitive_outside_evidence",
    }.issubset(_codes(result))
    assert all("example.test" not in issue.message for issue in result.issues)


def test_l2_checks_task_completion_video_only_candidate_bounds_and_password_request() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-video", task_type="send_video", user_text="要视频")],
    )
    package = _base_package()
    package.send_actions = [
        SendAction(
            action_id="send-image",
            action_type="image",
            evidence_id="evd-1",
            payload={"candidate_number": 2},
        )
    ]
    package.claims = [
        Claim(
            claim_id="claim-password",
            listing_id="lst-1",
            evidence_id="evd-1",
            text="看房密码已准备",
            support=["evd-1"],
        )
    ]

    result = validate_prepared_outbound_package(
        package,
        context=OutboundValidationContext(task_packet=task_packet, user_asked_password=False),
    )

    assert {
        "l2.task_not_answered",
        "l2.video_only_cannot_send_image",
        "l2.candidate_number_out_of_range",
        "l2.password_not_requested",
    }.issubset(_codes(result))


def test_l2_treats_reply_compose_signal_as_answered_by_visible_text() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-controlled-reply-compose-signal",
                task_type="reply_compose_signal",
                user_text="你好，在吗",
                constraints={"guidance": "引导客户直接问房源表、图片或视频需求"},
            )
        ],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text="你好，在的。你直接发小区、房号、预算、房源表、图片或视频需求，我马上帮你查。",
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_ignores_embedded_confirmed_room_fields_when_classifying_task_kind() -> None:
    package = _base_package()
    package.reply_text = "杨家新雅苑36-1-1102是100方三房两卫，客厅带阳台。"
    package.claims = [
        Claim(
            claim_id="claim-layout",
            listing_id="lst-1",
            evidence_id="evd-1",
            inventory_snapshot_id="snap-1",
            text="杨家新雅苑36-1-1102是100方三房两卫，客厅带阳台",
            support=["evd-1"],
        )
    ]
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-1-inventory-detail",
                task_type="inventory_search",
                user_text="第一套户型特点怎么样",
                constraints={
                    "confirmed_room": {
                        "label": "杨家新雅苑36-1-1102",
                        "row": {
                            "小区": "杨家新雅苑",
                            "房号": "36-1-1102",
                            "户型": "100方三房两卫客厅带阳台",
                            "has_password": True,
                        },
                    }
                },
                required_tools=["inventory.search"],
            )
        ],
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)
    assert "l2.password_not_requested" not in _codes(result)


def test_l2_counts_image_send_action_as_answering_image_task() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[TaskAtom(task_id="task-1-image-request", task_type="send_image", user_text="第二套图片")],
    )
    package = _base_package()
    package.reply_text = ""
    package.claims = []
    package.send_actions = [SendAction(action_id="send-image", action_type="image", evidence_id="evd-1")]

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l2_allows_password_task_to_be_answered_by_field_target_error() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        tasks=[
            TaskAtom(
                task_id="task-1-viewing-guidance",
                task_type="viewing_guidance",
                user_text="密码没有或者不对的话联系谁？",
            )
        ],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-1",
        turn_id="turn-1",
        reply_text="看房方式和密码要按具体房源查。你把小区+房号发我，我马上按那套确认。",
        evidence_bundle=ToolEvidenceBundle(
            tool_name="tool_resolver",
            evidence=[
                EvidenceItem(
                    evidence_id="evd-field-target-error",
                    evidence_type="field_target_error",
                    summary="看房方式/密码要按具体房源查。",
                    metadata={"controlled_error_code": "field_target_error"},
                )
            ],
        ),
    )

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.PASS
    assert "l2.task_not_answered" not in _codes(result)


def test_l3_returns_rewrite_reasons_without_mutating_facts() -> None:
    task_packet = StructuredTaskPacket(
        conversation_id="conv-1",
        turn_id="turn-1",
        inherited_constraints={"budget": "4000-4500"},
        tasks=[TaskAtom(task_id="task-video", task_type="send_video", user_text="星河苑 1-101 视频")],
    )
    package = _base_package()
    package.reply_text = "listing_id=lst-1，XX小区我稍后发你视频，你预算多少？"
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]

    result = validate_prepared_outbound_package(package, task_packet=task_packet)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert result.l3_rewrite_reasons
    assert {
        "l3.internal_name_leak",
        "l3.template_talk",
        "l3.repeats_known_condition",
        "l3.action_tense_error",
    }.issubset(_codes(result))
    assert package.reply_text == "listing_id=lst-1，XX小区我稍后发你视频，你预算多少？"


def test_l3_checks_action_caption_tense_and_internal_words_without_blocking_facts() -> None:
    package = _base_package()
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-1")]
    package.action_captions = [
        ActionCaption(action_id="send-video", action_type="video", text="listing_id=lst-1，稍后发你视频。"),
    ]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert {
        "l3.internal_name_leak",
        "l3.action_tense_error",
    }.issubset(_codes(result))


def test_l3_rewrites_generic_waiting_reply_without_blocking_facts() -> None:
    package = _base_package()
    package.reply_text = "我先帮您确认一下最新房态，避免发错，稍后给您回复。"

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.REWRITE_REQUIRED
    assert result.facts_passed is True
    assert result.send_allowed is False
    assert "l3.generic_waiting_reply" in _codes(result)


def test_l3_allows_partial_missing_media_reply_when_evidence_exists() -> None:
    package = _base_package()
    package.reply_text = "这是星河苑1-101的视频，另一套暂时没有视频。"
    package.send_actions = [SendAction(action_id="send-video", action_type="video", evidence_id="evd-video-1")]
    package.action_captions = [
        ActionCaption(action_id="send-video", action_type="video", text="这是星河苑1-101的视频。"),
    ]
    package.evidence_bundle = ToolEvidenceBundle(
        inventory_snapshot_id="snap-1",
        candidate_set_id="cand-1",
        tool_name="media.send",
        candidate_set=package.candidate_set,
        evidence=[
            EvidenceItem(
                evidence_id="evd-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="inventory_listing",
                summary="星河苑 1-101 押一付一 4200",
                metadata={"field_values": {"rent_pay1": "4200", "room_no": "1-101", "community": "星河苑"}},
            ),
            EvidenceItem(
                evidence_id="evd-video-1",
                listing_id="lst-1",
                inventory_snapshot_id="snap-1",
                evidence_type="video",
                summary="星河苑 1-101 视频",
            ),
            EvidenceItem(
                evidence_id="evd-missing-media-1",
                inventory_snapshot_id="snap-1",
                evidence_type="missing_media",
                summary="另一套暂未找到可发送视频。",
            ),
        ],
    )
    package.missing_items = ["另一套:视频"]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert "l3.action_tense_error" not in _codes(result)


def test_legacy_claim_without_structured_value_still_uses_evidence_refs() -> None:
    package = _base_package()
    package.claims = [
        Claim(
            claim_id="claim-legacy",
            listing_id="lst-1",
            evidence_id="evd-1",
            inventory_snapshot_id="snap-1",
            text="星河苑 1-101 押一付一 4200",
            support=["evd-1"],
        )
    ]

    result = validate_prepared_outbound_package(package)

    assert result.status == ValidationStatus.PASS
    assert not result.issues_for_level(ValidationLevel.L1)
