from __future__ import annotations

import asyncio
import inspect
import operator
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except Exception as exc:  # pragma: no cover - dependency guard
    StateGraph = None  # type: ignore[assignment]
    START = "__start__"  # type: ignore[assignment]
    END = "__end__"  # type: ignore[assignment]
    _LANGGRAPH_IMPORT_ERROR = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


MaybeAwaitable = Any | Awaitable[Any]
ResolveToolTargets = Callable[..., Any]
CollectRoomMedia = Callable[..., MaybeAwaitable]


class KfMediaBindingGraphState(TypedDict, total=False):
    actions: list[str]
    content: str
    context: dict[str, Any]
    understanding: dict[str, Any]
    inventory_rows: list[dict[str, Any]]
    pending_video: dict[str, Any]
    pending_video_rows: list[dict[str, Any]]
    pending_video_handled: bool
    media_request: dict[str, Any]
    original_video_request: dict[str, Any]
    wants_original_video: bool
    dual_llm_production: bool
    target_limit: int
    evidence: dict[str, Any]
    target_rows: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    selected_indices: list[int]
    status: str
    failures: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[str], operator.add]
    _deps: Any


@dataclass(frozen=True)
class KfMediaBindingGraphDeps:
    resolve_tool_targets: ResolveToolTargets
    collect_room_media: CollectRoomMedia
    original_video_sources_for_listings: Callable[[list[str]], dict[str, Any]]
    original_video_sources_for_paths: Callable[[list[Path]], dict[str, Any]]
    row_labeler: Callable[[dict[str, Any]], str]
    row_listing_id: Callable[[dict[str, Any]], str]
    rows_with_listing_ids: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    rows_with_candidate_numbers: Callable[[list[dict[str, Any]], list[int]], list[dict[str, Any]]]
    # 素材库无原视频链接证据时的兜底:对已绑定的本地视频文件生成签名直链
    # (None 或返回空列表即不兜底,保持原"无链接"语义)。
    signed_original_video_urls: Callable[[list[Path]], list[str]] | None = None


def build_kf_media_binding_graph_app(*, checkpointer: Any | None = None) -> Any:
    if StateGraph is None:
        raise RuntimeError("LangGraph is required for KF media binding graph.") from _LANGGRAPH_IMPORT_ERROR
    graph = StateGraph(KfMediaBindingGraphState)
    graph.add_node("resolve_targets", _resolve_targets_node)
    graph.add_node("collect_media", _collect_media_node)

    graph.add_edge(START, "resolve_targets")
    graph.add_conditional_edges(
        "resolve_targets",
        _route_after_resolve_targets,
        {"collect_media": "collect_media", "end": END},
    )
    graph.add_edge("collect_media", END)
    return graph.compile(checkpointer=checkpointer)


async def run_kf_media_binding_graph(
    deps: KfMediaBindingGraphDeps,
    *,
    actions: list[str],
    content: str,
    context: dict[str, Any] | None = None,
    understanding: dict[str, Any] | None = None,
    inventory_rows: list[dict[str, Any]] | None = None,
    pending_video: dict[str, Any] | None = None,
    pending_video_rows: list[dict[str, Any]] | None = None,
    pending_video_handled: bool = False,
    media_request: dict[str, Any] | None = None,
    original_video_request: dict[str, Any] | None = None,
    wants_original_video: bool = False,
    dual_llm_production: bool = False,
    target_limit: int = 5,
    base_evidence: dict[str, Any] | None = None,
    conversation_id: str = "kf-media-binding-graph",
    checkpointer: Any | None = None,
) -> KfMediaBindingGraphState:
    app = build_kf_media_binding_graph_app(checkpointer=checkpointer)
    rows = [row for row in inventory_rows or [] if isinstance(row, dict)]
    state: KfMediaBindingGraphState = {
        "actions": list(actions or []),
        "content": str(content or ""),
        "context": dict(context or {}),
        "understanding": dict(understanding or {}),
        "inventory_rows": rows,
        "rows": rows,
        "pending_video": dict(pending_video or {}),
        "pending_video_rows": [row for row in pending_video_rows or [] if isinstance(row, dict)],
        "pending_video_handled": bool(pending_video_handled),
        "media_request": dict(media_request or {}),
        "original_video_request": dict(original_video_request or {}),
        "wants_original_video": bool(wants_original_video),
        "dual_llm_production": bool(dual_llm_production),
        "target_limit": max(1, int(target_limit or 1)),
        "evidence": dict(base_evidence or {}),
        "failures": [],
        "trace": [],
        "_deps": deps,
    }
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    return await app.ainvoke(state, config=config)


