from __future__ import annotations

import ast
import csv
import json
from datetime import datetime
from pathlib import Path

from qa_artifacts import (
    run_rag_10windows_10turns_utf8,
    run_rag_3questions_10turns_utf8,
    run_rag_5questions_5turns_utf8,
    run_rag_random_guard_utf8,
    run_rag_test_text_window_utf8,
)


BAD_MOJIBAKE_PHRASES = ("涓囪揪", "鑽ｆ", "鐭虫", "鎴挎", "鍏嶆娂", "瑙嗛")


def _windows_constant_from_source() -> list[dict[str, object]]:
    source_path = Path(__file__).resolve().parents[1] / "qa_artifacts" / "run_rag_10windows_10turns_utf8.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "WINDOWS":
            return ast.literal_eval(node.value)
    raise AssertionError("WINDOWS constant not found")


def test_all_qa_scripts_are_utf8_clean() -> None:
    for path in Path("qa_artifacts").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in BAD_MOJIBAKE_PHRASES), path


def test_three_question_qa_inputs_pass_chinese_integrity() -> None:
    report = run_rag_3questions_10turns_utf8.chinese_integrity_report(
        run_rag_3questions_10turns_utf8.TURNS
    )

    assert report["passed"], report
    assert report["first_user_raw"] == "万达有什么2000以下的一室"


def test_five_question_qa_inputs_pass_chinese_integrity() -> None:
    report = run_rag_5questions_5turns_utf8.chinese_integrity_report(
        [seed for _, seed in run_rag_5questions_5turns_utf8.SEEDS]
        + [run_rag_5questions_5turns_utf8.next_user("", index, "") for index in range(2, 6)]
    )

    assert report["passed"], report
    assert report["first_user_raw"] == "万达附近1500左右还有哪些？客户想今天先看两套。"


def test_full_test_text_window_inputs_pass_chinese_integrity() -> None:
    questions = run_rag_test_text_window_utf8.load_questions()
    selected = questions[:10]
    report = run_rag_test_text_window_utf8.assert_utf8_inputs(selected, questions)

    assert report["full"]["passed"], report
    assert report["window"]["passed"], report
    assert report["window"]["first_user_raw"] == run_rag_10windows_10turns_utf8.WINDOWS[0]["turns"][0]


def test_required_single_window_inputs_pass_chinese_integrity() -> None:
    questions = run_rag_test_text_window_utf8.load_questions(
        run_rag_test_text_window_utf8.DEFAULT_WINDOW_INPUT_PATH
    )
    report = run_rag_test_text_window_utf8.assert_utf8_inputs(
        questions,
        run_rag_test_text_window_utf8.load_questions(),
        selected_source_path=run_rag_test_text_window_utf8.DEFAULT_WINDOW_INPUT_PATH,
    )

    assert report["full"]["passed"], report
    assert report["window"]["passed"], report
    assert report["window"]["first_user_raw"] == run_rag_10windows_10turns_utf8.WINDOWS[0]["turns"][0]


def test_l4_qa_inventory_fixture_contains_required_entities() -> None:
    fixture_path = Path("tests/fixtures/qa/test_inventory_cache.csv")
    index_path = Path("tests/fixtures/qa/test_rewrite_inventory_index.json")
    rows = list(csv.DictReader(fixture_path.read_text(encoding="utf-8-sig").splitlines()))
    labels = {f"{row.get('小区', '')}{row.get('房号', '')}" for row in rows}
    communities = {row.get("小区", "") for row in rows}
    index = json.loads(index_path.read_text(encoding="utf-8"))

    assert "兴业杨家府" in communities
    assert "杨家新雅苑" in communities
    assert "杨乐府" in communities
    assert "棠润府15-2-801B" in labels
    assert index["cache_meta"]["source_detail"] == "tests/fixtures/qa/test_inventory_cache.csv"
    assert index["row_count"] == len(rows)


