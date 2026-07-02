from __future__ import annotations

import asyncio
from typing import Any

from app.services.kf_send_graph import KfSendGraphDeps, run_kf_send_graph


def run(coro):
    return asyncio.run(coro)


def test_send_graph_sends_reduces_saves_and_marks_processed() -> None:
    async def run_case() -> None:
        calls: list[str] = []
        saved: list[dict[str, Any]] = []
        processed: list[str] = []

        async def build_audit_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("audit")
            assert kwargs["final_reply"] == "可以的，这套还在。"
            assert kwargs["graph_state"]["trace"] == ["understand_message", "intent_route:housing_tools"]
            return {}

        async def send_final_actions(**kwargs: Any) -> dict[str, Any]:
            calls.append("send")
            kwargs["stale_guard"]()
            assert kwargs["final_reply"] == "可以的，这套还在。"
            return {
                "sent_actions": [{"type": "text", "text": kwargs["final_reply"]}],
                "context": {**kwargs["context"], "sent": True},
            }

        def reduce_turn_context(context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append("reduce")
            assert kwargs["send_result"]["sent_actions"][0]["type"] == "text"
            assert kwargs["final_package"]["final_reply"] == "可以的，这套还在。"
            return {**context, "reduced": True}

        result = await run_kf_send_graph(
            KfSendGraphDeps(
                build_audit_artifact=build_audit_artifact,
                send_final_actions=send_final_actions,
                reduce_turn_context=reduce_turn_context,
                save_context=lambda _kf, _user, context: saved.append(context),
                mark_processed=lambda msgid: processed.append(msgid),
                stale_guard=lambda: calls.append("stale"),
            ),
            open_kfid="kf",
            external_userid="user",
            conversation_key="kf:user",
            content="问房源",
            msgids=["m1", "m2"],
            generation=3,
            context={"loaded": True},
            understanding={"effective_query": "问房源"},
            planner_result={"actions": ["search_inventory", "generate_reply"]},
            tool_evidence={"actions": ["search_inventory", "generate_reply"]},
            reply_result={"reply": "可以的，这套还在。"},
            final_reply="可以的，这套还在。",
            final_draft_reply="可以的，这套还在。",
            graph_state={"trace": ["understand_message", "intent_route:housing_tools"]},
        )

        assert calls == ["audit", "stale", "send", "stale", "reduce"]
        assert saved == [{"loaded": True, "sent": True, "reduced": True}]
        assert processed == ["m1", "m2"]
        assert result["status"] == "sent"
        assert result["trace"] == [
            "send_graph:audit_artifact",
            "send_graph:send_actions",
            "send_graph:reduce_sent_context",
            "send_graph:persist_context",
            "send_graph:mark_processed",
        ]

    run(run_case())


def test_send_graph_blocks_without_calling_send_tool() -> None:
    async def run_case() -> None:
        calls: list[str] = []
        saved: list[dict[str, Any]] = []
        processed: list[str] = []

        async def build_audit_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("audit")
            assert kwargs["final_reply"] == ""
            return {}

        async def fail_send(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("blocked reply must not call send_final_actions")

        def reduce_turn_context(context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append("reduce_blocked")
            assert kwargs["send_result"]["send_blocked"] is True
            assert kwargs["send_result"]["sent_actions"] == []
            assert kwargs["final_package"]["final_reply"] == ""
            return {**context, "blocked_reduced": True}

        result = await run_kf_send_graph(
            KfSendGraphDeps(
                build_audit_artifact=build_audit_artifact,
                send_final_actions=fail_send,
                reduce_turn_context=reduce_turn_context,
                save_context=lambda _kf, _user, context: saved.append(context),
                mark_processed=lambda msgid: processed.append(msgid),
                stale_guard=lambda: calls.append("stale"),
            ),
            open_kfid="kf",
            external_userid="user",
            conversation_key="kf:user",
            content="问房源",
            msgids=["m1"],
            generation=3,
            context={"loaded": True},
            understanding={},
            planner_result={},
            tool_evidence={"outbound_package": {"prepared": False}},
            reply_result={"send_blocked": True},
            final_reply="",
            final_draft_reply="bad draft",
        )

        assert calls == ["audit", "stale", "reduce_blocked"]
        assert saved == [{"loaded": True, "blocked_reduced": True}]
        assert processed == ["m1"]
        assert result["status"] == "send_blocked"
        assert result["trace"] == [
            "send_graph:audit_artifact",
            "send_graph:reduce_blocked_context",
            "send_graph:persist_context",
            "send_graph:mark_processed",
        ]

    run(run_case())


def test_send_graph_fails_closed_when_blocked_state_has_final_reply() -> None:
    async def run_case() -> None:
        calls: list[str] = []
        saved: list[dict[str, Any]] = []
        processed: list[str] = []

        async def build_audit_artifact(**kwargs: Any) -> dict[str, Any]:
            calls.append("audit")
            assert kwargs["final_reply"] == "这句不应该发出去"
            return {}

        async def fail_send(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("send_blocked state must not call send_final_actions")

        def reduce_turn_context(context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append("reduce_blocked")
            assert kwargs["send_result"]["send_blocked"] is True
            assert kwargs["final_package"]["final_reply"] == ""
            return {**context, "blocked_reduced": True}

        result = await run_kf_send_graph(
            KfSendGraphDeps(
                build_audit_artifact=build_audit_artifact,
                send_final_actions=fail_send,
                reduce_turn_context=reduce_turn_context,
                save_context=lambda _kf, _user, context: saved.append(context),
                mark_processed=lambda msgid: processed.append(msgid),
            ),
            open_kfid="kf",
            external_userid="user",
            conversation_key="kf:user",
            content="问房源",
            msgids=["m1"],
            generation=3,
            context={"loaded": True},
            understanding={},
            planner_result={},
            tool_evidence={},
            reply_result={"send_blocked": True},
            final_reply="这句不应该发出去",
            final_draft_reply="这句不应该发出去",
        )

        assert calls == ["audit", "reduce_blocked"]
        assert saved == [{"loaded": True, "blocked_reduced": True}]
        assert processed == ["m1"]
        assert result["status"] == "send_blocked"

    run(run_case())