async def _resolve_targets_node(state: KfMediaBindingGraphState) -> dict[str, Any]:
    deps = _deps(state)
    rows = [row for row in state.get("rows") or [] if isinstance(row, dict)]
    result = await _maybe_await(
        deps.resolve_tool_targets(
            actions=list(state.get("actions") or []),
            content=str(state.get("content") or ""),
            context=dict(state.get("context") or {}),
            understanding=dict(state.get("understanding") or {}),
            inventory_rows=rows,
            pending_video=dict(state.get("pending_video") or {}),
            pending_video_rows=[row for row in state.get("pending_video_rows") or [] if isinstance(row, dict)],
            pending_video_handled=bool(state.get("pending_video_handled")),
            target_limit=max(1, int(state.get("target_limit") or 1)),
        )
    )
    resolution = _resolution_to_dict(result)
    evidence = dict(state.get("evidence") or {})

    target_rows = [row for row in resolution.get("target_rows") or [] if isinstance(row, dict)]
    selected_indices = [int(item) for item in resolution.get("selected_indices") or [] if isinstance(item, int)]
    if resolution.get("candidate_binding"):
        evidence["candidate_binding"] = dict(resolution.get("candidate_binding") or {})
    if resolution.get("missing_target_reason"):
        evidence["missing_target_reason"] = str(resolution.get("missing_target_reason") or "")
    if resolution.get("selection_error"):
        evidence["selection_error"] = dict(resolution.get("selection_error") or {})
    if resolution.get("field_target_error"):
        evidence["field_target_error"] = dict(resolution.get("field_target_error") or {})
    if resolution.get("pending_video_context_bound"):
        evidence["pending_video_context_bound"] = dict(resolution.get("pending_video_context_bound") or {})

    original_binding = dict(resolution.get("original_video_target_binding") or {})
    if original_binding:
        original_request = dict(evidence.get("original_video_request") or state.get("original_video_request") or {})
        if original_request.get("requested"):
            original_request["target_binding"] = original_binding
            original_request["reason"] = "上一轮没有稳定匹配到视频目标，不能直接给原视频/高清源。"
            evidence["original_video_request"] = original_request

    if "inventory_rows_override" in resolution:
        rows = [row for row in resolution.get("inventory_rows_override") or [] if isinstance(row, dict)]
        evidence["inventory_rows"] = rows
    if resolution.get("clear_inventory_rows"):
        rows = []
        evidence["inventory_rows"] = []
    if resolution.get("clear_media_outputs"):
        for key in ("image_rows", "video_rows", "image_paths", "video_paths"):
            evidence[key] = []

    rows = deps.rows_with_listing_ids(rows)
    target_rows = deps.rows_with_candidate_numbers(
        deps.rows_with_listing_ids(target_rows),
        selected_indices,
    )
    evidence["inventory_rows"] = deps.rows_with_listing_ids(
        [row for row in evidence.get("inventory_rows") or rows if isinstance(row, dict)]
    )
    evidence["target_rows"] = target_rows
    if target_rows:
        evidence["allowed_rooms"] = _allowed_rooms_evidence(
            deps,
            target_rows=target_rows,
            selected_indices=selected_indices,
        )
    else:
        evidence.pop("allowed_rooms", None)

    query_state = dict((state.get("understanding") or {}).get("query_state") or {})
    if target_rows and query_state.get("pending_media_target_bound"):
        evidence["pending_media_target_bound"] = {
            "media_kind": str(query_state.get("media_kind") or ""),
            "target_labels": [deps.row_labeler(row) for row in target_rows],
        }
    if target_rows and selected_indices:
        rows = target_rows
        evidence["inventory_rows"] = target_rows

    return {
        "rows": rows,
        "target_rows": target_rows,
        "selected_indices": selected_indices,
        "evidence": evidence,
        "status": "targets_resolved",
        "trace": ["media_binding:resolve_targets"],
    }


