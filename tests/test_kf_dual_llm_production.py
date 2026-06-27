from __future__ import annotations

import asyncio

from app.services.kf_dual_llm_production import (
    compose_production_outbound_package,
    package_passed,
    package_retry_reason,
    tool_plan_from_task_packet,
)
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow


def test_llm1_production_tool_plan_strips_customer_visible_reply_fields() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "send first room video",
            "task_atoms": [
                {"task_id": "task-1", "task_type": "send_video"},
            ],
            "tool_plan": {
                "actions": ["search_inventory", "context_tools", "send_video", "generate_reply"],
                "reply_text": "must not pass through",
                "final_reply": "must not pass through",
            },
        },
        content="video for first room",
        source_label="llm1_production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["source"] == "llm1_production+production_task_packet"
    assert plan["actions"] == ["search_inventory", "context_tools", "send_video", "generate_reply"]
    assert plan["reply_text"] == ""
    assert "final_reply" not in plan


def test_llm2_production_ignores_llm_supplied_send_actions() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "plain answer",
                "task_atoms": [{"task_id": "task-1", "task_type": "reply_text"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="hello",
            source_label="llm1_production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "我这边按证据给你回复。",
                    "send_actions": [{"action_id": "llm-made-action", "action_type": "video"}],
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass", "llm2_decides_media_targets": True},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="我这边按证据给你回复。",
            planner_result={"actions": ["generate_reply"]},
        )

        assert package_passed(package)
        assert package.reply_text == "我这边按证据给你回复。"
        assert [action.action_id for action in package.send_actions] == ["send-text-1"]
        assert package.self_review["llm2_decides_media_targets"] is False
        assert package.self_review["ignored_llm_send_actions"] is True

    asyncio.run(run_case())


def test_llm2_production_rejects_unsupported_price() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "price",
                "task_atoms": [{"task_id": "task-1", "task_type": "reply_text"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="price",
            source_label="llm1_production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这套押一付一 9999。",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="这套价格我按证据说。",
            planner_result={"actions": ["generate_reply"]},
        )

        assert not package_passed(package)
        assert "unsupported_price_or_budget:9999" in package_retry_reason(package)

    asyncio.run(run_case())
