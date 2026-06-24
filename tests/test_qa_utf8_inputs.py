from __future__ import annotations

from pathlib import Path

from qa_artifacts import (
    run_rag_10windows_10turns_utf8,
    run_rag_3questions_10turns_utf8,
    run_rag_5questions_5turns_utf8,
    run_rag_random_guard_utf8,
    run_rag_test_text_window_utf8,
)


BAD_MOJIBAKE_PHRASES = ("涓囪揪", "鑽ｆ", "鐭虫", "鎴挎", "鍏嶆娂", "瑙嗛")


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
    assert report["window"]["first_user_raw"] == "万达附近1500左右还有哪些？先发几套视频我筛一下。"


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
    assert report["window"]["first_user_raw"] == "万达附近1500左右还有哪些？先发几套视频我筛一下。"


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
    assert all(len(window["turns"]) == 10 for window in windows)


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
