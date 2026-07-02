from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.kf_langgraph_flow import (
    KfProductionFlowDeps,
    run_kf_production_flow,
)


def run(coro):
    return asyncio.run(coro)


def test_production_flow_runs_owner_nodes_in_order() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            return {
                "effective_query": kwargs["content"],
                "tool_plan": {"actions": ["send_video", "generate_reply"]},
            }

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            calls.append("plan")
            assert kwargs["understanding"]["effective_query"] == "Need room video"
            return {
                "actions": ["send_video", "generate_reply"],
                "reply_text": "planner text must not become final reply",
            }

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            calls.append("tools")
            assert kwargs["actions"] == ["send_video", "generate_reply"]
            return {
                "actions": kwargs["actions"],
                "target_rows": [{"listing_id": "lst-1"}],
                "video_paths": ["C:/tmp/unit.mp4"],
            }

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            calls.append("reply")
            assert kwargs["tool_evidence"]["target_rows"][0]["listing_id"] == "lst-1"
            assert kwargs["planner_result"]["reply_text"] == "planner text must not become final reply"
            return {
                "reply": "LLM2 outbound package reply",
                "draft_reply": "LLM2 outbound package reply",
                "context": {"saved": True},
            }

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
            ),
            content="Need room video",
            context={"saved": False},
            signals={"wants_video": True},
        )

        assert calls == ["understand", "plan", "tools", "reply"]
        assert result["trace"] == [
            "understand_message",
            "intent_route:housing_tools",
            "record_understanding",
            "plan_actions",
            "execute_tools",
            "generate_reply",
        ]
        assert result["status"] == "ready_to_send"
        assert result["final_reply"] == "LLM2 outbound package reply"
        assert result["context"] == {"saved": True}

    run(run_case())


def test_production_flow_rewrites_when_planner_missing_tool_plan_before_tools() -> None:
    async def run_case() -> None:
        calls: list[str] = []
        feedbacks: list[dict[str, Any]] = []

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            feedbacks.append(kwargs["planner_feedback"])
            return {"effective_query": kwargs["content"], "retry_no": len(feedbacks)}

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            calls.append("plan")
            if len([item for item in calls if item == "plan"]) == 1:
                return {
                    "actions": [],
                    "need_rewrite_clarification": True,
                    "missing_evidence": "LLM1 missing tool_plan",
                    "reply_text": "",
                }
            assert kwargs["retry_reason"] == "LLM1 missing tool_plan"
            return {"actions": ["search_inventory", "generate_reply"], "reply_text": ""}

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            calls.append("tools")
            return {"actions": kwargs["actions"], "inventory_rows": [{"listing_id": "lst-2"}]}

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            calls.append("reply")
            return {"reply": "grounded reply", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
            ),
            content="Need inventory",
            max_attempts=2,
        )

        assert calls == ["understand", "plan", "understand", "plan", "tools", "reply"]
        assert feedbacks[0] == {}
        assert feedbacks[1]["need_rewrite_clarification"] is True
        assert feedbacks[1]["missing_evidence"] == "LLM1 missing tool_plan"
        assert result["attempt"] == 1
        assert result["final_reply"] == "grounded reply"

    run(run_case())


def test_production_flow_records_understanding_context_before_planner() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            return {
                "effective_query": kwargs["content"],
                "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
            }

        async def record_understanding(**kwargs: Any) -> dict[str, Any]:
            calls.append("record")
            assert kwargs["attempt"] == 0
            return {"context": {**kwargs["context"], "current_turn_started": True}}

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            calls.append("plan")
            assert kwargs["context"]["current_turn_started"] is True
            return {"actions": ["search_inventory", "generate_reply"], "reply_text": ""}

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            calls.append("tools")
            return {"actions": kwargs["actions"], "inventory_rows": [{"listing_id": "lst"}]}

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            calls.append("reply")
            return {"reply": "ok", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                record_understanding=record_understanding,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
            ),
            content="查房源",
            context={"loaded": True},
        )

        assert calls == ["understand", "record", "plan", "tools", "reply"]
        assert result["context"]["current_turn_started"] is True

    run(run_case())


