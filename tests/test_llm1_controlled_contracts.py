from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.main as main
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow


class AlwaysBadTaskPacketReplyGenerator:
    def __init__(self, *, task_type: str = "reply_text", actions: list[str] | None = None) -> None:
        self.calls = 0
        self.feedback: list[dict] = []
        self.task_type = task_type
        self.actions = list(actions or ["generate_reply"])

    async def build_kf_task_packet(self, **kwargs):
        self.calls += 1
        self.feedback.append(dict(kwargs.get("planner_feedback") or {}))
        content = str(kwargs.get("content") or "")
        return build_kf_task_packet_shadow(
            {
                "rewritten_query": content,
                "task_atoms": [{"task_id": "task-bad", "task_type": self.task_type, "user_text": content}],
                "tool_plan": {"actions": self.actions},
            },
            content=content,
            source_label="llm1_production",
            mode="production",
        ).packet


async def _apply_bad_llm1_case(
    *,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    bad_task_type: str = "reply_text",
    bad_actions: list[str] | None = None,
):
    fake_reply = AlwaysBadTaskPacketReplyGenerator(task_type=bad_task_type, actions=bad_actions)
    monkeypatch.setattr(main, "reply_generator", fake_reply)
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")

    result = await main._apply_llm1_production_task_packet(
        content=content,
        context={"conversation_id": "conv-contract"},
        result={"structured_task": {}, "planner_feedback": {}},
        rewrite_view={
            "raw_dialog_context": [{"role": "assistant", "content": "1. 星河苑1-101 2. 棠润府15-2-801B"}],
            "last_candidate_set": {
                "candidate_set_id": "cand-contract",
                "candidates": [
                    {"candidate_number": 1, "community": "星河苑", "room_no": "1-101"},
                    {"candidate_number": 2, "community": "棠润府", "room_no": "15-2-801B"},
                ],
            },
        },
        inventory_index={},
        inventory_read_context=SimpleNamespace(
            request_id="req-contract",
            turn_id="turn-contract",
            decision_id="case-contract",
            snapshot_id="snapshot-contract",
        ),
    )
    return fake_reply, result


@pytest.mark.parametrize(
    ("content", "expected_attempt", "expected_actions", "expected_task_type"),
    [
        ("房源表", "controlled_inventory_sheet_contract", ["send_inventory_sheet", "generate_reply"], "send_inventory_sheet"),
        (
            "发最新房源表图片，不要文字列表",
            "controlled_inventory_sheet_contract",
            ["send_inventory_sheet", "generate_reply"],
            "send_inventory_sheet",
        ),
        ("能免押吗", "controlled_deposit_contract", ["send_deposit_policy", "generate_reply"], "deposit_policy"),
        ("合同联系方式", "controlled_contract_contact_contract", ["send_contract_contact", "generate_reply"], "contract_contact"),
        (
            "密码多少",
            "controlled_viewing_contract",
            ["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"],
            "viewing_guidance",
        ),
        (
            "第一套户型特点怎么样",
            "controlled_inventory_field_contract",
            ["search_inventory", "context_tools", "generate_reply"],
            "inventory_search",
        ),
        (
            "星桥锦绣嘉苑2000左右的一室还有吗？名字可能打错了",
            "controlled_inventory_search_contract",
            ["search_inventory", "context_tools", "generate_reply"],
            "inventory_search",
        ),
        (
            "棠润府15-2-801B还在吗？客户可能把小区名写错了",
            "controlled_inventory_search_contract",
            ["search_inventory", "context_tools", "generate_reply"],
            "inventory_search",
        ),
    ],
)
def test_llm1_final_contract_retry_uses_controlled_packet_for_short_intents(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_attempt: str,
    expected_actions: list[str],
    expected_task_type: str,
) -> None:
    async def run_case() -> None:
        fake_reply, result = await _apply_bad_llm1_case(
            monkeypatch=monkeypatch,
            content=content,
            bad_task_type="inventory_search",
            bad_actions=["generate_reply"],
        )

        assert fake_reply.calls == 2
        assert fake_reply.feedback[1]["retry_target"] == "llm1"
        assert result["tool_plan"]["actions"] == expected_actions
        assert result["tool_plan"]["reply_text"] == ""
        assert result["tool_plan"]["owner"] == "llm1_fallback"
        assert result["llm1_task_packet"]["tasks"][0]["task_type"] == expected_task_type
        assert result["dual_llm_production"]["llm1"]["attempt"] == expected_attempt
        assert result["dual_llm_production"]["llm1"]["status"] == "pass"

    asyncio.run(run_case())


