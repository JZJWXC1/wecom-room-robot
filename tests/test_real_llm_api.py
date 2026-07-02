from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from app.config import settings
from app.services.kf_dual_llm_production import (
    compose_production_outbound_package,
    package_passed,
    package_retry_reason,
    tool_plan_from_task_packet,
    validate_production_outbound_package,
)
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow
from app.services.llm import ReplyGenerator

pytestmark = pytest.mark.online

_REAL_LLM_CANARY = "DO_NOT_ECHO_REAL_LLM_CANARY"


def _require_real_llm_api() -> tuple[str, str]:
    if os.environ.get("RUN_ONLINE_QA") != "1":
        pytest.skip("需要 RUN_ONLINE_QA=1 才运行真实在线测试")
    if os.environ.get("RUN_REAL_LLM_API_TESTS") != "1":
        pytest.skip("需要 RUN_REAL_LLM_API_TESTS=1 才调用真实 LLM API")

    provider = os.environ.get("REAL_LLM_API_PROVIDER", "dashscope").strip().lower()
    key_names = {
        "dashscope": "DASHSCOPE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    key_name = key_names.get(provider)
    if not key_name:
        pytest.skip(f"当前真实 LLM API 测试仅支持 dashscope/deepseek，不支持 {provider!r}")
    api_key = os.environ.get(key_name, "").strip()
    if not api_key:
        pytest.skip(f"{key_name} 未设置，跳过真实 LLM API 测试")
    return provider, api_key


def _configure_real_llm_provider(monkeypatch: pytest.MonkeyPatch, provider: str, api_key: str) -> None:
    monkeypatch.setattr(settings, "llm_rewrite_provider", provider)
    monkeypatch.setattr(settings, "llm_reply_provider", provider)
    if provider == "deepseek":
        monkeypatch.setattr(settings, "deepseek_api_key", api_key)
        if os.environ.get("DEEPSEEK_BASE_URL"):
            monkeypatch.setattr(settings, "deepseek_base_url", os.environ["DEEPSEEK_BASE_URL"])
        if os.environ.get("REAL_LLM_REWRITE_MODEL") or os.environ.get("DEEPSEEK_REWRITE_MODEL"):
            monkeypatch.setattr(
                settings,
                "deepseek_rewrite_model",
                os.environ.get("REAL_LLM_REWRITE_MODEL") or os.environ["DEEPSEEK_REWRITE_MODEL"],
            )
        if os.environ.get("REAL_LLM_REPLY_MODEL") or os.environ.get("DEEPSEEK_REPLY_MODEL"):
            monkeypatch.setattr(
                settings,
                "deepseek_reply_model",
                os.environ.get("REAL_LLM_REPLY_MODEL") or os.environ["DEEPSEEK_REPLY_MODEL"],
            )
        return

    monkeypatch.setattr(settings, "dashscope_api_key", api_key)
    if os.environ.get("DASHSCOPE_BASE_URL"):
        monkeypatch.setattr(settings, "dashscope_base_url", os.environ["DASHSCOPE_BASE_URL"])
    if os.environ.get("REAL_LLM_REWRITE_MODEL") or os.environ.get("DASHSCOPE_REWRITE_MODEL"):
        monkeypatch.setattr(
            settings,
            "dashscope_rewrite_model",
            os.environ.get("REAL_LLM_REWRITE_MODEL") or os.environ["DASHSCOPE_REWRITE_MODEL"],
        )
    if os.environ.get("REAL_LLM_REPLY_MODEL") or os.environ.get("DASHSCOPE_REPLY_MODEL"):
        monkeypatch.setattr(
            settings,
            "dashscope_reply_model",
            os.environ.get("REAL_LLM_REPLY_MODEL") or os.environ["DASHSCOPE_REPLY_MODEL"],
        )


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _assert_api_keys_not_in(payload: Any) -> None:
    dumped = _dump(payload)
    for key_name in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(key_name, "").strip()
        if value:
            assert value not in dumped


def _visible_text(package: Any) -> str:
    captions = " ".join(caption.text for caption in package.action_captions)
    return f"{package.reply_text} {captions}".strip()


def test_real_llm1_production_outputs_tool_plan_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, api_key = _require_real_llm_api()
    _configure_real_llm_provider(monkeypatch, provider, api_key)
    generator = ReplyGenerator()

    packet = asyncio.run(
        generator.build_kf_task_packet(
            content="房源表发我一下",
            raw_dialog_context=[],
            structured_memory={},
            inventory_index={
                "available_fields": ["小区", "房号", "押一付一", "押二付一"],
                "exact_community_hits": [],
                "area_aliases": [],
            },
            candidate_set=[],
            conversation_id="real-llm-api-test",
            turn_id="llm1-production-contract",
            mode="production",
        )
    )

    metadata = packet.legacy_unknown_fields["llm1_production"]
    metadata_plan = metadata["tool_plan"]
    plan = tool_plan_from_task_packet(packet)

    assert packet.tasks
    assert metadata["mode"] == "production"
    assert metadata["prompt_artifact"]["source"] == "production"
    assert plan.get("retry_required") is not True
    assert "send_inventory_sheet" in plan["actions"]
    assert plan["reply_text"] == ""
    assert "reply_text" not in metadata_plan
    assert "final_reply" not in metadata_plan
    assert "pre_tool_reply_text" not in metadata_plan
    _assert_api_keys_not_in(packet.to_safe_dict())


def test_real_llm2_production_package_preserves_program_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, api_key = _require_real_llm_api()
    _configure_real_llm_provider(monkeypatch, provider, api_key)
    generator = ReplyGenerator()
    task_build = build_kf_task_packet_shadow(
        {
            "rewritten_query": "棠润府15-2-801B视频发我",
            "task_atoms": [
                {
                    "task_id": "task-video",
                    "task_type": "send_video",
                    "user_text": "棠润府15-2-801B视频发我",
                },
                {
                    "task_id": "task-compose",
                    "task_type": "reply_compose_signal",
                    "user_text": "棠润府15-2-801B视频发我",
                },
            ],
            "tool_plan": {"actions": ["search_inventory", "context_tools", "send_video", "generate_reply"]},
        },
        content="棠润府15-2-801B视频发我",
        conversation_id="real-llm-api-test",
        turn_id="llm2-production-contract",
        source_label="llm1_production",
        mode="production",
    )

    package = asyncio.run(
        compose_production_outbound_package(
            reply_generator=generator,
            task_packet=task_build.packet,
            tool_evidence={
                "actions": ["search_inventory", "context_tools", "send_video", "generate_reply"],
                "video_paths": ["tests/fixtures/qa/real_llm_fixture_video.mp4"],
                "video_rows": [
                    {
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "community": "棠润府",
                        "room_no": "15-2-801B",
                        "listing_id": "lst-trf-801b",
                    }
                ],
                "inventory_listing_evidence": [
                    {
                        "evidence_id": "evd-listing-1",
                        "listing_id": "lst-trf-801b",
                        "evidence_type": "inventory_listing",
                        "summary": "棠润府15-2-801B 是本轮视频目标。",
                        "field_values": {"小区": "棠润府", "房号": "15-2-801B"},
                    }
                ],
                "field_values": {"test_canary_marker": _REAL_LLM_CANARY},
            },
            draft_reply="",
            planner_result={"actions": ["search_inventory", "context_tools", "send_video", "generate_reply"]},
        )
    )

    assert package_passed(package), package_retry_reason(package)
    assert package.reply_source == "kf_llm2_outbound_production"
    assert package.self_review["llm2_decides_media_targets"] is False
    assert [action.action_id for action in package.send_actions] == ["send-video-1"]
    assert [action.action_type for action in package.send_actions] == ["video"]
    assert {caption.action_id for caption in package.action_captions} == {"send-video-1"}

    visible = _visible_text(package)
    assert visible
    assert "视频" in visible
    assert _REAL_LLM_CANARY not in visible
    for forbidden in ("ToolEvidence", "send action", "evidence_id", "listing_id", "稍后", "等下", "会发你"):
        assert forbidden not in visible

    validation = validate_production_outbound_package(
        package,
        task_packet=task_build.packet,
        user_asked_password=False,
    )
    assert validation.send_allowed, validation.to_dict()
    _assert_api_keys_not_in(package.to_safe_dict())
