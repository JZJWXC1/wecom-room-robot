from __future__ import annotations

import asyncio
import ast
import csv
import json
import re
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


def test_send_receipt_fault_tests_are_utf8_clean() -> None:
    text = Path("tests/test_kf_send_receipt_faults.py").read_text(encoding="utf-8")
    mojibake_tokens = BAD_MOJIBAKE_PHRASES + ("灏忓尯", "鏄熸渤", "鎴垮彿")

    assert "星河苑" in text
    assert "小区" in text
    assert "房号" in text
    assert not any(token in text for token in mojibake_tokens)


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
    assert status["actual_case_count"] == 100
    assert status["expected_case_count"] == 100


def test_real_dialogue_fixture_loader_accepts_variable_window_counts(tmp_path: Path) -> None:
    fixture = tmp_path / "real_server_dialogues_sanitized.json"
    fixture.write_text(
        json.dumps(
            {
                "schema": "real_server_dialogues_sanitized.v1",
                "windows": [
                    {"id": "real_001", "source": "server_log_sanitized", "turns": ["你好", "房源表发我"]},
                    {"turns": ["这套视频发我"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    windows = run_rag_10windows_10turns_utf8.load_fixture_windows(fixture)
    integrity = run_rag_10windows_10turns_utf8.chinese_integrity_report(
        windows,
        required_tokens=(),
        expected_window_count=None,
        min_turn_count=1,
    )

    assert [window["id"] for window in windows] == ["real_001", "fixture_window_002"]
    assert integrity["passed"], integrity


def test_real_dialogue_release_integrity_requires_minimum_scope(tmp_path: Path) -> None:
    fixture = tmp_path / "real_server_dialogues_sanitized.json"
    fixture.write_text(
        json.dumps(
            {
                "schema": "real_server_dialogues_sanitized.v1",
                "windows": [
                    {"id": "real_001", "turns": ["你好", "房源表发我"]},
                    {"id": "real_002", "turns": ["这套视频发我"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    windows = run_rag_10windows_10turns_utf8.load_fixture_windows(fixture)

    integrity = run_rag_10windows_10turns_utf8.chinese_integrity_report(
        windows,
        required_tokens=(),
        expected_window_count=None,
        min_window_count=10,
        min_turn_count=100,
    )

    assert integrity["passed"] is False
    assert integrity["window_count"] == 2
    assert integrity["turn_count"] == 3
    assert integrity["min_window_count"] == 10
    assert integrity["min_turn_count"] == 100


def test_historical_failure_fixture_is_synthetic_and_sanitized() -> None:
    fixture_path = Path("tests/fixtures/qa/historical_failures_synthetic_sanitized.json")
    text = fixture_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    joined_turns = "\n".join(turn for window in payload["windows"] for turn in window["turns"])

    assert payload["schema"] == "historical_failures_synthetic_sanitized.v1"
    assert "real server" not in payload["description"].lower()
    assert "synthetic_sanitized_fixture" in {window["source"] for window in payload["windows"]}
    assert len(payload["windows"]) >= 3
    assert sum(len(window["turns"]) for window in payload["windows"]) >= 12
    assert not any(token in text for token in BAD_MOJIBAKE_PHRASES)
    assert "房源表" in joined_turns
    assert "视频" in joined_turns
    assert "图片" in joined_turns
    assert "密码" not in text
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)
    assert not re.search(r"\b[a-fA-F0-9]{32,}\b", text)
    assert not re.search(r"(?i)\b(sk-(proj-)?|gh[pousr]_|AKIA)[A-Za-z0-9_-]{12,}\b", text)


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


def test_qa_artifact_filename_is_unique_for_parallel_gate_runs() -> None:
    first = run_rag_10windows_10turns_utf8.artifact_path_for("rag_historical_failure_replay_sanitized")
    second = run_rag_10windows_10turns_utf8.artifact_path_for("rag_historical_failure_replay_sanitized")

    assert first != second
    assert first.name.startswith("rag_historical_failure_replay_sanitized_")
    assert first.suffix == ".json"
    assert all(ord(char) < 128 for char in first.name)
    assert re.search(r"_p\d+_\d+_\d{4}\.json$", first.name)


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
    assert failure["summary"]["artifact_role"] == "failure_log"
    assert failure["summary"]["contains_pass_transcript"] is False


def test_qa_artifact_machine_summary_separates_pass_transcript_from_failure_log() -> None:
    passed = run_rag_10windows_10turns_utf8.build_machine_summary(
        {
            "actual_case_count": 100,
            "expected_case_count": 100,
            "actual_window_count": 10,
            "selected_window_count": 10,
            "full_suite_completed": True,
            "quality_status": {"passed": True, "exit_code": 0},
        }
    )
    failed = run_rag_10windows_10turns_utf8.build_machine_summary(
        {
            "actual_case_count": 37,
            "expected_case_count": 100,
            "actual_window_count": 4,
            "selected_window_count": 10,
            "full_suite_completed": False,
            "quality_status": {"passed": False, "exit_code": 3, "business_failure": True},
        }
    )
    partial = run_rag_10windows_10turns_utf8.build_machine_summary(
        {
            "actual_case_count": 10,
            "expected_case_count": 10,
            "actual_window_count": 1,
            "selected_window_count": 1,
            "full_suite_completed": False,
            "quality_status": {"passed": True, "exit_code": 0},
        }
    )

    assert passed["artifact_role"] == "pass_transcript"
    assert passed["contains_pass_transcript"] is True
    assert passed["contains_failure_log"] is False
    assert passed["usable_for_release"] is True
    assert passed["actual_case_count"] == 100
    assert failed["artifact_role"] == "failure_log"
    assert failed["contains_pass_transcript"] is False
    assert failed["contains_failure_log"] is True
    assert failed["usable_for_release"] is False
    assert failed["actual_case_count"] == 37
    assert partial["artifact_role"] == "pass_transcript"
    assert partial["passed"] is True
    assert partial["usable_for_release"] is False
    assert partial["full_suite_completed"] is False


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


def test_qa_problem_allows_safe_missing_detail_clarification_without_tool_claims() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "想找附近的房子。",
            "bot": {"texts": ["你想看哪个小区或者大概预算多少？发一下我再按最新房源表帮你筛。"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你想看哪个小区或者大概预算多少？发一下我再按最新房源表帮你筛。",
                "constraint_proof": {},
            },
            "tool": {"target_rows": [], "inventory_rows": []},
        }
    )

    assert problem["severity"] == "info"


def test_qa_problem_keeps_sensitive_missing_detail_clarification_as_medium() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "门锁密码发我。",
            "bot": {"texts": ["你要哪套房子的密码？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你要哪套房子的密码？",
                "constraint_proof": {"wants_access": True},
            },
            "tool": {"target_rows": [], "inventory_rows": []},
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_keeps_price_flag_clarification_as_medium() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套多少钱？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {"wants_price": True},
            },
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_keeps_user_viewing_text_clarification_as_medium() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "今天能自己看吗？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {},
            },
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_keeps_contract_contact_key_clarification_as_medium() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "签合同找谁？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {"wants_contract_contact": True},
            },
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_reuses_price_and_booking_signals_as_medium() -> None:
    for user_text in (
        "月租多少？",
        "多少一月？",
        "一个月多少？",
        "怎么定房？",
        "定房找谁？",
        "订金多少？",
        "交订金怎么交？",
    ):
        problem = run_rag_10windows_10turns_utf8._turn_problem(
            {
                "user": user_text,
                "bot": {"texts": ["你说的是哪套房？"]},
                "chain_judgment": {
                    "status": "clarification",
                    "likely_link": "问题重写/意图分析",
                    "reason": "意图层生成追问。",
                },
                "rewrite": {
                    "clarification_text": "你说的是哪套房？",
                    "constraint_proof": {},
                },
            }
        )

        assert problem["severity"] == "medium", user_text


def test_qa_problem_keeps_inventory_sheet_requests_as_medium() -> None:
    for user_text in ("房源表发我", "空房表发一下", "表发我"):
        problem = run_rag_10windows_10turns_utf8._turn_problem(
            {
                "user": user_text,
                "bot": {"texts": ["你想看哪个小区？"]},
                "chain_judgment": {
                    "status": "clarification",
                    "likely_link": "问题重写/意图分析",
                    "reason": "意图层生成追问。",
                },
                "rewrite": {
                    "clarification_text": "你想看哪个小区？",
                    "constraint_proof": {},
                },
            }
        )

        assert problem["severity"] == "medium", user_text


def test_qa_problem_keeps_inventory_sheet_structural_signals_as_medium() -> None:
    cases = (
        {"intent": "inventory_sheet", "constraint_proof": {}, "query_state": {}},
        {"intent": "general", "constraint_proof": {"wants_inventory_sheet": True}, "query_state": {}},
        {"intent": "general", "constraint_proof": {}, "query_state": {"wants_inventory_sheet": True}},
    )
    for rewrite in cases:
        problem = run_rag_10windows_10turns_utf8._turn_problem(
            {
                "user": "最新表格给我",
                "bot": {"texts": ["你想看哪个小区？"]},
                "chain_judgment": {
                    "status": "clarification",
                    "likely_link": "问题重写/意图分析",
                    "reason": "意图层生成追问。",
                },
                "rewrite": {
                    "clarification_text": "你想看哪个小区？",
                    **rewrite,
                },
            }
        )

        assert problem["severity"] == "medium", rewrite


def test_qa_problem_keeps_query_state_and_tool_requirement_risks_as_medium() -> None:
    price_problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "哪套合适？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "query_state": {"price_range": [3000, 4000]},
                "constraint_proof": {},
            },
        }
    )
    viewing_problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "哪套合适？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
        }
    )
    price_contact_problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "哪套合适？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {},
                "tool_requirements": {"needs_price_contact": True},
            },
        }
    )
    deposit_policy_problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "哪套合适？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {},
                "tool_requirements": {"needs_deposit_policy": True},
            },
        }
    )

    assert price_problem["severity"] == "medium"
    assert viewing_problem["severity"] == "medium"
    assert price_contact_problem["severity"] == "medium"
    assert deposit_policy_problem["severity"] == "medium"


