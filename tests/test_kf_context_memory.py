import json
from pathlib import Path

from app.services import kf_context_memory, kf_send_receipts


class FakeStore:
    def __init__(self, contexts: dict[str, dict] | None = None) -> None:
        self.contexts = contexts or {}
        self.saved: dict[str, dict] = {}
        self.deleted: list[str] = []

    def get(self, key: str) -> dict | None:
        return self.contexts.get(key)

    def save(self, key: str, context: dict) -> None:
        self.saved[key] = context

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.contexts.pop(key, None)


class FakeLogger:
    def __init__(self) -> None:
        self.exceptions: list[str] = []

    def exception(self, message: str, *args) -> None:
        self.exceptions.append(message % args if args else message)


def test_normalize_media_context_keeps_only_current_rag_state() -> None:
    context = {
        "image_paths": ["a.png"],
        "video_paths": ["v.mp4"],
        "video_urls": ["https://example.com/v.mp4"],
        "recent_messages": [
            {"role": "客户", "content": str(index), "created_at": index}
            for index in range(12)
        ],
        "last_candidate_set": {
            "query": "拱墅万达1500左右",
            "intent": "inventory",
            "candidates": [{"小区": "荣润府", "房号": str(index)} for index in range(12)],
            "shown_count": 4,
            "total_count": 12,
            "created_at": 1,
        },
        "active_query_state": {
            "intent": "inventory",
            "area": "拱墅万达",
            "budget": "1500左右",
        },
        "structured_memory": {
            "raw_dialog_context": [],
            "turn_records": [],
            "current_turn_id": "",
        },
        "updated_at": 3,
    }

    normalized = kf_context_memory.normalize_media_context(context, candidate_limit=10)

    assert normalized["image_paths"] == [Path("a.png")]
    assert normalized["video_paths"] == [Path("v.mp4")]
    assert [item["content"] for item in normalized["recent_messages"]] == [
        str(index) for index in range(12)
    ]
    assert "pending_room_confirmation" not in normalized
    assert len(normalized["last_candidate_set"]["candidates"]) == 10
    assert normalized["last_candidate_set"]["shown_count"] == 4
    assert normalized["last_candidate_set"]["total_count"] == 12
    assert normalized["active_query_state"]["area"] == "拱墅万达"


def test_recent_context_loads_from_store_and_deletes_expired_context() -> None:
    key = kf_context_memory.conversation_key("kf", "wm")
    store = FakeStore(
        {
            key: {
                "video_paths": ["v.mp4"],
                "recent_messages": [{"role": "客户", "content": "视频", "created_at": 1}],
                "updated_at": 1,
            }
        }
    )
    logger = FakeLogger()
    memory: dict[str, dict] = {}

    context = kf_context_memory.recent_context(
        "kf",
        "wm",
        memory=memory,
        store=store,
        ttl_seconds=100,
        logger=logger,
        now=lambda: 50,
    )

    assert context
    assert memory[key]["video_paths"] == [Path("v.mp4")]
    expired = kf_context_memory.recent_context(
        "kf",
        "wm",
        memory=memory,
        store=store,
        ttl_seconds=10,
        logger=logger,
        now=lambda: 100,
    )

    assert expired is None
    assert key not in memory
    assert store.deleted == [key]


