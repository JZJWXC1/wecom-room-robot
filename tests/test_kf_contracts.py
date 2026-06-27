from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.services.kf_contracts import (
    ORCHESTRATOR_SHADOW_SCHEMA_VERSION,
    ORCHESTRATOR_SHADOW_TOP_LEVEL_KEYS,
    REDACTED,
    ActionCaption,
    CandidateItem,
    CandidateSet,
    Claim,
    ConstraintOperation,
    OrchestratorShadowArtifact,
    EvidenceItem,
    PreparedOutboundPackage,
    ResponseStrategy,
    RetryPacket,
    SendAction,
    SendReceipt,
    StructuredTaskPacket,
    TaskAtom,
    ToolEvidenceBundle,
    safe_artifact_payload,
)


FAKE_VIEWING_PASSWORD = "9999#"


def test_structured_task_packet_supports_multi_task_and_response_strategy() -> None:
    packet = StructuredTaskPacket(
        prompt_version="rewrite.v1",
        conversation_id="conv-1",
        turn_id="turn-2",
        case_id="case-3",
        audience="broker",
        response_strategy=ResponseStrategy.TOOL_FIRST,
        tasks=[
            TaskAtom(
                task_id="task-search",
                task_type="inventory_search",
                user_text="东新园 4000-5000 两室",
                constraint_operation=ConstraintOperation.INHERIT,
                constraints={"area": "东新园", "budget": [4000, 5000]},
                required_tools=["inventory.search"],
            ),
            TaskAtom(
                task_id="task-video",
                task_type="send_video",
                user_text="前两套视频",
                constraint_operation=ConstraintOperation.REPLACE,
                constraints={"candidate_numbers": [1, 2]},
                depends_on_task_ids=["task-search"],
                response_strategy=ResponseStrategy.SEND_MEDIA,
            ),
        ],
        rewritten_query="东新园 4000-5000 两室，前两套视频",
    )

    payload = packet.to_legacy_dict()

    assert payload["schema_version"] == "kf_rag_contracts.v1"
    assert payload["response_strategy"]["mode"] == "tool_first"
    assert payload["response_strategy"]["max_questions"] == 1
    assert [item["task_id"] for item in payload["tasks"]] == ["task-search", "task-video"]
    assert payload["tasks"][1]["response_strategy"]["mode"] == "send_media"
    assert payload["tasks"][1]["depends_on_task_ids"] == ["task-search"]


def test_response_strategy_accepts_legacy_string_and_structured_fields() -> None:
    packet = StructuredTaskPacket.from_legacy_dict(
        {
            "strategy": {
                "mode": "answer",
                "detail_level": "brief",
                "direct_answer_required": True,
                "acknowledge_context": False,
                "max_sentences": 1,
                "max_questions": 0,
                "avoid_repeat_fields": ["押金", "楼层"],
                "action_tense": "future",
                "future_knob": "保留未知字段",
            },
            "tasks": [
                {
                    "id": "task-answer",
                    "type": "reply_text",
                    "strategy": "answer",
                }
            ],
        }
    )

    payload = packet.to_legacy_dict()

    assert payload["response_strategy"]["mode"] == "answer"
    assert payload["response_strategy"]["detail_level"] == "brief"
    assert payload["response_strategy"]["direct_answer_required"] is True
    assert payload["response_strategy"]["acknowledge_context"] is False
    assert payload["response_strategy"]["max_sentences"] == 1
    assert payload["response_strategy"]["max_questions"] == 0
    assert payload["response_strategy"]["avoid_repeat_fields"] == ["押金", "楼层"]
    assert payload["response_strategy"]["action_tense"] == "future"
    assert payload["response_strategy"]["legacy_unknown_fields"]["future_knob"] == "保留未知字段"
    assert payload["tasks"][0]["response_strategy"]["mode"] == "answer"


def test_candidate_set_enforces_candidate_numbers() -> None:
    candidate_set = CandidateSet(
        candidate_set_id="cand-1",
        inventory_snapshot_id="snap-1",
        candidates=[
            CandidateItem(candidate_number=1, listing_id="lst-1", community="晨星花园", room_no="1-101A"),
            CandidateItem(candidate_number=2, listing_id="lst-2", community="晨星花园", room_no="1-102"),
        ],
    )

    assert [item["candidate_number"] for item in candidate_set.to_legacy_dict()["candidates"]] == [1, 2]

    with pytest.raises(ValidationError):
        CandidateSet(
            candidate_set_id="cand-bad",
            candidates=[
                CandidateItem(candidate_number=1, listing_id="lst-1"),
                CandidateItem(candidate_number=3, listing_id="lst-3"),
            ],
        )


