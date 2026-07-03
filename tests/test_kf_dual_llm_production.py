from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.services.kf_dual_llm_production import (
    DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE,
    compose_controlled_evidence_outbound_package,
    compose_production_outbound_package,
    package_passed,
    package_retry_reason,
    tool_plan_from_task_packet,
    validate_production_outbound_package,
)
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow
from app.services.kf_outbound_validation import ValidationStatus


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


def test_llm1_production_tool_plan_keeps_legal_actions_exactly() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "发房源表",
            "task_atoms": [
                {"task_id": "task-sheet", "task_type": "send_inventory_sheet"},
            ],
            "tool_plan": {"actions": ["send_inventory_sheet"]},
        },
        content="房源表发一下",
        source_label="llm1_production",
        mode="production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["actions"] == ["send_inventory_sheet"]
    assert not plan.get("retry_required")
    assert plan["reply_text"] == ""


def test_llm2_production_greeting_uses_controlled_renderer_without_provider_call() -> None:
    async def run_case() -> None:
        class FailingReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                raise AssertionError("pure greeting should use controlled renderer, not external LLM2")

        packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "你好，在吗",
                "response_strategy": {"mode": "answer"},
                "task_atoms": [
                    {
                        "task_id": "task-greeting",
                        "task_type": "reply_compose_signal",
                        "user_text": "你好，在吗",
                        "required_tools": ["reply.compose"],
                    }
                ],
                "tool_plan": {
                    "actions": ["generate_reply"],
                    "required_tools": ["reply.compose"],
                },
            },
            content="你好，在吗",
            source_label="llm1_production_greeting_contract",
            mode="production",
        ).packet

        package = await compose_production_outbound_package(
            reply_generator=FailingReplyGenerator(),
            task_packet=packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="",
        )
        validation = validate_production_outbound_package(package, task_packet=packet)

        assert package.reply_source == DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
        assert package.reply_text
        assert package.answered_task_ids == ["task-greeting"]
        assert package_passed(package)
        assert validation.status == ValidationStatus.PASS

    asyncio.run(run_case())


def test_llm1_production_tool_plan_normalizes_tool_name_aliases() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "第1和第3套视频",
            "task_atoms": [
                {"task_id": "task-video", "task_type": "send_video"},
            ],
            "tool_plan": {
                "actions": ["context_tools.get_candidate_context", "media.fetch", "media.video", "reply.compose"]
            },
        },
        content="筛出来的1和3视频发我",
        source_label="llm1_production",
        mode="production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["actions"] == ["context_tools", "send_video", "generate_reply"]
    assert not plan.get("retry_required")


def test_llm1_production_tool_plan_normalizes_specific_media_fetch_aliases() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "图片也发我",
            "task_atoms": [
                {"task_id": "task-image", "task_type": "send_image"},
            ],
            "tool_plan": {"actions": ["context_tools", "inventory.image.fetch", "reply_compose_signal"]},
        },
        content="图片也发我",
        source_label="llm1_production",
        mode="production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["actions"] == ["context_tools", "send_image", "generate_reply"]
    assert not plan.get("retry_required")


def test_llm1_production_empty_tool_plan_does_not_fallback_from_task_types() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "第1套视频",
            "task_atoms": [
                {"task_id": "task-video", "task_type": "send_video"},
            ],
            "tool_plan": {"actions": []},
        },
        content="第1套视频发我",
        source_label="llm1_production",
        mode="production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["actions"] == []
    assert plan["retry_required"] is True
    assert plan["need_rewrite_clarification"] is True
    assert "send_video" not in plan["actions"]
    assert "empty" in plan["missing_evidence"]


def test_llm1_production_unsupported_action_forces_retry_without_tools() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "查房源",
            "task_atoms": [
                {"task_id": "task-search", "task_type": "inventory_search"},
            ],
            "tool_plan": {"actions": ["search_inventory", "local_magic_action"]},
        },
        content="房源查一下",
        source_label="llm1_production",
        mode="production",
    )

    plan = tool_plan_from_task_packet(build.packet)

    assert plan["actions"] == []
    assert plan["retry_required"] is True
    assert plan["invalid_actions"] == ["local_magic_action"]
    assert "unsupported action" in plan["missing_evidence"]


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
        assert package.send_actions == []
        assert package.self_review["llm2_decides_media_targets"] is False
        assert package.self_review["ignored_llm_send_actions"] is True

    asyncio.run(run_case())


