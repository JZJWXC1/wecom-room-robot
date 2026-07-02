from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from app.services.kf_contracts import safe_artifact_payload


@dataclass(frozen=True)
class OutboundPackageDeps:
    row_label: Callable[[dict[str, Any]], str]
    row_brief: Callable[[dict[str, Any]], dict[str, Any]]
    safe_rule_evidence_for_summary: Callable[[Any], Any]


def build_legacy_outbound_package(
    reply_text: str,
    tool_evidence: dict[str, Any],
    *,
    deps: OutboundPackageDeps,
) -> dict[str, Any]:
    suppress_actions = bool(tool_evidence.get("suppress_actions"))
    inventory_images = [] if suppress_actions else [str(path) for path in tool_evidence.get("inventory_images") or []]
    image_paths = [] if suppress_actions else [str(path) for path in tool_evidence.get("image_paths") or []]
    video_paths = [] if suppress_actions else [str(path) for path in tool_evidence.get("video_paths") or []]
    image_rows = [] if suppress_actions else [row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict)]
    video_rows = [] if suppress_actions else [row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)]
    return {
        "text": safe_artifact_payload(reply_text),
        "inventory_images": inventory_images,
        "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。" if inventory_images else "",
        "image_paths": image_paths,
        "image_explanations": [f"这是{deps.row_label(row)}的图片。" for row in image_rows[: len(image_paths)]],
        "video_paths": video_paths,
        "video_explanations": [f"这是{deps.row_label(row)}的视频。" for row in video_rows[: len(video_paths)]],
        "missing_media": list(tool_evidence.get("missing_media") or []),
        "media_request": tool_evidence.get("media_request") or {},
        "media_status": tool_evidence.get("media_status") or {},
        "original_video_request": tool_evidence.get("original_video_request") or {},
        "original_video_urls": list(tool_evidence.get("original_video_urls") or []),
        "material_page_urls": list(tool_evidence.get("material_page_urls") or []),
        "rule_evidence": deps.safe_rule_evidence_for_summary(tool_evidence.get("rule_evidence") or {}),
        "reply_source": str(tool_evidence.get("deterministic_reply_source") or ""),
        "target_rooms": [deps.row_brief(row) for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)],
    }


def outbound_package_for_current_mode(
    reply_text: str,
    tool_evidence: dict[str, Any],
    *,
    production_mode: bool,
    deps: OutboundPackageDeps,
) -> dict[str, Any]:
    if production_mode:
        prepared_package = execution_package_from_prepared_outbound_package(
            reply_text=reply_text,
            tool_evidence=tool_evidence,
            deps=deps,
        )
        if prepared_package:
            return prepared_package
        return {
            "text": safe_artifact_payload(reply_text),
            "inventory_images": [],
            "inventory_explanation": "",
            "image_paths": [],
            "image_explanations": [],
            "video_paths": [],
            "video_explanations": [],
            "missing_media": list(tool_evidence.get("missing_media") or []),
            "media_request": tool_evidence.get("media_request") or {},
            "media_status": tool_evidence.get("media_status") or {},
            "reply_source": str(tool_evidence.get("deterministic_reply_source") or ""),
            "prepared_outbound_package": False,
            "legacy_builder_blocked_in_production": True,
            "actions_suppressed": bool(tool_evidence.get("suppress_actions")),
        }
    return tool_evidence.get("outbound_package") or build_legacy_outbound_package(
        reply_text,
        tool_evidence,
        deps=deps,
    )


