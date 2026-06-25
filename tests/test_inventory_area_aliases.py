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
    )
    assert result.returncode == 0, result.stdout + result.stderr


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


@pytest.mark.parametrize("path", ["app/services/llm.py"])
def test_customer_chain_files_match_fix1(path: str) -> None:
    assert_matches_fix1(path)


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
        "scripts/sync_feishu_region_inventory.py",
    ],
)
def test_sync_scripts_match_fix1(path: str) -> None:
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