def test_llm2_production_empty_output_uses_controlled_renderer() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video"}],
                "tool_plan": {"actions": ["send_video", "generate_reply"]},
            },
            content="第1套视频",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {}

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["send_video", "generate_reply"],
                "video_paths": ["C:/tmp/safe_fixture_video.mp4"],
                "video_rows": [{"小区": "星河苑", "房号": "1-101", "listing_id": "lst-101"}],
            },
            draft_reply="这是星河苑1-101房间的视频。",
            planner_result={"actions": ["send_video", "generate_reply"]},
        )

        assert package_passed(package)
        assert package.reply_source == DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
        assert package.reply_text == "这是星河苑1-101房间的视频。"
        assert "llm2_output_missing_visible_reply" in package.self_review["controlled_renderer_reason"]

    asyncio.run(run_case())


def test_llm2_production_controlled_renderer_builds_valid_evidence_package() -> None:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "第1套视频",
            "task_atoms": [{"task_id": "task-video", "task_type": "send_video"}],
            "tool_plan": {"actions": ["send_video", "generate_reply"]},
        },
        content="第1套视频",
        source_label="llm1_production",
        mode="production",
    )

    package = compose_controlled_evidence_outbound_package(
        task_packet=build.packet,
        tool_evidence={
            "actions": ["send_video", "generate_reply"],
            "video_paths": ["C:/tmp/safe_fixture_video.mp4"],
            "video_rows": [{"小区": "星河苑", "房号": "1-101", "listing_id": "lst-101"}],
        },
        planner_result={"actions": ["send_video", "generate_reply"]},
        reason="llm2_failed_contract_retry",
    )
    validation = validate_production_outbound_package(package, task_packet=build.packet)

    assert package_passed(package)
    assert package.reply_source == "kf_llm2_controlled_evidence_renderer"
    assert package.reply_text == "这是星河苑1-101房间的视频。"
    assert package.action_captions[0].text == "这是星河苑1-101房间的视频。"
    assert validation.status == ValidationStatus.PASS


def test_llm2_production_receives_controlled_evidence_not_local_fallback_text() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频，房源表也发我，免押怎么弄",
                "task_atoms": [
                    {"task_id": "task-video", "task_type": "send_video"},
                    {"task_id": "task-sheet", "task_type": "send_inventory_sheet"},
                    {"task_id": "task-deposit", "task_type": "deposit_policy"},
                ],
                "tool_plan": {
                    "actions": [
                        "send_video",
                        "send_inventory_sheet",
                        "explain_missing_media",
                        "send_deposit_policy",
                        "generate_reply",
                    ]
                },
            },
            content="第1套视频，房源表也发我，免押怎么弄",
            source_label="llm1_production",
            mode="production",
        )
        captured: dict[str, dict] = {}

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                captured.update(kwargs)
                return {
                    "reply_text": "这是星河苑1-101房间的视频，房源表也一起发你。免押按支付宝无忧住评估。",
                    "claims": [
                        {
                            "claim_id": "claim-deposit",
                            "task_id": "task-deposit",
                            "field": "deposit_policy",
                            "value": "支付宝无忧住",
                            "evidence_ref": "evd-rule-deposit-policy-1",
                            "text": "免押按支付宝无忧住评估",
                        }
                    ],
                    "action_captions": [
                        {"action_id": "send-video-1", "text": "这是星河苑1-101房间的视频。"},
                        {"action_id": "send-inventory_sheet-1", "text": "这是房源表。"},
                    ],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": [
                    "send_video",
                    "send_inventory_sheet",
                    "explain_missing_media",
                    "send_deposit_policy",
                    "generate_reply",
                ],
                "video_paths": ["C:/tmp/safe_fixture_video.mp4"],
                "video_rows": [{"小区": "星河苑", "房号": "1-101", "listing_id": "lst-101"}],
                "inventory_images": ["C:/tmp/sheet.png"],
                "missing_media": ["星河苑1-102"],
                "rule_evidence": {
                    "deposit_policy": {
                        "name": "支付宝无忧住信用免押",
                        "service_fee": {"3个月": "免押金额5.5%", "6-12个月": "免押金额8%"},
                    }
                },
            },
            draft_reply="旧本地 fallback 不应进入 LLM2 prompt",
            planner_result={
                "actions": [
                    "send_video",
                    "send_inventory_sheet",
                    "explain_missing_media",
                    "send_deposit_policy",
                    "generate_reply",
                ]
            },
        )

        evidence_bundle = captured["evidence_bundle"]
        evidence_types = {item["evidence_type"] for item in evidence_bundle["evidence"]}
        send_action_ids = {item["action_id"] for item in evidence_bundle["send_actions"]}
        dumped_prompt_inputs = json.dumps(captured, ensure_ascii=False)

        assert package_passed(package)
        assert {"video", "inventory_sheet", "missing_media", "deposit_policy"} <= evidence_types
        assert {"send-video-1", "send-inventory_sheet-1"} <= send_action_ids
        assert package.send_actions[0].listing_id == "lst-101"
        assert package.send_actions[0].payload["community"] == "星河苑"
        assert "旧本地 fallback" not in dumped_prompt_inputs

    asyncio.run(run_case())