def test_save_context_persists_only_safe_summary_without_customer_id_or_secrets() -> None:
    raw_customer_id = "wm_CUSTOMER_CANARY_12345678901234567890"
    synthetic_phone = "19900009999"
    synthetic_password = "246810#"
    synthetic_token = "access_token=token_CANARY_abcdefghijklmnopqrstuvwxyz"
    synthetic_signature = "msg_signature=sig_CANARY_abcdefghijklmnopqrstuvwxyz"
    source_hash = "a" * 40
    context = {
        "recent_messages": [
            {
                "role": "user",
                "content": f"手机{synthetic_phone}，看房密码{synthetic_password}，{synthetic_token}",
                "created_at": 1,
            }
        ],
        "last_candidate_set": {
            "query": f"万达 {synthetic_phone}",
            "intent": "inventory",
            "candidates": [
                {
                    "listing_id": "lst_safe_001",
                    "小区": "荣润府",
                    "房号": "15-2-801B",
                    "押一付一": "1800",
                    "看房方式密码": synthetic_password,
                    "手机号": synthetic_phone,
                    "token": synthetic_token,
                    "source_hash": source_hash,
                }
            ],
            "shown_count": 1,
            "total_count": 1,
            "created_at": 1,
        },
        "confirmed_room": {
            "row": {
                "listing_id": "lst_safe_001",
                "小区": "荣润府",
                "房号": "15-2-801B",
                "看房方式密码": synthetic_password,
                "备注": f"联系{synthetic_phone}",
            },
            "label": "荣润府15-2-801B",
            "intent": "viewing",
            "created_at": 1,
            "inventory_cache_meta": {
                "source_hash": source_hash,
                "msg_signature": synthetic_signature,
            },
        },
        "updated_at": 1,
    }
    store = FakeStore()
    memory: dict[str, dict] = {}

    kf_context_memory.save_context(
        "kf_CANARY_OPEN_ID_12345678901234567890",
        raw_customer_id,
        context,
        memory=memory,
        store=store,
        logger=FakeLogger(),
        now=lambda: 2,
    )

    [(saved_key, saved_context)] = store.saved.items()
    dumped = json.dumps({saved_key: saved_context}, ensure_ascii=False, default=str)

    assert saved_key.startswith("kfctx_")
    assert raw_customer_id not in dumped
    assert "kf_CANARY_OPEN_ID" not in dumped
    assert synthetic_phone not in dumped
    assert synthetic_password not in dumped
    assert "token_CANARY" not in dumped
    assert "sig_CANARY" not in dumped
    assert "看房方式密码" not in dumped
    assert saved_context["last_candidate_set"]["candidates"][0]["listing_id"] == "lst_safe_001"
    assert saved_context["last_candidate_set"]["candidates"][0]["has_password"] is True
    assert saved_context["confirmed_room"]["row"]["needs_contact"] is False


def test_save_context_preserves_send_receipt_ledger_internal_idempotency_key() -> None:
    context = {
        "structured_memory": {
            "current_turn_id": "turn-1",
            "turn_records": [{"turn_id": "turn-1", "msgids": ["msg-1"]}],
        },
        "updated_at": 1,
    }
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm_CUSTOMER_CANARY_12345678901234567890",
        context=context,
        msgids=["msg-1"],
        action_id="send-text-final",
        action_type="text",
        payload={"text_hash": kf_send_receipts.text_hash("这条我已经发送过")},
    )
    idempotency_key = kf_send_receipts.build_idempotency_key(action)
    context = kf_send_receipts.append_receipt(
        context,
        kf_send_receipts.build_sent_receipt(
            action,
            idempotency_key=idempotency_key,
            provider_result={"msgid": "msg_PROVIDER_CANARY_12345678901234567890"},
        ),
    )
    store = FakeStore()

    kf_context_memory.save_context(
        "kf",
        "wm_CUSTOMER_CANARY_12345678901234567890",
        context,
        memory={},
        store=store,
        logger=FakeLogger(),
        now=lambda: 2,
    )

    [(saved_key, saved_context)] = store.saved.items()
    dumped = json.dumps({saved_key: saved_context}, ensure_ascii=False, default=str)

    assert "CUSTOMER_CANARY" not in dumped
    assert "msg_PROVIDER_CANARY" not in dumped
    assert saved_context["send_receipts"]["receipts"][0]["idempotency_key"] == idempotency_key
    assert kf_send_receipts.find_successful_receipt(saved_context, idempotency_key) is not None


