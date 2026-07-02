from __future__ import annotations

import asyncio
from typing import Any

from app.services.kf_entry_graph import (
    KfEntryGraphDeps,
    enter_session_messages_from_dispatch_plan,
    run_kf_entry_graph,
    text_groups_from_dispatch_plan,
)


def run(coro):
    return asyncio.run(coro)


def test_entry_graph_classifies_groups_and_skips_processed_messages() -> None:
    async def run_case() -> None:
        processed = {"done-1"}

        deps = KfEntryGraphDeps(
            is_enter_session_event=lambda message: message.get("event") == "enter_session",
            should_auto_reply_message=lambda message: message.get("msgtype") == "text",
            message_id=lambda message: message.get("msgid", ""),
            is_processed=lambda msgid: msgid in processed,
            open_kfid=lambda message: message.get("open_kfid", ""),
            external_userid=lambda message: message.get("external_userid", ""),
            pending_item=lambda message: {
                "msgid": message.get("msgid", ""),
                "content": message.get("content", ""),
            },
        )

        state = await run_kf_entry_graph(
            deps,
            messages=[
                {"event": "enter_session", "msgid": "welcome-1"},
                {
                    "msgtype": "text",
                    "msgid": "m1",
                    "open_kfid": "kf-a",
                    "external_userid": "u1",
                    "content": "你好",
                },
                {
                    "msgtype": "text",
                    "msgid": "m2",
                    "open_kfid": "kf-a",
                    "external_userid": "u1",
                    "content": "房源表",
                },
                {
                    "msgtype": "text",
                    "msgid": "done-1",
                    "open_kfid": "kf-a",
                    "external_userid": "u1",
                    "content": "重复",
                },
                {"msgtype": "image", "msgid": "img-1"},
            ],
        )

        plan = state["dispatch_plan"]
        assert [message["msgid"] for message in enter_session_messages_from_dispatch_plan(plan)] == [
            "welcome-1"
        ]
        groups = text_groups_from_dispatch_plan(plan)
        assert groups == [
            {
                "open_kfid": "kf-a",
                "external_userid": "u1",
                "items": [
                    {"msgid": "m1", "content": "你好"},
                    {"msgid": "m2", "content": "房源表"},
                ],
            }
        ]
        assert plan["ignored_count"] == 2
        assert state["trace"] == [
            "entry_graph:classify_messages",
            "entry_graph:group_text_messages",
            "entry_graph:build_dispatch_plan",
        ]

    run(run_case())


def test_entry_graph_ignores_text_without_target_or_content() -> None:
    async def run_case() -> None:
        deps = KfEntryGraphDeps(
            is_enter_session_event=lambda _message: False,
            should_auto_reply_message=lambda _message: True,
            message_id=lambda message: message.get("msgid", ""),
            is_processed=lambda _msgid: False,
            open_kfid=lambda message: message.get("open_kfid", ""),
            external_userid=lambda message: message.get("external_userid", ""),
            pending_item=lambda message: {"content": message.get("content", "")},
        )

        state = await run_kf_entry_graph(
            deps,
            messages=[
                {"msgid": "missing-user", "open_kfid": "kf", "content": "你好"},
                {"msgid": "empty", "open_kfid": "kf", "external_userid": "u", "content": ""},
            ],
        )

        plan = state["dispatch_plan"]
        assert text_groups_from_dispatch_plan(plan) == []
        assert [item["reason"] for item in plan["ignored_messages"]] == [
            "missing_conversation_target",
            "empty_text_content",
        ]

    run(run_case())


def test_entry_graph_keeps_good_messages_when_one_message_raises() -> None:
    async def run_case() -> None:
        def should_auto_reply(message: dict[str, Any]) -> bool:
            if message.get("msgid") == "bad-classify":
                raise ValueError("bad payload")
            return True

        def pending_item(message: dict[str, Any]) -> dict[str, Any]:
            if message.get("msgid") == "bad-group":
                raise RuntimeError("bad content")
            return {"msgid": message.get("msgid", ""), "content": message.get("content", "")}

        deps = KfEntryGraphDeps(
            is_enter_session_event=lambda message: message.get("event") == "enter_session",
            should_auto_reply_message=should_auto_reply,
            message_id=lambda message: message.get("msgid", ""),
            is_processed=lambda _msgid: False,
            open_kfid=lambda message: message.get("open_kfid", ""),
            external_userid=lambda message: message.get("external_userid", ""),
            pending_item=pending_item,
        )

        state = await run_kf_entry_graph(
            deps,
            messages=[
                {"event": "enter_session", "msgid": "welcome-1"},
                {
                    "msgid": "bad-classify",
                    "msgtype": "text",
                    "open_kfid": "kf",
                    "external_userid": "u",
                    "content": "坏消息",
                },
                {
                    "msgid": "bad-group",
                    "msgtype": "text",
                    "open_kfid": "kf",
                    "external_userid": "u",
                    "content": "坏文本",
                },
                {
                    "msgid": "good",
                    "msgtype": "text",
                    "open_kfid": "kf",
                    "external_userid": "u",
                    "content": "房源表",
                },
            ],
        )

        plan = state["dispatch_plan"]
        assert [message["msgid"] for message in enter_session_messages_from_dispatch_plan(plan)] == [
            "welcome-1"
        ]
        assert text_groups_from_dispatch_plan(plan)[0]["items"] == [
            {"msgid": "good", "content": "房源表"}
        ]
        assert [item["reason"] for item in plan["ignored_messages"]] == [
            "classification_error",
            "grouping_error",
        ]

    run(run_case())