def test_production_flow_routes_final_selfcheck_retry_back_to_llm1() -> None:
    async def run_case() -> None:
        calls: list[str] = []
        feedbacks: list[dict[str, Any]] = []
        reply_calls = 0

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            feedbacks.append(kwargs["planner_feedback"])
            return {"effective_query": kwargs["content"], "retry_no": len(feedbacks)}

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            calls.append("plan")
            return {"actions": ["search_inventory", "generate_reply"], "reply_text": ""}

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            calls.append("tools")
            return {
                "actions": kwargs["actions"],
                "inventory_rows": [{"listing_id": "lst-3"}],
                "target_rows": [{"listing_id": "lst-3"}],
            }

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            nonlocal reply_calls
            calls.append("reply")
            reply_calls += 1
            if reply_calls == 1:
                return {
                    "reply": "",
                    "draft_reply": "bad draft",
                    "needs_planner_retry": True,
                    "planner_retry_reason": "final selfcheck failed",
                    "selfcheck": {"status": "retry"},
                    "context": kwargs["context"],
                }
            return {"reply": "fixed reply", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
            ),
            content="Need safer reply",
            max_attempts=2,
        )

        assert calls == [
            "understand",
            "plan",
            "tools",
            "reply",
            "understand",
            "plan",
            "tools",
            "reply",
        ]
        assert feedbacks[1]["planner_retry_reason"] == "final selfcheck failed"
        assert feedbacks[1]["selfcheck_result"] == {"status": "retry"}
        assert feedbacks[1]["tool_evidence_summary"]["inventory_rows"] == 1
        assert feedbacks[1]["tool_evidence_summary"]["target_rows"] == 1
        assert result["attempt"] == 1
        assert result["final_reply"] == "fixed reply"

    run(run_case())


def test_production_flow_stops_before_planner_when_llm1_needs_clarification() -> None:
    async def run_case() -> None:
        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            return {
                "needs_clarification": True,
                "clarification_text": "Need a room id",
            }

        async def fail_if_called(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("clarification should stop before planning")

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=fail_if_called,
                execute_tools=fail_if_called,
                generate_reply_result=fail_if_called,
            ),
            content="this one",
        )

        assert result["status"] == "needs_clarification"
        assert result["trace"] == ["understand_message", "record_understanding"]
        assert "final_reply" not in result

    run(run_case())


def test_business_qa_route_uses_knowledge_and_skips_housing_tools() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            return {
                "intent": "deposit",
                "effective_query": kwargs["content"],
                "tool_plan": {"actions": ["send_deposit_policy", "generate_reply"]},
            }

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("business QA should not use planner/tool action owner")

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("business QA should not call housing tools")

        async def retrieve_business_knowledge(**kwargs: Any) -> dict[str, Any]:
            calls.append("knowledge")
            return {
                "source": "unit_business_knowledge",
                "topics": ["deposit"],
                "rule_evidence": {
                    "deposit_policy": {
                        "service": "支付宝芝麻信用无忧住",
                        "is_free": False,
                    }
                },
            }

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            calls.append("reply")
            assert kwargs["planner_result"]["source"] == "langgraph_business_knowledge"
            assert kwargs["tool_evidence"]["business_knowledge"]["topics"] == ["deposit"]
            return {
                "reply": "免押是支付宝芝麻信用无忧住，不是免费。",
                "context": kwargs["context"],
            }

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
                retrieve_business_knowledge=retrieve_business_knowledge,
            ),
            content="免押是不是免费",
            signals={"wants_deposit": True},
        )

        assert calls == ["understand", "knowledge", "reply"]
        assert result["trace"] == [
            "understand_message",
            "intent_route:business_qa",
            "record_understanding",
            "business_knowledge",
            "generate_reply",
        ]
        assert result["route"] == "business_qa"
        assert result["final_reply"] == "免押是支付宝芝麻信用无忧住，不是免费。"

    run(run_case())


def test_business_signal_overrides_errant_housing_tool_plan_for_booking_question() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            calls.append("understand")
            return {
                "intent": "inventory",
                "effective_query": kwargs["content"],
                "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
            }

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("pure booking business QA must not use housing planner")

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("pure booking business QA must not call housing tools")

        async def retrieve_business_knowledge(**kwargs: Any) -> dict[str, Any]:
            calls.append("knowledge")
            return {
                "source": "unit_business_knowledge",
                "topics": ["contract_booking"],
                "rule_evidence": {"contract_contact": ["18758141785"]},
            }

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            calls.append("reply")
            assert kwargs["tool_evidence"]["rule_evidence"]["contract_contact"] == ["18758141785"]
            return {"reply": "定房和合同联系受控号码。", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
                retrieve_business_knowledge=retrieve_business_knowledge,
            ),
            content="客户如果看中了怎么定？",
            signals={"wants_contract_contact": True},
        )

        assert calls == ["understand", "knowledge", "reply"]
        assert result["route"] == "business_qa"
        assert result["route_reason"] == "business_intent_or_signal"
        assert result["final_reply"] == "定房和合同联系受控号码。"

    run(run_case())


