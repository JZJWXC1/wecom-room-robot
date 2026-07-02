from __future__ import annotations

import asyncio

import pytest

import app.main as main
from app.services import kf_context_memory
from app.services.kf_dual_llm_production import (
    DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE,
    tool_plan_from_task_packet,
)
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow


def _production_understanding(
    *,
    content: str,
    rewritten_query: str,
    task_atoms: list[dict],
    actions: list[str],
    intent: str = "inventory",
) -> dict:
    build = build_kf_task_packet_shadow(
        {
            "rewritten_query": rewritten_query,
            "task_atoms": task_atoms,
            "tool_plan": {"actions": actions},
        },
        content=content,
        source_label="llm1_production",
        mode="production",
    )
    return {
        "intent": intent,
        "effective_query": rewritten_query,
        "rewritten_query": rewritten_query,
        "llm1_task_packet": build.packet.to_safe_dict(),
        "tool_plan": tool_plan_from_task_packet(build.packet),
        "dual_llm_production": {
            "llm1": {"status": "pass", "source": "llm1_production"}
        },
    }


def test_plan_actions_production_does_not_call_legacy_planner_owners(monkeypatch) -> None:
    async def run_case() -> None:
        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        assert not hasattr(main, "kf_legacy_planner")

        planner = await main._plan_actions(
            content="房源表、免押、视频都发我一下",
            context=kf_context_memory.empty_context(),
            understanding=_production_understanding(
                content="房源表、免押、视频都发我一下",
                rewritten_query="房源表、免押、视频都发我一下",
                task_atoms=[{"task_id": "task-reply", "task_type": "reply_text"}],
                actions=["generate_reply"],
                intent="media",
            ),
            signals={
                "wants_inventory_sheet": True,
                "wants_deposit": True,
                "wants_video": True,
            },
        )

        assert planner["actions"] == ["generate_reply"]
        assert planner["reply_text"] == ""
        assert "send_inventory_sheet" not in planner["actions"]
        assert "send_deposit_policy" not in planner["actions"]
        assert "send_video" not in planner["actions"]

    asyncio.run(run_case())


def test_generate_reply_result_production_empty_llm2_uses_controlled_renderer_not_legacy_reply(
    monkeypatch,
) -> None:
    async def run_case() -> None:
        async def fail_retrieve_for_reply(**kwargs):
            raise AssertionError("production reply generation must not call legacy RAG retrieve")

        async def fake_snapshot(*args, **kwargs):
            return ""

        def fail_legacy_final_selfcheck(*args, **kwargs):
            raise AssertionError(
                "production final selfcheck must be owned by kf_outbound_validation"
            )

        class EmptyLlm2ReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {}

            async def assess_kf_final_reply(self, **kwargs):
                fail_legacy_final_selfcheck()

        tool_evidence = {
            "actions": ["send_video", "generate_reply"],
            "target_rows": [
                {
                    "listing_id": "lst-xinghe-101",
                    "小区": "星河苑",
                    "房号": "1-101",
                }
            ],
            "video_rows": [
                {
                    "listing_id": "lst-xinghe-101",
                    "小区": "星河苑",
                    "房号": "1-101",
                }
            ],
            "video_paths": ["C:/tmp/safe_fixture_video.mp4"],
        }

        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        monkeypatch.setattr(main, "reply_generator", EmptyLlm2ReplyGenerator())
        monkeypatch.setattr(main.agentic_rag, "retrieve_for_reply", fail_retrieve_for_reply)
        monkeypatch.setattr(main.inventory, "snapshot", fake_snapshot)
        assert not hasattr(main, "_legacy_reply_builder")
        monkeypatch.setattr(main.agentic_rag, "assess_reply", fail_legacy_final_selfcheck)
        monkeypatch.setattr(main, "_local_human_context_selfcheck", fail_legacy_final_selfcheck)

        result = await main._generate_reply_result(
            content="第1套视频发我",
            context=kf_context_memory.empty_context(),
            understanding=_production_understanding(
                content="第1套视频发我",
                rewritten_query="第1套视频",
                task_atoms=[{"task_id": "task-video", "task_type": "send_video"}],
                actions=["send_video", "generate_reply"],
                intent="media",
            ),
            tool_evidence=tool_evidence,
            planner_result={"actions": ["send_video", "generate_reply"], "reply_text": ""},
        )

        assert result["needs_planner_retry"] is False
        assert result["reply"] == "这是星河苑1-101房间的视频。"
        assert result["draft_reply"] == result["reply"]
        assert tool_evidence["deterministic_reply_source"] == (
            DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
        )
        assert tool_evidence["llm2_production_outbound_package"]["reply_source"] == (
            DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE
        )

    asyncio.run(run_case())