def test_qa_problem_keeps_availability_user_text_clarifications_as_medium() -> None:
    for user_text in (
        "这套还在？",
        "这套还有吗？",
        "现在空不空？",
        "什么时候可以入住？",
        "有房吗？",
        "这套在不在？",
        "这套什么时候空出？",
        "空出来了吗？",
        "什么时候能入住？",
        "现在能入住吗？",
        "这套还空着吗？",
        "空了吗？",
        "空了没？",
        "什么时候空？",
        "哪天空？",
        "现在能住吗？",
        "什么时候能搬？",
        "什么时候能搬进去？",
        "现在能搬进去吗？",
        "现房吗？",
        "已经空置了吗？",
        "能租吗？",
        "还 能租吗？",
        "可租吗？",
        "能不能租？",
        "可以租吗？",
        "什么时候退租？",
        "退租了吗？",
        "这套租掉了吗？",
        "这套租出去了吗？",
        "还没租掉吧？",
        "被租了吗？",
        "出租了吗？",
        "已经租了没？",
        "这套定掉了吗？",
        "已经定了吗？",
        "定出去了吗？",
        "预定了吗？",
        "现在可以住吗？",
        "什么时候可以住？",
        "几号可以住进去？",
        "还有空的吗？",
        "什么时候退房？",
    ):
        problem = run_rag_10windows_10turns_utf8._turn_problem(
            {
                "user": user_text,
                "bot": {"texts": ["你说的是哪套房？"]},
                "chain_judgment": {
                    "status": "clarification",
                    "likely_link": "问题重写/意图分析",
                    "reason": "意图层生成追问。",
                },
                "rewrite": {
                    "clarification_text": "你说的是哪套房？",
                    "constraint_proof": {},
                },
            }
        )

        assert problem["severity"] == "medium", user_text