def test_llm1_controlled_viewing_contract_preserves_candidate_number(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_case() -> None:
        _fake_reply, result = await _apply_bad_llm1_case(
            monkeypatch=monkeypatch,
            content="第二套今天能看吗",
            bad_task_type="inventory_search",
            bad_actions=["search_inventory", "generate_reply"],
        )

        assert result["dual_llm_production"]["llm1"]["attempt"] == "controlled_viewing_contract"
        assert result["selected_indices"] == [2]
        assert result["constraint_proof"]["selected_indices"] == [2]
        assert result["llm1_task_packet"]["tasks"][0]["constraints"]["candidate_numbers"] == [2]

    asyncio.run(run_case())


def test_controlled_llm1_contract_packet_rejects_unowned_actions() -> None:
    with pytest.raises(ValueError, match="whitelisted"):
        main._controlled_llm1_contract_packet(
            content="随便推荐几套吧",
            raw_dialog_context=[],
            structured_memory={},
            inventory_index={},
            candidate_set={},
            conversation_id="conv",
            turn_id="turn",
            case_id="case",
            inventory_snapshot_id="snapshot",
            candidate_set_id="cand",
            source_label="unit_controlled_bad_contract",
            task_type="inventory_search",
            actions=["recommend_inventory", "generate_reply"],
            required_tools=["reply.compose"],
            reason="unit should fail closed",
        )


def test_llm1_short_ack_after_bad_retry_uses_reply_compose_without_send_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_case() -> None:
        fake_reply, result = await _apply_bad_llm1_case(
            monkeypatch=monkeypatch,
            content="好的",
            bad_task_type="send_inventory_sheet",
            bad_actions=["send_inventory_sheet", "generate_reply"],
        )

        assert fake_reply.calls == 2
        assert result["tool_plan"]["actions"] == ["generate_reply"]
        assert result["llm1_task_packet"]["tasks"][0]["task_type"] == "reply_compose_signal"
        assert result["dual_llm_production"]["llm1"]["attempt"] == "controlled_ack_contract"

    asyncio.run(run_case())


def test_shadow_missing_tool_plan_uses_controlled_rewrite_requirements_for_inventory_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "shadow")

    result = main._plan_actions_shadow_or_legacy(
        initial_result={},
        understanding={
            "tool_requirements": {"needs_inventory_search": True},
            "constraint_proof": {
                "area": "拱墅万达",
                "budget_range": [1100, 1700],
                "layout": "一室",
            },
        },
        signals={},
    )

    assert result["actions"] == ["search_inventory", "context_tools", "generate_reply"]
    assert result["need_rewrite_clarification"] is False
    assert result["controlled_reason"] == "rewrite_requirements_inventory_search"
    assert result["source"].startswith("controlled_task_packet_from_rewrite_requirements")


def test_shadow_existing_missing_gate_uses_controlled_rewrite_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "shadow")

    result = main._plan_actions_shadow_or_legacy(
        initial_result={
            "actions": [],
            "need_rewrite_clarification": True,
            "missing_evidence": "LLM1/task packet did not provide tool_plan",
            "source": "missing_tool_plan_gate",
            "reply_text": "",
        },
        understanding={"tool_requirements": {"needs_inventory_search": True}},
        signals={},
    )

    assert result["actions"] == ["search_inventory", "context_tools", "generate_reply"]
    assert result["need_rewrite_clarification"] is False
    assert result["controlled_reason"] == "rewrite_requirements_inventory_search"


def test_shadow_structured_task_required_tools_use_controlled_rewrite_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "shadow")

    result = main._plan_actions_shadow_or_legacy(
        initial_result={
            "actions": [],
            "need_rewrite_clarification": True,
            "source": "missing_tool_plan_gate",
            "reply_text": "",
        },
        understanding={
            "structured_task": {
                "tasks": [
                    {
                        "task_type": "inventory_search",
                        "required_tools": ["inventory.search", "reply.compose"],
                    }
                ]
            }
        },
        signals={},
    )

    assert result["actions"] == ["search_inventory", "context_tools", "generate_reply"]
    assert result["need_rewrite_clarification"] is False
    assert result["controlled_reason"] == "rewrite_requirements_inventory_search"


def test_controlled_evidence_renderer_lists_inventory_candidates_from_tools() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="文教附近3500-4500有一室吗？客户想今天先筛两套。",
        understanding={"constraint_proof": {"layout": "一室", "budget_range": [3500, 4500]}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "generate_reply"],
            "inventory_rows": [
                {
                    "区域": "文教",
                    "小区": "学院路小区",
                    "房号": "1-101",
                    "户型分类": "一室",
                    "押一付一": "3800",
                    "备注": "民用水电",
                },
                {
                    "区域": "文教",
                    "小区": "翠苑一区",
                    "房号": "2-202",
                    "户型分类": "一室",
                    "押一付一": "4200",
                },
            ],
        },
    )

    assert "按最新房源表" in reply
    assert "学院路小区1-101" in reply
    assert "翠苑一区2-202" in reply
    assert "4600" not in reply


def test_controlled_evidence_renderer_reports_empty_inventory_candidates() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="文教附近3500-4500有一室吗？",
        understanding={"constraint_proof": {"layout": "一室", "budget_range": [3500, 4500]}},
        tool_evidence={"actions": ["search_inventory", "context_tools", "generate_reply"], "inventory_rows": []},
    )

    assert reply == "按最新房源表，这个条件暂时没查到匹配房源。你可以把预算、区域或户型放宽一点，我再查。"