def test_llm2_production_receives_target_error_evidence_not_legacy_error_text() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第2套原视频发我",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video"}],
                "tool_plan": {"actions": ["search_inventory", "context_tools", "send_video", "generate_reply"]},
            },
            content="第2套原视频发我",
            source_label="llm1_production",
            mode="production",
        )
        captured: dict[str, dict] = {}

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                captured.update(kwargs)
                return {
                    "reply_text": "上一轮没有第2套，你回有效序号或小区加房号，我按那套查视频和原片。",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["search_inventory", "context_tools", "send_video", "generate_reply"],
                "selection_error": {
                    "requested_indices": [2],
                    "candidate_count": 1,
                    "candidate_labels": ["星河苑1-101"],
                },
                "field_target_error": {
                    "field": "视频",
                    "reason": "original_video_followup_missing_stable_video_target",
                    "candidate_labels": ["星河苑1-101"],
                },
                "original_video_request": {"requested": True, "has_original_source": False},
            },
            draft_reply="旧本地错误话术不应进入 LLM2 prompt",
            planner_result={"actions": ["search_inventory", "context_tools", "send_video", "generate_reply"]},
        )

        evidence_bundle = captured["evidence_bundle"]
        evidence_types = {item["evidence_type"] for item in evidence_bundle["evidence"]}
        dumped_prompt_inputs = json.dumps(captured, ensure_ascii=False)

        assert package_passed(package)
        assert {"selection_error", "field_target_error", "original_video_unavailable"} <= evidence_types
        assert "旧本地错误话术" not in dumped_prompt_inputs

    asyncio.run(run_case())


def test_production_validator_accepts_evidence_bound_missing_media_reply() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频发我",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video"}],
                "tool_plan": {"actions": ["context_tools", "send_video", "generate_reply"]},
            },
            content="第1套视频发我",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "小洋坝家园一区6-201C目前暂未找到可发送的视频。",
                    "answered_task_ids": ["task-video"],
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["context_tools", "send_video", "generate_reply"],
                "missing_media": ["小洋坝家园一区6-201C:视频"],
                "missing_media_targets": [
                    {"community": "小洋坝家园一区", "room": "6-201C", "listing_id": "lst-xyd-201c"}
                ],
            },
            draft_reply="旧本地缺视频话术不应进入 production 最终回复",
            planner_result={"actions": ["context_tools", "send_video", "generate_reply"]},
        )

        result = validate_production_outbound_package(package, task_packet=build.packet)
        issue_codes = {issue.code for issue in result.issues}

        assert package.answered_task_ids == ["task-video"]
        assert result.passed
        assert "l2.task_not_answered" not in issue_codes

    asyncio.run(run_case())