def test_qa_problem_keeps_viewing_and_access_user_text_as_medium() -> None:
    for user_text in (
        "可以看吗？",
        "可以去看吗？",
        "能约看吗？",
        "约个时间看下可以吗？",
        "明天方便看吗？",
        "这套有钥匙吗？",
        "门禁码发一下？",
        "怎么开门？",
        "门禁怎么进？",
        "能带看吗？",
        "可以现场看一下吗？",
        "可以上门看吗？",
        "能带我看吗？",
        "能进去吗？",
        "进得去吗？",
        "可以自助吗？",
        "可以自助看吗？",
        "门怎么开？",
        "现在能过去吗？",
        "我等下过去可以吗？",
        "能去现场吗？",
        "现场可以吗？",
        "有人带吗？",
        "要提前联系吗？",
    ):
        problem = run_rag_10windows_10turns_utf8._turn_problem(
            {
                "user": user_text,
                "bot": {"texts": ["你说的是哪套房？"]},
                "chain_judgment": {
                    "status": "clarification",
                    "likely_link": "问题重写/意图分析",
                    "reason": "意图层生成追问。",
                },
                "rewrite": {
                    "clarification_text": "你说的是哪套房？",
                    "constraint_proof": {},
                },
            }
        )

        assert problem["severity"] == "medium", user_text


