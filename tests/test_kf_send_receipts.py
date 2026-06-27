from concurrent.futures import ThreadPoolExecutor
import json
import threading

from app.services import kf_outbox, kf_send_receipts


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


def test_outbox_persists_success_and_blocks_duplicate_after_restart(tmp_path) -> None:
    context = _context("turn-1", "msg-persist")
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm_CUSTOMER_CANARY_12345678901234567890",
        context=context,
        msgids=["msg-persist"],
        action_id="send-text-final",
        action_type="text",
        payload={
            "text_hash": kf_send_receipts.text_hash("房源表发你了"),
            "token": "token_CANARY_should_not_persist",
            "phone": "19900009999",
        },
    )
    path = tmp_path / "kf_send_outbox.jsonl"
    first_ledger = kf_outbox.LocalKfOutboxLedger(path)
    decision = first_ledger.reserve(action)
    assert decision.should_send is True

    sent = kf_send_receipts.build_sent_receipt(
        action,
        idempotency_key=decision.idempotency_key,
        provider_result={"msgid": "msg_PROVIDER_CANARY_12345678901234567890"},
    )
    first_ledger.record_receipt(
        sent,
        action=action,
        idempotency_key=decision.idempotency_key,
        outbox_id=decision.outbox_id,
    )

    restarted_ledger = kf_outbox.LocalKfOutboxLedger(path)
    replay_decision = restarted_ledger.reserve(action)
    assert replay_decision.should_send is False
    assert replay_decision.existing_receipt is not None
    assert replay_decision.existing_receipt.status == "sent"
    assert replay_decision.duplicate_of == sent.receipt_id

    dumped = path.read_text(encoding="utf-8")
    assert "CUSTOMER_CANARY" not in dumped
    assert "PROVIDER_CANARY" not in dumped
    assert "token_CANARY" not in dumped
    assert "19900009999" not in dumped
    assert decision.idempotency_key in dumped
    assert sent.receipt_id in dumped


def test_outbox_failed_receipt_allows_replay_after_restart(tmp_path) -> None:
    context = _context("turn-1", "msg-replay")
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=context,
        msgids=["msg-replay"],
        action_id="send-image-1",
        action_type="image",
        payload={"material_hash": "image-hash"},
    )
    path = tmp_path / "kf_send_outbox.jsonl"
    first_ledger = kf_outbox.LocalKfOutboxLedger(path)
    first = first_ledger.reserve(action)
    failed = kf_send_receipts.build_failed_receipt(
        action,
        idempotency_key=first.idempotency_key,
        error=RuntimeError("invalid media"),
    )
    first_ledger.record_receipt(
        failed,
        action=action,
        idempotency_key=first.idempotency_key,
        outbox_id=first.outbox_id,
    )

    restarted_ledger = kf_outbox.LocalKfOutboxLedger(path)
    second = restarted_ledger.reserve(action)
    assert second.should_send is True
    assert second.attempt == 2
    assert second.outbox_id != first.outbox_id


def test_outbox_pending_reservation_blocks_blind_replay(tmp_path) -> None:
    context = _context("turn-1", "msg-pending")
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=context,
        msgids=["msg-pending"],
        action_id="send-video-1",
        action_type="video",
        payload={"material_hash": "video-hash"},
    )
    path = tmp_path / "kf_send_outbox.jsonl"
    first_ledger = kf_outbox.LocalKfOutboxLedger(path)
    first = first_ledger.reserve(action)
    assert first.should_send is True

    restarted_ledger = kf_outbox.LocalKfOutboxLedger(path)
    second = restarted_ledger.reserve(action)
    assert second.should_send is False
    assert second.reason == "pending_outbox_blocks_duplicate"
    assert second.duplicate_of == first.outbox_id


def test_outbox_corruption_diagnostics_fail_closed_without_bad_line_leak(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    path.write_text(
        '{"schema_version":"kf_outbox.v1","record_type":"reserved","token":"token_CANARY_should_not_leak"\n',
        encoding="utf-8",
    )
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-corrupt", "msg-corrupt"),
        msgids=["msg-corrupt"],
        action_id="send-text-final",
        action_type="text",
        payload={"text_hash": "hash-corrupt"},
    )

    ledger = kf_outbox.LocalKfOutboxLedger(path)
    decision = ledger.reserve(action)
    diagnostics = ledger.diagnostics()

    assert decision.should_send is False
    assert decision.reason == "outbox_corruption_blocks_send"
    assert decision.metadata["blocking_status"] == "outbox_corruption"
    assert decision.metadata["corruption_count"] == 1
    assert decision.metadata["corrupted_line_numbers"] == [1]
    assert diagnostics.corruption_count == 1
    assert diagnostics.corrupted_line_numbers == (1,)
    assert diagnostics.fail_closed is True

    dumped = json.dumps(
        {"decision": decision.metadata, "diagnostics": diagnostics.to_safe_dict()},
        ensure_ascii=False,
    )
    assert "token_CANARY" not in dumped
    assert "should_not_leak" not in dumped


def test_outbox_file_lock_blocks_concurrent_duplicate_reservation(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context("turn-lock", "msg-lock"),
        msgids=["msg-lock"],
        action_id="send-video-1",
        action_type="video",
        payload={"material_hash": "video-hash-lock"},
    )
    idempotency_key = kf_send_receipts.build_idempotency_key(action)
    barrier = threading.Barrier(2)

    def reserve_once() -> kf_outbox.OutboxDecision:
        ledger = kf_outbox.LocalKfOutboxLedger(path)
        barrier.wait(timeout=5)
        return ledger.reserve(action, idempotency_key=idempotency_key)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve_once) for _ in range(2)]
        decisions = [future.result(timeout=10) for future in futures]

    allowed = [decision for decision in decisions if decision.should_send]
    blocked = [decision for decision in decisions if not decision.should_send]
    assert len(allowed) == 1
    assert len(blocked) == 1
    assert blocked[0].reason == "pending_outbox_blocks_duplicate"
    assert blocked[0].duplicate_of == allowed[0].outbox_id
    assert blocked[0].metadata["blocking_status"] == "pending_outbox"

    ledger = kf_outbox.LocalKfOutboxLedger(path)
    reserved_records = [
        record
        for record in ledger.records()
        if record.get("record_type") == kf_outbox.OUTBOX_RECORD_RESERVED
    ]
    assert len(reserved_records) == 1
    assert reserved_records[0]["idempotency_key"] == idempotency_key
    assert reserved_records[0]["outbox_id"] == allowed[0].outbox_id
    assert ledger.diagnostics().corruption_count == 0


def test_uncertain_receipt_blocks_replay_after_restart(tmp_path) -> None:
    context = _context("turn-1", "msg-timeout")
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=context,
        msgids=["msg-timeout"],
        action_id="send-video-1",
        action_type="video",
        payload={"material_hash": "video-hash"},
    )
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    first = ledger.reserve(action)
    uncertain = kf_send_receipts.build_uncertain_receipt(
        action,
        idempotency_key=first.idempotency_key,
        error=TimeoutError("send timed out after request body was written"),
    )
    ledger.record_receipt(
        uncertain,
        action=action,
        idempotency_key=first.idempotency_key,
        outbox_id=first.outbox_id,
    )

    second = kf_outbox.LocalKfOutboxLedger(path).reserve(action)
    assert second.should_send is False
    assert second.existing_receipt is not None
    assert second.existing_receipt.status == "send_uncertain"