def test_save_context_preserves_uncertain_send_receipt_as_blocking() -> None:
    context = {
        "structured_memory": {
            "current_turn_id": "turn-uncertain",
            "turn_records": [{"turn_id": "turn-uncertain", "msgids": ["msg-uncertain"]}],
        },
        "updated_at": 1,
    }
    action = kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="wm_CUSTOMER_CANARY_12345678901234567890",
        context=context,
        msgids=["msg-uncertain"],
        action_id="send-video-1",
        action_type="video",
        payload={"material_hash": "video-hash"},
    )
    idempotency_key = kf_send_receipts.build_idempotency_key(action)
    context = kf_send_receipts.append_receipt(
        context,
        kf_send_receipts.build_uncertain_receipt(
            action,
            idempotency_key=idempotency_key,
            error=TimeoutError("access_token=abc send timed out"),
        ),
    )
    store = FakeStore()

    kf_context_memory.save_context(
        "kf",
        "wm_CUSTOMER_CANARY_12345678901234567890",
        context,
        memory={},
        store=store,
        logger=FakeLogger(),
        now=lambda: 2,
    )

    [(saved_key, saved_context)] = store.saved.items()
    dumped = json.dumps({saved_key: saved_context}, ensure_ascii=False, default=str)

    assert "CUSTOMER_CANARY" not in dumped
    assert "access_token" not in dumped
    receipt = saved_context["send_receipts"]["receipts"][0]
    assert receipt["status"] == "send_uncertain"
    assert receipt["idempotency_key"] == idempotency_key
    assert kf_send_receipts.find_blocking_receipt(saved_context, idempotency_key) is not None


def test_append_and_format_dialog_context() -> None:
    context = None
    for index in range(32):
        context = kf_context_memory.append_dialog_message(
            context,
            role="客户",
            content=f"消息{index}",
            now=lambda index=index: index,
        )

    assert context is not None
    assert [item["content"] for item in context["recent_messages"]] == [
        f"消息{index}" for index in range(2, 32)
    ]
    assert [item["content"] for item in context["structured_memory"]["raw_dialog_context"]] == [
        f"消息{index}" for index in range(2, 32)
    ]
    assert kf_context_memory.format_dialog_context(context).splitlines()[-1] == "客户: 消息31"
    assert kf_context_memory.append_dialog_message(context, role="客户", content="   ") is None


def test_rewrite_memory_view_exposes_recent_raw_dialog_pairs_without_internal_trace() -> None:
    context = None
    for index in range(7):
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content=f"客户问题{index}",
            now=lambda index=index: index,
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content=f"机器人回复{index}",
            now=lambda index=index: index + 0.5,
        )

    view = kf_context_memory.rewrite_memory_view(context)

    assert [pair["user"] for pair in view["recent_dialog_pairs"]] == [
        f"客户问题{index}" for index in range(1, 7)
    ]
    assert view["recent_dialog_pairs"][-1]["assistant"] == "机器人回复6"
    assert "planner_result" not in view
    assert "selfcheck_result" not in view


def test_last_candidate_set_replaces_old_pending_confirmation_state() -> None:
    context = {
        "last_candidate_set": {
            "query": "拱墅万达1500左右",
            "intent": "inventory",
            "candidates": [{"小区": "荣润府", "房号": "15-2-801B"}],
            "created_at": 20,
            "shown_count": 1,
            "total_count": 6,
        }
    }

    candidate_set = kf_context_memory.last_candidate_set(context)

    assert candidate_set["intent"] == "inventory"
    assert candidate_set["candidates"][0]["小区"] == "荣润府"
    assert candidate_set["shown_count"] == 1
    assert candidate_set["total_count"] == 6