def test_llm2_production_package_does_not_build_claim_legacy_reply() -> None:
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
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="legacy shadow baseline reply",
            planner_result={"actions": ["generate_reply"]},
            reply_result={"reply": "legacy shadow baseline reply"},
        )
        dumped = json.dumps(package.to_legacy_dict(), ensure_ascii=False)

        assert package_passed(package)
        assert "claim-legacy-reply" not in dumped
        assert "dual_llm_production.baseline_adapter" not in dumped

    asyncio.run(run_case())


def test_llm2_production_gates_retry_required_llm1_packet_without_calling_llm2() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频",
                "tool_plan": {"actions": ["search_inventory", "send_video", "generate_reply"]},
            },
            content="第1套视频",
            legacy_planner={"actions": ["search_inventory", "send_video", "generate_reply"]},
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                raise AssertionError("retry-required LLM1 packet must gate before LLM2")

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="legacy reply",
            planner_result={"actions": ["generate_reply"]},
        )

        assert not package_passed(package)
        assert "LLM1 production output missing or invalid task_atoms" in package_retry_reason(package)
        assert package.reply_text == ""

    asyncio.run(run_case())


def test_llm1_production_bad_task_shape_gates_before_llm2() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频",
                "task_atoms": [{"task_id": "task-bad", "constraints": {"selected_indices": [1]}}],
                "tool_plan": {"actions": ["search_inventory", "send_video", "generate_reply"]},
            },
            content="第1套视频",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                raise AssertionError("bad LLM1 task shape must not call production LLM2")

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="legacy reply",
            planner_result={"actions": ["generate_reply"]},
        )

        assert not package_passed(package)
        assert package.reply_text == ""
        assert "LLM1 production output missing or invalid task_atoms" in package_retry_reason(package)

    asyncio.run(run_case())


def test_llm2_production_malformed_claims_and_captions_force_retry() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "这套视频发我",
                "task_atoms": [{"task_id": "task-1", "task_type": "send_video"}],
                "tool_plan": {"actions": ["send_video", "generate_reply"]},
            },
            content="这套视频发我",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这是星河苑1-101房间的视频。",
                    "claims": [
                        {
                            "claim_id": "claim-bad",
                            "task_id": "task-1",
                            "field": "candidate_summary",
                            "value": "星河苑1-101",
                            "evidence_ref": "missing-evidence",
                        }
                    ],
                    "action_captions": [
                        {
                            "caption_id": "caption-bad",
                            "action_id": "missing-action",
                            "action_type": "video",
                            "text": "这是星河苑1-101房间的视频。",
                        }
                    ],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["send_video", "generate_reply"],
                "video_paths": ["C:/tmp/safe_fixture_video.mp4"],
            },
            draft_reply="这是这套房间的视频。",
            planner_result={"actions": ["send_video", "generate_reply"]},
        )

        reason = package_retry_reason(package)
        assert not package_passed(package)
        assert package.reply_text == ""
        assert "claim_1_missing_valid_evidence_ref" in reason
        assert "caption_1_unknown_action_id" in reason
        assert package.self_review["send_actions_preserved"] is True

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


def test_llm2_production_package_must_pass_outbound_validation_after_self_review_pass() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video", "user_text": "第1套视频"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="第1套视频",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "我这边按证据给你回复。",
                    "answered_task_ids": ["task-video"],
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={"actions": ["generate_reply"]},
            draft_reply="legacy reply",
            planner_result={"actions": ["generate_reply"]},
        )
        validation = validate_production_outbound_package(
            package,
            task_packet=build.packet,
            user_asked_password=False,
        )

        assert package_passed(package)
        assert validation.status == ValidationStatus.BLOCKED
        assert any(issue.code == "l2.task_not_answered" for issue in validation.blocking_issues)

    asyncio.run(run_case())