def test_production_flow_preserves_sendable_tool_evidence_across_retry() -> None:
    async def run_case() -> None:
        tool_calls = 0
        reply_calls = 0

        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            return {"effective_query": kwargs["content"], "retry": True}

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            return {"actions": ["send_video", "generate_reply"], "reply_text": ""}

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            nonlocal tool_calls
            tool_calls += 1
            if tool_calls == 1:
                return {
                    "actions": kwargs["actions"],
                    "video_paths": ["C:/tmp/video.mp4"],
                    "target_rows": [{"listing_id": "lst-video"}],
                }
            return {"actions": kwargs["actions"], "video_paths": [], "target_rows": []}

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            nonlocal reply_calls
            reply_calls += 1
            if reply_calls == 1:
                return {
                    "reply": "",
                    "needs_planner_retry": True,
                    "planner_retry_reason": "selfcheck retry",
                    "context": kwargs["context"],
                }
            assert kwargs["tool_evidence"]["video_paths"] == ["C:/tmp/video.mp4"]
            assert kwargs["tool_evidence"]["target_rows"] == [{"listing_id": "lst-video"}]
            return {"reply": "video reply", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
                has_sendable_actions=lambda evidence: bool(evidence.get("video_paths")),
                merge_preserved_sendable_evidence=lambda current, preserved: {
                    **current,
                    "video_paths": preserved.get("video_paths") or current.get("video_paths") or [],
                    "target_rows": preserved.get("target_rows") or current.get("target_rows") or [],
                },
            ),
            content="video please",
            max_attempts=2,
        )

        assert result["final_reply"] == "video reply"
        assert result["preserved_sendable_evidence"]["video_paths"] == ["C:/tmp/video.mp4"]

    run(run_case())


def test_production_flow_keeps_reply_node_tool_evidence_mutations() -> None:
    async def run_case() -> None:
        async def understand_message(**kwargs: Any) -> dict[str, Any]:
            return {
                "effective_query": kwargs["content"],
                "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
            }

        async def plan_actions(**kwargs: Any) -> dict[str, Any]:
            return {"actions": ["search_inventory", "generate_reply"], "reply_text": ""}

        async def execute_tools(**kwargs: Any) -> dict[str, Any]:
            return {"actions": kwargs["actions"], "inventory_rows": [{"listing_id": "lst"}]}

        async def generate_reply_result(**kwargs: Any) -> dict[str, Any]:
            kwargs["tool_evidence"]["llm2_production_outbound_package"] = {
                "reply_text": "prepared text",
                "reply_source": "kf_llm2_outbound_production",
            }
            kwargs["tool_evidence"]["outbound_package"] = {"prepared_outbound_package": True}
            return {"reply": "prepared text", "context": kwargs["context"]}

        result = await run_kf_production_flow(
            KfProductionFlowDeps(
                understand_message=understand_message,
                plan_actions=plan_actions,
                execute_tools=execute_tools,
                generate_reply_result=generate_reply_result,
            ),
            content="查房源",
        )

        assert result["tool_evidence"]["llm2_production_outbound_package"]["reply_text"] == "prepared text"
        assert result["tool_evidence"]["outbound_package"]["prepared_outbound_package"] is True

    run(run_case())


def test_main_langgraph_gate_requires_feature_flag_and_production(monkeypatch) -> None:
    import app.main as main

    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
    monkeypatch.setattr(main.settings, "kf_langgraph_enabled", True)
    assert main._langgraph_production_flow_enabled() is True

    monkeypatch.setattr(main.settings, "kf_langgraph_enabled", False)
    with pytest.raises(RuntimeError, match="requires KF_LANGGRAPH_ENABLED=true"):
        main._langgraph_production_flow_enabled()

    monkeypatch.setattr(main.settings, "kf_langgraph_enabled", True)
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "shadow")
    assert main._langgraph_production_flow_enabled() is False