def test_fixture_questions_match_windows_constant_without_importing_source_script() -> None:
    windows = _windows_constant_from_source()
    full_fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "qa" / "test_text_full_utf8.json").read_text(encoding="utf-8")
    )
    single_fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "qa" / "single_window_required_utf8.json").read_text(encoding="utf-8")
    )
    flattened = [turn for window in windows for turn in window["turns"]]

    assert single_fixture["questions"] == windows[0]["turns"]
    assert full_fixture["questions"] == flattened


def test_partial_10window_qa_cannot_be_marked_full_suite_completed() -> None:
    status = run_rag_10windows_10turns_utf8.build_completion_status(
        selected_completed=True,
        selected_window_count=1,
        expected_full_window_count=10,
        expected_selected_turn_count=10,
        actual_window_count=1,
        actual_turn_count=10,
        expected_full_turn_count=100,
        full_suite_requested=False,
    )

    assert status["completed"] is True
    assert status["full_suite_completed"] is False
    assert status["selected_window_count"] == 1


def test_full_10window_qa_completion_requires_all_windows_and_turns() -> None:
    status = run_rag_10windows_10turns_utf8.build_completion_status(
        selected_completed=True,
        selected_window_count=10,
        expected_full_window_count=10,
        expected_selected_turn_count=100,
        actual_window_count=10,
        actual_turn_count=100,
        expected_full_turn_count=100,
        full_suite_requested=True,
    )

    assert status["completed"] is True
    assert status["full_suite_completed"] is True


def test_random_guard_qa_generation_is_utf8_clean_and_covers_required_categories() -> None:
    windows = run_rag_random_guard_utf8.generate_random_guard_windows(seed=20260624)
    integrity = run_rag_10windows_10turns_utf8.chinese_integrity_report(windows, required_tokens=())
    coverage = run_rag_random_guard_utf8.coverage_report(windows)

    assert integrity["passed"], integrity
    assert coverage["passed"], coverage
    assert windows[0]["turns"][0]


def test_qa_artifact_filename_is_ascii_and_not_based_on_user_text() -> None:
    path = run_rag_10windows_10turns_utf8.artifact_path_for(
        '客户问:新天地? "4000/5000" \\ 换行\n',
        now=datetime(2026, 6, 24, 20, 30, 5),
    )

    assert path.name == "qa_artifact_20260624_203005.json" or path.name.endswith("_20260624_203005.json")
    assert all(ord(char) < 128 for char in path.name)
    assert not any(char in path.name for char in '<>:"/\\|?*\r\n')
    assert "客户" not in path.name
    assert "新天地" not in path.name


def test_qa_artifact_atomic_write_supports_chinese_space_path_and_bad_user_text(tmp_path) -> None:
    target = tmp_path / "中文 目录" / "window_004_turn_035.json"
    payload = {
        "created_at": "2026-06-24T20:30:05",
        "completed": True,
        "quality_status": {"passed": True, "exit_code": 0},
        "windows": [
            {
                "turns": [
                    {
                        "user": '冒号: 问号? 斜杠/反斜杠\\ "引号"\n换行',
                        "bot": {"texts": ["正常写入"]},
                        "problem": {"severity": "info"},
                    }
                ]
            }
        ],
    }

    run_rag_10windows_10turns_utf8._write_json_atomic(target, payload)

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["windows"][0]["turns"][0]["user"] == payload["windows"][0]["turns"][0]["user"]
    assert target.name == "window_004_turn_035.json"


def test_qa_artifact_failure_payload_is_infrastructure_error_not_completed() -> None:
    payload = {
        "completed": True,
        "full_suite_completed": True,
        "quality_status": {"passed": True, "exit_code": 0, "infrastructure_errors": []},
        "windows": [],
    }
    failure = run_rag_10windows_10turns_utf8._artifact_write_failure_payload(
        payload,
        OSError(22, "Invalid argument"),
        Path("bad:name.json"),
    )

    assert failure["completed"] is False
    assert failure["full_suite_completed"] is False
    assert failure["quality_status"]["passed"] is False
    assert failure["quality_status"]["infrastructure_error"] is True
    assert failure["quality_status"]["exit_code"] == 2
    assert failure["quality_status"]["infrastructure_errors"][0]["target_path"] == "bad:name.json"


