from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text
from app.services.inventory_query import (
    has_new_anchor_outside_rows,
    parse_inventory_query,
    row_matches_hard_constraints,
)


def test_normalize_search_text_keeps_chinese_entities() -> None:
    normalized = normalize_search_text("万达有什么2000以下的一室")

    assert normalized == "万达有什么2000以下的一室"
    assert "万达" in normalized
    assert "一室" in normalized
    assert fuzzy_contains_score("棠闰府", "棠润府") > 0
    assert fuzzy_contains_score("荣润府", "棠润府") == 0


def test_parse_budget_room_type_and_area_anchor() -> None:
    query = parse_inventory_query("东站有没有2000左右的两室啊")

    assert query.price_range == (1500, 2500)
    assert "两室" in query.room_type_labels
    assert query.anchor_terms == ("东站",)


def test_parse_bare_price_range_followup() -> None:
    query = parse_inventory_query("4000-5000 的呢")

    assert query.price_range == (4000, 5000)
    assert query.room_refs == ()


def test_budget_ranges_are_not_room_refs() -> None:
    below_query = parse_inventory_query("万达2000以下的一室")
    range_query = parse_inventory_query("拱墅万达 2500-3500 两室")
    zero_range_query = parse_inventory_query("拱墅万达 0-2000 一室 视频")

    assert below_query.room_refs == ()
    assert range_query.room_refs == ()
    assert zero_range_query.room_refs == ()


def test_parse_inventory_query_removes_business_filler_from_anchor() -> None:
    wanda_query = parse_inventory_query("万达有什么2000以下的一室")
    shiqiao_query = parse_inventory_query("石桥附近5000左右有两室吗？最好整租。")
    deposit_query = parse_inventory_query("荣润府有没有押一付一的？预算1600到1800。")

    assert wanda_query.anchor_terms == ("万达",)
    assert shiqiao_query.anchor_terms == ("石桥",)
    assert deposit_query.anchor_terms == ("荣润府",)


def test_room_ref_does_not_swallow_following_price() -> None:
    query = parse_inventory_query("合峙悦府6-1-1204B是不是1500？今天能看吗？")

    assert query.room_refs == ("6-1-1204b",)


def test_room_ref_supports_letter_prefix_building() -> None:
    query = parse_inventory_query("东方茂T3-1540视频发我")

    assert query.room_refs == ("t3-1540",)


def test_letter_prefix_room_ref_can_match_prefix_in_community_name() -> None:
    query = parse_inventory_query("东方茂T3-1540是不是一室一厅？")
    row = {"小区": "东方茂商业中心T", "房号": "3-1540", "户型分类": "一室一厅"}

    assert row_matches_hard_constraints(row, query)


def test_specific_room_type_does_not_degrade_to_broad_type() -> None:
    query = parse_inventory_query("两室一厅有没有")

    assert query.room_type_labels == ("两室一厅",)


def test_one_room_broad_match_includes_one_room_living_room() -> None:
    query = parse_inventory_query("万达有什么2000以下的一室")
    one_room = {"户型分类": "一室", "押一付一": "1600"}
    one_room_living = {"户型分类": "一室一厅", "押一付一": "1900"}
    two_room = {"户型分类": "两室一厅", "押一付一": "1900"}

    assert query.room_type_labels == ("一室",)
    assert row_matches_hard_constraints(one_room, query)
    assert row_matches_hard_constraints(one_room_living, query)
    assert not row_matches_hard_constraints(two_room, query)


def test_single_room_request_matches_one_room_without_living_room() -> None:
    query = parse_inventory_query("北部软件园附近便宜点的单间还有吗？客户预算1800以内。")
    one_room = {"户型分类": "一室", "押一付一": "1600"}
    one_room_living = {"户型分类": "一室一厅", "押二付一": "1800"}
    studio = {"户型分类": "开间", "押一付一": "1500"}
    two_room = {"户型分类": "两室一厅", "押一付一": "1700"}
    expensive_one_room = {"户型分类": "一室一厅", "押一付一": "2100"}

    assert query.room_type_labels == ("单间",)
    assert row_matches_hard_constraints(one_room, query)
    assert not row_matches_hard_constraints(one_room_living, query)
    assert row_matches_hard_constraints(studio, query)
    assert not row_matches_hard_constraints(two_room, query)
    assert not row_matches_hard_constraints(expensive_one_room, query)


def test_one_room_with_living_room_request_is_exact() -> None:
    query = parse_inventory_query("万达2000以下一室带厅的有哪些")
    one_room = {"户型分类": "一室", "押一付一": "1600"}
    one_room_living = {"户型分类": "一室一厅", "押一付一": "1900"}

    assert query.room_type_labels == ("一室一厅",)
    assert not row_matches_hard_constraints(one_room, query)
    assert row_matches_hard_constraints(one_room_living, query)


def test_row_must_satisfy_budget_and_room_type() -> None:
    query = parse_inventory_query("东站有没有2000左右的两室啊")
    one_room = {"区域": "东站", "小区": "京漾东韵府", "房号": "4-2-601B", "户型": "一室朝南独立厨卫", "押一付": "2000"}
    expensive_two_room = {"区域": "东站", "小区": "东站两室小区", "房号": "1-201", "户型": "两室一厅", "押一付": "4200"}
    matched = {"区域": "东站", "小区": "东站两室小区", "房号": "1-202", "户型": "两室一厅", "押一付": "2200"}

    assert not row_matches_hard_constraints(one_room, query)
    assert not row_matches_hard_constraints(expensive_two_room, query)
    assert row_matches_hard_constraints(matched, query)


def test_feature_request_must_match_room_description() -> None:
    query = parse_inventory_query("华丰附近有没有带燃气的一室一厅？")
    no_gas = {"户型分类": "一室一厅", "户型描述": "一室一厅朝南带阳台", "押一付一": "4200"}
    with_gas = {"户型分类": "一室一厅", "户型描述": "一室一厅带燃气阳台", "押一付一": "4300"}

    assert query.room_type_labels == ("一室一厅",)
    assert query.feature_labels == ("燃气",)
    assert not row_matches_hard_constraints(no_gas, query)
    assert row_matches_hard_constraints(with_gas, query)


def test_new_anchor_outside_rows_detects_new_query_not_old_candidates() -> None:
    old_candidates = [
        {"区域": "东站", "小区": "京漾东韵府", "房号": "4-2-601B"},
        {"区域": "东站", "小区": "骏塘名庭", "房号": "8-1101A"},
    ]

    assert has_new_anchor_outside_rows("杨家府视频有没有", old_candidates)
    assert not has_new_anchor_outside_rows("东站这些视频有没有", old_candidates)
