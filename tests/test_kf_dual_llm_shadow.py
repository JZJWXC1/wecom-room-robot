from __future__ import annotations

import json

from app.services.kf_contracts import PreparedOutboundPackage, ResponseStrategy, StructuredTaskPacket
from app.services.kf_dual_llm_shadow import (
    DUAL_LLM_SHADOW_SCHEMA_VERSION,
    build_program_outbound_contract_inputs,
    build_dual_llm_shadow_record,
    build_shadow_task_packet,
    compose_shadow_outbound,
)


def test_build_shadow_task_packet_supports_multi_task_and_candidate_numbers() -> None:
    packet = build_shadow_task_packet(
        {
            "rewritten_query": "棠润府前两套视频",
            "constraints": {"小区": "棠润府"},
            "candidate_numbers": [1, 2],
        },
        {"actions": ["search_inventory", "send_video", "generate_reply"]},
        content="前两套发视频",
        candidate_set={
            "candidate_set_id": "cset-dual-1",
            "candidates": [
                {"candidate_number": 1, "小区": "棠润府", "房号": "15-2-801B"},
                {"candidate_number": 2, "小区": "棠润府", "房号": "15-2-802B"},
            ],
        },
        conversation_id="conv-cn",
        turn_id="turn-1",
    )

    payload = packet.to_legacy_dict()

    assert isinstance(packet, StructuredTaskPacket)
    assert payload["response_strategy"]["mode"] == ResponseStrategy.SEND_MEDIA.mode
    assert payload["response_strategy"]["detail_level"] == "normal"
    assert [task["task_type"] for task in payload["tasks"]] == ["inventory_search", "send_video", "reply_text"]
    assert payload["tasks"][1]["constraints"]["candidate_numbers"] == [1, 2]
    assert payload["rewritten_query"] == "棠润府前两套视频"


def test_compose_shadow_outbound_binds_candidate_numbers_and_video_actions_from_evidence() -> None:
    packet = build_shadow_task_packet(
        {"candidate_numbers": [1, 2], "constraints": {"小区": "棠润府"}},
        {"actions": ["send_video"]},
        content="这两套视频",
        candidate_set={
            "candidate_set_id": "cset-dual-2",
            "candidates": [
                {"candidate_number": 1, "小区": "棠润府", "房号": "15-2-801B"},
                {"candidate_number": 2, "小区": "棠润府", "房号": "15-2-802B"},
            ],
        },
    )
    package = compose_shadow_outbound(
        packet,
        {
            "actions": ["send_video"],
            "target_rows": [
                {"listing_id": "lst-1", "小区": "棠润府", "房号": "15-2-801B", "押一付一": "3800"},
                {"listing_id": "lst-2", "小区": "棠润府", "房号": "15-2-802B", "押一付一": "3900"},
            ],
            "video_paths": ["room_database/video/棠润府15-2-801B/demo.mp4", "room_database/video/棠润府15-2-802B/demo.mp4"],
        },
        "这是这两套房间的视频。",
    )
    record = build_dual_llm_shadow_record(
        legacy_rewrite={"candidate_numbers": [1, 2], "constraints": {"小区": "棠润府"}},
        legacy_planner={"actions": ["send_video"]},
        tool_evidence={
            "actions": ["send_video"],
            "target_rows": [
                {"listing_id": "lst-1", "小区": "棠润府", "房号": "15-2-801B"},
                {"listing_id": "lst-2", "小区": "棠润府", "房号": "15-2-802B"},
            ],
            "video_paths": ["a.mp4", "b.mp4"],
        },
        legacy_reply_text="这是这两套房间的视频。",
    )

    assert package.response_strategy == ResponseStrategy.SEND_MEDIA
    assert [item.candidate_number for item in package.candidate_set.candidates] == [1, 2]
    assert [action.action_type for action in package.send_actions] == ["video", "video"]
    assert [caption.action_id for caption in package.action_captions] == ["send-video-1", "send-video-2"]
    assert package.selfcheck_profile == "dual_llm_shadow.selfcheck.v1"
    assert record["llm2"]["candidate_binding"]["media"][0]["candidate_number"] == 1
    assert record["llm2"]["candidate_binding"]["media"][1]["candidate_number"] == 2
    assert record["llm2"]["response_strategy"]["mode"] == "send_media"
    assert record["llm2"]["action_captions"][0]["action_id"] == "send-video-1"