async def _collect_media_node(state: KfMediaBindingGraphState) -> dict[str, Any]:
    deps = _deps(state)
    actions = list(state.get("actions") or [])
    target_rows = [row for row in state.get("target_rows") or [] if isinstance(row, dict)]
    evidence = dict(state.get("evidence") or {})
    media_request = dict(state.get("media_request") or {})
    specs: list[tuple[str, Any]] = []
    if "send_image" in actions and target_rows:
        specs.append(
            (
                "image",
                _maybe_await(deps.collect_room_media(target_rows, media_kind="image", limit=state.get("target_limit") or 5)),
            )
        )
    if "send_video" in actions and target_rows:
        specs.append(
            (
                "video",
                _maybe_await(deps.collect_room_media(target_rows, media_kind="video", limit=state.get("target_limit") or 5)),
            )
        )
    if not specs:
        return {
            "evidence": evidence,
            "status": "media_not_requested",
            "trace": ["media_binding:collect_media:skipped"],
        }

    results = await asyncio.gather(*(spec[1] for spec in specs), return_exceptions=True)
    failures: list[dict[str, Any]] = []
    for (media_kind, _), result in zip(specs, results):
        if isinstance(result, Exception):
            failures.append({"stage": f"collect_{media_kind}", "reason": str(result)})
            paths: list[Path] = []
            matched_rows: list[dict[str, Any]] = []
            missing = [deps.row_labeler(row) for row in target_rows]
            sync_result = {"failed": [{"source": "collect_room_media", "reason": str(result)}]}
        else:
            paths, matched_rows, missing, sync_result = _collect_result_parts(result)
        _merge_collected_media(
            evidence,
            deps=deps,
            media_kind=media_kind,
            paths=paths,
            matched_rows=matched_rows,
            missing=missing,
            sync_result=sync_result,
            target_rows=target_rows,
            media_request=media_request,
            wants_original_video=bool(state.get("wants_original_video")),
            dual_llm_production=bool(state.get("dual_llm_production")),
        )

    status = "media_collect_failed" if failures else "media_collected"
    return {
        "evidence": evidence,
        "failures": failures,
        "status": status,
        "trace": ["media_binding:collect_media"],
    }


def _route_after_resolve_targets(state: KfMediaBindingGraphState) -> str:
    actions = set(state.get("actions") or [])
    target_rows = [row for row in state.get("target_rows") or [] if isinstance(row, dict)]
    if target_rows and actions.intersection({"send_image", "send_video"}):
        return "collect_media"
    return "end"


def _merge_collected_media(
    evidence: dict[str, Any],
    *,
    deps: KfMediaBindingGraphDeps,
    media_kind: str,
    paths: list[Path],
    matched_rows: list[dict[str, Any]],
    missing: list[str],
    sync_result: dict[str, Any],
    target_rows: list[dict[str, Any]],
    media_request: dict[str, Any],
    wants_original_video: bool,
    dual_llm_production: bool,
) -> None:
    media_manifest_evidence: list[dict[str, Any]] = []
    if isinstance(sync_result, dict):
        raw_manifest_evidence = sync_result.pop("_media_manifest_evidence", [])
        media_manifest_evidence = [
            dict(item)
            for item in raw_manifest_evidence
            if isinstance(item, dict)
        ]
    if media_manifest_evidence:
        evidence.setdefault("media_manifest_evidence", []).extend(media_manifest_evidence)
        evidence[f"{media_kind}_media_manifest_evidence"] = media_manifest_evidence

    if media_kind == "image":
        evidence["image_paths"] = [str(path) for path in paths]
        evidence["image_rows"] = deps.rows_with_listing_ids(matched_rows)
        if sync_result:
            evidence.setdefault("media_sync", {})["image"] = sync_result
        evidence.setdefault("missing_media", []).extend(f"{label}:图片" for label in missing)
        evidence.setdefault("media_status", {})["image"] = {
            "requested_count": media_request.get("requested_count") or len(target_rows),
            "sent_count": len(paths),
            "missing_rooms": missing,
            "sync_status": sync_result,
        }
        return

    evidence["video_paths"] = [str(path) for path in paths]
    evidence["video_rows"] = deps.rows_with_listing_ids(matched_rows)
    if sync_result:
        evidence.setdefault("media_sync", {})["video"] = sync_result
    evidence.setdefault("missing_media", []).extend(f"{label}:视频" for label in missing)
    requested_count = int(media_request.get("requested_count") or len(target_rows) or 0)
    evidence.setdefault("media_status", {})["video"] = {
        "requested_count": requested_count,
        "sent_count": len(paths),
        "missing_rooms": missing,
        "sync_status": sync_result,
    }
    if wants_original_video:
        source_summary = _original_video_source_summary(
            deps,
            paths=paths,
            matched_rows=matched_rows,
            dual_llm_production=dual_llm_production,
        )
        evidence["original_video_paths"] = source_summary.get("original_video_paths") or []
        evidence["original_video_urls"] = source_summary.get("original_video_urls") or []
        evidence["material_page_urls"] = source_summary.get("material_page_urls") or []
        if source_summary.get("media_manifest_evidence"):
            original_evidence = [
                dict(item)
                for item in source_summary.get("media_manifest_evidence") or []
                if isinstance(item, dict)
            ]
            evidence["original_video_media_manifest_evidence"] = original_evidence
            evidence.setdefault("media_manifest_evidence", []).extend(original_evidence)
        if source_summary.get("source_records"):
            evidence["original_video_source_records"] = source_summary["source_records"]
        evidence["original_video_request"] = {
            "requested": True,
            "has_original_source": bool(
                evidence.get("original_video_paths")
                or evidence.get("original_video_urls")
                or evidence.get("material_page_urls")
            ),
            "has_sendable_video": bool(paths),
            "sendable_video_count": len(paths),
            "missing_rooms": missing,
            "reason": "当前素材库只提供企业微信可发送视频，没有单独的原视频/高清下载链接证据。",
        }
    if requested_count and len(paths) < requested_count:
        _suggest_pending_video_memory(
            evidence,
            labels=missing,
            reason="missing_or_pending_video",
            requested_count=requested_count,
            sent_count=len(paths),
        )


