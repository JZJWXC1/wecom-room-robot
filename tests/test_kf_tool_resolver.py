from __future__ import annotations

from app.main import _should_restore_candidate_rows_for_media_followup
from app.services.kf_tool_resolver import resolve_tool_targets


def test_media_followup_without_index_binds_recent_candidate_rows() -> None:
    context = {
        "last_candidate_set": {
            "candidates": [
                {"candidate_number": 1, "listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                {"candidate_number": 2, "listing_id": "lst-b", "小区": "星桥锦绣嘉苑", "房号": "20-1606B"},
            ]
        }
    }

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_image", "generate_reply"],
        content="图片也发我。",
        understanding={"constraint_proof": {}, "structured_task": {"original_text": "图片也发我。"}},
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert [row["房号"] for row in result.target_rows] == ["20-1606A", "20-1606B"]
    assert result.candidate_binding["status"] == "bound"
    assert result.candidate_binding["source"] == "media_candidate_context"
    assert result.missing_target_reason == ""


def test_scoped_community_media_request_does_not_reuse_current_candidate_rows() -> None:
    context = {
        "last_candidate_set": {
            "candidates": [
                {"candidate_number": 1, "listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                {"candidate_number": 2, "listing_id": "lst-b", "小区": "星桥锦绣嘉苑", "房号": "20-1606B"},
            ]
        }
    }

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_video", "generate_reply"],
        content="星桥锦绣嘉苑有视频吗",
        understanding={
            "constraint_proof": {"communities": ["星桥锦绣嘉苑"], "wants_video": True},
            "structured_task": {"original_text": "星桥锦绣嘉苑有视频吗"},
        },
        context=context,
        inventory_rows=[
            {"candidate_number": 1, "listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
            {"candidate_number": 2, "listing_id": "lst-b", "小区": "星桥锦绣嘉苑", "房号": "20-1606B"},
        ],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.field_target_error["reason"] == "community_media_request_missing_room_ref"


def test_selected_media_index_out_of_range_returns_selection_error() -> None:
    context = {
        "last_candidate_set": {
            "candidates": [
                {"candidate_number": 1, "listing_id": "lst-a", "小区": "华丰新苑", "房号": "20-1-504"},
                {"candidate_number": 2, "listing_id": "lst-b", "小区": "石桥铭苑", "房号": "21-1201A"},
            ]
        }
    }

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_video", "generate_reply"],
        content="筛出来的1和3视频发我。",
        understanding={
            "constraint_proof": {"wants_video": True},
            "structured_task": {"original_text": "筛出来的1和3视频发我。"},
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "requested_candidate_index_out_of_range"
    assert result.candidate_binding["status"] == "error"


def test_selected_index_without_candidate_context_does_not_bind_single_search_row() -> None:
    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "generate_reply"],
        content="第一套图也发一下。",
        understanding={
            "constraint_proof": {"selected_indices": [1]},
            "structured_task": {"original_text": "第一套图也发一下。"},
        },
        context={},
        inventory_rows=[
            {"candidate_number": 1, "listing_id": "lst-a", "小区": "兴业杨家府", "房号": "4-1502"}
        ],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.inventory_rows_override == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.candidate_binding["status"] == "error"


def test_selected_first_field_followup_binds_confirmed_room_without_candidate_set() -> None:
    confirmed_row = {
        "listing_id": "lst-yangjia",
        "小区": "杨家新雅苑",
        "房号": "36-1-1102",
        "户型": "100方三房两卫客厅带阳台",
        "户型分类": "三室一厅",
    }
    context = {"confirmed_room": {"label": "杨家新雅苑36-1-1102", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "generate_reply"],
        content="第一套户型特点怎么样",
        understanding={
            "context_reference": False,
            "constraint_proof": {},
            "structured_task": {
                "original_text": "第一套户型特点怎么样",
                "tool_requirements": {},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == [confirmed_row]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"


def test_viewing_password_followup_binds_confirmed_room_without_context_pronoun() -> None:
    confirmed_row = {
        "listing_id": "lst-xingqiao",
        "小区": "星桥锦绣嘉苑",
        "房号": "20-1606A",
        "看房方式密码": "提前联系",
    }
    context = {"confirmed_room": {"label": "星桥锦绣嘉苑20-1606A", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"],
        content="看房密码多少，今天可以看吗？",
        understanding={
            "context_reference": False,
            "constraint_proof": {},
            "structured_task": {
                "original_text": "看房密码多少，今天可以看吗？",
                "tool_requirements": {"needs_viewing_policy": True},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == [confirmed_row]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"


def test_note_followup_binds_confirmed_room_as_video_material_request() -> None:
    confirmed_row = {
        "listing_id": "lst-longyin",
        "小区": "白田畈龙吟府",
        "房号": "4-902B",
    }
    context = {"confirmed_room": {"label": "白田畈龙吟府4-902B", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
        content="笔记发我",
        understanding={
            "intent": "media",
            "constraint_proof": {"wants_video": True},
            "structured_task": {
                "original_text": "笔记发我",
                "tool_requirements": {"needs_video": True},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == [confirmed_row]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"
    assert result.candidate_binding["source"] == "confirmed_room"


def test_plural_price_comparison_with_only_confirmed_room_returns_selection_error() -> None:
    # 口径变更(P0-2 部署批,台账 20260704):原契约允许"这两套哪个价格低"由
    # 单套 confirmed room 绑定作答,与判分锚"复数序号目标不完整=high"
    # (tests/test_qa_utf8_inputs.py)及 docs/rag-rule-ownership.md 的
    # "candidate_binding 只能绑定显式候选集"裁决直接矛盾——单套价格无法回答
    # 两套比较,必须反问重列候选。
    confirmed_row = {
        "listing_id": "lst-shiqiao",
        "小区": "石桥铭苑",
        "房号": "21-1201A",
        "押一付一": "4500",
    }
    context = {"confirmed_room": {"label": "石桥铭苑21-1201A", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "generate_reply"],
        content="这两套哪个价格低一点？",
        understanding={
            "intent": "production_llm1",
            "constraint_proof": {"wants_price": True},
            "structured_task": {
                "original_text": "这两套哪个价格低一点？",
                "tool_requirements": {"needs_inventory_search": True},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [1, 2]
    assert result.candidate_binding["status"] == "error"


def test_explicit_media_indices_still_require_candidate_context_despite_confirmed_room() -> None:
    confirmed_row = {
        "listing_id": "lst-shiqiao",
        "小区": "石桥铭苑",
        "房号": "21-1201A",
    }
    context = {"confirmed_room": {"label": "石桥铭苑21-1201A", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="筛出来的1和3视频发我",
        understanding={
            "intent": "production_llm1",
            "constraint_proof": {"wants_video": True},
            "structured_task": {
                "original_text": "筛出来的1和3视频发我",
                "tool_requirements": {"needs_video": True},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [1, 3]


def test_field_followup_binds_confirmed_room_from_llm1_task_constraints() -> None:
    confirmed_row = {
        "listing_id": "lst-yangjia",
        "小区": "杨家新雅苑",
        "房号": "36-1-1102",
        "户型": "100方三房两卫客厅带阳台",
        "户型分类": "三室一厅",
    }

    result = resolve_tool_targets(
        actions=["search_inventory", "generate_reply"],
        content="第一套户型特点怎么样",
        understanding={
            "context_reference": False,
            "constraint_proof": {},
            "structured_task": {
                "llm1_task_packet": {
                    "tasks": [
                        {
                            "task_id": "task-1-inventory_detail",
                            "task_type": "inventory_search",
                            "constraints": {
                                "confirmed_room": {
                                    "label": "杨家新雅苑36-1-1102",
                                    "row": confirmed_row,
                                }
                            },
                        }
                    ]
                }
            },
        },
        context={},
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == [confirmed_row]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"


def test_selected_out_of_range_still_errors_when_only_confirmed_room_exists() -> None:
    confirmed_row = {
        "listing_id": "lst-yangjia",
        "小区": "杨家新雅苑",
        "房号": "36-1-1102",
    }
    context = {"confirmed_room": {"label": "杨家新雅苑36-1-1102", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "generate_reply"],
        content="第三套户型特点怎么样",
        understanding={
            "context_reference": False,
            "constraint_proof": {},
            "structured_task": {
                "original_text": "第三套户型特点怎么样",
                "tool_requirements": {},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.candidate_binding["status"] == "error"


def test_selected_media_request_does_not_bind_llm1_confirmed_room_without_candidate_set() -> None:
    confirmed_row = {
        "listing_id": "lst-dongxin",
        "小区": "东新园",
        "房号": "8-1201",
    }

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_video", "generate_reply"],
        content="第一个房源的视频发我。",
        understanding={
            "context_reference": False,
            "constraint_proof": {"wants_video": True},
            "structured_task": {
                "llm1_task_packet": {
                    "tasks": [
                        {
                            "task_id": "task-1-media",
                            "task_type": "send_media",
                            "constraints": {
                                "confirmed_room": {
                                    "label": "东新园8-1201",
                                    "row": confirmed_row,
                                }
                            },
                        }
                    ]
                },
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.candidate_binding["status"] == "error"


def test_selected_media_request_does_not_bind_confirmed_room_without_candidate_set() -> None:
    confirmed_row = {
        "listing_id": "lst-dongxin",
        "小区": "东新园",
        "房号": "8-1201",
    }
    context = {"confirmed_room": {"label": "东新园8-1201", "row": confirmed_row}}

    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_video", "generate_reply"],
        content="第一个房源的视频发我。",
        understanding={
            "context_reference": False,
            "constraint_proof": {"wants_video": True},
            "structured_task": {
                "original_text": "第一个房源的视频发我。",
                "tool_requirements": {"needs_video": True},
            },
        },
        context=context,
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.candidate_binding["status"] == "error"


def test_selected_image_request_does_not_treat_pending_video_as_candidate_context() -> None:
    result = resolve_tool_targets(
        actions=["search_inventory", "context_tools", "send_image", "explain_missing_media", "generate_reply"],
        content="不是3号，是第二套图片",
        understanding={
            "constraint_proof": {"wants_image": True},
            "structured_task": {
                "original_text": "不是3号，是第二套图片",
                "tool_requirements": {"needs_image": True},
            },
        },
        context={},
        inventory_rows=[],
        pending_video={"labels": ["石桥铭苑21-1201A"], "requested_count": 1},
        pending_video_rows=[{"小区": "石桥铭苑", "房号": "21-1201A"}],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [2]
    assert result.candidate_binding["status"] == "error"


def test_media_followup_restore_predicate_only_allows_short_context_requests() -> None:
    short_followup = {
        "constraint_proof": {"wants_video": True},
        "structured_task": {"original_text": "有视频就先发视频。"},
    }
    note_followup = {
        "constraint_proof": {"wants_video": True},
        "structured_task": {"original_text": "笔记发我"},
    }
    scoped_request = {
        "constraint_proof": {"wants_video": True, "communities": ["星桥锦绣嘉苑"]},
        "structured_task": {"original_text": "星桥锦绣嘉苑有视频吗"},
    }

    assert _should_restore_candidate_rows_for_media_followup(
        content="有视频就先发视频。",
        understanding=short_followup,
        actions=["search_inventory", "send_video", "generate_reply"],
    )
    assert _should_restore_candidate_rows_for_media_followup(
        content="笔记发我",
        understanding=note_followup,
        actions=["search_inventory", "send_video", "generate_reply"],
    )
    assert not _should_restore_candidate_rows_for_media_followup(
        content="星桥锦绣嘉苑有视频吗",
        understanding=scoped_request,
        actions=["search_inventory", "send_video", "generate_reply"],
    )


def test_plural_indices_with_single_pending_video_label_return_selection_error() -> None:
    # 回归(shiqiao_whole_rent turn8):候选集被清空后,序号[1,2]的原视频请求
    # 不得由仅剩 1 条的待发视频记录半桶水绑定成单套(幻觉绑定)。
    pending_row = {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"}

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="这两套有没有原视频或者高清点的？",
        understanding={
            "intent": "media",
            "context_reference": True,
            "constraint_proof": {
                "selected_indices": [1, 2],
                "wants_video": True,
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "这两套有没有原视频或者高清点的？",
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[
            {"candidate_number": 1, "listing_id": "lst-shiqiao-a", "小区": "石桥铭苑", "房号": "6-1102"},
            {"candidate_number": 2, "listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"},
        ],
        pending_video={"labels": ["石桥铭苑21-1201A"], "requested_count": 2},
        pending_video_rows=[pending_row],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.inventory_rows_override == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [1, 2]
    assert result.candidate_binding["status"] == "error"


def test_pending_video_continuation_without_indices_still_binds_pending_rows() -> None:
    # 正向保护:无显式序号的原视频跟进,仍应绑定待发视频记录(合法续发场景)。
    pending_row = {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"}

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="有原视频吗？发我一下。",
        understanding={
            "intent": "media",
            "context_reference": True,
            "constraint_proof": {"wants_video": True, "wants_original_video": True},
            "structured_task": {
                "original_text": "有原视频吗？发我一下。",
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[],
        pending_video={"labels": ["石桥铭苑21-1201A"], "requested_count": 1},
        pending_video_rows=[pending_row],
        target_limit=5,
    )

    assert result.target_rows == [pending_row]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"
    assert result.candidate_binding["source"] == "pending_video_labels"


def test_plural_context_selection_with_only_confirmed_room_returns_selection_error() -> None:
    # 回归:复数指代选择(这两套)不可能由单套 confirmed room 满足,
    # 候选集缺失时必须报 missing_current_candidate_set 反问,不得降级绑单套。
    confirmed_row = {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"}

    result = resolve_tool_targets(
        actions=["search_inventory", "generate_reply"],
        content="这两套哪套水电更划算？",
        understanding={
            "intent": "inventory",
            "context_reference": True,
            "constraint_proof": {"selected_indices": [1, 2]},
            "structured_task": {
                "original_text": "这两套哪套水电更划算？",
                "tool_requirements": {"needs_utilities": True},
            },
        },
        context={"confirmed_room": {"label": "石桥铭苑21-1201A", "row": confirmed_row}},
        inventory_rows=[],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [1, 2]

def test_out_of_range_index_with_single_pending_video_returns_selection_error() -> None:
    # 回归(第二裁判 20260704 P1):pending 只有 1 条时,"第2套原视频"按条数比较
    # (1>=1)会穿透覆盖检查并错绑唯一一条;必须按最大序号拦截(2>1)。
    pending_row = {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"}

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="第2套原视频发我",
        understanding={
            "intent": "media",
            "constraint_proof": {
                "selected_indices": [2],
                "wants_video": True,
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "第2套原视频发我",
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[],
        pending_video={"labels": ["石桥铭苑21-1201A"], "requested_count": 2},
        pending_video_rows=[pending_row],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [2]
    assert result.candidate_binding["status"] == "error"


def test_out_of_range_index_plain_video_with_single_pending_reports_error() -> None:
    # 回归(第二裁判 20260704 P1 第二半):"第2套视频"(非原视频)同状态下
    # 此前既无目标也无 selection_error(静默漏报),必须显式报错反问。
    pending_row = {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"}

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="第2套视频发我",
        understanding={
            "intent": "media",
            "constraint_proof": {"selected_indices": [2], "wants_video": True},
            "structured_task": {
                "original_text": "第2套视频发我",
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[],
        pending_video={"labels": ["石桥铭苑21-1201A"], "requested_count": 2},
        pending_video_rows=[pending_row],
        target_limit=5,
    )

    assert result.target_rows == []
    assert result.selection_error["reason"] == "missing_current_candidate_set"
    assert result.selection_error["requested_indices"] == [2]


def test_full_coverage_indices_with_matching_pending_rows_still_bind() -> None:
    # 正向保护:序号[1,2]配 2 条待发视频记录(原视频跟进),最大序号未越界,
    # 仍应绑定全部待发行(合法续发场景不受越界拦截影响)。
    pending_rows = [
        {"listing_id": "lst-shiqiao-a", "小区": "石桥铭苑", "房号": "6-1102"},
        {"listing_id": "lst-shiqiao-b", "小区": "石桥铭苑", "房号": "21-1201A"},
    ]

    result = resolve_tool_targets(
        actions=["search_inventory", "send_video", "generate_reply"],
        content="1和2的原视频发我",
        understanding={
            "intent": "media",
            "context_reference": True,
            "constraint_proof": {
                "selected_indices": [1, 2],
                "wants_video": True,
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "1和2的原视频发我",
                "tool_requirements": {"needs_video": True},
            },
        },
        context={},
        inventory_rows=[],
        pending_video={"labels": ["石桥铭苑6-1102", "石桥铭苑21-1201A"], "requested_count": 2},
        pending_video_rows=pending_rows,
        target_limit=5,
    )

    assert [row["房号"] for row in result.target_rows] == ["6-1102", "21-1201A"]
    assert result.selection_error == {}
    assert result.candidate_binding["status"] == "bound"
    assert result.candidate_binding["source"] == "pending_video_labels"