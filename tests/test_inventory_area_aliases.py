from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import app.main as main
from app.services.inventory_snapshot_builder import SnapshotBuilder
from app.services.inventory_snapshot_models import InventorySourceMetadata
from app.services.inventory_snapshot_offline import scan_safe_artifacts_for_canaries
from app.services.inventory_snapshot_reconciliation import compare_rewrite_inventory_index, reconcile_inventory_snapshot
from app.services.inventory_snapshot_shadow import InventorySnapshotShadowCoordinator
from app.services.region_inventory_constants import (
    AREA_ALIAS_DEFINITIONS,
    AreaAliasDefinition,
    area_alias_index_entries,
    validate_area_alias_definitions,
)
from app.services.rewrite_inventory_index import (
    DEFAULT_AREA_ALIASES,
    build_rewrite_inventory_index,
    slice_rewrite_inventory_index,
)


DONGXIN_AREA = "东新园 杭氧 新天地"
FIX1_SHA = "d0f080ddbacc15784b341c6cdc28d8d241f4a8a3"
REPO_ROOT = Path(__file__).resolve().parents[1]


def assert_matches_fix1(path: str) -> None:
    result = subprocess.run(
        ["git", "diff", "--exit-code", FIX1_SHA, "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr


ALLOWED_LLM_RAG_V2_ENTITY_HINTS = (
    "房号里的数字不能拆出来当预算或价格",
    "“客户又问/客户问/又问/再问/那小区”等只是话语前缀",
    "房号数字不能进入 query_state.budget / budget_range / budget_label",
    "“客户又问杨家新雅苑有没有三室的”里的小区是“杨家新雅苑”",
)


ALLOWED_LLM_PROMPT_OWNERSHIP_REPLACEMENTS = (
    ("本阶段不生成客户可见回复，只输出内部澄清需求、结构化任务和工具计划。", "本阶段不生成客户可见回复，只输出追问、结构化任务和工具计划。"),
    ("Validator / Tool Resolver 回流证据", "Planner 回流证据"),
    ("Validator 或 Tool Resolver 回传缺失证据", "Planner 回传缺失证据"),
    ("回流证据不是客户可见内容", "Planner 反馈不是客户可见内容"),
    ("Validator / Tool Resolver 回传的内部缺失证据", "Planner 回传的内部缺失证据"),
    ("内部澄清需求摘要，不得写客户可见追问句", "需要澄清时给用户的一句话"),
    ("最终客户可见话术只能由 LLM2 在工具取证后生成", "最终话术只能在工具执行后生成"),
    ("LLM1 只给出工具计划，具体目标绑定由 Tool Resolver 基于候选和证据完成", "Planner 只负责后续工具规划"),
    ("Validator 或 Tool Resolver 回传 need_rewrite_clarification", "Planner 回传 need_rewrite_clarification"),
    ("并只标记当前缺少的真实字段", "并只追问当前缺少的真实字段"),
    ("不替 LLM1、Tool Resolver 或 LLM2 生成客户可见回答；不通过时按失败层级生成回流证据", "不替 Planner 生成客户可见回答；不通过时给 Planner 重规划证据"),
    ("检查 LLM1 工具计划与 Tool Resolver 动作是否完成结构化任务", "检查 Planner/工具动作是否完成结构化任务"),
    ("用于校验 LLM1 工具计划和 Tool Resolver 动作有没有跑偏", "用于校验 Planner 和动作有没有跑偏"),
    ("LLM1 工具计划和 Tool Resolver 必须按区域和预算执行", "Planner/工具必须按区域和预算执行"),
    ("给 LLM1 / Tool Resolver 的重规划说明", "给 Planner 的重规划说明"),
    ("LLM1 工具计划或 Tool Resolver 动作不满足 StructuredTask", "Planner 动作不满足 StructuredTask"),
    ("先看 LLM1 工具计划、Tool Resolver 动作和文本是否已经满足问题重写后的真实需求", "先看 Planner/工具动作和文本是否已经满足问题重写后的真实需求"),
    ("重规划说明要写清楚证据", "planner_retry_reason 要写清楚证据"),
)


def normalize_allowed_llm_prompt_ownership_replacements(lines: list[str]) -> list[str]:
    result: list[str] = []
    for line in lines:
        normalized = line
        for current_fragment, baseline_fragment in ALLOWED_LLM_PROMPT_OWNERSHIP_REPLACEMENTS:
            normalized = normalized.replace(current_fragment, baseline_fragment)
        result.append(normalized)
    return result


def strip_llm_shadow_only_blocks(lines: list[str]) -> list[str]:
    result: list[str] = []
    skip_until: str | None = None
    skip_import_block = False

    for line in lines:
        if skip_import_block:
            if line == ")":
                skip_import_block = False
            continue

        if line == "from app.services.kf_contracts import safe_artifact_payload":
            continue
        if line == "from app.services.kf_dual_llm_production import DUAL_LLM_PRODUCTION_LLM1_PROMPT_VERSION":
            continue
        if line == "from app.services.kf_llm1_task_packet import (":
            skip_import_block = True
            continue

        if skip_until:
            if line.startswith(skip_until):
                skip_until = None
                result.append(line)
            continue

        if line.startswith("    async def build_kf_task_packet("):
            skip_until = "    async def rewrite_kf_message("
            continue
        if line.startswith("    async def compose_kf_outbound_shadow("):
            skip_until = "    async def assess_kf_final_reply("
            continue

        result.append(line)

    return result


def strip_removed_legacy_plan_reply(lines: list[str]) -> list[str]:
    result: list[str] = []
    skip_until: tuple[str, ...] = ()
    for line in lines:
        if skip_until:
            if any(line.startswith(prefix) for prefix in skip_until):
                skip_until = ()
                result.append(line)
            continue
        if line.startswith("    async def plan_kf_reply_text("):
            skip_until = (
                "    async def compose_kf_outbound_shadow(",
                "    async def assess_kf_final_reply(",
            )
            continue
        result.append(line)
    return result


def test_llm_customer_chain_matches_fix1_except_rag_v2_entity_hints_and_v1_pruned_reply() -> None:
    current_lines = (REPO_ROOT / "app/services/llm.py").read_text(encoding="utf-8").splitlines()
    baseline = subprocess.run(
        ["git", "show", f"{FIX1_SHA}:app/services/llm.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()

    current_lines = strip_removed_legacy_plan_reply(strip_llm_shadow_only_blocks(current_lines))
    baseline = strip_removed_legacy_plan_reply(baseline)
    normalized_current = normalize_allowed_llm_prompt_ownership_replacements(current_lines)
    filtered_current = [
        line for line in normalized_current
        if not any(hint in line for hint in ALLOWED_LLM_RAG_V2_ENTITY_HINTS)
    ]
    current_text = "\n".join(current_lines)

    assert filtered_current == baseline
    for hint in ALLOWED_LLM_RAG_V2_ENTITY_HINTS:
        assert hint in current_text
    for current_fragment, _baseline_fragment in ALLOWED_LLM_PROMPT_OWNERSHIP_REPLACEMENTS:
        assert current_fragment in current_text


def rows_with_viewing(password: str = "1234#") -> list[dict[str, Any]]:
    return [
        {
            "区域": DONGXIN_AREA,
            "小区": "晨星花园",
            "房号": "1-101",
            "户型描述": "朝南一室",
            "户型分类": "一室",
            "押一付一": "3200",
            "押二付一": "3000",
            "看房方式密码": password,
            "备注": "民水民电",
        }
    ]


def source_metadata() -> InventorySourceMetadata:
    return InventorySourceMetadata(source_kind="area_alias_unit_test", source_version="v1")


def alias_pairs(index: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item.get("normalized_alias") or ""), str(item.get("canonical_area") or item.get("canonical") or ""))
        for item in index.get("area_aliases") or []
        if item.get("status", "active") == "active"
    }


def test_main_customer_chain_keeps_m1d2a_inventory_read_router_gate() -> None:
    source = (REPO_ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "inventory_read_turn" in source
    assert "InventoryReadRouter" not in source
    assert "inventory_read_context" in source
    assert "SnapshotInventoryReadProvider" not in source
    assert "inventory_snapshot_reader" not in source


@pytest.mark.parametrize(
    "path",
    [
        "scripts/refresh_rag_inventory_cache.py",
    ],
)
def test_sync_scripts_match_fix1(path: str) -> None:
    # The region sync script intentionally diverges after FIX1 to publish the
    # production media manifest from the synced target drive. Keep the cache-only
    # script pinned to FIX1 so area alias behavior cannot drift silently there.
    assert_matches_fix1(path)


def test_confirmed_aliases_resolve_to_dongxin_area() -> None:
    alias_map = {
        item["alias"]: item["canonical_area"]
        for item in area_alias_index_entries()
    }

    assert alias_map["新填地"] == DONGXIN_AREA
    assert alias_map["东新"] == DONGXIN_AREA

    legacy_hits = main._area_alias_hits("客户问新填地附近两室")
    assert any(hit["raw_text"] == "新填地" and hit["canonical"] == "东新园\n杭氧\n新天地" for hit in legacy_hits)
    legacy_hits = main._area_alias_hits("东新有没有一室")
    assert any(hit["raw_text"] == "东新" and hit["canonical"] == "东新园\n杭氧\n新天地" for hit in legacy_hits)


def test_legacy_and_snapshot_indexes_share_active_alias_set() -> None:
    rows = rows_with_viewing()
    legacy_index = build_rewrite_inventory_index(rows)
    snapshot, report = SnapshotBuilder().build(rows, source_metadata(), generated_at="2026-06-25T00:00:00Z")

    assert report.ok
    assert alias_pairs(legacy_index) == alias_pairs(snapshot.rewrite_index)
    assert alias_pairs(legacy_index) == alias_pairs({"area_aliases": area_alias_index_entries()})


def test_legacy_default_aliases_are_shared_api_projection() -> None:
    expected = {
        item["alias"]: item["canonical_area"]
        for item in area_alias_index_entries()
    }

    assert DEFAULT_AREA_ALIASES == expected
    assert DEFAULT_AREA_ALIASES["新填地"] == DONGXIN_AREA
    assert DEFAULT_AREA_ALIASES["东新"] == DONGXIN_AREA


def test_area_alias_coverage_validator_is_clean() -> None:
    result = validate_area_alias_definitions()

    assert result.ok is True
    assert result.to_dict() == {
        "missing_valid_aliases": 0,
        "unresolved_aliases": 0,
        "active_alias_conflicts": 0,
        "unknown_canonical_areas": 0,
        "ambiguous_direct_mappings": 0,
    }


def test_area_aliases_never_enter_community_set() -> None:
    index = build_rewrite_inventory_index(rows_with_viewing())
    alias_names = {item["alias"] for item in index["area_aliases"]}
    community_names = {item["name"] for item in index["communities"]}

    assert alias_names.isdisjoint(community_names)
    assert {"新填地", "东新"}.isdisjoint(community_names)


@pytest.mark.parametrize("alias", ["新填地", "东新"])
def test_removing_confirmed_alias_fails_coverage(alias: str) -> None:
    definitions = tuple(item for item in AREA_ALIAS_DEFINITIONS if item.alias != alias)
    result = validate_area_alias_definitions(definitions)

    assert result.ok is False
    assert result.missing_valid_aliases == 1


def test_normalized_alias_conflict_is_blocking() -> None:
    legacy_index = build_rewrite_inventory_index(rows_with_viewing())
    snapshot_index = dict(legacy_index)
    snapshot_index["area_aliases"] = list(legacy_index["area_aliases"]) + [
        AreaAliasDefinition("东新", "拱墅万达 北部软件园 城北万象城", "test_conflict").to_index_entry()
    ]

    mismatches, _ = compare_rewrite_inventory_index(legacy_index, snapshot_index)

    assert any(
        item["code"] == "rewrite_index_area_alias_coverage"
        and item["severity"] == "blocking"
        and item["active_alias_conflicts"] == 1
        for item in mismatches
    )


def test_ambiguous_and_obsolete_aliases_are_not_active_index_entries() -> None:
    ambiguous = AreaAliasDefinition("武林", "", "test", status="ambiguous", ambiguity=True)
    obsolete = AreaAliasDefinition("老东新", DONGXIN_AREA, "test", status="obsolete")
    definitions = AREA_ALIAS_DEFINITIONS + (ambiguous, obsolete)
    entries = area_alias_index_entries(definitions)
    active_aliases = {item["alias"] for item in entries}

    assert "武林" not in active_aliases
    assert "老东新" not in active_aliases
    assert "东新" in active_aliases


def test_existing_area_query_aliases_do_not_regress() -> None:
    for query in ("万达附近两室", "新天地4000左右两室", "东新园有没有一室"):
        hits = main._area_alias_hits(query)
        assert hits, query
        assert hits[0]["canonical"]

    index = build_rewrite_inventory_index(rows_with_viewing())
    sliced = slice_rewrite_inventory_index(index, query="新天地4000左右两室")
    assert sliced["exact_area_hits"]
    assert sliced["exact_community_hits"] == []


def test_area_alias_entries_are_utf8_and_deterministically_ordered() -> None:
    first = area_alias_index_entries()
    second = area_alias_index_entries()
    text = json.dumps(first, ensure_ascii=False)

    assert first == second
    assert first == sorted(first, key=lambda item: (str(item["normalized_alias"]), str(item["alias"])))
    assert "新填地" in text
    assert "东新园 杭氧 新天地" in text
    assert "\ufffd" not in text


def test_local_shadow_reconciliation_has_no_area_alias_mismatch(tmp_path: Path) -> None:
    rows = rows_with_viewing(password="778899#")
    legacy_index = build_rewrite_inventory_index(rows)
    snapshot, report = SnapshotBuilder().build(rows, source_metadata(), generated_at="2026-06-25T00:00:00Z")
    assert report.ok

    reconciliation = reconcile_inventory_snapshot(
        legacy_rows=rows,
        snapshot=snapshot,
        legacy_rewrite_index=legacy_index,
    )
    codes = {item["code"] for item in reconciliation.rewrite_index_mismatches}
    coverage = reconciliation.safe_summary["area_alias_coverage"]

    assert reconciliation.passed is True
    assert reconciliation.severity_counts["blocking"] == 0
    assert "rewrite_index_mismatch.area_aliases" not in codes
    assert coverage["missing_valid_aliases"] == 0
    assert coverage["unresolved_aliases"] == 0
    assert coverage["active_alias_conflicts"] == 0
    assert coverage["unknown_canonical_areas"] == 0

    result = InventorySnapshotShadowCoordinator(mode="shadow", root=tmp_path / "shadow").run(
        legacy_rows=rows,
        source_metadata=source_metadata(),
        legacy_rewrite_index=legacy_index,
        sync_run_id="area-alias-local-001",
    )
    scan_passed, issues = scan_safe_artifacts_for_canaries(tmp_path / "shadow")

    assert result["reconciliation_passed"] is True
    assert result["blocking_count"] == 0
    assert scan_passed is True
    assert issues == []
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "shadow").rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".csv", ".txt", ".md"}
        and "private" not in path.relative_to(tmp_path / "shadow").parts
    )
    assert "778899#" not in public_text


def test_alias_helpers_do_not_depend_on_local_diagnostics() -> None:
    assert not Path(".local/m1c3-diagnostics/area_aliases.json").exists()
    alias_map = {
        item["alias"]: item["canonical_area"]
        for item in area_alias_index_entries()
    }
    assert alias_map["新填地"] == DONGXIN_AREA
