from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
        assert package.send_actions == []
        assert package.self_review["llm2_decides_media_targets"] is False
        assert package.self_review["ignored_llm_send_actions"] is True

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