def test_continue_search_strategy_records_tool_plan_without_media_decision() -> None:
    record = build_dual_llm_shadow_record(
        legacy_rewrite={"constraints": {"预算": "4000-5000"}},
        legacy_planner={"actions": ["continue_search", "generate_reply"], "source": "legacy_planner"},
        tool_evidence={"actions": ["continue_search", "generate_reply"], "inventory_rows": []},
        legacy_reply_text="我再帮你继续找符合预算的房源。",
    )

    assert record["llm1"]["response_strategy"]["mode"] == ResponseStrategy.TOOL_FIRST.mode
    assert record["llm1"]["tool_plan"]["continue_search"] is True
    assert record["llm2"]["self_review"]["llm2_decides_media_targets"] is False


def test_dual_shadow_record_prefers_true_llm1_packet_and_records_legacy_diff() -> None:
    record = build_dual_llm_shadow_record(
        llm1_shadow_output={
            "rewritten_query": "房源表发一下",
            "task_atoms": [{"task_id": "task-1-sheet", "task_type": "send_inventory_sheet"}],
            "tool_plan": {"actions": ["send_inventory_sheet"], "required_tools": ["inventory.sheet_artifact"]},
        },
        legacy_rewrite={"rewritten_query": "拱墅万达房源查询"},
        legacy_planner={"actions": ["search_inventory", "generate_reply"]},
        tool_evidence={"actions": ["send_inventory_sheet"], "inventory_image_paths": ["room_database/sheet/current.png"]},
        legacy_reply_text="房源表发你了，可以先给客户看整体。",
    )

    assert record["llm1"]["source"] == "llm1_shadow"
    assert [task["task_type"] for task in record["llm1"]["task_atoms"]] == ["send_inventory_sheet"]
    assert record["llm1"]["tool_plan"]["actions"] == ["send_inventory_sheet"]
    assert record["llm1"]["legacy_diff"]["status"] == "diff"
    assert "tool_actions" in record["llm1"]["legacy_diff"]["changed_fields"]
    assert record["llm2"]["send_actions"][0]["action_type"] == "image"


def test_invalid_legacy_strategy_falls_back_without_breaking_shadow() -> None:
    packet = build_shadow_task_packet(
        {"response_strategy": "future_unknown_strategy"},
        {"actions": ["generate_reply"]},
        content="帮我看看还有没有",
    )
    package = compose_shadow_outbound(
        packet,
        {"actions": ["generate_reply"], "response_strategy": "future_unknown_strategy"},
        "我帮你继续看一下。",
    )

    assert packet.response_strategy == ResponseStrategy.ANSWER
    assert package.response_strategy == ResponseStrategy.ANSWER


def test_claims_evidence_and_legacy_roundtrip_are_safe_and_supported() -> None:
    packet = build_shadow_task_packet(
        {"tasks": [{"id": "search", "type": "inventory_search", "text": "新塘河 预算 4000"}]},
        {"actions": ["search_inventory", "generate_reply"]},
        conversation_id="conv-roundtrip",
        turn_id="turn-roundtrip",
    )
    package = compose_shadow_outbound(
        packet,
        {
            "actions": ["search_inventory", "generate_reply"],
            "inventory_rows": [{"listing_id": "lst-9", "小区": "新塘河", "房号": "9-402B", "押一付一": "4200"}],
            "inventory_listing_evidence": [
                {
                    "evidence_id": "evd-source",
                    "listing_id": "lst-9",
                    "summary": "新塘河 9-402B 押一付一 4200",
                    "metadata": {"source_hash": "hash-1"},
                }
            ],
        },
        "新塘河 9-402B 押一付一 4200。",
    )
    roundtrip = PreparedOutboundPackage.from_legacy_dict(package.to_legacy_dict())

    assert roundtrip.to_legacy_dict()["claims"][0]["support"] == ["evd-candidate-1"]
    assert roundtrip.to_legacy_dict()["claims"][0]["field"] == "candidate_summary"
    assert roundtrip.to_legacy_dict()["claims"][0]["evidence_ref"] == "evd-candidate-1"
    assert roundtrip.to_legacy_dict()["evidence_bundle"]["evidence"][0]["listing_id"] == "lst-9"
    assert roundtrip.to_legacy_dict()["evidence_bundle"]["evidence"][0]["field_values"]["community"] == "新塘河"
    assert roundtrip.to_legacy_dict()["reply_text"] == "新塘河 9-402B 押一付一 4200。"