def test_qa_problem_uses_real_rewrite_tool_requirements_for_risk() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "哪套合适？",
            "bot": {"texts": ["你说的是哪套房？"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是哪套房？",
                "constraint_proof": {},
                "tool_requirements": {"needs_viewing_policy": True},
            },
        }
    )

    assert problem["severity"] == "medium"


def test_qa_problem_allows_media_missing_target_clarification_without_send_actions() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "视频发我。",
            "bot": {"texts": ["你要哪套房间的视频？发一下小区和房号我再找。"], "video_count": 0},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你要哪套房间的视频？发一下小区和房号我再找。",
                "constraint_proof": {"wants_video": True},
            },
            "tool": {"target_rows": [], "inventory_rows": [], "video_count": 0},
            "send": {"actions": []},
        }
    )

    assert problem["severity"] == "info"


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


def test_qa_problem_detects_negated_community_bound_as_positive_constraint() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "石桥区域就行，不是只问石桥铭苑。",
            "bot": {"texts": ["暂时没查到石桥铭苑符合条件的房源。"]},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {
                "constraint_proof": {
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "communities": ["石桥铭苑"],
                }
            },
            "tool": {"target_rows": []},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "问题重写/实体归一"
    assert "否定" in problem["reason"]


def test_qa_problem_detects_confusable_community_target_pollution_without_rewrite_proof() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "杨家新雅苑那套也发视频，最好清楚一点。",
            "bot": {"texts": ["本地暂时没找到视频：杨乐府9-604B。"], "video_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_video": True}},
            "tool": {"target_rows": ["杨乐府9-604B"], "video_count": 0},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "问题重写/实体归一"
    assert "相似小区" in problem["reason"]


def test_qa_problem_detects_selected_indices_with_same_turn_scope_but_no_prior_candidates() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "新天地前两套视频发我。",
            "bot": {"texts": ["先发这两套。"], "video_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {
                "constraint_proof": {
                    "area": "东新园 杭氧 新天地",
                    "budget_range": [4000, 5000],
                    "selected_indices": [1, 2],
                    "wants_video": True,
                }
            },
            "tool": {"target_rows": ["白田畈龙吟府4-902B", "杨乐府9-604B"], "video_count": 0},
            "blackbox": {"last_candidate_count_before_turn": 0, "last_candidate_count_after_turn": 2},
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


