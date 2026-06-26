from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from app.services.kf_contracts import (
    ORCHESTRATOR_SHADOW_SCHEMA_VERSION,
    OrchestratorShadowArtifact,
    safe_artifact_payload,
)


BASELINE_COMMIT = "693a9c899d1cb1a4565ad67e4e600fc9559da4dd"
MAX_SUMMARY_ITEMS = 10

ACCESS_REQUEST_MARKERS = (
    "看房",
    "密码",
    "自助",
    "开门",
    "打不开",
    "预约",
    "今天看",
    "明天看",
)
ACCESS_SECRET_KEY_MARKERS = (
    "看房方式密码",
    "看房密码",
    "密码",
    "password",
    "viewing",
)
ACCESS_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{3,8}#(?![A-Za-z0-9])")

ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "listing_id": ("listing_id", "listingId", "房源ID", "房源编号"),
    "area": ("区域", "area"),
    "community": ("小区", "小区名", "community"),
    "room_no": ("房号", "房间号", "room", "room_no"),
    "rent_pay1": ("押一付一", "押一付", "押一付一月租金", "rent_pay1"),
    "rent_pay2": ("押二付一", "押二付", "押二付一月租金", "rent_pay2"),
    "source_hash": ("source_hash", "inventory_source_hash"),
    "source_kind": ("source_kind", "inventory_source_kind"),
}


def build_shadow_artifact(
    *,
    content: str,
    open_kfid: str = "",
    external_userid: str = "",
    msgids: list[str] | None = None,
    generation: int | str = "",
    inventory_read_context: Any = None,
    understanding: dict[str, Any] | None = None,
    planner_result: dict[str, Any] | None = None,
    tool_evidence: dict[str, Any] | None = None,
    reply_result: dict[str, Any] | None = None,
    final_reply: str = "",
    mode: str = "shadow",
) -> dict[str, Any]:
    evidence = dict(tool_evidence or {})
    reply_payload = dict(reply_result or {})
    inventory_read = _inventory_read_summary(inventory_read_context, evidence)
    evidence_items = _evidence_items(evidence)
    inventory_candidates = _candidate_summaries(
        evidence.get("inventory_rows") or [],
        evidence_items,
        context_source_hash=str(inventory_read.get("source_hash") or ""),
        context_source_kind=str(inventory_read.get("source_kind") or ""),
    )
    target_candidates = _candidate_summaries(
        evidence.get("target_rows") or [],
        evidence_items,
        context_source_hash=str(inventory_read.get("source_hash") or ""),
        context_source_kind=str(inventory_read.get("source_kind") or ""),
    )
    candidate_lookup = _candidate_lookup(inventory_candidates, target_candidates)
    media_bindings = {
        "images": _media_binding_summaries(
            kind="image",
            rows=evidence.get("image_rows") or [],
            paths=evidence.get("image_paths") or [],
            evidence_items=evidence_items,
            candidate_lookup=candidate_lookup,
            context_source_hash=str(inventory_read.get("source_hash") or ""),
            context_source_kind=str(inventory_read.get("source_kind") or ""),
        ),
        "videos": _media_binding_summaries(
            kind="video",
            rows=evidence.get("video_rows") or [],
            paths=evidence.get("video_paths") or [],
            evidence_items=evidence_items,
            candidate_lookup=candidate_lookup,
            context_source_hash=str(inventory_read.get("source_hash") or ""),
            context_source_kind=str(inventory_read.get("source_kind") or ""),
        ),
    }
    access_boundary = _access_boundary_summary(content, final_reply, evidence)
    legacy_pipeline = _legacy_pipeline_summary(
        understanding=understanding or {},
        planner_result=planner_result or {},
        tool_evidence=evidence,
        reply_result=reply_payload,
        final_reply=final_reply,
        inventory_candidates=inventory_candidates,
        target_candidates=target_candidates,
        media_bindings=media_bindings,
        access_boundary=access_boundary,
    )
    risk_reasons = _risk_reasons(
        inventory_read=inventory_read,
        evidence_items=evidence_items,
        inventory_candidates=inventory_candidates,
        target_candidates=target_candidates,
        media_bindings=media_bindings,
        safe_candidate_payload=legacy_pipeline,
    )
    shadow_a = {
        "diff": {
            "customer_visible_reply_changed": False,
            "send_actions_changed": False,
            "fact_source_changed": False,
            "legacy_action_count": len(_string_list(evidence.get("actions"))),
            "candidate_count": len(inventory_candidates),
            "target_candidate_count": len(target_candidates),
            "media_binding_count": len(media_bindings["images"]) + len(media_bindings["videos"]),
        },
        "verdict": _verdict(risk_reasons),
        "risk_reasons": risk_reasons,
    }
    turn = {
        "conversation_hash": _stable_hash([open_kfid, external_userid]) if open_kfid or external_userid else "",
        "turn_hash": _stable_hash([content, msgids or [], generation]),
        "content_hash": _stable_hash(content),
        "reply_hash": _stable_hash(final_reply),
        "message_ids_hash": _stable_hash(msgids or []),
        "message_count": len(msgids or []),
    }
    artifact = OrchestratorShadowArtifact(
        mode=mode,
        artifact_id=_artifact_id(turn, inventory_read, legacy_pipeline, shadow_a),
        created_at=_now_utc_iso(),
        baseline_commit=BASELINE_COMMIT,
        turn=turn,
        inventory_read=inventory_read,
        legacy_pipeline=legacy_pipeline,
        shadow_a=shadow_a,
        integration_notes=_integration_notes(risk_reasons),
    )
    return artifact.to_safe_dict()