def outbound_package_rows_for_kind(tool_evidence: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = "video_rows" if kind == "video" else "image_rows" if kind == "image" else ""
    return [row for row in tool_evidence.get(key) or [] if isinstance(row, dict)] if key else []


def execution_package_from_prepared_outbound_package(
    *,
    reply_text: str,
    tool_evidence: dict[str, Any],
    deps: OutboundPackageDeps,
) -> dict[str, Any]:
    package = _prepared_outbound_package_payload(tool_evidence)
    if not package:
        return {}
    captions_by_action_id = _outbound_package_caption_by_action_id(package)
    send_actions = [item for item in package.get("send_actions") or [] if isinstance(item, dict)]
    prepared_actions: list[dict[str, Any]] = []
    inventory_images: list[str] = []
    image_paths: list[str] = []
    video_paths: list[str] = []
    inventory_caption = ""
    image_captions: list[str] = []
    video_captions: list[str] = []
    for fallback_position, action in enumerate(send_actions, start=1):
        kind = _outbound_package_action_kind(action)
        if kind not in {"inventory_sheet", "image", "video"}:
            continue
        position = _outbound_package_action_position(action, fallback_position)
        path = _outbound_package_path_for_action(
            action,
            tool_evidence=tool_evidence,
            kind=kind,
            position=position,
        )
        if not path:
            continue
        action_id = str(action.get("action_id") or "").strip()
        caption = captions_by_action_id.get(action_id, {})
        caption_text = str(caption.get("text") or "").strip()
        prepared_action = {
            "action_id": action_id,
            "action_type": str(action.get("action_type") or "").strip(),
            "kind": kind,
            "path": path,
            "position": position,
            "caption": caption_text,
            "caption_id": str(caption.get("caption_id") or "").strip(),
            "send_action": safe_artifact_payload(action),
        }
        prepared_actions.append(prepared_action)
        if kind == "inventory_sheet":
            inventory_images.append(path)
            if caption_text and not inventory_caption:
                inventory_caption = caption_text
        elif kind == "image":
            image_paths.append(path)
            if caption_text:
                image_captions.append(caption_text)
        elif kind == "video":
            video_paths.append(path)
            if caption_text:
                video_captions.append(caption_text)
    return {
        "text": safe_artifact_payload(str(package.get("reply_text") or reply_text or "")),
        "prepared_outbound_package": True,
        "prepared_package_source": "llm2_production_outbound_package",
        "inventory_images": inventory_images,
        "inventory_explanation": inventory_caption,
        "image_paths": image_paths,
        "image_explanations": image_captions,
        "video_paths": video_paths,
        "video_explanations": video_captions,
        "prepared_actions": prepared_actions,
        "action_captions": safe_artifact_payload(package.get("action_captions") or []),
        "send_actions": safe_artifact_payload(send_actions),
        "missing_media": list(tool_evidence.get("missing_media") or []),
        "media_request": tool_evidence.get("media_request") or {},
        "media_status": tool_evidence.get("media_status") or {},
        "original_video_request": tool_evidence.get("original_video_request") or {},
        "original_video_urls": list(tool_evidence.get("original_video_urls") or []),
        "material_page_urls": list(tool_evidence.get("material_page_urls") or []),
        "rule_evidence": deps.safe_rule_evidence_for_summary(tool_evidence.get("rule_evidence") or {}),
        "reply_source": str(package.get("reply_source") or tool_evidence.get("deterministic_reply_source") or ""),
        "target_rooms": [deps.row_brief(row) for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)],
    }


def _prepared_outbound_package_payload(tool_evidence: dict[str, Any]) -> dict[str, Any]:
    payload = tool_evidence.get("llm2_production_outbound_package")
    return dict(payload) if isinstance(payload, dict) and payload else {}


def _outbound_package_action_kind(action: dict[str, Any]) -> str:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    raw_kind = str(metadata.get("kind") or payload.get("kind") or "").strip().lower()
    if raw_kind in {"inventory_sheet", "sheet", "inventory"}:
        return "inventory_sheet"
    if raw_kind in {"video", "image"}:
        return raw_kind
    action_type = str(action.get("action_type") or "").strip().lower()
    marker = f"{action.get('action_id') or ''} {action.get('evidence_id') or ''}".lower()
    if "inventory_sheet" in marker or "inventory-sheet" in marker:
        return "inventory_sheet"
    if action_type == "video":
        return "video"
    if action_type == "image":
        return "image"
    return ""


def _outbound_package_action_position(action: dict[str, Any], fallback: int) -> int:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    for source in (payload, metadata, action):
        for key in ("media_number", "position", "display_order", "index"):
            try:
                value = int(source.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    marker = f"{action.get('action_id') or ''} {action.get('evidence_id') or ''}"
    match = re.search(r"(?:video|image|inventory[_-]?sheet)[_-](\d+)", marker, re.I)
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return fallback
    return fallback


def _outbound_package_paths_for_kind(tool_evidence: dict[str, Any], kind: str) -> list[str]:
    if kind == "video":
        return [str(path) for path in tool_evidence.get("video_paths") or [] if str(path).strip()]
    if kind == "image":
        return [str(path) for path in tool_evidence.get("image_paths") or [] if str(path).strip()]
    if kind == "inventory_sheet":
        return [
            str(path)
            for path in (tool_evidence.get("inventory_image_paths") or tool_evidence.get("inventory_images") or [])
            if str(path).strip()
        ]
    return []


def _outbound_package_path_for_action(
    action: dict[str, Any],
    *,
    tool_evidence: dict[str, Any],
    kind: str,
    position: int,
) -> str:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    for source in (payload, metadata, action):
        for key in ("local_path", "path", "file_path", "media_path"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    paths = _outbound_package_paths_for_kind(tool_evidence, kind)
    return paths[position - 1] if 0 < position <= len(paths) else ""


def _outbound_package_caption_by_action_id(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    captions = [item for item in package.get("action_captions") or [] if isinstance(item, dict)]

    def sort_key(item: dict[str, Any]) -> int:
        try:
            return int(item.get("display_order"))
        except (TypeError, ValueError):
            return 10_000

    result: dict[str, dict[str, Any]] = {}
    for caption in sorted(captions, key=sort_key):
        action_id = str(caption.get("action_id") or "").strip()
        if action_id and action_id not in result:
            result[action_id] = caption
    return result