def test_constraint_operations_cover_inherit_replace_exclude_and_clear() -> None:
    packet = StructuredTaskPacket(
        conversation_id="conv-constraints",
        turn_id="turn-constraints",
        inherited_constraints={"area": "东新园"},
        replaced_constraints={"budget": [4000, 5000]},
        excluded_constraints={"layout": "两室"},
        cleared_constraint_keys=["community"],
        tasks=[
            TaskAtom(task_id="inherit", task_type="search", constraint_operation=ConstraintOperation.INHERIT),
            TaskAtom(task_id="replace", task_type="search", constraint_operation=ConstraintOperation.REPLACE),
            TaskAtom(task_id="exclude", task_type="search", constraint_operation=ConstraintOperation.EXCLUDE),
            TaskAtom(task_id="clear", task_type="search", constraint_operation=ConstraintOperation.CLEAR),
        ],
    )

    operations = [item["constraint_operation"] for item in packet.to_legacy_dict()["tasks"]]

    assert operations == ["inherit", "replace", "exclude", "clear"]
    assert packet.to_legacy_dict()["cleared_constraint_keys"] == ["community"]


def test_evidence_claim_and_send_action_contracts_share_trace_ids() -> None:
    candidate = CandidateSet(
        candidate_set_id="cand-claim",
        inventory_snapshot_id="snap-claim",
        candidates=[CandidateItem(candidate_number=1, listing_id="lst-claim", evidence_id="evd-claim")],
    )
    evidence = EvidenceItem(
        evidence_id="evd-claim",
        listing_id="lst-claim",
        inventory_snapshot_id="snap-claim",
        source_record_id="row-claim",
        evidence_type="inventory_listing",
        summary="晨星花园 1-101A 押一付一 1800",
        field_values={"rent_pay1": 1800, "看房密码": FAKE_VIEWING_PASSWORD},
        sensitivity="public",
        fetched_at="2026-06-27T00:00:00Z",
        metadata={"source_hash": "hash-1"},
    )
    claim = Claim(
        claim_id="claim-price",
        task_id="task-answer",
        evidence_id="evd-claim",
        listing_id="lst-claim",
        field="rent_pay1",
        value=1800,
        evidence_ref="evd-claim",
        text_span={"start": 8, "end": 12},
        sensitivity="public",
        text="1-101A 押一付一是 1800",
        support=["evd-claim"],
    )
    package = PreparedOutboundPackage(
        conversation_id="conv-claim",
        turn_id="turn-claim",
        candidate_set_id="cand-claim",
        inventory_snapshot_id="snap-claim",
        reply_text="这套押一付一 1800。",
        response_strategy=ResponseStrategy.ANSWER,
        answered_task_ids=["task-answer"],
        candidate_set=candidate,
        evidence_bundle=ToolEvidenceBundle(
            tool_name="inventory.search",
            source_record_id="tool-run-1",
            field_values={"result_count": 1},
            sensitivity="public",
            fetched_at="2026-06-27T00:00:00Z",
            evidence=[evidence],
            candidate_set=candidate,
        ),
        claims=[claim],
        action_captions=[
            ActionCaption(
                caption_id="caption-send-text",
                action_id="send-text",
                action_type="text",
                text="文本回复",
            )
        ],
        send_actions=[SendAction(action_id="send-text", action_type="text", evidence_id="evd-claim")],
        missing_items=["video"],
        self_review={"status": "pass"},
        selfcheck_profile="contract.selfcheck.v1",
    )

    payload = package.to_legacy_dict()

    assert payload["answered_task_ids"] == ["task-answer"]
    assert payload["claims"][0]["task_id"] == "task-answer"
    assert payload["claims"][0]["field"] == "rent_pay1"
    assert payload["claims"][0]["value"] == 1800
    assert payload["claims"][0]["evidence_ref"] == "evd-claim"
    assert payload["claims"][0]["text_span"] == {"start": 8, "end": 12}
    assert payload["claims"][0]["sensitivity"] == "public"
    assert payload["claims"][0]["support"] == ["evd-claim"]
    assert payload["evidence_bundle"]["evidence"][0]["listing_id"] == "lst-claim"
    assert payload["evidence_bundle"]["evidence"][0]["source_record_id"] == "row-claim"
    assert payload["evidence_bundle"]["evidence"][0]["field_values"]["rent_pay1"] == 1800
    assert payload["evidence_bundle"]["evidence"][0]["field_values"]["看房密码"] == REDACTED
    assert payload["evidence_bundle"]["evidence"][0]["fetched_at"] == "2026-06-27T00:00:00Z"
    assert payload["action_captions"][0]["action_id"] == "send-text"
    assert payload["send_actions"][0]["evidence_id"] == "evd-claim"
    assert payload["missing_items"] == ["video"]
    assert payload["self_review"]["status"] == "pass"
    assert payload["selfcheck_profile"] == "contract.selfcheck.v1"


