from app.services import kf_orchestrator_flow


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
