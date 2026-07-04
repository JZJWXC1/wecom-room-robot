# 出站台账 msgid 域防重(第三层防线):
# 幂等键是轮次域设计(含 turn_id/payload_hash 等),重复回调把同一 msgid
# 推成新轮次后,LLM 重放产出的动作幂等键全变,按键去重拦不住跨轮重放
# (生产实证 2026-07-04 16:01 房源表图片重复外发)。本 gate 断言:同一
# msgid 域(客户消息集合摘要)的同一逻辑动作,跨轮次只能物理外发一次;
# 新的客户消息产生新 msgid 域,不会误拦合法重复请求。
# 注:孤儿工作包 judge/patches/ 的"重复外发动作检测"是 QA runner 判分
# gate(AB-AB 序列折叠),本文件是生产台账层的独立重实现,未采纳孤儿包代码。
from __future__ import annotations

from app.services import kf_outbox, kf_send_receipts


def _turn_context(turn_id: str, msgid: str) -> dict:
    return {
        "structured_memory": {
            "current_turn_id": turn_id,
            "turn_records": [{"turn_id": turn_id, "msgids": [msgid]}],
        }
    }


def _replay_action(
    turn_id: str,
    *,
    msgid: str = "msg-1",
    text: str,
    action_id: str = "send-text-final-reply",
    action_type: str = "text",
):
    # 模拟重复回调开出的新轮次:msgid 集合相同(msgid 域相同),但每轮的
    # 证据链 id 不同(生产的 inventory read context 按 generation/time_ns
    # 派生)→ 幂等键不同,复现台账按键去重拦不住跨轮重放的缺口。
    return kf_send_receipts.build_send_action(
        open_kfid="kf_x",
        external_userid="wm_x",
        context=_turn_context(turn_id, msgid),
        msgids=[msgid],
        action_id=action_id,
        action_type=action_type,
        evidence_id=f"evidence-{turn_id}",
        payload={"text_hash": kf_send_receipts.text_hash(text)},
    )


def test_scope_guard_key_stable_across_turns_and_empty_without_msgids() -> None:
    first = kf_send_receipts.msgid_scope_guard_key(_replay_action("turn-1", text="回复甲"))
    second = kf_send_receipts.msgid_scope_guard_key(_replay_action("turn-2", text="回复乙"))
    assert first and first == second

    other_msgid = kf_send_receipts.msgid_scope_guard_key(
        _replay_action("turn-3", msgid="msg-2", text="回复丙")
    )
    assert other_msgid and other_msgid != first

    no_msgid_action = kf_send_receipts.build_send_action(
        open_kfid="kf_x",
        external_userid="wm_x",
        context={"structured_memory": {"current_turn_id": "turn-9", "turn_records": []}},
        msgids=None,
        action_id="send-text-welcome",
        action_type="text",
        payload={"text_hash": kf_send_receipts.text_hash("欢迎语")},
    )
    assert kf_send_receipts.msgid_scope_guard_key(no_msgid_action) == ""


def test_sent_receipt_blocks_replay_from_new_turn(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)

    first_action = _replay_action("turn-1", text="房源表已发,姐看下")
    first_key = kf_send_receipts.build_idempotency_key(first_action)
    reserved = ledger.reserve(first_action, idempotency_key=first_key)
    assert reserved.should_send is True
    sent = kf_send_receipts.build_sent_receipt(
        first_action, idempotency_key=first_key, provider_result={"errcode": 0}
    )
    ledger.record_receipt(sent, action=first_action, idempotency_key=first_key, outbox_id=reserved.outbox_id)

    replay_action = _replay_action("turn-2", text="这是最新房源表,收好")
    replay_key = kf_send_receipts.build_idempotency_key(replay_action)
    assert replay_key != first_key

    decision = ledger.reserve(replay_action, idempotency_key=replay_key)
    assert decision.should_send is False
    assert decision.reason == "msgid_scope_blocks_duplicate"
    assert decision.metadata.get("scope_guard") == "msgid_scope"
    assert decision.metadata.get("blocking_status") == kf_send_receipts.SENT_STATUS
    assert decision.duplicate_of