def test_qa_canonical_hash_ignores_dynamic_fields_but_keeps_result_content() -> None:
    first = {
        "created_at": "2026-06-24T20:30:05",
        "completed": False,
        "quality_status": {"exit_code": 3, "high_count": 6, "medium_count": 2},
        "windows": [{"turns": [{"elapsed_sec": 1.23, "bot": {"texts": ["回复A"]}, "problem": {"severity": "high"}}]}],
    }
    second = {
        **first,
        "created_at": "2026-06-24T20:31:05",
        "windows": [{"turns": [{"elapsed_sec": 9.99, "bot": {"texts": ["回复A"]}, "problem": {"severity": "high"}}]}],
    }
    changed_reply = {
        **first,
        "windows": [{"turns": [{"elapsed_sec": 1.23, "bot": {"texts": ["回复B"]}, "problem": {"severity": "high"}}]}],
    }

    assert run_rag_10windows_10turns_utf8.canonical_result_hash(first) == run_rag_10windows_10turns_utf8.canonical_result_hash(second)
    assert run_rag_10windows_10turns_utf8.canonical_result_hash(first) != run_rag_10windows_10turns_utf8.canonical_result_hash(changed_reply)


def test_chinese_integrity_report_uses_repo_relative_posix_script_path() -> None:
    windows = [{"id": "sample", "turns": ["万达 视频 图片 房源表 免押 水电 密码 定房 原视频"] * 10}]
    report = run_rag_10windows_10turns_utf8.chinese_integrity_report(windows, required_tokens=())

    assert report["script_path"] == "qa_artifacts/run_rag_10windows_10turns_utf8.py"
    assert "\\" not in report["script_path"]
    assert all(len(window["turns"]) == 10 for window in windows)


def test_fact_based_random_guard_uses_rewrite_inventory_index_rows() -> None:
    sample_index = {
        "signature": "sig-test",
        "generated_at": "2026-06-24T15:00:00+08:00",
        "area_aliases": [
            {"alias": "新天地", "canonical": "东新园 杭氧 新天地"},
            {"alias": "万达", "canonical": "拱墅万达 北部软件园 城北万象城"},
        ],
        "similar_communities": [
            {"name": "棠润府", "options": [{"name": "荣润府"}]},
        ],
        "room_index": [
            {
                "area": "东新园 杭氧 新天地",
                "community": "棠润府",
                "room_no": "15-2-801B",
                "layout": "一室一厅",
                "price_yayi": "1600",
                "price_yaer": "1400",
            },
            {
                "area": "拱墅万达 北部软件园 城北万象城",
                "community": "兴业杨家府",
                "room_no": "10-1-1205",
                "layout": "两室一厅",
                "price_yayi": "3900",
                "price_yaer": "3700",
            },
            {
                "area": "拱墅万达 北部软件园 城北万象城",
                "community": "荣润府",
                "room_no": "1-602A",
                "layout": "一室一厅",
                "price_yayi": "1900",
                "price_yaer": "1700",
            },
        ],
    }

    windows = run_rag_random_guard_utf8.generate_fact_based_guard_windows(
        sample_index,
        seed=20260624,
        count=10,
    )
    coverage = run_rag_random_guard_utf8.coverage_report(windows)

    assert len(windows) == 10
    assert coverage["passed"], coverage
    assert all(window["generation_source"] == "rewrite_inventory_index" for window in windows)
    joined = "\n".join(turn for window in windows for turn in window["turns"])
    assert "棠润府" in joined or "兴业杨家府" in joined
    assert "15-2-801B" in joined or "10-1-1205" in joined
    assert "有视频吗？" in joined