def test_qa_problem_detects_media_send_without_stable_listing_id() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套视频发我。",
            "bot": {"texts": ["找到了，这套视频先发你。"], "video_count": 1},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_video": True}},
            "tool": {
                "target_rows": ["棠润府15-2-801B"],
                "video_rows": ["棠润府15-2-801B"],
                "video_count": 1,
                "video_listing_ids": [],
            },
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "素材目标绑定"
    assert "listing_id" in problem["reason"]


def test_qa_problem_allows_media_send_with_stable_listing_id() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套视频发我。",
            "bot": {"texts": ["找到了，这套视频先发你。"], "video_count": 1},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_video": True}},
            "tool": {
                "target_rows": ["棠润府15-2-801B"],
                "video_rows": ["棠润府15-2-801B"],
                "video_count": 1,
                "video_listing_ids": ["lst_fixture_tangrun"],
            },
            "send": {"sent_actions": [{"type": "video", "count": 1}]},
        }
    )

    assert problem["severity"] == "info"


def test_qa_problem_detects_unsolicited_password_text() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套水电怎么收？",
            "bot": {"texts": ["这套水电是民用，另外看房密码是123456#。"]},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_water_electric": True}},
            "tool": {"target_rows": ["棠润府15-2-801B"]},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "看房密码边界"


def test_qa_problem_detects_media_action_tense_mismatch() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套视频发我。",
            "bot": {"texts": ["视频已准备好。"], "video_count": 1},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_video": True}},
            "tool": {"video_count": 1, "video_listing_ids": ["lst_fixture_tangrun"]},
            "send": {"sent_actions": [{"type": "video", "count": 1}]},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "发送阶段"


def test_qa_problem_detects_sent_claim_without_send_action() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套视频发我。",
            "bot": {"texts": ["视频已经发送给你了。"], "video_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"wants_video": True}},
            "tool": {"video_count": 0, "video_listing_ids": []},
            "send": {"sent_actions": []},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "发送阶段"


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


def test_qa_problem_allows_scalar_selected_index_with_prior_candidates() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "第二套也发图片，不要串到别的小区。",
            "bot": {"texts": ["棠润府10-1004C这套本地暂时没找到图片。"], "image_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"selected_indices": 2, "wants_image": True}},
            "tool": {"target_rows": ["棠润府10-1004C"], "image_count": 0},
            "blackbox": {"last_candidate_count_before_turn": 3},
        }
    )

    assert problem["severity"] == "info"


def test_qa_problem_blocks_scalar_selected_index_without_prior_candidates() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "第二套也发图片，不要串到别的小区。",
            "bot": {"texts": ["棠润府10-1004C这套本地暂时没找到图片。"], "image_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"selected_indices": 2, "wants_image": True}},
            "tool": {"target_rows": ["棠润府10-1004C"], "image_count": 0},
            "blackbox": {"last_candidate_count_before_turn": 0},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "Planner/工具目标绑定"


def test_qa_problem_blocks_multi_selected_indices_when_targets_are_partial() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "第一套和第三套都发图片，不要漏。",
            "bot": {"texts": ["棠润府10-1004C这套本地暂时没找到图片。"], "image_count": 0},
            "chain_judgment": {"status": "pass", "likely_link": "人工复核", "reason": ""},
            "rewrite": {"constraint_proof": {"selected_indices": [1, 3], "wants_image": True}},
            "tool": {"target_rows": ["棠润府10-1004C"], "image_count": 0},
            "blackbox": {"last_candidate_count_before_turn": 3},
        }
    )

    assert problem["severity"] == "high"
    assert problem["likely_link"] == "Planner/工具目标绑定"


def test_qa_problem_allows_confirmation_question_without_stage_options() -> None:
    problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "荣润府有吗？客户可能写错了。",
            "bot": {"texts": ["你说的是棠润府吗？确认一下，我再按最新房源表查。"]},
            "chain_judgment": {
                "status": "clarification",
                "likely_link": "问题重写/意图分析",
                "reason": "意图层生成追问。",
            },
            "rewrite": {
                "clarification_text": "你说的是棠润府吗？确认一下，我再按最新房源表查。",
                "constraint_proof": {},
            },
            "stage_timings": [{"stage": "rewrite_intent", "summary": {"entity_resolution": {}}}],
        }
    )

    assert problem["severity"] == "info"