def test_pending_reservation_blocks_concurrent_replay(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)

    first_action = _replay_action("turn-1", text="房源表已发")
    first_key = kf_send_receipts.build_idempotency_key(first_action)
    reserved = ledger.reserve(first_action, idempotency_key=first_key)
    assert reserved.should_send is True

    # 首轮已 reserve 未回执(发送在途),并发重放轮必须被在途记录阻断。
    replay_action = _replay_action("turn-2", text="最新房源表")
    replay_key = kf_send_receipts.build_idempotency_key(replay_action)
    decision = ledger.reserve(replay_action, idempotency_key=replay_key)
    assert decision.should_send is False
    assert decision.reason == "msgid_scope_pending_blocks_duplicate"
    assert decision.duplicate_of == reserved.outbox_id


def test_failed_receipt_allows_retry_from_new_turn(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)

    first_action = _replay_action("turn-1", text="房源表已发")
    first_key = kf_send_receipts.build_idempotency_key(first_action)
    reserved = ledger.reserve(first_action, idempotency_key=first_key)
    failed = kf_send_receipts.build_failed_receipt(
        first_action, idempotency_key=first_key, error=RuntimeError("upload failed")
    )
    ledger.record_receipt(failed, action=first_action, idempotency_key=first_key, outbox_id=reserved.outbox_id)

    # 首轮明确失败(客户没收到),后续轮次补发不受 msgid 域阻断。
    retry_action = _replay_action("turn-2", text="重新给姐发房源表")
    retry_key = kf_send_receipts.build_idempotency_key(retry_action)
    decision = ledger.reserve(retry_action, idempotency_key=retry_key)
    assert decision.should_send is True


def test_new_customer_message_scope_not_blocked(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)

    first_action = _replay_action("turn-1", msgid="msg-1", text="房源表已发")
    first_key = kf_send_receipts.build_idempotency_key(first_action)
    reserved = ledger.reserve(first_action, idempotency_key=first_key)
    sent = kf_send_receipts.build_sent_receipt(
        first_action, idempotency_key=first_key, provider_result={"errcode": 0}
    )
    ledger.record_receipt(sent, action=first_action, idempotency_key=first_key, outbox_id=reserved.outbox_id)

    # 客户第二天再要一次房源表:新 msgid → 新 msgid 域,必须放行。
    second_request = _replay_action("turn-2", msgid="msg-2", text="房源表再发一次")
    second_key = kf_send_receipts.build_idempotency_key(second_request)
    decision = ledger.reserve(second_request, idempotency_key=second_key)
    assert decision.should_send is True


def test_same_key_retry_semantics_unchanged(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    ledger = kf_outbox.LocalKfOutboxLedger(path)

    action = _replay_action("turn-1", text="房源表已发")
    key = kf_send_receipts.build_idempotency_key(action)
    first = ledger.reserve(action, idempotency_key=key)
    assert first.should_send is True
    failed = kf_send_receipts.build_failed_receipt(action, idempotency_key=key, error=RuntimeError("boom"))
    ledger.record_receipt(failed, action=action, idempotency_key=key, outbox_id=first.outbox_id)

    # 同轮次同键重试:msgid 域守卫不得干扰既有 attempt 递增语义。
    second = ledger.reserve(action, idempotency_key=key)
    assert second.should_send is True
    assert second.attempt == 2


def test_scope_guard_survives_cold_reload_and_cross_instance(tmp_path) -> None:
    path = tmp_path / "kf_send_outbox.jsonl"
    warm = kf_outbox.LocalKfOutboxLedger(path)

    first_action = _replay_action("turn-1", text="房源表已发")
    first_key = kf_send_receipts.build_idempotency_key(first_action)
    reserved = warm.reserve(first_action, idempotency_key=first_key)
    sent = kf_send_receipts.build_sent_receipt(
        first_action, idempotency_key=first_key, provider_result={"errcode": 0}
    )
    warm.record_receipt(sent, action=first_action, idempotency_key=first_key, outbox_id=reserved.outbox_id)

    # 冷实例(等价旧全量读)与 warm 实例决策必须一致:进程重启后守卫仍生效。
    replay_action = _replay_action("turn-2", text="最新房源表")
    replay_key = kf_send_receipts.build_idempotency_key(replay_action)
    cold = kf_outbox.LocalKfOutboxLedger(path).reserve(replay_action, idempotency_key=replay_key)
    warm_decision = warm.reserve(replay_action, idempotency_key=replay_key)
    assert cold.should_send is False
    assert warm_decision.should_send is False
    assert cold.reason == warm_decision.reason == "msgid_scope_blocks_duplicate"