def test_deterministic_signals_treat_short_graph_phrase_as_room_image() -> None:
    assert main._deterministic_signals("第一套图也发一下。")["wants_image"] is True
    assert main._deterministic_signals("第一个图发我。")["wants_image"] is True
    assert main._deterministic_signals("房源表图片发我一下。")["wants_inventory_sheet"] is True
    assert main._deterministic_signals("房源表图片发我一下。")["wants_image"] is False


def test_candidate_set_memory_ignores_selected_index_followup_rows() -> None:
    assert (
        main._should_remember_candidate_set(
            content="第一套图也发一下。",
            understanding={
                "intent": "inventory",
                "effective_query": "3500-4500 一室 在租房源",
                "constraint_proof": {"selected_indices": [1], "budget_range": [3500, 4500]},
            },
            rows=[{"小区": "兴业杨家府", "房号": "4-1502"}],
        )
        is False
    )


def test_controlled_evidence_renderer_contract_contact_from_business_rule() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="客户看中了怎么定房，合同联系谁？",
        understanding={"constraint_proof": {}},
        tool_evidence={"actions": ["send_contract_contact", "generate_reply"]},
    )

    assert "定房" in reply
    assert "18758141785" in reply
    assert "13282125992" in reply
    assert "19941091943" in reply


def test_controlled_evidence_renderer_viewing_contact_does_not_leak_password() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="这套今天能看吗，密码多少？",
        understanding={"constraint_proof": {}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"],
            "target_rows": [{"小区": "杨家新雅苑", "房号": "15-603"}],
        },
    )

    assert "杨家新雅苑15-603" in reply
    assert "直接联系" in reply
    assert "密码是" not in reply
    assert "18758141785" in reply


def test_controlled_evidence_renderer_unbound_video_request_asks_for_room_identity() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="1和2视频发我。",
        understanding={"constraint_proof": {"wants_video": True, "selected_indices": [1, 2]}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
            "candidate_binding": {
                "status": "error",
                "selected_indices": [1, 2],
                "candidate_count": 0,
            },
            "inventory_rows": [],
            "target_rows": [],
            "video_paths": [],
            "missing_media": [],
        },
    )

    assert "上一轮没有可用候选编号" in reply
    assert "第1套、第2套" in reply


def test_controlled_evidence_renderer_original_video_followup_does_not_reask_identity() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="第一个有没有原视频，客户要保存转发。",
        understanding={"constraint_proof": {"wants_original_video": True}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
            "field_target_error": {"reason": "original_video_followup_missing_stable_video_target"},
        },
    )

    assert "原视频/高清下载链接" in reply
    assert "回我序号" not in reply
    assert "小区名+房号" not in reply


def test_controlled_evidence_renderer_selection_error_without_candidate_context() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="第一套图也发一下。",
        understanding={"constraint_proof": {"selected_indices": [1]}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "generate_reply"],
            "selection_error": {
                "reason": "missing_current_candidate_set",
                "requested_indices": [1],
                "candidate_count": 0,
            },
        },
    )

    assert "第1套" in reply
    assert "不能按" in reply
    assert "上一轮没有可用候选编号" in reply


def test_controlled_evidence_renderer_blocks_unbound_selection_even_with_rows() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="第一套图也发一下。",
        understanding={"constraint_proof": {"selected_indices": [1]}},
        tool_evidence={
            "actions": ["search_inventory", "context_tools", "generate_reply"],
            "candidate_binding": {
                "status": "error",
                "selected_indices": [1],
                "candidate_count": 0,
            },
            "inventory_rows": [{"小区": "兴业杨家府", "房号": "4-1502"}],
        },
    )

    assert "上一轮没有可用候选编号" in reply
    assert "兴业杨家府4-1502" not in reply


def test_controlled_evidence_renderer_inventory_sheet_confirms_png_send() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="房源表先发我一下。",
        understanding={"constraint_proof": {"wants_inventory_sheet": True}},
        tool_evidence={
            "actions": ["send_inventory_sheet", "generate_reply"],
            "inventory_images": ["room_database/inventory/a.png"],
        },
    )

    assert reply == "房源表发你了，你可以让客户先整体看一下。"


def test_controlled_evidence_renderer_deposit_policy_from_business_rule() -> None:
    reply = main._controlled_evidence_reply_from_tools(
        content="能免押吗？",
        understanding={"constraint_proof": {"wants_deposit": True}},
        tool_evidence={"actions": ["send_deposit_policy", "generate_reply"]},
    )

    assert "支付宝无忧住" in reply
    assert "5.5%-8%" in reply


def test_shadow_missing_tool_plan_still_blocks_without_rewrite_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "shadow")

    result = main._plan_actions_shadow_or_legacy(
        initial_result={},
        understanding={},
        signals={},
    )

    assert result["actions"] == []
    assert result["need_rewrite_clarification"] is True
    assert result["source"] == "missing_tool_plan_gate"