def test_production_validator_accepts_evidence_bound_viewing_contact_for_password_request() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "密码多少",
                "task_atoms": [
                    {
                        "task_id": "task-password",
                        "task_type": "inventory_search",
                        "user_text": "密码多少",
                    }
                ],
                "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
            },
            content="密码多少",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这套看房要先联系确认，我把联系方式发你。",
                    "claims": [
                        {
                            "claim_id": "claim-viewing-contact",
                            "task_id": "task-password",
                            "field": "viewing_contact",
                            "value": "需要联系确认",
                            "evidence_ref": "evd-controlled-viewing-contact-1",
                        }
                    ],
                    "action_captions": [
                        {
                            "caption_id": "caption-viewing-contact",
                            "action_id": "send-controlled-viewing-contact-1",
                            "action_type": "viewing_contact",
                            "text": "这套看房要先联系确认，我把联系方式发你。",
                        }
                    ],
                    "self_review": {"status": "pass", "llm2_decides_media_targets": False},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_listing_evidence": [
                    {
                        "evidence_id": "evd-controlled-viewing-contact-1",
                        "listing_id": "lst-201c",
                        "evidence_type": "viewing_contact",
                        "summary": "星河苑1-101看房需要联系确认。",
                        "source_kind": "viewing_rule_evidence",
                        "source_record_id": "row-hash-201c",
                        "field_values": {"room": "星河苑1-101", "needs_contact": True},
                        "sensitivity": "controlled_contact",
                        "metadata": {"controlled_channel": "viewing_contact", "evidence_bound": True},
                    }
                ],
                "send_actions": [
                    {
                        "evidence_id": "evd-controlled-viewing-contact-1",
                        "listing_id": "lst-201c",
                        "action_id": "send-controlled-viewing-contact-1",
                        "action_type": "viewing_contact",
                        "payload": {"evidence_ref": "evd-controlled-viewing-contact-1", "room": "星河苑1-101"},
                        "metadata": {"controlled_channel": "viewing_contact", "evidence_bound": True},
                        "sensitive_payload": {"contact_numbers": ["18800000000"]},
                    }
                ],
            },
            draft_reply="",
            planner_result={"actions": ["search_inventory", "generate_reply"]},
        )
        validation = validate_production_outbound_package(
            package,
            task_packet=build.packet,
            user_asked_password=True,
        )

        assert package_passed(package)
        assert validation.status == ValidationStatus.PASS
        assert package.reply_text == "看房需要联系确认，联系方式如下。"
        assert package.self_review["reply_text_owner"] == "controlled_template"

    asyncio.run(run_case())


def test_production_validator_accepts_target_error_reply_for_unbound_video_request() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "有视频就先发视频",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video", "user_text": "有视频就先发视频"}],
                "tool_plan": {"actions": ["context_tools", "send_video", "generate_reply"]},
            },
            content="有视频就先发视频",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这条我还没法直接发视频，先帮我确认具体哪一套。",
                    "claims": [
                        {
                            "claim_id": "claim-missing-target",
                            "task_id": "task-video",
                            "field": "missing_target",
                            "value": "需要先确认具体房源",
                            "evidence_ref": "evd-target-missing-1",
                        }
                    ],
                    "action_captions": [],
                    "self_review": {"status": "pass", "llm2_decides_media_targets": False},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["context_tools", "send_video", "generate_reply"],
                "missing_target_reason": "media_target_unbound",
            },
            draft_reply="",
            planner_result={"actions": ["context_tools", "send_video", "generate_reply"]},
        )
        validation = validate_production_outbound_package(
            package,
            task_packet=build.packet,
            user_asked_password=False,
        )

        assert package_passed(package), package_retry_reason(package)
        assert validation.status == ValidationStatus.PASS

    asyncio.run(run_case())


