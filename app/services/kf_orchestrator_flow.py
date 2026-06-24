from __future__ import annotations

from typing import Any


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def tool_plan_from_understanding(understanding: dict[str, Any]) -> dict[str, Any]:
    raw_plan = understanding.get("tool_plan")
    if not isinstance(raw_plan, dict):
        task = understanding.get("structured_task")
        if isinstance(task, dict):
            raw_plan = task.get("tool_plan")
    if not isinstance(raw_plan, dict):
        return {}

    plan = dict(raw_plan)
    plan["actions"] = _string_list(plan.get("actions"))
    plan["source"] = str(plan.get("source") or "orchestrator_pre_tool_plan")
    if plan.get("need_rewrite_clarification"):
        plan["reply_text"] = ""
        plan.setdefault(
            "missing_evidence",
            "Orchestrator 工具前阶段认为目标或证据不足，需要问题重写层补证据。",
        )
    else:
        plan.pop("reply", None)
        plan.pop("final_reply", None)
        plan["reply_text"] = ""
    return plan


def planner_reply_selfcheck(planner_reply_result: dict[str, Any]) -> dict[str, Any]:
    selfcheck = planner_reply_result.get("selfcheck")
    return dict(selfcheck) if isinstance(selfcheck, dict) else {}


def planner_reply_selfcheck_status(planner_reply_result: dict[str, Any]) -> str:
    return str(planner_reply_selfcheck(planner_reply_result).get("status") or "pass").lower()