def _inventory_read_summary(inventory_read_context: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    if inventory_read_context is not None and hasattr(inventory_read_context, "to_log_dict"):
        raw = dict(inventory_read_context.to_log_dict())
    else:
        raw = dict(evidence.get("inventory_read_context") or {})
    return {
        "decision_id": str(raw.get("decision_id") or ""),
        "source_kind": str(raw.get("source_kind") or ""),
        "selection_mode": str(raw.get("selection_mode") or ""),
        "source_hash": str(raw.get("source_hash") or ""),
        "schema_version": str(raw.get("schema_version") or ""),
        "snapshot_id": str(raw.get("snapshot_id") or ""),
        "fallback_used": bool(raw.get("fallback_used")),
        "has_context": bool(raw),
    }


def _legacy_pipeline_summary(
    *,
    understanding: dict[str, Any],
    planner_result: dict[str, Any],
    tool_evidence: dict[str, Any],
    reply_result: dict[str, Any],
    final_reply: str,
    inventory_candidates: list[dict[str, Any]],
    target_candidates: list[dict[str, Any]],
    media_bindings: dict[str, list[dict[str, Any]]],
    access_boundary: dict[str, Any],
) -> dict[str, Any]:
    selfcheck = dict(reply_result.get("selfcheck") or {})
    planner_reply_result = dict(tool_evidence.get("planner_reply_result") or {})
    outbound_package = dict(tool_evidence.get("outbound_package") or {})
    return {
        "intent": str(understanding.get("intent") or ""),
        "actions": _string_list(tool_evidence.get("actions")),
        "reply_source": str(
            tool_evidence.get("deterministic_reply_source")
            or outbound_package.get("reply_source")
            or reply_result.get("reply_source")
            or ""
        ),
        "reply_hash": _stable_hash(final_reply),
        "planner": {
            "source": str(planner_result.get("source") or planner_result.get("reply_source") or ""),
            "need_rewrite_clarification": bool(planner_result.get("need_rewrite_clarification")),
            "reply_source": str(planner_reply_result.get("source") or ""),
            "selfcheck_status": str((planner_reply_result.get("selfcheck") or {}).get("status") or ""),
        },
        "selfcheck": {
            "status": str(selfcheck.get("status") or ""),
            "source": str(selfcheck.get("source") or ""),
            "needs_planner_retry": bool(reply_result.get("needs_planner_retry")),
        },
        "counts": {
            "inventory_rows": len([row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]),
            "target_rows": len([row for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)]),
            "inventory_images": len(tool_evidence.get("inventory_images") or []),
            "images": len(tool_evidence.get("image_paths") or []),
            "videos": len(tool_evidence.get("video_paths") or []),
            "inventory_listing_evidence": len(tool_evidence.get("inventory_listing_evidence") or []),
            "missing_media": len(tool_evidence.get("missing_media") or []),
        },
        "inventory_candidates": inventory_candidates,
        "target_candidates": target_candidates,
        "media_bindings": media_bindings,
        "access_boundary": access_boundary,
        "outbound_package": {
            "has_package": bool(outbound_package),
            "inventory_image_count": len(outbound_package.get("inventory_images") or []),
            "image_count": len(outbound_package.get("image_paths") or []),
            "video_count": len(outbound_package.get("video_paths") or []),
            "missing_media_count": len(outbound_package.get("missing_media") or []),
        },
    }


