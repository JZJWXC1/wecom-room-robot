from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.services.kf_media_binding_graph import (
    KfMediaBindingGraphDeps,
    run_kf_media_binding_graph,
)
from app.services.kf_tool_resolver import ToolResolverResult


def run(coro):
    return asyncio.run(coro)


def _row_label(row: dict[str, Any]) -> str:
    return f"{row.get('小区', '')}{row.get('房号', '')}".strip() or "这套房源"


def _row_listing_id(row: dict[str, Any]) -> str:
    return str(row.get("listing_id") or "")


def _rows_with_listing_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _rows_with_candidate_numbers(rows: list[dict[str, Any]], candidate_numbers: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        enriched = dict(row)
        if index < len(candidate_numbers):
            enriched["candidate_number"] = candidate_numbers[index]
        result.append(enriched)
    return result


def _deps(
    *,
    resolver_result: ToolResolverResult,
    collect_error: bool = False,
) -> KfMediaBindingGraphDeps:
    async def collect_room_media(
        rows: list[dict[str, Any]],
        *,
        media_kind: str,
        limit: int,
    ) -> tuple[list[Path], list[dict[str, Any]], list[str], dict[str, Any]]:
        if collect_error:
            raise RuntimeError(f"{media_kind} unavailable")
        if media_kind == "image":
            return (
                [Path("C:/tmp/room-a.jpg")],
                [rows[0]],
                [_row_label(row) for row in rows[1:]],
                {
                    "_media_manifest_evidence": [
                        {
                            "listing_id": rows[0]["listing_id"],
                            "media_type": "image",
                            "media_id": "img-1",
                        }
                    ]
                },
            )
        return (
            [Path("C:/tmp/room-a.mp4")],
            [rows[0]],
            [_row_label(row) for row in rows[1:]],
            {
                "_media_manifest_evidence": [
                    {
                        "listing_id": rows[0]["listing_id"],
                        "media_type": "video",
                        "media_id": "vid-1",
                    }
                ]
            },
        )

    return KfMediaBindingGraphDeps(
        resolve_tool_targets=lambda **_kwargs: resolver_result,
        collect_room_media=collect_room_media,
        original_video_sources_for_listings=lambda listing_ids: {
            "original_video_paths": [],
            "original_video_urls": ["https://example.test/original.mp4"] if listing_ids else [],
            "material_page_urls": ["https://example.test/page"] if listing_ids else [],
            "source_records": [{"listing_id": listing_ids[0]}] if listing_ids else [],
            "media_manifest_evidence": [
                {
                    "listing_id": listing_ids[0],
                    "media_type": "original_video",
                    "media_id": "orig-1",
                }
            ]
            if listing_ids
            else [],
        },
        original_video_sources_for_paths=lambda _paths: {
            "original_video_paths": [],
            "original_video_urls": [],
            "material_page_urls": [],
            "source_records": [],
            "media_manifest_evidence": [],
        },
        row_labeler=_row_label,
        row_listing_id=_row_listing_id,
        rows_with_listing_ids=_rows_with_listing_ids,
        rows_with_candidate_numbers=_rows_with_candidate_numbers,
    )


def test_media_binding_graph_resolves_targets_and_collects_media_evidence() -> None:
    async def run_case() -> None:
        rows = [
            {"listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
            {"listing_id": "lst-b", "小区": "星桥锦绣嘉苑", "房号": "20-1606B"},
        ]
        result = await run_kf_media_binding_graph(
            _deps(
                resolver_result=ToolResolverResult(
                    target_rows=rows,
                    selection_error={},
                    field_target_error={},
                    missing_target_reason="",
                    candidate_binding={"status": "bound", "source": "unit"},
                    selected_indices=[1, 2],
                )
            ),
            actions=["send_image", "send_video", "generate_reply"],
            content="这两套图片和视频发我",
            understanding={"constraint_proof": {"wants_original_video": True}},
            inventory_rows=rows,
            media_request={"requested_count": 2},
            wants_original_video=True,
            dual_llm_production=True,
            target_limit=5,
            base_evidence={"missing_media": [], "media_manifest_evidence": []},
        )

        evidence = result["evidence"]
        assert result["trace"] == ["media_binding:resolve_targets", "media_binding:collect_media"]
        assert [row["candidate_number"] for row in evidence["target_rows"]] == [1, 2]
        assert evidence["image_paths"] == [str(Path("C:/tmp/room-a.jpg"))]
        assert evidence["video_paths"] == [str(Path("C:/tmp/room-a.mp4"))]
        assert evidence["missing_media"] == [
            "星桥锦绣嘉苑20-1606B:图片",
            "星桥锦绣嘉苑20-1606B:视频",
        ]
        assert evidence["media_status"]["video"]["requested_count"] == 2
        assert evidence["media_status"]["video"]["sent_count"] == 1
        assert evidence["memory_reducer"]["pending_video_sends"]["labels"] == ["星桥锦绣嘉苑20-1606B"]
        assert evidence["original_video_request"]["has_original_source"] is True
        assert evidence["original_video_media_manifest_evidence"][0]["media_id"] == "orig-1"

    run(run_case())


def test_media_binding_graph_selection_error_clears_media_outputs() -> None:
    async def run_case() -> None:
        result = await run_kf_media_binding_graph(
            _deps(
                resolver_result=ToolResolverResult(
                    target_rows=[],
                    selection_error={"reason": "missing_current_candidate_set"},
                    field_target_error={},
                    missing_target_reason="missing_current_candidate_set",
                    candidate_binding={"status": "error"},
                    inventory_rows_override=[],
                    clear_inventory_rows=True,
                    clear_media_outputs=True,
                    selected_indices=[1],
                )
            ),
            actions=["send_video", "generate_reply"],
            content="第一套视频发我",
            inventory_rows=[{"listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"}],
            base_evidence={"image_paths": ["old.jpg"], "video_paths": ["old.mp4"]},
        )

        evidence = result["evidence"]
        assert result["trace"] == ["media_binding:resolve_targets"]
        assert evidence["selection_error"]["reason"] == "missing_current_candidate_set"
        assert evidence["inventory_rows"] == []
        assert evidence["target_rows"] == []
        assert evidence["image_paths"] == []
        assert evidence["video_paths"] == []

    run(run_case())


def test_media_binding_graph_collect_failure_becomes_blocking_evidence() -> None:
    async def run_case() -> None:
        rows = [{"listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"}]
        result = await run_kf_media_binding_graph(
            _deps(
                resolver_result=ToolResolverResult(
                    target_rows=rows,
                    selection_error={},
                    field_target_error={},
                    missing_target_reason="",
                    candidate_binding={"status": "bound"},
                ),
                collect_error=True,
            ),
            actions=["send_video", "generate_reply"],
            content="视频发我",
            inventory_rows=rows,
            media_request={"requested_count": 1},
        )

        evidence = result["evidence"]
        assert result["status"] == "media_collect_failed"
        assert result["failures"][0]["stage"] == "collect_video"
        assert evidence["video_paths"] == []
        assert evidence["missing_media"] == ["星桥锦绣嘉苑20-1606A:视频"]
        assert evidence["media_status"]["video"]["sent_count"] == 0

    run(run_case())
