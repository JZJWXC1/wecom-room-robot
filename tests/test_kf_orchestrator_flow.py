import json

from app.services import kf_orchestrator_flow, kf_orchestrator_shadow
from app.services.inventory_read_models import InventoryReadContext


def _shadow_context(source_hash: str = "hash-a") -> InventoryReadContext:
    return InventoryReadContext(
        request_id="req-shadow",
        turn_id="turn-shadow",
        source_kind="legacy",
        source_hash=source_hash,
        schema_version="inventory_read.test",
        selected_at="2026-06-27T00:00:00Z",
        decision_id="ird_shadow",
        selection_mode="disabled",
    )


def test_tool_plan_from_understanding_strips_pre_tool_reply() -> None:
    plan = kf_orchestrator_flow.tool_plan_from_understanding(
        {
            "tool_plan": {
                "actions": ["search_inventory", "generate_reply"],
                "reply_text": "不应该在工具前发客户",
                "final_reply": "也不应该保留",
            }
        }
    )

    assert plan["actions"] == ["search_inventory", "generate_reply"]
    assert plan["reply_text"] == ""
    assert "final_reply" not in plan


def test_tool_plan_from_understanding_keeps_rewrite_clarification_internal() -> None:
    plan = kf_orchestrator_flow.tool_plan_from_understanding(
        {
            "structured_task": {
                "tool_plan": {
                    "actions": [],
                    "need_rewrite_clarification": True,
                }
            }
        }
    )

    assert plan["need_rewrite_clarification"] is True
    assert plan["reply_text"] == ""
    assert "missing_evidence" in plan


def test_planner_reply_selfcheck_status_defaults_to_pass() -> None:
    assert kf_orchestrator_flow.planner_reply_selfcheck_status({}) == "pass"
    assert (
        kf_orchestrator_flow.planner_reply_selfcheck_status(
            {"selfcheck": {"status": "retry"}}
        )
        == "retry"
    )


def test_orchestrator_shadow_builder_keeps_candidate_media_and_access_boundary_safe() -> None:
    row = {
        "listing_id": "lst-001",
        "小区": "晨星花园",
        "房号": "1-101A",
        "押一付一": "1800",
        "看房方式密码": "9999#",
    }
    artifact = kf_orchestrator_shadow.build_shadow_artifact(
        content="客户问 1 号视频和看房密码，电话 19900009999",
        open_kfid="kf-secret",
        external_userid="wm-secret",
        msgids=["msg-secret"],
        generation=1,
        inventory_read_context=_shadow_context("hash-a"),
        understanding={"intent": "viewing"},
        planner_result={"source": "test"},
        tool_evidence={
            "actions": ["search_inventory", "send_video", "generate_reply"],
            "inventory_rows": [row],
            "target_rows": [row],
            "video_rows": [row],
            "video_paths": ["C:/room_database/晨星花园/1-101A/9999#/video.mp4"],
            "inventory_listing_evidence": [
                {
                    "evidence_id": "evd-001",
                    "listing_id": "lst-001",
                    "source_kind": "legacy",
                    "source_hash": "hash-a",
                    "community": "晨星花园",
                    "room_no": "1-101A",
                }
            ],
            "outbound_package": {"video_paths": ["C:/room_database/晨星花园/1-101A/9999#/video.mp4"]},
        },
        reply_result={"selfcheck": {"status": "pass"}},
        final_reply="这是晨星花园1-101A的视频。看房需要提前联系。",
    )

    dumped = json.dumps(artifact, ensure_ascii=False)
    legacy = artifact["legacy_pipeline"]
    candidate = legacy["inventory_candidates"][0]
    video_binding = legacy["media_bindings"]["videos"][0]
    access_boundary = legacy["access_boundary"]

    assert artifact["schema_version"] == "rag_v2_orchestrator_shadow.v1"
    assert artifact["baseline_commit"] == "693a9c899d1cb1a4565ad67e4e600fc9559da4dd"
    assert artifact["inventory_read"]["decision_id"] == "ird_shadow"
    assert artifact["inventory_read"]["source_hash"] == "hash-a"
    assert candidate["candidate_number"] == 1
    assert candidate["listing_id"] == "lst-001"
    assert candidate["source_hash"] == "hash-a"
    assert video_binding["candidate_number"] == 1
    assert video_binding["listing_id"] == "lst-001"
    assert video_binding["bound"] is True
    assert access_boundary["customer_requested_access"] is True
    assert access_boundary["evidence_access_text_present"] is True
    assert access_boundary["sensitive_access_value_count"] >= 1
    assert artifact["shadow_a"]["verdict"] == "pass"
    assert "9999#" not in dumped
    assert "19900009999" not in dumped
    assert "客户问" not in dumped
    assert "C:/room_database" not in dumped


def test_orchestrator_shadow_builder_blocks_mixed_source_hash() -> None:
    rows = [
        {"listing_id": "lst-001", "小区": "晨星花园", "房号": "1-101A"},
        {"listing_id": "lst-002", "小区": "晨星花园", "房号": "1-102"},
    ]
    artifact = kf_orchestrator_shadow.build_shadow_artifact(
        content="这两套还在吗",
        inventory_read_context=_shadow_context("hash-a"),
        understanding={"intent": "inventory"},
        planner_result={"source": "test"},
        tool_evidence={
            "actions": ["search_inventory", "generate_reply"],
            "inventory_rows": rows,
            "inventory_listing_evidence": [
                {
                    "evidence_id": "evd-001",
                    "listing_id": "lst-001",
                    "source_kind": "legacy",
                    "source_hash": "hash-a",
                    "community": "晨星花园",
                    "room_no": "1-101A",
                },
                {
                    "evidence_id": "evd-002",
                    "listing_id": "lst-002",
                    "source_kind": "legacy",
                    "source_hash": "hash-b",
                    "community": "晨星花园",
                    "room_no": "1-102",
                },
            ],
        },
        reply_result={"selfcheck": {"status": "pass"}},
        final_reply="这两套还在。",
    )

    assert artifact["shadow_a"]["verdict"] == "blocked"
    assert "mixed_source_hash" in artifact["shadow_a"]["risk_reasons"]