def test_qa_tool_summary_keeps_listing_ids_for_media_binding_audit() -> None:
    summary = run_rag_test_text_window_utf8._summarize_stage_result(
        "tools",
        {
            "target_rows": [
                {"小区": "棠润府", "房号": "15-2-801B", "listing_id": "lst_fixture_tangrun"},
            ],
            "video_rows": [
                {"小区": "棠润府", "房号": "15-2-801B", "listing_id": "lst_fixture_tangrun"},
            ],
            "image_rows": [
                {"小区": "棠润府", "房号": "15-2-801B", "listing_id": "lst_fixture_tangrun"},
            ],
            "video_paths": ["video.mp4"],
            "image_paths": ["image.png"],
        },
    )

    assert summary["target_listing_ids"] == ["lst_fixture_tangrun"]
    assert summary["video_listing_ids"] == ["lst_fixture_tangrun"]
    assert summary["image_listing_ids"] == ["lst_fixture_tangrun"]


def test_qa_runner_installs_and_restores_offline_feishu_stub() -> None:
    import app.main as app_main

    original = app_main.FeishuClient
    originals = run_rag_test_text_window_utf8.install_offline_service_stubs()
    try:
        assert app_main.FeishuClient is run_rag_test_text_window_utf8.OfflineFeishuClient
        result = asyncio.run(app_main.FeishuClient().sync_media_for_rooms([]))
        assert result["skipped"][0]["source"] == "offline_qa_replay"
    finally:
        run_rag_test_text_window_utf8.restore_offline_service_stubs(originals)

    assert app_main.FeishuClient is original


def test_qa_quality_status_marks_high_risk_as_business_failure() -> None:
    quality = run_rag_10windows_10turns_utf8.build_quality_status(
        [
            {
                "window_id": "sample",
                "turns": [
                    {
                        "turn": 1,
                        "user": "这套视频发我。",
                        "bot": {"texts": ["视频已经发送给你了。"]},
                        "problem": {"severity": "high", "likely_link": "发送阶段", "reason": "sent mismatch"},
                    }
                ],
            }
        ],
        completed=True,
    )

    assert quality["passed"] is False
    assert quality["business_failure"] is True
    assert quality["exit_code"] == 3
    assert quality["business_failures"][0]["severity"] == "high"


def test_qa_quality_status_marks_any_medium_as_release_blocker() -> None:
    quality = run_rag_10windows_10turns_utf8.build_quality_status(
        [
            {
                "window_id": "sample",
                "turns": [
                    {
                        "turn": 1,
                        "user": "杨家府还有房子吗？客户说名字可能没记准。",
                        "bot": {"texts": ["最新房源表里暂时没查到杨家府这个小区。"]},
                        "problem": {
                            "severity": "medium",
                            "likely_link": "问题重写/意图分析",
                            "reason": "需要人工复核的小区确认。",
                        },
                    }
                ],
            }
        ],
        completed=True,
    )

    assert quality["passed"] is False
    assert quality["business_failure"] is True
    assert quality["high_count"] == 0
    assert quality["medium_count"] == 1
    assert quality["medium_threshold"] == 0
    assert quality["exit_code"] == 4


def test_qa_problem_marks_llm_timeout_and_bad_packages_as_high() -> None:
    timeout_problem = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "这套视频发我。",
            "bot": {"texts": []},
            "error": "TimeoutError: LLM planner timed out",
        }
    )
    llm1_bad_packet = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "第1套视频发我。",
            "bot": {"texts": ["我再确认一下。"]},
            "chain_judgment": {
                "status": "retry_needed",
                "likely_link": "LLM1结构化任务包",
                "reason": "LLM1 production output missing or invalid task_atoms",
            },
        }
    )
    llm2_bad_packet = run_rag_10windows_10turns_utf8._turn_problem(
        {
            "user": "房源表发我。",
            "bot": {"texts": []},
            "chain_judgment": {
                "status": "no_output",
                "likely_link": "LLM2出站包",
                "reason": "LLM2 returned malformed outbound package",
            },
        }
    )

    assert timeout_problem["severity"] == "high"
    assert llm1_bad_packet["severity"] == "high"
    assert llm1_bad_packet["likely_link"] == "LLM1结构化任务包"
    assert llm2_bad_packet["severity"] == "high"
    assert llm2_bad_packet["likely_link"] == "LLM2出站包"


