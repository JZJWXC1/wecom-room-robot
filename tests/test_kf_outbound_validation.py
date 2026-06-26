from __future__ import annotations

from app.services.kf_contracts import (
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