def test_sensitive_password_and_phone_are_redacted_from_safe_outputs_and_repr() -> None:
    package = PreparedOutboundPackage(
        conversation_id="conv-secret",
        turn_id="turn-secret",
        reply_text=f"看房密码是 {FAKE_VIEWING_PASSWORD}，电话 19900009999",
        evidence_bundle=ToolEvidenceBundle(
            tool_name="viewing.lookup",
            evidence=[
                EvidenceItem(
                    evidence_id="evd-secret",
                    listing_id="lst-secret",
                    evidence_type="viewing",
                    summary=f"门锁密码 {FAKE_VIEWING_PASSWORD}",
                    sensitive_metadata={"viewing_password": FAKE_VIEWING_PASSWORD},
                )
            ],
            raw_tool_result={"viewing_password": FAKE_VIEWING_PASSWORD},
        ),
        send_actions=[
            SendAction(
                action_id="send-secret",
                action_type="text",
                payload={"text": f"密码 {FAKE_VIEWING_PASSWORD}"},
                sensitive_payload={"viewing_password": FAKE_VIEWING_PASSWORD},
            )
        ],
    )

    safe_json = json.dumps(package.to_legacy_dict(), ensure_ascii=False)
    model_repr = repr(package)

    assert FAKE_VIEWING_PASSWORD not in safe_json
    assert "19900009999" not in safe_json
    assert FAKE_VIEWING_PASSWORD not in model_repr
    assert "raw_tool_result" not in package.to_legacy_dict()["evidence_bundle"]


def test_legacy_dict_roundtrip_records_unknown_fields_without_silent_drop() -> None:
    legacy = {
        "reply_text": "有的，这套可以看。",
        "strategy": "answer",
        "conversation_id": "conv-legacy",
        "turn_id": "turn-legacy",
        "send_actions": [{"action_id": "send-1", "action_type": "text"}],
        "legacy_extra": {"kept": True},
        "raw_viewing_password": FAKE_VIEWING_PASSWORD,
    }

    package = PreparedOutboundPackage.from_legacy_dict(legacy)
    payload = package.to_legacy_dict()

    assert payload["conversation_id"] == "conv-legacy"
    assert payload["response_strategy"]["mode"] == "answer"
    assert payload["legacy_unknown_fields"]["legacy_extra"] == {"kept": True}
    assert payload["legacy_unknown_fields"]["raw_viewing_password"] == REDACTED
    assert FAKE_VIEWING_PASSWORD not in json.dumps(payload, ensure_ascii=False)


def test_unknown_constructor_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskAtom(task_id="task", task_type="search", unexpected_field=True)


def test_candidate_item_from_legacy_aliases() -> None:
    item = CandidateItem.from_legacy_dict(
        {
            "number": 1,
            "listing_id": "lst-alias",
            "community_name": "云杉苑",
            "room": "A-302",
            "ignored_future_field": "保留但不静默丢弃",
        }
    )

    payload = item.to_legacy_dict()

    assert payload["candidate_number"] == 1
    assert payload["community"] == "云杉苑"
    assert payload["room_no"] == "A-302"
    assert payload["legacy_unknown_fields"]["ignored_future_field"] == "保留但不静默丢弃"


def test_retry_packet_and_send_receipt_redact_error_text() -> None:
    retry = RetryPacket(
        conversation_id="conv-retry",
        turn_id="turn-retry",
        reason=f"自检发现密码 {FAKE_VIEWING_PASSWORD}",
        retry_instruction="去掉看房密码后重写",
    )
    receipt = SendReceipt(
        action_id="send-1",
        action_type="text",
        status="failed",
        error_message="token=abc123 phone 19900009999",
    )

    assert FAKE_VIEWING_PASSWORD not in json.dumps(retry.to_legacy_dict(), ensure_ascii=False)
    assert "19900009999" not in json.dumps(receipt.to_legacy_dict(), ensure_ascii=False)
    assert "abc123" not in json.dumps(receipt.to_legacy_dict(), ensure_ascii=False)


def test_utf8_chinese_integrity_in_contract_payload() -> None:
    packet = StructuredTaskPacket(
        conversation_id="中文会话",
        turn_id="第2轮",
        tasks=[
            TaskAtom(
                task_id="task-cn",
                task_type="inventory_search",
                user_text="新填地 4000-5000 的呢",
                constraints={"区域": "新填地", "预算": "4000-5000"},
            )
        ],
    )

    payload = json.dumps(packet.to_legacy_dict(), ensure_ascii=False)

    assert "中文会话" in payload
    assert "新填地" in payload
    assert "\\u65b0" not in payload


