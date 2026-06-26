from __future__ import annotations

import json

from app.services.kf_contracts import ResponseStrategy, StructuredTaskPacket
from app.services.kf_llm1_task_packet import build_kf_task_packet, build_kf_task_packet_shadow


def _candidate_set() -> dict:
    return {
        "candidate_set_id": "cset-1",
        "candidates": [
            {"candidate_number": 1, "listing_id": "lst-1", "小区": "棠润府", "房号": "15-2-801B"},
            {"candidate_number": 2, "listing_id": "lst-2", "小区": "棠润府", "房号": "15-2-802B"},
        ],
    }


def test_build_kf_task_packet_supports_true_llm1_multi_task_constraints_and_tool_plan() -> None:
    result = build_kf_task_packet_shadow(
        {
            "rewritten_query": "棠润府第2套视频，顺便确认免押条件",
            "response_strategy": {"mode": "send_media"},
            "constraints": {
                "inherit": {"小区": "棠润府"},
                "replace": {"素材": "视频"},
                "exclude": {"房态": "已租"},
                "clear": ["预算"],
            },
            "task_atoms": [
                {
                    "task_id": "task-1-search",
                    "task_type": "inventory_search",
                    "constraint_operation": "inherit",
                    "constraints": {"小区": "棠润府"},
                    "required_tools": ["inventory.search"],
                },
                {
                    "task_id": "task-2-video",
                    "task_type": "send_video",
                    "constraint_operation": "replace",
                    "constraints": {"candidate_numbers": [2]},
                },
                {
                    "task_id": "task-3-deposit",
                    "task_type": "deposit_policy",
                    "constraint_operation": "inherit",
                    "constraints": {"免押": "条件"},
                },
            ],
            "candidate_binding": {"selected_candidate_numbers": [2]},
            "tool_plan": {
                "actions": ["search_inventory", "context_tools", "send_video", "send_deposit_policy", "generate_reply"],
                "required_tools": ["inventory.search", "context.memory", "media.video", "deposit.policy", "reply.compose"],
                "reason": "先绑定候选，再查视频和免押政策",
            },
            "reply_text": "这句不应该进入 LLM1 shadow artifact",
        },
        content="第2套视频，免押也说下",
        candidate_set=_candidate_set(),
        legacy_rewrite={"constraints": {"小区": "棠润府"}, "candidate_numbers": [1]},
        legacy_planner={"actions": ["search_inventory", "send_video", "generate_reply"]},
        conversation_id="conv-llm1",
        turn_id="turn-1",
    )
    packet = result.packet
    payload = packet.to_legacy_dict()

    assert isinstance(packet, StructuredTaskPacket)
    assert packet.response_strategy == ResponseStrategy.SEND_MEDIA
    assert [task["task_type"] for task in payload["tasks"]] == ["inventory_search", "send_video", "deposit_policy"]
    assert payload["tasks"][1]["constraint_operation"] == "replace"
    assert payload["tasks"][1]["constraints"]["candidate_numbers"] == [2]
    assert payload["tasks"][1]["required_tools"] == ["media.video"]
    assert payload["inherited_constraints"] == {"小区": "棠润府"}
    assert payload["replaced_constraints"] == {"素材": "视频"}
    assert payload["excluded_constraints"] == {"房态": "已租"}
    assert payload["cleared_constraint_keys"] == ["预算"]
    assert result.tool_plan["actions"] == [
        "search_inventory",
        "context_tools",
        "send_video",
        "send_deposit_policy",
        "generate_reply",
    ]
    assert result.candidate_binding["selected_candidate_numbers"] == [2]
    assert result.legacy_diff["status"] == "diff"
    assert "这句不应该" not in json.dumps(result.to_safe_dict(), ensure_ascii=False)


def test_build_kf_task_packet_drops_candidate_numbers_without_candidate_set() -> None:
    packet = build_kf_task_packet(
        {
            "rewritten_query": "第3套视频",
            "task_atoms": [
                {
                    "task_id": "task-1-video",
                    "task_type": "send_video",
                    "constraint_operation": "replace",
                    "constraints": {"candidate_numbers": [3]},
                }
            ],
            "candidate_binding": {"selected_candidate_numbers": [3]},
            "tool_plan": {"actions": ["send_video", "generate_reply"]},
        },
        content="第3套视频",
    )

    assert "candidate_numbers" not in packet.tasks[0].constraints
    binding = packet.legacy_unknown_fields["llm1_shadow"]["candidate_binding"]
    assert binding["status"] == "no_candidate_set"
    assert binding["selected_candidate_numbers"] == []
    assert binding["dropped_candidate_numbers"] == [3]


def test_llm1_shadow_prompt_and_artifact_redact_sensitive_values_and_reply_fields() -> None:
    phone = "19900009999"
    password = "9999#"
    result = build_kf_task_packet_shadow(
        {
            "rewritten_query": f"客户电话 {phone} 想看房",
            "task_atoms": [
                {
                    "task_id": "task-1-viewing",
                    "task_type": "viewing_guidance",
                    "constraints": {"看房密码": password, "手机号": phone, "token": "abc123"},
                }
            ],
            "tool_plan": {"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
            "reply_text": f"密码 {password} 电话 {phone} token=abc123",
        },
        content=f"电话 {phone}，密码 {password}，token=abc123",
        structured_memory={"raw_customer_content": f"{phone} {password}", "token": "abc123"},
    )
    dumped = json.dumps(result.to_safe_dict(), ensure_ascii=False)

    assert phone not in dumped
    assert password not in dumped
    assert "abc123" not in dumped
    assert "reply_text" not in dumped
    assert "raw_customer_content" not in dumped


def test_llm1_shadow_records_legacy_diff_without_changing_packet_output() -> None:
    result = build_kf_task_packet_shadow(
        {
            "rewritten_query": "房源表发一下",
            "task_atoms": [{"task_id": "task-1-sheet", "task_type": "send_inventory_sheet"}],
            "tool_plan": {"actions": ["send_inventory_sheet"]},
        },
        content="表发一下",
        legacy_rewrite={"rewritten_query": "附近房源查询"},
        legacy_planner={"actions": ["search_inventory", "generate_reply"]},
    )

    assert result.packet.tasks[0].task_type == "send_inventory_sheet"
    assert result.tool_plan["actions"] == ["send_inventory_sheet"]
    assert result.legacy_diff["status"] == "diff"
    assert "tool_actions" in result.legacy_diff["changed_fields"]