def test_qa_quality_status_records_feishu_sync_failure_as_infrastructure_error() -> None:
    quality = run_rag_10windows_10turns_utf8.build_quality_status(
        [
            {
                "window_id": "feishu_fault",
                "turns": [
                    {
                        "turn": 1,
                        "user": "这套视频发我。",
                        "error": "Feishu sync failed in offline replay stub",
                        "problem": {"severity": "high", "likely_link": "房源/素材同步"},
                    }
                ],
            }
        ],
        completed=False,
    )

    assert quality["passed"] is False
    assert quality["infrastructure_error"] is True
    assert quality["exit_code"] == 2
    assert quality["infrastructure_errors"][0]["reason"] == "Feishu sync failed in offline replay stub"


def test_fast_gate_secret_scan_allows_only_obvious_fixture_placeholders() -> None:
    script = Path("scripts/rag-v2-test-gates.ps1").read_text(encoding="utf-8")

    assert "dummy|fake|test|example|placeholder" in script
    assert "OpenAI-style key" in script
    assert "assigned runtime secret" in script
    assert "sk-(proj-)?" in script


def test_fast_gate_includes_send_receipt_and_fault_replay_tests() -> None:
    script = Path("scripts/rag-v2-test-gates.ps1").read_text(encoding="utf-8")
    production_smoke_body = script.split("function Invoke-ProductionSmoke", 1)[1].split(
        "function Invoke-RealDialogueReplay",
        1,
    )[0]

    assert '"tests/test_kf_send_receipts.py"' in script
    assert '"tests/test_kf_send_receipt_faults.py"' in script
    assert '"tests/test_qa_utf8_inputs.py"' in script
    assert "smoke_dual_llm_production.py" in script
    assert "real_server_dialogues_sanitized.json" in script
    assert "AllowMissingRealDialogues" in script
    assert "historical_failures_synthetic_sanitized.json" in script
    assert "AllowMissingHistoricalFailures" in script
    assert "Invoke-HistoricalFailureReplay" in script
    assert "historical failure replay QA" in script
    assert "Save-SanitizedQaArtifact" in script
    assert "--min-window-count" in script
    assert "--min-turn-count" in script
    assert "run_rag_random_guard_utf8.py" in script
    assert "video upload transcode retry gate" in script
    assert "Assert-QaArtifactReleaseGate" in script
    assert "high=0 medium=0" in script
    assert "ReleaseBlockers" in script
    assert "production cutover is blocked" in script
    assert "release/current rehearsal local artifact" in script
    assert "Invoke-ReleaseRehearsal" in script
    assert "release blocker audit" in script
    assert '$env:APP_ENV = "test"' in production_smoke_body
    assert '"scripts/smoke_dual_llm_production.py"' in production_smoke_body
    assert "--allow-live-llm" not in production_smoke_body


def test_production_smoke_is_contract_only_and_send_guarded() -> None:
    script = Path("scripts/smoke_dual_llm_production.py").read_text(encoding="utf-8")
    offline_source = script.split("async def _run_live_smoke", 1)[0]

    assert '"contract_only": True' in script
    assert '"send_transport_invoked": False' in script
    assert '"llm_transport_invoked": False' in script
    assert '"env_file_read": False' in script
    assert "FakeReplyGenerator" in script
    assert "from app.config import settings" not in offline_source
    assert "from app.services.llm import ReplyGenerator" not in offline_source
    assert "send_action_count == 0" in script
    assert "send_text(" not in script
    assert "send_image(" not in script
    assert "send_video(" not in script
