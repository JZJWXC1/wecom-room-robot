from __future__ import annotations

from pathlib import Path

from qa_artifacts import (
    run_rag_3questions_10turns_utf8,
    run_rag_5questions_5turns_utf8,
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