def _original_video_source_summary(
    deps: KfMediaBindingGraphDeps,
    *,
    paths: list[Path],
    matched_rows: list[dict[str, Any]],
    dual_llm_production: bool,
) -> dict[str, Any]:
    if dual_llm_production:
        listing_ids = [
            deps.row_listing_id(row)
            for row in deps.rows_with_listing_ids(matched_rows)
            if deps.row_listing_id(row)
        ]
        summary = deps.original_video_sources_for_listings(listing_ids)
    else:
        summary = deps.original_video_sources_for_paths(paths)
    if (
        deps.signed_original_video_urls is not None
        and paths
        and not (summary.get("original_video_urls") or summary.get("material_page_urls"))
    ):
        # 素材库没有原视频链接证据时,退回对已绑定视频文件生成签名直链;
        # 签名生成失败/密钥未配置返回空列表,保持原"无链接"语义不放大声称。
        signed_urls = [str(url) for url in deps.signed_original_video_urls(paths) if str(url or "").strip()]
        if signed_urls:
            summary = dict(summary)
            summary["original_video_urls"] = signed_urls
    return summary


def _suggest_pending_video_memory(
    evidence: dict[str, Any],
    *,
    labels: list[str] | None = None,
    reason: str = "send_pending",
    requested_count: int = 0,
    sent_count: int = 0,
) -> None:
    if not labels:
        return
    created_at = time.time()
    reducer = evidence.setdefault("memory_reducer", {})
    if not isinstance(reducer, dict):
        reducer = {}
        evidence["memory_reducer"] = reducer
    reducer["pending_video_sends"] = {
        "paths": [],
        "labels": list(labels or []),
        "reason": reason,
        "created_at": created_at,
        "requested_count": requested_count,
        "sent_count": sent_count,
    }


def _collect_result_parts(value: Any) -> tuple[list[Path], list[dict[str, Any]], list[str], dict[str, Any]]:
    if not isinstance(value, tuple) or len(value) != 4:
        return [], [], [], {"failed": [{"source": "collect_room_media", "reason": "invalid_result"}]}
    paths, matched_rows, missing, sync_result = value
    return (
        [Path(path) for path in paths or [] if path],
        [row for row in matched_rows or [] if isinstance(row, dict)],
        [str(item).strip() for item in missing or [] if str(item).strip()],
        dict(sync_result or {}),
    )


def _allowed_rooms_evidence(
    deps: KfMediaBindingGraphDeps,
    *,
    target_rows: list[dict[str, Any]],
    selected_indices: list[int],
) -> dict[str, Any]:
    rows = [row for row in deps.rows_with_listing_ids(target_rows) if isinstance(row, dict)]
    labels = _unique_nonempty(deps.row_labeler(row) for row in rows)
    return {
        "source": "kf_tool_resolver.target_rows",
        "count": len(rows),
        "listing_ids": _unique_nonempty(deps.row_listing_id(row) for row in rows),
        "labels": labels,
        "room_keys": _unique_nonempty(_media_room_key(label) for label in labels),
        "selected_indices": [int(item) for item in selected_indices if isinstance(item, int)],
    }


def _media_room_key(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())


def _unique_nonempty(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _resolution_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        return dict(payload or {}) if isinstance(payload, dict) else {}
    result: dict[str, Any] = {}
    for key in (
        "target_rows",
        "selection_error",
        "field_target_error",
        "missing_target_reason",
        "candidate_binding",
        "inventory_rows_override",
        "clear_inventory_rows",
        "clear_media_outputs",
        "pending_video_context_bound",
        "original_video_target_binding",
        "selected_indices",
    ):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deps(state: KfMediaBindingGraphState) -> KfMediaBindingGraphDeps:
    deps = state.get("_deps")
    if not isinstance(deps, KfMediaBindingGraphDeps):
        raise RuntimeError("KfMediaBindingGraphDeps missing from state")
    return deps
