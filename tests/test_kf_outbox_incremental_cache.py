# 发送阶段出站台账(kf_outbox)增量缓存回归:
# reserve 不再全量重读台账文件,只解析新增字节;本文件守护缓存与
# 旧全量读语义完全一致(冷实例每次全量重建 == 旧实现),以及
# 离线 QA 台账路径隔离(多 QA 进程不得共用 data/kf_send_outbox.jsonl)。
from __future__ import annotations

import os
from pathlib import Path

from app.services import kf_outbox, kf_send_receipts


def _context(turn_id: str = "turn-1", msgid: str = "msg-1") -> dict:
    return {
        "structured_memory": {
            "current_turn_id": turn_id,
            "turn_records": [{"turn_id": turn_id, "msgids": [msgid]}],
        }
    }


def _action(turn_id: str, msgid: str, action_id: str = "send-text-final", action_type: str = "text"):
    return kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm",
        context=_context(turn_id, msgid),
        msgids=[msgid],
        action_id=action_id,
        action_type=action_type,
        payload={"text_hash": f"hash-{turn_id}-{action_id}"},
    )


def test_warm_cache_sees_appends_from_other_instance(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    warm = kf_outbox.LocalKfOutboxLedger(path)
    first_action = _action("turn-1", "msg-1", action_id="send-text-1")
    first = warm.reserve(first_action)
    assert first.should_send is True

    other = kf_outbox.LocalKfOutboxLedger(path)
    second_action = _action("turn-2", "msg-2", action_id="send-text-2")
    other_decision = other.reserve(second_action)
    assert other_decision.should_send is True

    # warm 实例必须通过尾部续读看到 other 实例追加的 pending 记录。
    duplicate = warm.reserve(second_action)
    assert duplicate.should_send is False
    assert duplicate.reason == "pending_outbox_blocks_duplicate"
    assert duplicate.duplicate_of == other_decision.outbox_id


def test_warm_cache_matches_cold_read_decisions(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    warm = kf_outbox.LocalKfOutboxLedger(path)
    action = _action("turn-1", "msg-1", action_id="send-video-1", action_type="video")
    key = kf_send_receipts.build_idempotency_key(action)

    reserved = warm.reserve(action, idempotency_key=key)
    assert reserved.should_send is True
    failed = kf_send_receipts.build_failed_receipt(action, idempotency_key=key, error=RuntimeError("upload failed"))
    warm.record_receipt(failed, action=action, idempotency_key=key, outbox_id=reserved.outbox_id)

    # FAILED 是终态但不阻断:warm 实例应放行重试并落新 pending,
    # 之后的冷实例(等价旧全量读)必须读到同一 pending 并阻断重复。
    warm_retry = warm.reserve(action, idempotency_key=key)
    assert warm_retry.should_send is True
    assert warm_retry.attempt == 2
    cold_retry = kf_outbox.LocalKfOutboxLedger(path).reserve(action, idempotency_key=key)
    assert cold_retry.should_send is False
    assert cold_retry.reason == "pending_outbox_blocks_duplicate"
    assert cold_retry.duplicate_of == warm_retry.outbox_id

    sent = kf_send_receipts.build_sent_receipt(action, idempotency_key=key, provider_result={"errcode": 0})
    warm.record_receipt(sent, action=action, idempotency_key=key, outbox_id=warm_retry.outbox_id)

    warm_after_sent = warm.reserve(action, idempotency_key=key)
    cold_after_sent = kf_outbox.LocalKfOutboxLedger(path).reserve(action, idempotency_key=key)
    assert warm_after_sent.should_send is False
    assert cold_after_sent.should_send is False
    assert warm_after_sent.reason == cold_after_sent.reason
    assert warm_after_sent.metadata.get("blocking_status") == cold_after_sent.metadata.get("blocking_status")


def test_attempt_increments_with_warm_cache_after_failed_receipt(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    action = _action("turn-1", "msg-1", action_id="send-video-1", action_type="video")
    key = kf_send_receipts.build_idempotency_key(action)

    first = ledger.reserve(action, idempotency_key=key)
    assert first.should_send is True
    assert first.attempt == 1
    failed = kf_send_receipts.build_failed_receipt(action, idempotency_key=key, error=RuntimeError("boom"))
    ledger.record_receipt(failed, action=action, idempotency_key=key, outbox_id=first.outbox_id)

    second = ledger.reserve(action, idempotency_key=key)
    assert second.should_send is True
    assert second.attempt == 2
    assert second.outbox_id != first.outbox_id


def test_reserve_avoids_full_reread_on_warm_cache(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    for index in range(5):
        decision = ledger.reserve(_action(f"turn-{index}", f"msg-{index}", action_id=f"send-text-{index}"))
        assert decision.should_send is True
    # 首次同步做一次全量重建,之后只允许尾部续读。
    with ledger._locked_records():
        ledger._sync_cache_unlocked()
    assert ledger._cache_full_resync_count == 1
    assert ledger._cache_offset == path.stat().st_size
    assert ledger._cache_record_count == 5


def test_cache_resyncs_after_file_replaced_or_truncated(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    action = _action("turn-1", "msg-1", action_id="send-video-1", action_type="video")
    key = kf_send_receipts.build_idempotency_key(action)
    first = ledger.reserve(action, idempotency_key=key)
    assert first.should_send is True
    blocked = ledger.reserve(action, idempotency_key=key)
    assert blocked.should_send is False

    # 台账被轮转清空后,同一 warm 实例必须整体重建缓存并重新放行。
    path.unlink()
    path.write_text("", encoding="utf-8")
    after_rotate = ledger.reserve(action, idempotency_key=key)
    assert after_rotate.should_send is True
    assert ledger._cache_full_resync_count >= 2


def test_corrupted_tail_line_fails_closed_on_warm_cache(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    good = ledger.reserve(_action("turn-1", "msg-1", action_id="send-text-1"))
    assert good.should_send is True

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"record_type":"reserved","broken":\n')

    decision = ledger.reserve(_action("turn-2", "msg-2", action_id="send-text-2"))
    assert decision.should_send is False
    assert decision.reason == "outbox_corruption_blocks_send"
    assert decision.metadata["corruption_count"] == 1


def test_partial_tail_write_fails_closed_and_recovers_after_completion(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)
    good = ledger.reserve(_action("turn-1", "msg-1", action_id="send-text-1"))
    assert good.should_send is True

    # 模拟写入中途崩溃:尾行没有换行符,必须 fail-closed 而不是当作不存在。
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"record_type":"reserved"')
    blocked = ledger.reserve(_action("turn-2", "msg-2", action_id="send-text-2"))
    assert blocked.should_send is False
    assert blocked.reason == "outbox_corruption_blocks_send"

    # 尾行补全成完整 JSON 行后恢复放行(与旧全量读语义一致:完整可解析即有效)。
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(',"idempotency_key":"send:tail","outbox_id":"outbox:tail","attempt":1,"status":"pending"}\n')
    recovered = ledger.reserve(_action("turn-2", "msg-2", action_id="send-text-2"))
    assert recovered.should_send is True


def test_offline_guard_isolates_outbox_path_per_process() -> None:
    # conftest 在收集阶段已调用 activate_offline_test_mode()。
    configured = os.environ.get("KF_SEND_OUTBOX_PATH") or ""
    assert configured, "离线模式必须显式设置 KF_SEND_OUTBOX_PATH"
    resolved = Path(configured)
    assert resolved.name != "kf_send_outbox.jsonl" or "offline_outbox" in str(resolved.parent), (
        "离线 QA 不得复用共享台账 data/kf_send_outbox.jsonl"
    )
    assert str(os.getpid()) in resolved.name
    default_ledger = kf_outbox.LocalKfOutboxLedger()
    assert default_ledger.path == resolved
    assert default_ledger.path != kf_outbox.DEFAULT_OUTBOX_PATH
