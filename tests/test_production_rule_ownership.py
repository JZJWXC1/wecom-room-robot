from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    marker = f"def {name}("
    start = source.index(marker)
    next_def = source.find("\ndef ", start + len(marker))
    next_async_def = source.find("\nasync def ", start + len(marker))
    candidates = [index for index in (next_def, next_async_def) if index != -1]
    end = min(candidates) if candidates else len(source)
    return source[start:end]


def test_production_action_owner_is_llm1_tool_plan_not_legacy_planner() -> None:
    source = read_source("app/main.py")
    body = function_body(source, "_plan_actions")
    production_branch = body.split("if _dual_llm_production_enabled():", 1)[1].split(
        "\n    if result:",
        1,
    )[0]

    assert "llm1_production_missing_tool_plan_gate" in production_branch
    assert "fallback_actions_from_structured_task" not in production_branch
    assert "ensure_required_actions" not in production_branch


def test_production_action_contract_never_invents_actions() -> None:
    source = read_source("app/main.py")
    body = function_body(source, "_ensure_planner_action_contract")
    production_branch = body.split("if _dual_llm_production_enabled():", 1)[1].split(
        "\n    if result.get(\"need_rewrite_clarification\"):",
        1,
    )[0]

    assert "result[\"actions\"] = []" in production_branch
    assert "LLM1 production tool_plan.actions 为空" in production_branch
    assert "fallback_actions_from_structured_task" not in production_branch
    assert "ensure_required_actions" not in production_branch


def test_llm1_production_prompt_excludes_legacy_planner_summary() -> None:
    source = read_source("app/services/llm.py")

    assert "legacy_rewrite=None if production_mode else legacy_rewrite" in source
    assert "legacy_planner=None if production_mode else legacy_planner" in source
    assert "include_legacy_summary=not production_mode" in source
    assert "production 下必须直接输出 tool_plan.actions" in source
    assert "不生成客户可见回复" in source


def test_legacy_reply_module_is_removed() -> None:
    assert not (ROOT / "app/services/kf_legacy_reply.py").exists()
    assert not (ROOT / "app/services/kf_legacy_planner.py").exists()


def test_production_visible_reply_owner_is_llm2_or_controlled_renderer() -> None:
    source = read_source("app/services/kf_dual_llm_production.py")

    assert "async def compose_production_outbound_package" in source
    assert "def compose_controlled_evidence_outbound_package" in source
    assert "DUAL_LLM_PRODUCTION_CONTROLLED_RENDERER_SOURCE" in source
    assert "llm2_output_missing_visible_reply" in source


def test_production_fact_owner_is_outbound_validation_not_final_llm_selfcheck() -> None:
    source = read_source("app/main.py")

    assert "llm_selfcheck_skipped_by_kf_outbound_validation" in source
    assert "production 发送前事实和动作只由 validate_prepared_outbound_package 校验" in source
    assert "send_blocked" in source


def test_legacy_rag_retrieve_and_selfcheck_are_non_production_only() -> None:
    source = read_source("app/main.py")
    body = function_body(source, "_generate_reply_result")
    retrieve_block = body.split("if production_mode:", 1)[1].split(
        "deterministic_reply_source = \"\"",
        1,
    )[0]
    selfcheck_block = body.split("with selfcheck_stage:", 1)[1].split(
        "llm_status = str(llm_selfcheck.get",
        1,
    )[0]

    assert "rag_retrieve_skipped_in_production" in retrieve_block
    assert "rag_result = await agentic_rag.retrieve_for_reply" in retrieve_block
    assert retrieve_block.index("elif skip_rag_retrieve_for_controlled_renderer") < retrieve_block.index(
        "rag_result = await agentic_rag.retrieve_for_reply"
    )
    assert "source\": \"kf_outbound_validation\"" in selfcheck_block
    assert "agentic_rag.assess_reply" in selfcheck_block
    assert selfcheck_block.index("if _dual_llm_production_enabled():") < selfcheck_block.index(
        "else:"
    ) < selfcheck_block.index("agentic_rag.assess_reply")


def test_tool_resolver_owns_target_binding_without_visible_text() -> None:
    source = read_source("app/services/kf_tool_resolver.py")

    assert "def resolve_tool_targets" in source
    assert "Bind tool evidence rows to the LLM1 task without generating visible text." in source
    assert "candidate_binding" in source
    assert "selection_error" in source
    assert "field_target_error" in source