def test_orchestrator_shadow_artifact_schema_and_redaction() -> None:
    artifact = OrchestratorShadowArtifact(
        artifact_id="orch_shadow_test",
        created_at="2026-06-27T00:00:00Z",
        baseline_commit="693a9c899d1cb1a4565ad67e4e600fc9559da4dd",
        turn={
            "content_hash": "hash-only",
            "raw_customer_content": "客户原文 19900009999",
        },
        inventory_read={
            "decision_id": "ird_test",
            "source_kind": "legacy",
            "selection_mode": "disabled",
            "source_hash": "source-hash",
        },
        legacy_pipeline={
            "raw_tool_result": {"token": "abc", "password": FAKE_VIEWING_PASSWORD},
            "safe_note": f"token=abc123 电话 19900009999 密码 {FAKE_VIEWING_PASSWORD}",
        },
        shadow_a={"diff": {}, "verdict": "pass", "risk_reasons": []},
        integration_notes=[f"不要泄漏 {FAKE_VIEWING_PASSWORD} 和 19900009999"],
    )

    payload = artifact.to_safe_dict()
    dumped = json.dumps(payload, ensure_ascii=False)

    assert payload["schema_version"] == ORCHESTRATOR_SHADOW_SCHEMA_VERSION
    assert payload["mode"] == "shadow"
    assert payload["baseline_commit"] == "693a9c899d1cb1a4565ad67e4e600fc9559da4dd"
    assert tuple(payload.keys()) == ORCHESTRATOR_SHADOW_TOP_LEVEL_KEYS
    assert "raw_customer_content" not in payload["turn"]
    assert "raw_tool_result" not in payload["legacy_pipeline"]
    assert "19900009999" not in dumped
    assert FAKE_VIEWING_PASSWORD not in dumped
    assert "abc123" not in dumped


def test_safe_artifact_payload_preserves_explicit_git_commit_fields_only() -> None:
    commit = "693a9c899d1cb1a4565ad67e4e600fc9559da4dd"
    long_hash = "a" * 64

    payload = safe_artifact_payload(
        {
            "baseline_commit": commit,
            "commit": commit,
            "commit_hash": commit,
            "note": f"free text commit {commit} and hash {long_hash}",
            "other_hash": long_hash,
        }
    )

    assert payload["baseline_commit"] == commit
    assert payload["commit"] == commit
    assert payload["commit_hash"] == commit
    assert commit not in payload["note"]
    assert long_hash not in payload["note"]
    assert payload["other_hash"] != long_hash


def test_safe_artifact_payload_omits_raw_customer_content_and_tool_results() -> None:
    payload = safe_artifact_payload(
        {
            "content_hash": "kept",
            "message_content": "客户原文不应出现",
            "nested": {
                "raw_tool_result": {"secret": "plain"},
                "text": "phone 19900009999 token=abc123 password=secret",
            },
        }
    )
    dumped = json.dumps(payload, ensure_ascii=False)

    assert payload["content_hash"] == "kept"
    assert "message_content" not in payload
    assert "raw_tool_result" not in payload["nested"]
    assert "客户原文不应出现" not in dumped
    assert "19900009999" not in dumped
    assert "abc123" not in dumped
    assert "secret" not in dumped


def test_safe_artifact_payload_redacts_boundary_ids_signatures_and_long_runtime_values() -> None:
    source_hash = "b" * 40
    long_runtime_id = "wm_CUSTOMER_CANARY_1234567890abcdefghijklmnop"
    payload = safe_artifact_payload(
        {
            "source_hash": source_hash,
            "msg_signature": "sig_CANARY_abcdefghijklmnopqrstuvwxyz",
            "signature": "sha1_CANARY_abcdefghijklmnopqrstuvwxyz",
            "external_userid": long_runtime_id,
            "openid": "open_CANARY_1234567890abcdefghijklmnop",
            "unionid": "union_CANARY_1234567890abcdefghijklmnop",
            "cursor": "cursor_CANARY_1234567890abcdefghijklmnop",
            "welcome_code": "welcome_CANARY_1234567890abcdefghijklmnop",
            "media_id": "media_CANARY_1234567890abcdefghijklmnop",
            "note": (
                "手机 19900009999，看房密码 246810#，"
                "token=token_CANARY_abcdefghijklmnopqrstuvwxyz，"
                f"runtime={long_runtime_id}，hash={'a' * 64}"
            ),
        }
    )
    dumped = json.dumps(payload, ensure_ascii=False)

    assert payload["source_hash"] == source_hash
    assert "sig_CANARY" not in dumped
    assert "sha1_CANARY" not in dumped
    assert "CUSTOMER_CANARY" not in dumped
    assert "open_CANARY" not in dumped
    assert "union_CANARY" not in dumped
    assert "cursor_CANARY" not in dumped
    assert "welcome_CANARY" not in dumped
    assert "media_CANARY" not in dumped
    assert "19900009999" not in dumped
    assert "246810#" not in dumped
    assert "token_CANARY" not in dumped
    assert "a" * 64 not in dumped
