from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from qa_artifacts import run_rag_random_guard_utf8 as random_guard
from qa_artifacts import run_rag_10windows_10turns_utf8 as runner
from qa_artifacts import run_rag_test_text_window_utf8 as qa_base


def run(coro):
    return asyncio.run(coro)


class _FakeWeCom:
    events: list[Any] = []


class _FakeStore:
    pass


def test_random_guard_runner_stops_after_first_blocking_problem(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def run_case() -> None:
        artifact = tmp_path / "random_guard.json"
        send_calls: list[tuple[str, int]] = []

        async def fake_send_turn(
            fake: Any,
            *,
            conversation_id: str,
            turn_index: int,
            user_text: str,
            turn_timeout: float,
        ) -> dict[str, Any]:
            send_calls.append((conversation_id, turn_index))
            return {
                "turn": turn_index,
                "user": user_text,
                "error": "",
                "bot": {"texts": ["bad reply"]},
                "stage_timings": [],
            }

        monkeypatch.setattr(runner, "artifact_path_for", lambda prefix: artifact)
        monkeypatch.setattr(runner, "chinese_integrity_report", lambda *args, **kwargs: {"passed": True})
        monkeypatch.setattr(runner.base, "install_offline_service_stubs", lambda: {})
        monkeypatch.setattr(runner.base, "restore_offline_service_stubs", lambda originals: None)
        monkeypatch.setattr(runner.base, "CaptureWeComKf", _FakeWeCom)
        monkeypatch.setattr(runner.base, "MemoryContextStore", _FakeStore)
        monkeypatch.setattr(runner.base, "send_turn", fake_send_turn)
        monkeypatch.setattr(runner, "_first_context_summary", lambda store: {})
        monkeypatch.setattr(runner, "_enrich_turn_report", lambda *args, **kwargs: None)
        monkeypatch.setattr(runner, "_serialize_context_store", lambda store: {})
        monkeypatch.setattr(
            runner,
            "_turn_problem",
            lambda turn: {
                "severity": "high",
                "likely_link": "unit",
                "reason": "unit blocking problem",
            },
        )

        result = await runner.run_all(
            windows=[
                {"id": "random_1", "turns": ["q1", "q2"]},
                {"id": "random_2", "turns": ["q3"]},
            ],
            artifact_prefix="random_guard",
            conversation_prefix="conv_random_guard",
            expected_window_count=2,
            min_turn_count=1,
            fail_fast_on_problem=True,
        )

        data = json.loads(result.read_text(encoding="utf-8"))

        assert result == artifact
        assert send_calls == [("conv_random_guard_1_random_1", 1)]
        assert data["fail_fast_on_problem"] is True
        assert data["quality_status"]["high_count"] == 1
        assert data["quality_status"]["exit_code"] == 3
        assert len(data["windows"]) == 1
        assert len(data["windows"][0]["turns"]) == 1

    run(run_case())


def test_random_guard_runner_can_collect_all_problems_when_fail_fast_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def run_case() -> None:
        artifact = tmp_path / "random_guard.json"
        captured: dict[str, Any] = {}

        async def fake_run_all(**kwargs: Any) -> Path:
            captured.update(kwargs)
            artifact.write_text(
                json.dumps(
                    {
                        "quality_status": {"passed": True, "business_failures": []},
                        "summary": {},
                        "windows": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return artifact

        monkeypatch.setattr(random_guard, "coverage_report", lambda *args, **kwargs: {"passed": True})
        monkeypatch.setattr(random_guard, "chinese_integrity_report", lambda *args, **kwargs: {"passed": True})
        monkeypatch.setattr(random_guard, "generate_random_guard_windows", lambda seed=None: [])
        monkeypatch.setattr(random_guard, "run_all", fake_run_all)
        monkeypatch.setattr(random_guard, "_apply_tool_coverage_gate", lambda path: path)

        result = await random_guard.run_random_guard(
            seed=9,
            turn_timeout=12,
            fail_fast_on_problem=False,
        )

        assert result == artifact
        assert captured["fail_fast_on_problem"] is False
        assert captured["turn_timeout"] == 12

    run(run_case())


def test_random_guard_tool_invocation_coverage_requires_core_broker_tools() -> None:
    windows = [
        {
            "window_id": "window_ok",
            "turns": [
                {
                    "turn": 1,
                    "user": "万达附近2000以内有哪些？",
                    "planner": {"actions": [{"type": "search_inventory"}]},
                    "tool": {"inventory_row_count": 3, "target_row_count": 2},
                },
                {
                    "turn": 2,
                    "user": "房源表发我",
                    "planner": {"actions": [{"type": "send_inventory_sheet"}]},
                    "tool": {"inventory_image_count": 1, "inventory_sheet_artifact_evidence_count": 1},
                },
                {
                    "turn": 3,
                    "user": "前两套视频发我",
                    "planner": {"actions": [{"type": "send_video"}]},
                    "tool": {"video_count": 2, "video_media_manifest_evidence_count": 2},
                },
                {
                    "turn": 4,
                    "user": "第一套图片也发一下",
                    "planner": {"actions": [{"type": "send_image"}]},
                    "tool": {"image_count": 1, "image_media_manifest_evidence_count": 1},
                },
                {
                    "turn": 5,
                    "user": "有原视频或者高清链接吗？",
                    "planner": {"actions": [{"type": "send_original_video_link"}]},
                    "tool": {"original_video_request": {"requested": True}, "material_page_url_count": 1},
                },
                {
                    "turn": 6,
                    "user": "这套价格和水电怎么收？",
                    "planner": {"actions": [{"type": "search_inventory"}]},
                    "tool": {"target_row_count": 1},
                },
                {
                    "turn": 7,
                    "user": "这套今天能看吗，密码多少？",
                    "planner": {"actions": [{"type": "resolve_viewing_password"}]},
                    "tool": {"target_row_count": 1, "viewing_instruction_evidence_count": 1},
                },
                {
                    "turn": 8,
                    "user": "如果还没空出来能约看吗？",
                    "planner": {"actions": [{"type": "explain_unavailable_viewing"}]},
                    "tool": {"target_row_count": 1, "viewing_instruction_evidence_count": 1},
                },
                {
                    "turn": 9,
                    "user": "客户看中了怎么定房，合同联系谁？",
                    "planner": {"actions": [{"type": "send_contract_contact"}]},
                    "tool": {"rule_evidence": {"contract_contact": True}},
                },
                {
                    "turn": 10,
                    "user": "这套能不能免押，服务费怎么算？",
                    "planner": {"actions": [{"type": "send_deposit_policy"}]},
                    "tool": {"rule_evidence": {"deposit_policy": True}},
                },
            ],
        }
    ]

    report = random_guard.tool_invocation_coverage_report(windows)

    assert report["passed"], report
    assert set(report["covered_categories"]) == set(random_guard.TOOL_INVOCATION_CATEGORIES)


def test_random_guard_tool_coverage_does_not_accept_planner_only_actions() -> None:
    windows = [
        {
            "window_id": "planner_only",
            "turns": [
                {
                    "turn": 1,
                    "user": "房源表、视频、图片、合同、免押都查一下",
                    "planner": {
                        "actions": [
                            {"type": "search_inventory"},
                            {"type": "send_inventory_sheet"},
                            {"type": "send_video"},
                            {"type": "send_image"},
                            {"type": "send_contract_contact"},
                            {"type": "send_deposit_policy"},
                        ]
                    },
                    "tool": {},
                }
            ],
        }
    ]

    report = random_guard.tool_invocation_coverage_report(windows)

    assert report["passed"] is False
    assert "房源查询" in report["missing_categories"]
    assert "房间视频" in report["missing_categories"]
    assert "房间图片" in report["missing_categories"]
    assert "房源表图片" in report["missing_categories"]


def test_random_guard_tool_coverage_does_not_accept_send_only_video() -> None:
    turn = {
        "turn": 1,
        "user": "前两套视频发我",
        "planner": {"actions": [{"type": "send_video"}]},
        "tool": {"video_count": 0, "video_rows": [], "video_paths": []},
        "send": {"sent_actions": [{"type": "video", "count": 1}]},
    }

    hits = random_guard._tool_category_hits(turn)

    assert "房间视频" not in hits


def test_random_guard_tool_coverage_ignores_generic_count_without_tool_evidence() -> None:
    turn = {
        "turn": 1,
        "user": "万达附近2000以内有哪些？房源表也发一下",
        "planner": {
            "actions": [
                {"type": "search_inventory"},
                {"type": "send_inventory_sheet"},
            ]
        },
        "tool": {"count": 99},
        "send": {"sent_actions": [{"type": "inventory_sheet", "count": 1}]},
    }

    hits = random_guard._tool_category_hits(turn)

    assert "房源查询" not in hits
    assert "房源表图片" not in hits


def test_random_guard_tool_coverage_rejects_inventory_sheet_error_without_artifact() -> None:
    turn = {
        "turn": 1,
        "user": "房源表发我",
        "tool": {
            "actions": ["send_inventory_sheet", "generate_reply"],
            "inventory_image_count": 0,
            "inventory_sheet_artifact_evidence_count": 0,
            "inventory_sheet_artifact_error": {"code": "sheet_artifact_missing"},
        },
        "send": {"sent_actions": [{"type": "text", "count": 1}]},
    }

    hits = random_guard._tool_category_hits(turn)

    assert "房源表图片" not in hits


def test_random_guard_tool_coverage_counts_missing_image_evidence() -> None:
    turn = {
        "turn": 1,
        "user": "第一套图片也发一下",
        "tool": {
            "target_rows": [{"label": "华丰欣苑14-2-901"}],
            "missing_media": ["华丰欣苑14-2-901:图片"],
            "media_status": {
                "image": {
                    "requested_count": 1,
                    "sent_count": 0,
                    "missing_rooms": ["华丰欣苑14-2-901"],
                }
            },
        },
    }

    hits = random_guard._tool_category_hits(turn)

    assert "房间图片" in hits


def test_random_guard_tool_coverage_gate_marks_incomplete_pass_artifact_failed(tmp_path: Path) -> None:
    artifact = tmp_path / "random_guard_pass_without_tools.json"
    payload = {
        "completed": True,
        "full_suite_completed": True,
        "actual_case_count": 200,
        "expected_case_count": 200,
        "actual_window_count": 20,
        "selected_window_count": 20,
        "quality_status": {"passed": True, "exit_code": 0, "business_failures": []},
        "windows": [{"window_id": "empty", "turns": []}],
    }
    artifact.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    random_guard._apply_tool_coverage_gate(artifact)
    data = json.loads(artifact.read_text(encoding="utf-8"))

    assert data["quality_status"]["passed"] is False
    assert data["quality_status"]["business_failure"] is True
    assert data["quality_status"]["exit_code"] == 4
    assert data["summary"]["contains_failure_log"] is True
    assert data["random_guard_tool_coverage"]["missing_categories"]


def test_random_guard_tool_coverage_accepts_real_stage_summary_lists() -> None:
    turn = {
        "turn": 1,
        "user": "这套价格和水电怎么收？",
        "planner": {"actions": [{"type": "search_inventory"}]},
        "tool": {
            "inventory_rows": [{"label": "星河苑1-101"}],
            "target_rows": [{"label": "星河苑1-101"}],
        },
    }

    hits = random_guard._tool_category_hits(turn)

    assert "房源查询" in hits
    assert "价格水电" in hits


def test_random_guard_counts_langgraph_business_knowledge_contract_summary() -> None:
    summary = qa_base._summarize_stage_result(
        "tools",
        {
            "source": "langgraph_business_knowledge",
            "topics": ["contract_booking"],
            "cards": [{"id": "contract_booking"}],
            "knowledge_context": "合同、定金和订房需要联系受控号码。",
            "rule_evidence": {"contract_contact": ["18758141785"]},
        },
    )
    turn = {
        "turn": 1,
        "user": "客户看中了怎么定房，合同联系谁？",
        "tool": summary,
    }

    hits = random_guard._tool_category_hits(turn)

    assert "合同定房" in hits
    assert summary["actions"] == ["send_contract_contact", "generate_reply"]
