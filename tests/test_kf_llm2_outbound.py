from __future__ import annotations

import json

from app.services.kf_contracts import (
    CandidateItem,
    CandidateSet,
    EvidenceItem,
    ResponseStrategy,
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