def _candidate_summaries(
    rows: Any,
    evidence_items: list[dict[str, Any]],
    *,
    context_source_hash: str,
    context_source_kind: str,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for index, row in enumerate([item for item in rows or [] if isinstance(item, dict)][:MAX_SUMMARY_ITEMS], start=1):
        descriptor = _row_descriptor(row, evidence_items)
        source_hash = descriptor["source_hash"] or context_source_hash
        source_kind = descriptor["source_kind"] or context_source_kind
        summaries.append(
            {
                "candidate_number": index,
                "listing_id": descriptor["listing_id"],
                "row_hash": _stable_hash(_row_identity(row)),
                "label_hash": _stable_hash(_row_label(row)),
                "source_kind": source_kind,
                "source_hash": source_hash,
                "has_price": bool(_row_value(row, ROW_ALIASES["rent_pay1"]) or _row_value(row, ROW_ALIASES["rent_pay2"])),
                "has_access_text": _row_has_access_text(row),
            }
        )
    return summaries


def _media_binding_summaries(
    *,
    kind: str,
    rows: Any,
    paths: Any,
    evidence_items: list[dict[str, Any]],
    candidate_lookup: dict[str, int],
    context_source_hash: str,
    context_source_kind: str,
) -> list[dict[str, Any]]:
    safe_rows = [item for item in rows or [] if isinstance(item, dict)]
    safe_paths = [str(path) for path in paths or [] if str(path).strip()]
    max_count = min(max(len(safe_rows), len(safe_paths)), MAX_SUMMARY_ITEMS)
    bindings: list[dict[str, Any]] = []
    for index in range(max_count):
        row = safe_rows[index] if index < len(safe_rows) else {}
        descriptor = _row_descriptor(row, evidence_items) if row else {}
        row_hash = _stable_hash(_row_identity(row)) if row else ""
        candidate_number = (
            candidate_lookup.get(str(descriptor.get("listing_id") or ""))
            or candidate_lookup.get(row_hash)
        )
        bindings.append(
            {
                "kind": kind,
                "media_number": index + 1,
                "candidate_number": candidate_number,
                "listing_id": str(descriptor.get("listing_id") or ""),
                "row_hash": row_hash,
                "path_hash": _stable_hash(safe_paths[index]) if index < len(safe_paths) else "",
                "source_kind": str(descriptor.get("source_kind") or context_source_kind),
                "source_hash": str(descriptor.get("source_hash") or context_source_hash),
                "bound": bool(candidate_number and descriptor.get("listing_id")),
            }
        )
    return bindings


def _candidate_lookup(*candidate_groups: list[dict[str, Any]]) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for candidates in candidate_groups:
        for item in candidates:
            number = int(item.get("candidate_number") or 0)
            if number <= 0:
                continue
            listing_id = str(item.get("listing_id") or "")
            row_hash = str(item.get("row_hash") or "")
            if listing_id:
                lookup.setdefault(listing_id, number)
            if row_hash:
                lookup.setdefault(row_hash, number)
    return lookup


def _risk_reasons(
    *,
    inventory_read: dict[str, Any],
    evidence_items: list[dict[str, Any]],
    inventory_candidates: list[dict[str, Any]],
    target_candidates: list[dict[str, Any]],
    media_bindings: dict[str, list[dict[str, Any]]],
    safe_candidate_payload: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    context_source_hash = str(inventory_read.get("source_hash") or "")
    context_source_kind = str(inventory_read.get("source_kind") or "")
    source_hashes = {
        str(item.get("source_hash") or "")
        for item in [
            *evidence_items,
            *inventory_candidates,
            *target_candidates,
            *media_bindings.get("images", []),
            *media_bindings.get("videos", []),
        ]
        if str(item.get("source_hash") or "")
    }
    source_kinds = {
        str(item.get("source_kind") or "")
        for item in [
            *evidence_items,
            *inventory_candidates,
            *target_candidates,
            *media_bindings.get("images", []),
            *media_bindings.get("videos", []),
        ]
        if str(item.get("source_kind") or "")
    }
    if not inventory_read.get("has_context"):
        reasons.append("missing_inventory_read_context")
    if len(source_hashes) > 1 or (context_source_hash and any(item != context_source_hash for item in source_hashes)):
        reasons.append("mixed_source_hash")
    if len(source_kinds) > 1 or (context_source_kind and any(item != context_source_kind for item in source_kinds)):
        reasons.append("mixed_source_kind")
    if any(not str(item.get("listing_id") or "") for item in [*inventory_candidates, *target_candidates]):
        reasons.append("missing_listing_id")
    if any(not item.get("bound") for item in [*media_bindings.get("images", []), *media_bindings.get("videos", [])]):
        reasons.append("media_binding_unresolved")
    safe_json = json.dumps(safe_artifact_payload(safe_candidate_payload), ensure_ascii=False, sort_keys=True, default=str)
    if ACCESS_CODE_PATTERN.search(safe_json):
        reasons.append("safe_artifact_secret_leak")
    return _dedupe(reasons)


def _verdict(risk_reasons: list[str]) -> str:
    blocking = {
        "mixed_source_hash",
        "mixed_source_kind",
        "safe_artifact_secret_leak",
    }
    if any(reason in blocking for reason in risk_reasons):
        return "blocked"
    if risk_reasons:
        return "review"
    return "pass"


def _integration_notes(risk_reasons: list[str]) -> list[str]:
    notes = [
        "shadow artifact only; not written to structured memory, LLM input, final_reply, tool_evidence, or outbound_package",
        "no customer-visible reply, send action, or fact source is changed by A-Orchestrator-Shadow",
    ]
    if "mixed_source_hash" in risk_reasons:
        notes.append("Integration Note: mixed source_hash observed; later primary orchestration must block before customer-visible facts")
    if "media_binding_unresolved" in risk_reasons:
        notes.append("Integration Note: media binding lacks listing_id or candidate number for at least one artifact row")
    return notes


def _access_boundary_summary(content: str, final_reply: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_requested_access": _customer_requested_access(content),
        "reply_mentions_access_code": bool(ACCESS_CODE_PATTERN.search(final_reply or "")),
        "evidence_access_text_present": _evidence_has_access_text(evidence),
        "sensitive_access_value_count": _access_secret_count(evidence),
    }


def _customer_requested_access(content: str) -> bool:
    text = str(content or "")
    return any(marker in text for marker in ACCESS_REQUEST_MARKERS)


def _evidence_has_access_text(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _is_access_key(key_text) and str(item or "").strip():
                return True
            if _evidence_has_access_text(item):
                return True
    if isinstance(value, list):
        return any(_evidence_has_access_text(item) for item in value)
    if isinstance(value, tuple):
        return any(_evidence_has_access_text(item) for item in value)
    return False


def _access_secret_count(value: Any, *, key: str = "") -> int:
    if isinstance(value, dict):
        return sum(_access_secret_count(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return sum(_access_secret_count(item) for item in value)
    if isinstance(value, tuple):
        return sum(_access_secret_count(item) for item in value)
    if isinstance(value, str):
        if _is_access_key(key) or ACCESS_CODE_PATTERN.search(value):
            return len(ACCESS_CODE_PATTERN.findall(value))
    return 0


def _row_has_access_text(row: dict[str, Any]) -> bool:
    return any(_is_access_key(key) and str(value or "").strip() for key, value in row.items())


def _is_access_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in key or marker in lowered for marker in ACCESS_SECRET_KEY_MARKERS)


def _evidence_items(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in evidence.get("inventory_listing_evidence") or []:
        item = _to_plain_dict(raw)
        if item:
            result.append(item)
    return result


def _row_descriptor(row: dict[str, Any], evidence_items: list[dict[str, Any]]) -> dict[str, str]:
    direct_listing_id = _row_value(row, ROW_ALIASES["listing_id"])
    direct_source_hash = _row_value(row, ROW_ALIASES["source_hash"])
    direct_source_kind = _row_value(row, ROW_ALIASES["source_kind"])
    community = _row_value(row, ROW_ALIASES["community"])
    room_no = _row_value(row, ROW_ALIASES["room_no"])
    matched = _match_evidence(
        evidence_items,
        listing_id=direct_listing_id,
        community=community,
        room_no=room_no,
    )
    return {
        "listing_id": direct_listing_id or str(matched.get("listing_id") or ""),
        "source_hash": direct_source_hash or str(matched.get("source_hash") or ""),
        "source_kind": direct_source_kind or str(matched.get("source_kind") or ""),
    }


def _match_evidence(
    evidence_items: list[dict[str, Any]],
    *,
    listing_id: str,
    community: str,
    room_no: str,
) -> dict[str, Any]:
    if listing_id:
        for item in evidence_items:
            if str(item.get("listing_id") or "") == listing_id:
                return item
    if community or room_no:
        for item in evidence_items:
            if str(item.get("community") or "") == community and str(item.get("room_no") or "") == room_no:
                return item
    return {}


def _row_identity(row: dict[str, Any]) -> dict[str, str]:
    return {
        "listing_id": _row_value(row, ROW_ALIASES["listing_id"]),
        "community": _row_value(row, ROW_ALIASES["community"]),
        "room_no": _row_value(row, ROW_ALIASES["room_no"]),
    }


def _row_label(row: dict[str, Any]) -> str:
    return f"{_row_value(row, ROW_ALIASES['community'])}{_row_value(row, ROW_ALIASES['room_no'])}".strip()


def _row_value(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _artifact_id(
    turn: dict[str, Any],
    inventory_read: dict[str, Any],
    legacy_pipeline: dict[str, Any],
    shadow_a: dict[str, Any],
) -> str:
    return "orch_shadow_" + _stable_hash(
        {
            "turn": turn,
            "inventory_read": inventory_read,
            "legacy_pipeline": legacy_pipeline,
            "shadow_a": shadow_a,
        }
    )[:24]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(safe_artifact_payload(value), ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