def test_production_validator_accepts_candidate_number_claims_from_evidence_field_values() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "筛出来的1和3视频发我",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video", "user_text": "筛出来的1和3视频发我"}],
                "tool_plan": {"actions": ["context_tools", "send_video", "generate_reply"]},
            },
            content="筛出来的1和3视频发我",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这是第1套和第3套的视频。",
                    "claims": [
                        {
                            "claim_id": "claim-candidate-1",
                            "task_id": "task-video",
                            "field": "candidate_number",
                            "value": 1,
                            "evidence_ref": "evd-video-1",
                        },
                        {
                            "claim_id": "claim-candidate-3",
                            "task_id": "task-video",
                            "field": "candidate_number",
                            "value": 3,
                            "evidence_ref": "evd-video-3",
                        },
                    ],
                    "action_captions": [
                        {"action_id": "send-video-1", "text": "这是第1套的视频。"},
                        {"action_id": "send-video-3", "text": "这是第3套的视频。"},
                    ],
                    "self_review": {"status": "pass", "llm2_decides_media_targets": False},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["context_tools", "send_video", "generate_reply"],
                "target_rows": [
                    {"candidate_number": 1, "listing_id": "lst-1", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"candidate_number": 3, "listing_id": "lst-3", "小区": "石桥铭苑", "房号": "21-1201B"},
                ],
                "video_paths": [
                    "room_database/video/石桥铭苑6-1102/demo.mp4",
                    "room_database/video/石桥铭苑21-1201B/demo.mp4",
                ],
            },
            draft_reply="",
            planner_result={"actions": ["context_tools", "send_video", "generate_reply"]},
        )
        validation = validate_production_outbound_package(package, task_packet=build.packet)

        assert package_passed(package), package_retry_reason(package)
        assert validation.status == ValidationStatus.PASS

    asyncio.run(run_case())


def test_production_validator_rejects_unbound_viewing_contact_for_password_request() -> None:
    async def run_case() -> None:
        build = build_kf_task_packet_shadow(
            {
                "rewritten_query": "密码多少",
                "task_atoms": [{"task_id": "task-password", "task_type": "reply_text", "user_text": "密码多少"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="密码多少",
            source_label="llm1_production",
            mode="production",
        )

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "这套看房要先联系确认。",
                    "claims": [],
                    "action_captions": [
                        {
                            "caption_id": "caption-viewing-contact",
                            "action_id": "send-controlled-viewing-contact-1",
                            "action_type": "viewing_contact",
                            "text": "这套看房要先联系确认。",
                        }
                    ],
                    "self_review": {"status": "pass", "llm2_decides_media_targets": False},
                }

        package = await compose_production_outbound_package(
            reply_generator=FakeReplyGenerator(),
            task_packet=build.packet,
            tool_evidence={
                "actions": ["generate_reply"],
                "send_actions": [
                    {
                        "action_id": "send-controlled-viewing-contact-1",
                        "action_type": "viewing_contact",
                        "payload": {"room": "星河苑1-101"},
                        "metadata": {"controlled_channel": "viewing_contact"},
                    }
                ],
            },
            draft_reply="",
            planner_result={"actions": ["generate_reply"]},
        )
        validation = validate_production_outbound_package(
            package,
            task_packet=build.packet,
            user_asked_password=True,
        )

        assert validation.status == ValidationStatus.BLOCKED
        assert any(issue.code == "l2.task_not_answered" for issue in validation.blocking_issues)

    asyncio.run(run_case())


def test_production_smoke_defaults_to_offline_contract(monkeypatch, capsys) -> None:
    from scripts import smoke_dual_llm_production

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("KF_DUAL_LLM_MODE", "production")

    exit_code = asyncio.run(smoke_dual_llm_production._run_offline_smoke())
    captured = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(captured[-1])

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["offline"] is True
    assert payload["env_file_read"] is False
    assert payload["fake_llm"] is True
    assert payload["llm_transport_invoked"] is False
    assert payload["send_transport_invoked"] is False
    assert payload["send_action_count"] == 0
    assert payload["llm2_call_count"] == 1


def test_production_smoke_offline_path_does_not_import_config_or_real_llm() -> None:
    script = Path("scripts/smoke_dual_llm_production.py").read_text(encoding="utf-8")
    offline_source = script.split("async def _run_live_smoke", 1)[0]

    assert "from app.config import settings" not in offline_source
    assert "from app.services.llm import ReplyGenerator" not in offline_source
    assert "FakeReplyGenerator" in offline_source
    assert '"env_file_read": False' in offline_source
    assert '"llm_transport_invoked": False' in offline_source
    assert "--allow-live-llm" in script