def test_send_final_actions_production_blocks_before_any_text_without_prepared_package(
    monkeypatch,
) -> None:
    async def run_case() -> None:
        async def fail_send_text_with_receipt(**kwargs):
            raise AssertionError("production must validate PreparedOutboundPackage before sending text")

        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        monkeypatch.setattr(main, "_send_text_with_receipt", fail_send_text_with_receipt)

        result = await main._send_final_actions(
            open_kfid="kf",
            external_userid="user",
            context={},
            final_reply="这段没有 PreparedOutboundPackage，不能先发。",
            tool_evidence={"actions": ["generate_reply"]},
            msgids=["msg-1"],
        )

        assert result["send_blocked"] is True
        assert result["sent_actions"] == []
        assert "before any visible send action" in result["reason"]

    asyncio.run(run_case())


def test_controlled_viewing_contact_does_not_mix_password_for_plain_viewing() -> None:
    tool_evidence = {
        "actions": ["explain_unavailable_viewing", "generate_reply"],
        "rule_evidence": {"viewing_contact": list(main.CONTACT_NUMBERS)},
    }

    main._attach_controlled_outbound_channels(
        tool_evidence,
        content="怎么看房，今天能不能自己看？",
        understanding={"effective_query": "咨询今天怎么自己看房"},
    )

    evidence = tool_evidence["inventory_listing_evidence"][0]
    action = tool_evidence["send_actions"][0]
    assert evidence["summary"] == "看房需要联系确认。"
    assert "密码" not in evidence["summary"]
    assert evidence["field_values"]["room"] == "看房"
    assert action["sensitive_payload"]["room"] == "看房"
    assert action["metadata"]["viewing_exception"] is False


def test_controlled_viewing_contact_keeps_exception_when_user_reports_door_issue() -> None:
    tool_evidence = {
        "actions": ["explain_unavailable_viewing", "generate_reply"],
        "rule_evidence": {"viewing_contact": list(main.CONTACT_NUMBERS)},
    }

    main._attach_controlled_outbound_channels(
        tool_evidence,
        content="门打不开怎么办？",
        understanding={"effective_query": "咨询门打不开怎么办"},
    )

    evidence = tool_evidence["inventory_listing_evidence"][0]
    action = tool_evidence["send_actions"][0]
    assert evidence["summary"] == "看房或密码异常需要联系确认。"
    assert evidence["field_values"]["room"] == "看房/密码异常"
    assert action["sensitive_payload"]["room"] == "看房/密码异常"
    assert action["metadata"]["viewing_exception"] is True


def test_langgraph_business_knowledge_does_not_call_legacy_rag_retrieve(monkeypatch) -> None:
    async def run_case() -> None:
        async def fail_retrieve_for_reply(**kwargs):
            raise AssertionError("business_qa must use business knowledge, not legacy RAG retrieve")

        monkeypatch.setattr(main.agentic_rag, "retrieve_for_reply", fail_retrieve_for_reply)

        result = await main._retrieve_business_knowledge_for_langgraph(
            content="免押是不是免费？服务费怎么算？",
            context=kf_context_memory.empty_context(),
            understanding={"intent": "deposit", "effective_query": "免押是不是免费？服务费怎么算？"},
            signals={"wants_deposit": True},
        )

        assert result["source"] == "langgraph_business_knowledge"
        assert "deposit_waiver" in result["topics"]
        assert "无忧住" in result["knowledge_context"]
        assert result["rule_evidence"]["deposit_policy"]["name"] == "支付宝无忧住信用免押"

    asyncio.run(run_case())


@pytest.mark.parametrize(
    "content",
    [
        "客户如果看中了怎么定？",
        "客户想定其中一套怎么操作？",
        "最后说下免押和定房流程。",
    ],
)
def test_deterministic_signals_classify_booking_questions_as_contract_contact(content: str) -> None:
    signals = main._deterministic_signals(content)

    assert signals["wants_contract_contact"] is True


@pytest.mark.parametrize("content", ["这个水电", "哪套更低"])
def test_llm1_contract_rejects_field_questions_with_media_actions(content: str) -> None:
    needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
        content,
        {
            "actions": [
                "search_inventory",
                "context_tools",
                "send_video",
                "explain_missing_media",
                "generate_reply",
            ]
        },
    )

    assert needs_retry
    assert "must not include" in reason
    assert "send_video" in reason
    assert "explain_missing_media" in reason
