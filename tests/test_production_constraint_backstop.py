# -*- coding: utf-8 -*-
"""production 理解兜底安全阀回归(2026-07-04 生产实证固化)。

实证时间线:turn2 "新天地还有哪些两室5000以内的房子"约束链正常;
turn3 "5000以上的有吗" LLM1 丢失继承约束,proof 全空 -> 区域白名单不触发,
全表裸搜出皋塘组一室一厅"翰皋名府8-1403"并被措辞成"新天地的两室"。
安全阀只补空槽、不覆盖 LLM1 结论、显式锚点/序号选择/明确 clear 一律不动。
"""
from __future__ import annotations

import app.main as main


def _production_result(**overrides) -> dict:
    result = {
        "rewritten_query": overrides.get("content", "5000以上的有吗"),
        "effective_query": overrides.get("content", "5000以上的有吗"),
        "intent": "production_llm1",
        "selected_indices": [],
        "constraint_proof": {},
        "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
        "llm1_task_packet": {},
        "structured_task": {},
    }
    result.update(overrides)
    return result


def _rewrite_view_with_prior_scoped_query() -> dict:
    return {
        "last_turn_record": {
            "user_raw": "新天地还有哪些两室5000以内的房子",
            "rewritten_query": "",
            "query_state": {},
            "assistant_sent_summary": {"final_reply": ""},
        },
        "recent_turn_records": [],
        "raw_dialog_context": [],
    }


def test_budget_direction_reversal_followup_inherits_area_layout_budget() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="5000以上的有吗",
        result=_production_result(),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    proof = result["constraint_proof"]
    assert "新天地" in str(proof.get("area") or "")
    assert "两室" in str(proof.get("layout") or "")
    assert proof.get("budget_range") == [5000, 99999]
    marker = result.get("constraint_inheritance_backstop") or {}
    assert marker.get("stage") == "production_understanding_backstop"
    assert set(marker.get("filled") or {}) == {"area", "layout", "budget_range"}
    assert result["structured_task"]["constraint_inheritance_backstop"] == marker


def test_explicit_anchor_query_is_not_injected_with_stale_scope() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="皋塘运都有房吗？",
        result=_production_result(content="皋塘运都有房吗？"),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    assert result["constraint_proof"] == {}
    assert "constraint_inheritance_backstop" not in result


def test_llm1_provided_scope_is_never_overridden() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="5000以上的有吗",
        result=_production_result(constraint_proof={"area": "闸弄口\n新塘\n元宝塘\n东站"}),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    assert result["constraint_proof"] == {"area": "闸弄口\n新塘\n元宝塘\n东站"}
    assert "constraint_inheritance_backstop" not in result


def test_cleared_area_key_disables_backstop() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="5000以上的有吗",
        result=_production_result(
            llm1_task_packet={"cleared_constraint_keys": ["area"]},
        ),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    assert result["constraint_proof"] == {}
    assert "constraint_inheritance_backstop" not in result


def test_selected_indices_followup_skips_backstop() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="第1套视频发我",
        result=_production_result(
            content="第1套视频发我",
            selected_indices=[1],
            tool_plan={"actions": ["search_inventory", "send_video", "generate_reply"]},
        ),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    assert result["constraint_proof"] == {}
    assert "constraint_inheritance_backstop" not in result


def test_packet_inherited_constraints_take_priority_over_memory() -> None:
    result = main._backstop_production_constraint_inheritance(
        content="5000以上的有吗",
        result=_production_result(
            llm1_task_packet={
                "inherited_constraints": {"area": "东新园\n杭氧\n新天地", "layout": "两室一厅"},
            },
        ),
        rewrite_view={"last_turn_record": None, "recent_turn_records": [], "raw_dialog_context": []},
    )

    proof = result["constraint_proof"]
    assert proof.get("area") == "东新园\n杭氧\n新天地"
    assert proof.get("layout") == "两室一厅"
    assert proof.get("budget_range") == [5000, 99999]


def test_layout_change_followup_prefers_current_text_over_stale_memory() -> None:
    # 回归(2026-07-05 审计 H2):production LLM1 违约给空约束时,客户改口户型
    # ("三室的呢")的本轮明确表达必须压过记忆里的旧"两室",否则户型变更追问
    # 被 stale 户型覆盖、按错误户型检索。与 budget 的"本轮文本优先"对称。
    result = main._backstop_production_constraint_inheritance(
        content="三室的呢",
        result=_production_result(content="三室的呢"),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    proof = result["constraint_proof"]
    assert "三室" in str(proof.get("layout") or ""), proof
    assert "两室" not in str(proof.get("layout") or "")
    assert "新天地" in str(proof.get("area") or "")


def test_explicit_area_word_followup_gates_out_as_llm1_owned() -> None:
    # 边界固化(2026-07-05 审计 H2):区域词是词表锚点,"东站的呢"这类带明确区域
    # 的追问在锚点门即提前返回、归 LLM1 own,不进兜底注入(与户型词门控不同)。
    # 这解释了为何 H2 的真实缺陷只在 layout:户型词不是锚点、会走到兜底。
    result = main._backstop_production_constraint_inheritance(
        content="东站的呢",
        result=_production_result(content="东站的呢"),
        rewrite_view=_rewrite_view_with_prior_scoped_query(),
    )

    assert result["constraint_proof"] == {}
    assert "constraint_inheritance_backstop" not in result
