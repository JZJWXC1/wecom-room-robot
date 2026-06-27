import json

from app.services import kf_send_receipts


def _context(turn_id: str = "turn-1", msgid: str = "msg-1") -> dict:
    return {
        "structured_memory": {
            "current_turn_id": turn_id,
            "turn_records": [{"turn_id": turn_id, "msgids": [msgid]}],
        }
    }


def test_idempotency_key_uses_msgid_scope_and_listing_binding() -> None:
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-a", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-1",
        evidence_id="evd-1",
        payload={"material_hash": "media-hash"},
    )
    same_callback_action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-b", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-1",
        evidence_id="evd-1",
        payload={"material_hash": "media-hash"},
    )
    other_listing_action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-b", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-2",
        evidence_id="evd-2",
        payload={"material_hash": "media-hash"},
    )

    assert action.turn_id == same_callback_action.turn_id
    assert kf_send_receipts.build_idempotency_key(action) == kf_send_receipts.build_idempotency_key(same_callback_action)
    assert kf_send_receipts.build_idempotency_key(action) != kf_send_receipts.build_idempotency_key(other_listing_action)


def test_idempotency_key_changes_when_fact_or_media_evidence_changes() -> None:
    base = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-a", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst_aaaaaaaaaaaaaaaa",
        evidence_id="evd-1",
        inventory_snapshot_id="snap-1",
        source_hash="source-hash-a",
        candidate_set_id="cand-1",
        media_id="med-a",
        sha256="sha-a",
        payload={"media_id": "med-a", "sha256": "sha-a"},
    )
    changed_source = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-a", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst_aaaaaaaaaaaaaaaa",
        evidence_id="evd-1",
        inventory_snapshot_id="snap-1",
        source_hash="source-hash-b",
        candidate_set_id="cand-1",
        media_id="med-a",
        sha256="sha-a",
    )
    changed_media_hash = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-a", "msg-a"),
        msgids=["msg-a"],
        action_id="send-video-1",
        action_type="video",
        listing_id="lst_aaaaaaaaaaaaaaaa",
        evidence_id="evd-1",
        inventory_snapshot_id="snap-1",
        source_hash="source-hash-a",
        candidate_set_id="cand-1",
        media_id="med-a",
        sha256="sha-b",
    )

    assert kf_send_receipts.build_idempotency_key(base) != kf_send_receipts.build_idempotency_key(changed_source)
    assert kf_send_receipts.build_idempotency_key(base) != kf_send_receipts.build_idempotency_key(changed_media_hash)

    receipt = kf_send_receipts.build_sent_receipt(base, provider_result={"msgid": "provider-1"})
    payload = receipt.to_safe_dict()
    assert payload["listing_id"] == "lst_aaaaaaaaaaaaaaaa"
    assert payload["evidence_id"] == "evd-1"
    assert payload["inventory_snapshot_id"] == "snap-1"
    assert payload["source_hash"] == "source-hash-a"
    assert payload["candidate_set_id"] == "cand-1"
    assert payload["media_id"] == "med-a"
    assert payload["sha256"] == "sha-a"


def test_idempotency_key_separates_customers_and_new_turns_without_msgids() -> None:
    first_customer = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm-a",
        context=_context("turn-a", ""),
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-1",
        evidence_id="evd-1",
        payload={"material_hash": "media-hash"},
    )
    second_customer = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm-b",
        context=_context("turn-a", ""),
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-1",
        evidence_id="evd-1",
        payload={"material_hash": "media-hash"},
    )
    later_turn = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm-a",
        context=_context("turn-b", ""),
        action_id="send-video-1",
        action_type="video",
        listing_id="lst-1",
        evidence_id="evd-1",
        payload={"material_hash": "media-hash"},
    )

    assert kf_send_receipts.build_idempotency_key(first_customer) != kf_send_receipts.build_idempotency_key(second_customer)
    assert kf_send_receipts.build_idempotency_key(first_customer) != kf_send_receipts.build_idempotency_key(later_turn)


def test_success_receipt_blocks_duplicate_and_records_skip_receipt() -> None:
    context = _context()
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=context,
        msgids=["msg-1"],
        action_id="send-text-final",
        action_type="text",
        payload={"text_hash": "hash-1"},
    )
    sent = kf_send_receipts.build_sent_receipt(action, provider_result={"msgid": "provider-1"})
    context = kf_send_receipts.append_receipt(context, sent)

    existing = kf_send_receipts.find_successful_receipt(context, sent.idempotency_key)
    assert existing is not None

    duplicate = kf_send_receipts.build_duplicate_receipt(action, existing)
    context = kf_send_receipts.append_receipt(context, duplicate)

    statuses = [item["status"] for item in context["send_receipts"]["receipts"]]
    assert statuses == ["sent", "skipped_duplicate"]
    assert context["send_receipts"]["receipts"][-1]["duplicate_of"] == sent.receipt_id


def test_failed_receipt_is_redacted_and_does_not_block_retry() -> None:
    context = _context()
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=context,
        msgids=["msg-1"],
        action_id="send-image-1",
        action_type="image",
        payload={"material_hash": "hash-1"},
    )
    failed = kf_send_receipts.build_failed_receipt(
        action,
        error=RuntimeError("token=abc123 phone 19900009999"),
    )
    context = kf_send_receipts.append_receipt(context, failed)

    dumped = json.dumps(context, ensure_ascii=False)
    assert "abc123" not in dumped
    assert "19900009999" not in dumped
    assert kf_send_receipts.find_successful_receipt(context, failed.idempotency_key) is None