def test_pending_video_sends_are_deduped_and_clearable() -> None:
    context = kf_context_memory.remember_pending_video_sends(
        None,
        paths=[Path("a.mp4"), Path("b.mp4")],
        labels=["荣润府15-2-801B", "合峙悦府6-1-1204B"],
        requested_count=2,
        sent_count=1,
        now=lambda: 10,
    )
    context = kf_context_memory.remember_pending_video_sends(
        context,
        paths=[Path("b.mp4"), Path("c.mp4")],
        labels=["合峙悦府6-1-1204B", "星桥锦绣嘉苑20-1606A"],
        requested_count=3,
        sent_count=1,
        now=lambda: 11,
    )

    pending = kf_context_memory.pending_video_sends(context)
    assert pending["paths"] == [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
    assert pending["requested_count"] == 3

    context = kf_context_memory.clear_pending_video_sends(context, sent_paths=[Path("a.mp4")])
    assert kf_context_memory.pending_video_sends(context)["paths"] == [Path("b.mp4"), Path("c.mp4")]


def test_structured_memory_records_minimal_turn_records_and_assistant_summary() -> None:
    context = kf_context_memory.start_structured_turn(
        None,
        state={"intent": "inventory", "effective_query": "拱墅万达1500左右"},
        user_input={"content": "万达1500左右有哪些", "message_kind": "new_or_standalone"},
        rewrite_result={
            "rewritten_query": "拱墅万达1500左右在租房源",
            "intent": "inventory",
            "query_state": {"intent": "inventory", "area": "拱墅万达"},
        },
        now=lambda: 100,
    )
    context = kf_context_memory.record_structured_trace_event(
        context,
        "planner_result",
        {"actions": ["search_inventory"], "reason": "search"},
        now=lambda: 101,
    )
    context = kf_context_memory.record_structured_trace_event(
        context,
        "selfcheck_result",
        {"action": "retry", "retry_reason": "missing budget"},
        now=lambda: 102,
    )
    context = kf_context_memory.record_structured_assistant_output(
        context,
        draft_reply="初稿",
        final_reply="最终回复",
        sent_action={"type": "text", "count": 1, "items": ["最终回复"]},
        blocked_action={"type": "video", "reason": "not requested"},
        candidate_state={"candidate_set": {"shown_count": 2}},
        now=lambda: 103,
    )

    record = context["structured_memory"]["turn_records"][-1]
    assert record["rewritten_query"] == "拱墅万达1500左右在租房源"
    assert record["intent"] == "inventory"
    assert record["query_state"]["area"] == "拱墅万达"
    assert "planner_result" not in record
    assert "selfcheck_result" not in record
    assert "tool_evidence" not in record
    assert "blocked_actions" not in record["assistant_sent_summary"]
    assert record["assistant_sent_summary"]["final_reply"] == "最终回复"
    assert record["assistant_sent_summary"]["sent_actions"][0]["type"] == "text"
    assert record["assistant_sent_summary"]["candidate_state"]["candidate_set"]["shown_count"] == 2


def test_update_structured_state_records_rewrite_retry() -> None:
    context = kf_context_memory.start_structured_turn(
        None,
        state={"intent": "unclear"},
        user_input={"content": "视频发我"},
        rewrite_result={"intent": "unclear"},
        now=lambda: 1,
    )
    context = kf_context_memory.update_structured_state(
        context,
        state={"intent": "media", "effective_query": "根据上一轮候选发视频"},
        rewrite_result={
            "intent": "media",
            "planner_feedback": {"missing_evidence": "缺少上下文绑定证据"},
        },
        now=lambda: 2,
    )

    memory = context["structured_memory"]
    assert "state" not in memory
    assert memory["turn_records"][-1]["intent"] == "media"
    assert "planner_feedback" not in memory["turn_records"][-1]


def test_structured_memory_summary_keeps_recent_black_box_fields() -> None:
    context = None
    for index in range(12):
        context = kf_context_memory.start_structured_turn(
            context,
            state={"intent": "inventory", "index": index},
            user_input={"content": f"q{index}"},
            rewrite_result={"rewritten_query": f"rq{index}"},
            now=lambda index=index: float(index + 1),
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply=f"a{index}",
            sent_action={"type": "text", "count": 1, "items": [f"a{index}"]},
            now=lambda index=index: float(index + 1),
        )

    memory = context["structured_memory"]
    assert len(memory["turn_records"]) == 10
    summary = kf_context_memory.structured_memory_summary(context)
    assert [item["assistant_sent_summary"]["final_reply"] for item in summary["recent_turn_records"]] == [
        "a9",
        "a10",
        "a11",
    ]