def test_main_langgraph_branch_uses_existing_send_boundary(monkeypatch) -> None:
    import app.main as main

    async def run_case() -> None:
        flow_calls: list[dict[str, Any]] = []
        sends: list[dict[str, Any]] = []
        saved_contexts: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        processed: list[str] = []

        async def fake_flow(deps: Any, **kwargs: Any) -> dict[str, Any]:
            flow_calls.append(kwargs)
            return {
                "status": "ready_to_send",
                "trace": ["understand_message", "record_understanding", "plan_actions", "execute_tools", "generate_reply"],
                "understanding": {"effective_query": kwargs["content"]},
                "context": {"from_graph": True},
                "planner_result": {"actions": ["generate_reply"], "reply_text": ""},
                "tool_evidence": {"actions": ["generate_reply"]},
                "reply_result": {
                    "reply": "LangGraph final reply",
                    "draft_reply": "LangGraph final reply",
                    "context": {"from_graph": True},
                },
                "final_reply": "LangGraph final reply",
                "final_draft_reply": "LangGraph final reply",
            }

        async def fake_send_final_actions(**kwargs: Any) -> dict[str, Any]:
            sends.append(kwargs)
            return {
                "sent_actions": [{"type": "text", "text": kwargs["final_reply"]}],
                "context": kwargs["context"],
            }

        monkeypatch.setattr(main.kf_langgraph_flow, "run_kf_production_flow", fake_flow)
        monkeypatch.setattr(main, "_send_final_actions", fake_send_final_actions)
        monkeypatch.setattr(main, "_save_context", lambda open_kfid, external_userid, context: saved_contexts.append(context))
        monkeypatch.setattr(main, "_build_orchestrator_shadow_artifact", lambda **kwargs: artifacts.append(kwargs) or kwargs)
        monkeypatch.setattr(main, "_raise_if_stale_kf_turn", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            main,
            "wecom_kf",
            SimpleNamespace(state_store=SimpleNamespace(mark_processed=lambda msgid: processed.append(msgid))),
        )

        await main._process_text_turn_with_langgraph_production_flow(
            open_kfid="kf",
            external_userid="user",
            conversation_key="kf:user",
            content="客户问房源",
            msgids=["msg-1"],
            generation=1,
            context={"structured_turn": True},
            signals={"wants_inventory": True},
            merged_message_count=1,
            timer=main.kf_turn_flow.RagStageTimer(),
            inventory_read_context=None,
        )

        assert flow_calls[0]["content"] == "客户问房源"
        assert "understanding" not in flow_calls[0]
        assert sends[0]["final_reply"] == "LangGraph final reply"
        assert sends[0]["tool_evidence"] == {"actions": ["generate_reply"]}
        assert artifacts[0]["final_reply"] == "LangGraph final reply"
        assert artifacts[0]["graph_state"]["trace"] == [
            "understand_message",
            "record_understanding",
            "plan_actions",
            "execute_tools",
            "generate_reply",
        ]
        assert artifacts[0]["graph_state"]["status"] == "ready_to_send"
        assert processed == ["msg-1"]
        assert saved_contexts

    run(run_case())


def test_main_production_text_turn_enters_full_langgraph_before_understanding(monkeypatch) -> None:
    import app.main as main

    async def run_case() -> None:
        flow_calls: list[dict[str, Any]] = []
        cleanup_calls: list[tuple[str, int]] = []

        async def fail_understand_message(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("production LangGraph must own the understanding node")

        async def fake_langgraph_turn(**kwargs: Any) -> None:
            flow_calls.append(kwargs)

        async def fake_cleanup(conversation_key: str, generation: int) -> None:
            cleanup_calls.append((conversation_key, generation))

        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        monkeypatch.setattr(main.settings, "kf_langgraph_enabled", True)
        monkeypatch.setattr(main, "_create_inventory_read_context", lambda **kwargs: None)
        monkeypatch.setattr(main, "_load_context", lambda open_kfid, external_userid: {})
        monkeypatch.setattr(main, "_remember_inventory_read_context", lambda context, inventory_read_context: context)
        monkeypatch.setattr(main, "_deterministic_signals", lambda content: {"unit": True})
        monkeypatch.setattr(main, "_understand_message", fail_understand_message)
        monkeypatch.setattr(main, "_process_text_turn_with_langgraph_production_flow", fake_langgraph_turn)
        monkeypatch.setattr(main, "_cleanup_kf_turn", fake_cleanup)

        await main._process_text_turn(
            open_kfid="kf",
            external_userid="user",
            pending_items=[{"msgid": "msg-1", "content": "房源表发我"}],
            generation=7,
        )

        assert len(flow_calls) == 1
        assert flow_calls[0]["content"] == "房源表发我"
        assert flow_calls[0]["signals"] == {"unit": True}
        assert flow_calls[0]["msgids"] == ["msg-1"]
        assert cleanup_calls == [(main._conversation_key("kf", "user"), 7)]

    run(run_case())