def test_program_outbound_inventory_field_values_are_rich_and_password_redacted() -> None:
    packet = build_shadow_task_packet(
        {"tasks": [{"id": "search", "type": "inventory_search", "text": "杨家府 1和3"}]},
        {"actions": ["search_inventory", "compact_listing", "generate_reply"]},
        conversation_id="conv-rich-fields",
        turn_id="turn-rich-fields",
    )

    evidence_bundle, _, _ = build_program_outbound_contract_inputs(
        task_packet=packet,
        tool_evidence={
            "actions": ["search_inventory", "compact_listing", "generate_reply"],
            "target_rows": [
                {
                    "listing_id": "lst-yjf-46-1204a",
                    "区域": "石桥街道 华丰 石桥 永佳 半山",
                    "小区": "杨家新雅苑",
                    "房号": "46-1204A",
                    "户型描述": "一室一厅朝南带阳台",
                    "押一付一": "2500",
                    "押二付一": "2300",
                    "备注": "水30/月，电1元/度",
                    "看房方式密码": "101004# 看房提前联系",
                }
            ],
        },
        planner_result={"actions": ["search_inventory", "compact_listing", "generate_reply"]},
    )

    candidate = next(item for item in evidence_bundle.evidence if item.evidence_type == "inventory_candidate")
    fields = candidate.field_values
    dumped = json.dumps(evidence_bundle.to_safe_dict(), ensure_ascii=False, sort_keys=True)

    assert fields["community"] == "杨家新雅苑"
    assert fields["room_no"] == "46-1204A"
    assert fields["area"] == "石桥街道 华丰 石桥 永佳 半山"
    assert fields["layout_description"] == "一室一厅朝南带阳台"
    assert fields["rent_pay1"] == 2500
    assert fields["rent_pay2"] == 2300
    assert fields["utilities"] == "水30/月，电1元/度"
    assert fields["has_viewing_text"] is True
    assert "101004#" not in dumped
    assert "看房方式密码" not in dumped


def test_shadow_artifact_redacts_password_phone_and_token_from_repr_and_json() -> None:
    secret_password = "9999#"
    phone = "19900009999"
    record = build_dual_llm_shadow_record(
        legacy_rewrite={"constraints": {"手机号": phone, "token": "abc123"}},
        legacy_planner={"actions": ["generate_reply"]},
        tool_evidence={
            "actions": ["generate_reply"],
            "inventory_rows": [{"小区": "云栖", "房号": "1-101", "看房密码": secret_password, "手机号": phone}],
            "raw_tool_result": {"password": secret_password, "token": "abc123"},
        },
        legacy_reply_text=f"看房密码 {secret_password}，电话 {phone}，token=abc123",
    )
    dumped = json.dumps(record, ensure_ascii=False)

    assert record["schema_version"] == DUAL_LLM_SHADOW_SCHEMA_VERSION
    assert secret_password not in dumped
    assert phone not in dumped
    assert "abc123" not in dumped
    assert "raw_tool_result" not in dumped


def test_utf8_chinese_is_preserved_in_shadow_contract_payloads() -> None:
    packet = build_shadow_task_packet(
        {"rewritten_query": "你好，想看滨江两房", "constraints": {"区域": "滨江", "户型": "两房"}},
        {"actions": ["search_inventory", "generate_reply"]},
        content="你好，想看滨江两房",
    )
    package = compose_shadow_outbound(
        packet,
        {"actions": ["search_inventory"], "inventory_rows": [{"小区": "滨江雅苑", "房号": "3-1201"}]},
        "滨江雅苑 3-1201 目前可以看。",
    )
    dumped = json.dumps(package.to_legacy_dict(), ensure_ascii=False)

    assert "滨江雅苑" in dumped
    assert "你好，想看滨江两房" in json.dumps(packet.to_legacy_dict(), ensure_ascii=False)
    assert "\\u6ee8" not in dumped
