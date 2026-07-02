from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import kf_send_receipts
from app.services.kf_receipt_graph import KfReceiptGraphDeps, run_kf_receipt_graph


def run(coro):
    return asyncio.run(coro)


def _action(context: dict[str, Any] | None = None):
    return kf_send_receipts.build_send_action(
        open_kfid="kf",
        external_userid="user",
        context=context or {},
        action_id="send-text",
        action_type="text",
        payload={"text_hash": "hash-1"},
        metadata={"text_hash": "hash-1"},
        msgids=["m1"],
    )


def _deps(
    *,
    find_blocking_receipt,
    reserve_outbox,
    send_call,
    recorded: list[dict[str, Any]],
) -> KfReceiptGraphDeps:
    def record_persistent_receipt(receipt, **kwargs):
        recorded.append({"receipt": receipt.to_safe_dict(), **kwargs})

    def duplicate_from_outbox(action, decision, *, idempotency_key: str):
        return kf_send_receipts.build_duplicate_receipt(
            action,
            getattr(decision, "existing_receipt", None),
            idempotency_key=idempotency_key,
            duplicate_of=getattr(decision, "duplicate_of", ""),
            metadata={
                "duplicate_reason": getattr(decision, "reason", ""),
                **dict(getattr(decision, "metadata", {}) or {}),
            },
        )

    return KfReceiptGraphDeps(
        find_blocking_receipt=find_blocking_receipt,
        reserve_outbox=reserve_outbox,
        send_call=send_call,
        append_receipt=kf_send_receipts.append_receipt,
        record_persistent_receipt=record_persistent_receipt,
        build_duplicate_receipt=kf_send_receipts.build_duplicate_receipt,
        build_duplicate_from_outbox_decision=duplicate_from_outbox,
        build_error_receipt=kf_send_receipts.build_failed_receipt,
        build_sent_receipt=kf_send_receipts.build_sent_receipt,
    )


def test_receipt_graph_records_successful_send() -> None:
    async def run_case() -> None:
        context: dict[str, Any] = {}
        action = _action(context)
        key = kf_send_receipts.build_idempotency_key(action)
        recorded: list[dict[str, Any]] = []
        calls: list[str] = []

        state = await run_kf_receipt_graph(
            _deps(
                find_blocking_receipt=lambda _context, _key: None,
                reserve_outbox=lambda _action, _key: SimpleNamespace(
                    should_send=True,
                    outbox_id="outbox-1",
                ),
                send_call=lambda: calls.append("send") or {"msgid": "provider-1"},
                recorded=recorded,
            ),
            context=context,
            action=action,
            idempotency_key=key,
            receipt_metadata={"text_role": "reply"},
        )

        assert calls == ["send"]
        assert state["sent"] is True
        assert state["receipt_payload"]["status"] == kf_send_receipts.SENT_STATUS
        assert recorded[0]["outbox_id"] == "outbox-1"
        assert state["trace"] == [
            "receipt_graph:check_context_receipt",
            "receipt_graph:reserve_outbox",
            "receipt_graph:execute_send",
        ]

    run(run_case())


def test_receipt_graph_context_receipt_blocks_duplicate_without_send() -> None:
    async def run_case() -> None:
        context: dict[str, Any] = {}
        action = _action(context)
        key = kf_send_receipts.build_idempotency_key(action)
        existing = kf_send_receipts.build_sent_receipt(action, idempotency_key=key)
        recorded: list[dict[str, Any]] = []

        state = await run_kf_receipt_graph(
            _deps(
                find_blocking_receipt=lambda _context, _key: existing,
                reserve_outbox=lambda _action, _key: pytest.fail("reserve must not run"),
                send_call=lambda: pytest.fail("send must not run"),
                recorded=recorded,
            ),
            context=context,
            action=action,
            idempotency_key=key,
        )

        assert state["sent"] is False
        assert state["status"] == "context_duplicate_blocked"
        assert state["receipt_payload"]["status"] == kf_send_receipts.SKIPPED_DUPLICATE_STATUS
        assert [item["receipt"]["status"] for item in recorded] == [
            kf_send_receipts.SENT_STATUS,
            kf_send_receipts.SKIPPED_DUPLICATE_STATUS,
        ]

    run(run_case())


def test_receipt_graph_outbox_blocks_duplicate_without_send() -> None:
    async def run_case() -> None:
        context: dict[str, Any] = {}
        action = _action(context)
        key = kf_send_receipts.build_idempotency_key(action)
        recorded: list[dict[str, Any]] = []

        state = await run_kf_receipt_graph(
            _deps(
                find_blocking_receipt=lambda _context, _key: None,
                reserve_outbox=lambda _action, _key: SimpleNamespace(
                    should_send=False,
                    reason="pending_outbox_blocks_duplicate",
                    duplicate_of="outbox-old",
                    metadata={"blocking_status": "pending_outbox"},
                    existing_receipt=None,
                ),
                send_call=lambda: pytest.fail("send must not run"),
                recorded=recorded,
            ),
            context=context,
            action=action,
            idempotency_key=key,
        )

        assert state["status"] == "outbox_duplicate_blocked"
        assert state["receipt_payload"]["metadata"]["duplicate_reason"] == "pending_outbox_blocks_duplicate"
        assert recorded[0]["receipt"]["status"] == kf_send_receipts.SKIPPED_DUPLICATE_STATUS

    run(run_case())


def test_receipt_graph_records_failed_receipt_before_reraising() -> None:
    async def run_case() -> None:
        context: dict[str, Any] = {}
        action = _action(context)
        key = kf_send_receipts.build_idempotency_key(action)
        recorded: list[dict[str, Any]] = []

        async def fail_send():
            raise RuntimeError("temporary timeout")

        with pytest.raises(RuntimeError):
            await run_kf_receipt_graph(
                _deps(
                    find_blocking_receipt=lambda _context, _key: None,
                    reserve_outbox=lambda _action, _key: SimpleNamespace(
                        should_send=True,
                        outbox_id="outbox-failed",
                    ),
                    send_call=fail_send,
                    recorded=recorded,
                ),
                context=context,
                action=action,
                idempotency_key=key,
            )

        assert recorded[0]["outbox_id"] == "outbox-failed"
        assert recorded[0]["receipt"]["status"] == kf_send_receipts.FAILED_STATUS
        assert recorded[0]["receipt"]["error_code"] == "RuntimeError"

    run(run_case())