def test_qa_problem_detects_target_rows_violating_rewrite_community() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "杨家新雅苑那套也发视频，最好清楚一点。",
            "bot": {"texts": ["本地暂时没找到视频：兴业杨家府4-1502。"], "video_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"communities": ["杨家新雅苑"]}},
            "tool": {"target_rows": ["兴业杨家府4-1502"], "inventory_rows": ["杨家新雅苑15-603"]},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "Planner/工具目标绑定"


def test_qa_problem_allows_real_entity_clarification_options() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "杨家府还有房子吗？客户说名字可能没记准。",
            "bot": {
                "texts": [
                    "你说的“杨家府”我这边有几个相近小区：兴业杨家府、杨乐府、杨家新雅苑。你确认下是哪一个，我再按最新房源表查。"
                ]
            },
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的“杨家府”我这边有几个相近小区：兴业杨家府、杨乐府、杨家新雅苑。你确认下是哪一个，我再按最新房源表查。",
                "constraint_proof": {},
            },
            "stage_timings": [
                {
                    "stage": "rewrite_intent",
                    "summary": {
                        "entity_resolution": {
                            "community_options": [
                                {
                                    "raw_text": "杨家府",
                                    "options": ["兴业杨家府", "杨乐府", "杨家新雅苑"],
                                }
                            ]
                        }
                    },
                }
            ],
        }
    )

    assert problem["severity"] == "info"


def test_qa_problem_keeps_not_found_clarification_as_medium() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "杨家府还有房子吗？客户说名字可能没记准。",
            "bot": {"texts": ["最新房源表里暂时没查到杨家府这个小区。你确认一下小区名。"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "最新房源表里暂时没查到杨家府这个小区。你确认一下小区名。",
                "constraint_proof": {},
            },
            "stage_timings": [
                {"stage": "rewrite_intent", "summary": {"entity_resolution": {"community_options": []}}}
            ],
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_detects_area_query_narrowed_to_specific_community() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "石桥街道附近3000-4000有两室吗？客户想今天先筛两套。",
            "bot": {"texts": ["暂时没查到石桥铭苑符合条件的房源。"], "video_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {
                "constraint_proof": {
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "communities": ["石桥铭苑"],
                    "budget_range": [3000, 4000],
                    "layout": "两室",
                }
            },
            "tool": {"target_rows": [], "inventory_rows": []},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "问题重写/实体归一"


def test_qa_problem_detects_unrequested_original_video_batch() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "如果客户说视频糊，有没有原视频链接？",
            "bot": {"texts": ["按你说的条件先发这5套视频。"], "video_count": 5},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_original_video": True}},
            "tool": {
                "target_rows": [
                    "小洋坝家园二区7-1001E",
                    "大华海派风景2-1-402A",
                    "棠润府15-2-801B",
                ],
                "video_count": 5,
            },
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "素材目标绑定"


def test_qa_problem_detects_unbound_original_video_followup() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "有原视频或者清楚一点的吗？客户嫌转发后有点糊。",
            "bot": {
                "texts": [
                    "我这边还没绑定到具体房源，暂时没法发视频。你回我序号，或者直接发小区名+房号，我马上按那套查。"
                ],
                "video_count": 0,
            },
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_original_video": True}},
            "tool": {"target_rows": [], "video_count": 0},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "素材目标绑定"


def test_qa_problem_detects_selected_indices_bound_without_candidates() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "第1和第3套图片发我。",
            "bot": {"texts": ["本地暂时没找到图片：棠润府10-1004C。"], "image_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"selected_indices": [1, 3], "wants_image": True}},
            "tool": {"target_rows": ["棠润府10-1004C"], "image_count": 0},
            "blackbox": {"last_candidate_count_after_turn": 0},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "Planner/工具目标绑定"


def test_qa_problem_allows_selected_indices_bound_to_prior_pending_media_targets() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这两套有没有原视频或者高清点的？",
            "bot": {
                "texts": ["我这边暂时没稳定匹配到对应素材，不能乱发视频或图片。"],
                "video_count": 0,
            },
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {
                "constraint_proof": {
                    "selected_indices": [1, 2],
                    "wants_video": True,
                    "wants_original_video": True,
                }
            },
            "tool": {
                "target_rows": ["石桥铭苑6-1102", "华丰欣苑14-2-901"],
                "video_count": 0,
            },
            "blackbox": {
                "last_candidate_count_after_turn": 0,
                "pending_video_sends_before_turn": {
                    "labels": ["石桥铭苑6-1102", "华丰欣苑14-2-901"],
                    "requested_count": 2,
                },
            },
        }
    )

    assert problem["severity"] == "info"
