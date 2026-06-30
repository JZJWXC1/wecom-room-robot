from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable

from app.services.kf_contracts import redact_sensitive_text, redact_sensitive_value


DEFAULT_MESSAGE_LIMIT = 30
DEFAULT_CANDIDATE_LIMIT = 10
DEFAULT_TURN_TRACE_LIMIT = 10
MAX_STRUCTURED_TEXT_CHARS = 500
MAX_STRUCTURED_ACTION_ITEMS = 10
SAFE_CONTEXT_SCHEMA_VERSION = "wecom_kf_context.safe.v1"


def _bounded_int(value: Any, *, default: int = 0, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)


def _bounded_float(value: Any, *, default: float = 0.0, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def conversation_key(open_kfid: str, external_userid: str) -> str:
    raw = f"{str(open_kfid or '').strip()}:{str(external_userid or '').strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"kfctx_{digest}"


def safe_context_storage_key(key: str) -> str:
    text = str(key or "").strip()
    if re.fullmatch(r"kfctx_[0-9a-f]{32}", text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"kfctx_{digest}"


def empty_context(*, now: Callable[[], float] = time.time) -> dict[str, Any]:
    return {
        "image_paths": [],
        "video_paths": [],
        "video_urls": [],
        "recent_messages": [],
        "updated_at": now(),
    }


def normalize_last_candidate_set(
    value: Any,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    candidates = [
        item
        for item in value.get("candidates") or []
        if isinstance(item, dict)
    ]
    if not candidates:
        return {}
    shown_count = _bounded_int(value.get("shown_count"), default=0)
    total_count = _bounded_int(value.get("total_count"), default=len(candidates))
    shown_count = min(shown_count, len(candidates))
    return {
        "intent": str(value.get("intent") or "details"),
        "query": str(value.get("query") or ""),
        "candidates": candidates[:candidate_limit],
        "created_at": float(value.get("created_at") or now()),
        "inventory_cache_meta": dict(value.get("inventory_cache_meta") or {}),
        "shown_count": shown_count,
        "total_count": max(total_count, len(candidates)),
    }


def normalize_confirmed_room_context(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    row = value.get("row")
    if not isinstance(row, dict):
        return {}
    return {
        "row": row,
        "label": str(value.get("label") or "").strip(),
        "intent": str(value.get("intent") or "details"),
        "created_at": float(value.get("created_at") or now()),
        "inventory_cache_meta": dict(value.get("inventory_cache_meta") or {}),
    }


def normalize_reference_confirmation(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    status = str(value.get("status") or "").strip()
    kind = str(value.get("kind") or "").strip()
    raw_text = str(value.get("raw_text") or "").strip()
    if not status or not kind or not raw_text:
        return {}
    return {
        "status": status,
        "kind": kind,
        "raw_text": raw_text,
        "original_query": str(value.get("original_query") or "").strip(),
        "suggested_text": str(value.get("suggested_text") or "").strip(),
        "rewritten_query": str(value.get("rewritten_query") or "").strip(),
        "options": [str(item).strip() for item in value.get("options") or [] if str(item).strip()][:5],
        "confidence": str(value.get("confidence") or "medium"),
        "reason": str(value.get("reason") or ""),
        "created_at": float(value.get("created_at") or now()),
    }


def normalize_last_message_understanding(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    original_content = str(value.get("original_content") or "").strip()
    if not original_content:
        return {}
    return {
        "original_content": original_content,
        "rewritten_query": str(value.get("rewritten_query") or "").strip(),
        "effective_query": str(value.get("effective_query") or "").strip(),
        "intent": str(value.get("intent") or "general"),
        "context_reference": bool(value.get("context_reference")),
        "selected_indices": [
            int(item)
            for item in value.get("selected_indices") or []
            if isinstance(item, int)
        ][:DEFAULT_CANDIDATE_LIMIT],
        "query_state": normalize_active_query_state(value.get("query_state"), now=now),
        "is_clarification_answer": bool(value.get("is_clarification_answer")),
        "candidate_continuation": bool(value.get("candidate_continuation")),
        "intent_confidence": _bounded_float(value.get("intent_confidence")),
        "needs_clarification": bool(value.get("needs_clarification")),
        "created_at": float(value.get("created_at") or now()),
    }


def normalize_active_query_state(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    string_keys = (
        "intent",
        "area",
        "area_alias",
        "budget",
        "layout",
        "media_kind",
        "effective_query",
        "rewritten_query",
    )
    bool_keys = (
        "wants_video",
        "wants_image",
        "wants_inventory_sheet",
        "is_clarification_answer",
    )
    for key in string_keys:
        text = str(value.get(key) or "").strip()
        if text:
            normalized[key] = text
    for key in bool_keys:
        if key in value:
            normalized[key] = bool(value.get(key))
    if normalized:
        normalized["created_at"] = float(value.get("created_at") or now())
    return normalized


def normalize_active_context_binding(
    value: Any,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    content = str(value.get("content") or "").strip()
    rows = [row for row in value.get("rows") or [] if isinstance(row, dict)]
    if not content and not rows:
        return {}
    return {
        "content": content,
        "selected_indices": [
            int(item)
            for item in value.get("selected_indices") or []
            if isinstance(item, int)
        ][:candidate_limit],
        "rows": rows[:candidate_limit],
        "created_at": float(value.get("created_at") or now()),
    }


def normalize_pending_video_sends(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    paths = [Path(item) for item in value.get("paths") or [] if item]
    labels = [str(item).strip() for item in value.get("labels") or [] if str(item).strip()]
    if not paths and not labels:
        return {}
    return {
        "paths": paths[:DEFAULT_CANDIDATE_LIMIT],
        "labels": labels[:DEFAULT_CANDIDATE_LIMIT],
        "reason": str(value.get("reason") or "send_pending"),
        "created_at": float(value.get("created_at") or now()),
        "attempts": _bounded_int(value.get("attempts"), default=0),
        "requested_count": _bounded_int(value.get("requested_count"), default=len(paths) or len(labels)),
        "sent_count": _bounded_int(value.get("sent_count"), default=0),
    }


def normalize_pending_media_target(
    value: Any,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    media_kind = str(value.get("media_kind") or value.get("video_or_image") or value.get("kind") or "").strip().lower()
    if media_kind not in {"video", "image"}:
        return {}
    rows = [
        row
        for row in (value.get("candidate_rows") or value.get("rows") or [])
        if isinstance(row, dict)
    ][:candidate_limit]
    labels = [
        str(item).strip()
        for item in (value.get("candidate_labels") or value.get("labels") or [])
        if str(item).strip()
    ][:candidate_limit]
    if not rows and not labels:
        return {}
    return {
        "media_kind": media_kind,
        "candidate_rows": rows,
        "candidate_labels": labels,
        "created_at": float(value.get("created_at") or now()),
        "reason": str(value.get("reason") or "pending_media_target").strip(),
    }


def _clip_structured_text(value: Any, *, limit: int = MAX_STRUCTURED_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _jsonable_structured_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable_structured_value(item)
            for key, item in value.items()
            if item not in ("", None, [], {})
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_structured_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def room_key_from_row(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    community = str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip()
    room_no = str(row.get("房号") or row.get("房间号") or row.get("门牌号") or "").strip()
    return f"{community}{room_no}".strip()


def _listing_id_from_row(row: dict[str, Any]) -> str:
    for key in ("listing_id", "listingId", "房源ID", "房源编号"):
        value = str(row.get(key) or "").strip()
        if value:
            return redact_sensitive_text(value)
    return ""


def _has_viewing_password(viewing_text: str) -> bool:
    return bool(re.search(r"(?<!\d)\d{3,8}#?(?!\d)", viewing_text))


def _viewing_needs_contact(viewing_text: str) -> bool:
    if not viewing_text:
        return False
    return (
        not _has_viewing_password(viewing_text)
        or any(word in viewing_text for word in ("提前联系", "预约", "转租", "联系", "密码不对", "打不开", "空出", "未空"))
    )


def summarize_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    viewing = str(row.get("看房方式密码") or row.get("看房方式") or row.get("密码") or row.get("看房密码") or "").strip()
    has_password = _has_viewing_password(viewing)
    needs_contact = _viewing_needs_contact(viewing)
    summary = {
        "listing_id": _listing_id_from_row(row),
        "key": room_key_from_row(row),
        "community": str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip(),
        "小区": str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip(),
        "room_no": str(row.get("房号") or row.get("房间号") or row.get("门牌号") or "").strip(),
        "房号": str(row.get("房号") or row.get("房间号") or row.get("门牌号") or "").strip(),
        "layout": str(row.get("户型") or row.get("户型描述") or "").strip(),
        "户型": str(row.get("户型") or row.get("户型描述") or "").strip(),
        "layout_type": str(row.get("户型分类") or "").strip(),
        "rent_one": str(row.get("押一付一") or row.get("押一") or "").strip(),
        "押一付一": str(row.get("押一付一") or row.get("押一") or "").strip(),
        "rent_two": str(row.get("押二付一") or row.get("押二") or "").strip(),
        "押二付一": str(row.get("押二付一") or row.get("押二") or "").strip(),
        "has_password": has_password,
        "needs_contact": needs_contact,
        "viewing_mode": (
            "contact_required"
            if needs_contact
            else "password_available"
            if has_password
            else ""
        ),
        "remark": str(row.get("备注") or "").strip(),
        "备注": str(row.get("备注") or "").strip(),
    }
    return {
        key: redact_sensitive_value(value)
        for key, value in summary.items()
        if value not in ("", None, [], {})
    }


def summarize_rows(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for row in list(rows or [])[:limit]:
        summary = summarize_row(row)
        if summary:
            summaries.append(summary)
    return summaries


def _normalize_sent_or_blocked_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    action = {
        "type": str(value.get("type") or "").strip(),
        "count": _bounded_int(value.get("count"), default=0),
        "room_keys": [
            str(item).strip()
            for item in list(value.get("room_keys") or [])[:MAX_STRUCTURED_ACTION_ITEMS]
            if str(item).strip()
        ],
        "items": [
            _clip_structured_text(item, limit=160)
            for item in list(value.get("items") or [])[:MAX_STRUCTURED_ACTION_ITEMS]
            if str(item).strip()
        ],
        "reason": _clip_structured_text(value.get("reason"), limit=240),
        "created_at": float(value.get("created_at") or time.time()),
    }
    return {key: item for key, item in action.items() if item not in ("", None, [], {})}


def _normalize_raw_dialog_context(
    value: Any,
    *,
    now: Callable[[], float] = time.time,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in list(value or [])[-message_limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = _clip_structured_text(item.get("content"), limit=1800)
        if not role or not content:
            continue
        messages.append(
            {
                "role": role,
                "content": content,
                "created_at": float(item.get("created_at") or now()),
            }
        )
    return messages[-message_limit:]


def _minimal_query_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "intent",
        "area",
        "budget",
        "budget_range",
        "price_range",
        "layout",
        "room_type",
        "media_kind",
        "wants_video",
        "wants_original_video",
        "wants_image",
        "wants_viewing",
        "wants_utilities",
        "wants_inventory_sheet",
        "pending_video_action",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if item not in ("", None, [], {}):
            result[key] = _jsonable_structured_value(item)
    return result


def _minimal_rewrite_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    query_state = _minimal_query_state(value.get("query_state"))
    result = {
        "rewritten_query": _clip_structured_text(
            value.get("rewritten_query") or value.get("effective_query"),
            limit=1000,
        ),
        "intent": str(value.get("intent") or query_state.get("intent") or "").strip(),
        "query_state": query_state,
        "needs_clarification": bool(value.get("needs_clarification")),
    }
    return {key: item for key, item in result.items() if item not in ("", None, [], {})}


def _normalize_assistant_sent_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sent_actions = [
        action
        for action in (
            _normalize_sent_or_blocked_action(action)
            for action in value.get("sent_actions") or []
        )
        if action
    ][-MAX_STRUCTURED_ACTION_ITEMS:]
    summary = {
        "final_reply": _clip_structured_text(
            value.get("final_reply") or value.get("text") or value.get("reply"),
            limit=1200,
        ),
        "sent_actions": sent_actions,
        "candidate_state": _jsonable_structured_value(value.get("candidate_state") or {}),
    }
    return {key: item for key, item in summary.items() if item not in ("", None, [], {})}


def _normalize_turn_record(
    value: Any,
    *,
    default_index: int,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    rewrite_source = value.get("rewrite_result") if "rewrite_result" in value else value
    assistant_source = value.get("assistant_sent_summary") or value.get("assistant_output") or {}
    user_source = dict(value.get("user_input") or {})
    user_raw = _clip_structured_text(
        value.get("user_raw") or user_source.get("content"),
        limit=1200,
    )
    record = {
        "turn_id": str(value.get("turn_id") or "").strip(),
        "turn_index": _bounded_int(value.get("turn_index"), default=default_index, minimum=1),
        "created_at": float(value.get("created_at") or now()),
        "user_raw": user_raw,
        "rewritten_query": _minimal_rewrite_fields(rewrite_source).get("rewritten_query", ""),
        "intent": _minimal_rewrite_fields(rewrite_source).get("intent", ""),
        "query_state": _minimal_rewrite_fields(rewrite_source).get("query_state", {}),
        "needs_clarification": bool(
            value.get("needs_clarification")
            or _minimal_rewrite_fields(rewrite_source).get("needs_clarification")
        ),
        "assistant_sent_summary": _normalize_assistant_sent_summary(assistant_source),
    }
    return {key: item for key, item in record.items() if item not in ("", None, [], {})}


def normalize_structured_memory(
    value: Any,
    *,
    trace_limit: int = DEFAULT_TURN_TRACE_LIMIT,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"raw_dialog_context": [], "turn_records": [], "current_turn_id": ""}
    raw_dialog_context = _normalize_raw_dialog_context(
        value.get("raw_dialog_context") or value.get("recent_messages") or [],
        now=now,
    )
    source_records = value.get("turn_records")
    if not isinstance(source_records, list):
        source_records = value.get("turn_trace") or []
    records: list[dict[str, Any]] = []
    for index, item in enumerate(list(source_records)[-trace_limit:], start=1):
        record = _normalize_turn_record(item, default_index=index, now=now)
        if record:
            records.append(record)
    for index, record in enumerate(records, start=1):
        record.setdefault("turn_index", index)
        record.setdefault("turn_id", f"turn-{index}")
    return {
        "raw_dialog_context": raw_dialog_context,
        "turn_records": records[-trace_limit:],
        "current_turn_id": str(value.get("current_turn_id") or "").strip(),
    }


def normalize_media_context(
    context: dict[str, Any],
    *,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    recent_messages: list[dict[str, Any]] = []
    for item in context.get("recent_messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            recent_messages.append(
                {
                    "role": role,
                    "content": content,
                    "created_at": float(item.get("created_at") or now()),
                }
            )

    normalized = {
        "image_paths": [Path(item) for item in context.get("image_paths") or []],
        "video_paths": [Path(item) for item in context.get("video_paths") or []],
        "video_urls": list(context.get("video_urls") or []),
        "recent_messages": recent_messages[-message_limit:],
        "updated_at": float(context.get("updated_at") or now()),
    }
    last_candidate_set = normalize_last_candidate_set(
        context.get("last_candidate_set"),
        candidate_limit=candidate_limit,
        now=now,
    )
    if last_candidate_set:
        normalized["last_candidate_set"] = last_candidate_set
    confirmed = normalize_confirmed_room_context(context.get("confirmed_room"), now=now)
    if confirmed:
        normalized["confirmed_room"] = confirmed
    pending_reference = normalize_reference_confirmation(
        context.get("pending_reference_confirmation"),
        now=now,
    )
    if pending_reference:
        normalized["pending_reference_confirmation"] = pending_reference
    last_understanding = normalize_last_message_understanding(
        context.get("last_message_understanding"),
        now=now,
    )
    if last_understanding:
        normalized["last_message_understanding"] = last_understanding
    active_binding = normalize_active_context_binding(
        context.get("active_context_binding"),
        candidate_limit=candidate_limit,
        now=now,
    )
    if active_binding:
        normalized["active_context_binding"] = active_binding
    active_query_state = normalize_active_query_state(
        context.get("active_query_state"),
        now=now,
    )
    if active_query_state:
        normalized["active_query_state"] = active_query_state
    pending_videos = normalize_pending_video_sends(
        context.get("pending_video_sends"),
        now=now,
    )
    if pending_videos:
        normalized["pending_video_sends"] = pending_videos
    pending_media_target = normalize_pending_media_target(
        context.get("pending_media_target"),
        candidate_limit=candidate_limit,
        now=now,
    )
    if pending_media_target:
        normalized["pending_media_target"] = pending_media_target
    structured_memory = normalize_structured_memory(
        context.get("structured_memory"),
        now=now,
    )
    if structured_memory["raw_dialog_context"] or structured_memory["turn_records"]:
        normalized["structured_memory"] = structured_memory
    return normalized


def _normalize_send_receipts_for_storage(context: dict[str, Any]) -> dict[str, Any]:
    from app.services import kf_send_receipts

    ledger = kf_send_receipts.normalize_receipt_ledger(context)
    if ledger.get("receipts"):
        return ledger
    return {}


def sanitize_context_for_storage(
    context: dict[str, Any],
    *,
    now: Callable[[], float] = time.time,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    normalized = normalize_media_context(
        context,
        message_limit=message_limit,
        candidate_limit=candidate_limit,
        now=now,
    )
    normalized["schema_version"] = SAFE_CONTEXT_SCHEMA_VERSION
    send_receipts = _normalize_send_receipts_for_storage(context)

    if "last_candidate_set" in normalized:
        candidate_set = dict(normalized["last_candidate_set"])
        candidate_set["query"] = redact_sensitive_text(candidate_set.get("query", ""))
        candidate_set["candidates"] = summarize_rows(candidate_set.get("candidates") or [], limit=candidate_limit)
        candidate_set["inventory_cache_meta"] = redact_sensitive_value(candidate_set.get("inventory_cache_meta") or {})
        normalized["last_candidate_set"] = {
            key: value for key, value in candidate_set.items() if value not in ("", None, [], {})
        }

    if "confirmed_room" in normalized:
        confirmed = dict(normalized["confirmed_room"])
        confirmed["label"] = redact_sensitive_text(confirmed.get("label", ""))
        confirmed["row"] = summarize_row(confirmed.get("row"))
        confirmed["inventory_cache_meta"] = redact_sensitive_value(confirmed.get("inventory_cache_meta") or {})
        normalized["confirmed_room"] = {
            key: value for key, value in confirmed.items() if value not in ("", None, [], {})
        }

    if "active_context_binding" in normalized:
        binding = dict(normalized["active_context_binding"])
        binding["content"] = redact_sensitive_text(binding.get("content", ""))
        binding["rows"] = summarize_rows(binding.get("rows") or [], limit=candidate_limit)
        normalized["active_context_binding"] = {
            key: value for key, value in binding.items() if value not in ("", None, [], {})
        }

    if "pending_media_target" in normalized:
        pending_target = dict(normalized["pending_media_target"])
        pending_target["candidate_labels"] = [
            redact_sensitive_text(label)
            for label in pending_target.get("candidate_labels") or []
            if str(label).strip()
        ][:candidate_limit]
        pending_target["candidate_rows"] = summarize_rows(
            pending_target.get("candidate_rows") or [],
            limit=candidate_limit,
        )
        normalized["pending_media_target"] = {
            key: value for key, value in pending_target.items() if value not in ("", None, [], {})
        }

    safe_context = redact_sensitive_value(_jsonable_structured_value(normalized))
    if send_receipts:
        safe_context["send_receipts"] = send_receipts
    return safe_context


def context_is_expired(
    context: dict[str, Any],
    *,
    ttl_seconds: int,
    now: Callable[[], float] = time.time,
) -> bool:
    return now() - float(context.get("updated_at", 0)) > ttl_seconds


def recent_context(
    open_kfid: str,
    external_userid: str,
    *,
    memory: dict[str, dict[str, Any]],
    store: Any,
    ttl_seconds: int,
    logger: Any,
    now: Callable[[], float] = time.time,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any] | None:
    key = conversation_key(open_kfid, external_userid)
    context = memory.get(key)
    if not context:
        try:
            context = store.get(key)
        except Exception:
            logger.exception("WeCom KF context load failed")
            return None
        if not context:
            return None
        context = normalize_media_context(
            context,
            message_limit=message_limit,
            candidate_limit=candidate_limit,
            now=now,
        )
        memory[key] = context
    if context_is_expired(context, ttl_seconds=ttl_seconds, now=now):
        memory.pop(key, None)
        try:
            store.delete(key)
        except Exception:
            logger.exception("WeCom KF expired context cleanup failed")
        return None
    return context


def save_context(
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    *,
    memory: dict[str, dict[str, Any]],
    store: Any,
    logger: Any,
    now: Callable[[], float] = time.time,
) -> None:
    key = conversation_key(open_kfid, external_userid)
    context["updated_at"] = now()
    memory[key] = context
    try:
        store.save(key, sanitize_context_for_storage(context, now=now))
    except Exception:
        logger.exception("WeCom KF context save failed")


def remember_media_context(
    context: dict[str, Any] | None,
    *,
    image_paths: list[Path] | None = None,
    video_paths: list[Path] | None = None,
    video_urls: list[str] | None = None,
    now: Callable[[], float] = time.time,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    current = context or {}
    return {
        **current,
        "image_paths": image_paths if image_paths is not None else current.get("image_paths", []),
        "video_paths": video_paths if video_paths is not None else current.get("video_paths", []),
        "video_urls": video_urls if video_urls is not None else current.get("video_urls", []),
        "last_candidate_set": current.get("last_candidate_set", {}),
        "confirmed_room": current.get("confirmed_room", {}),
        "pending_reference_confirmation": current.get("pending_reference_confirmation", {}),
        "last_message_understanding": current.get("last_message_understanding", {}),
        "active_context_binding": current.get("active_context_binding", {}),
        "active_query_state": current.get("active_query_state", {}),
        "pending_video_sends": current.get("pending_video_sends", {}),
        "pending_media_target": current.get("pending_media_target", {}),
        "structured_memory": current.get("structured_memory", {}),
        "recent_messages": list(current.get("recent_messages") or [])[-message_limit:],
        "updated_at": now(),
    }


def clear_video_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    context["video_paths"] = []
    context["video_urls"] = []
    return context


def clear_image_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    context["image_paths"] = []
    return context


def append_dialog_message(
    context: dict[str, Any] | None,
    *,
    role: str,
    content: str,
    now: Callable[[], float] = time.time,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    max_content_chars: int = 1800,
) -> dict[str, Any] | None:
    content = content.strip()
    if not content:
        return None
    current = context or empty_context(now=now)
    recent_messages = list(current.get("recent_messages") or [])
    recent_messages.append(
        {
            "role": role,
            "content": content[:max_content_chars],
            "created_at": now(),
        }
    )
    current["recent_messages"] = recent_messages[-message_limit:]
    memory = normalize_structured_memory(current.get("structured_memory"), now=now)
    memory["raw_dialog_context"] = _normalize_raw_dialog_context(
        current["recent_messages"],
        now=now,
        message_limit=message_limit,
    )
    current["structured_memory"] = memory
    return current


def start_structured_turn(
    context: dict[str, Any] | None,
    *,
    state: dict[str, Any],
    user_input: dict[str, Any],
    rewrite_result: dict[str, Any],
    now: Callable[[], float] = time.time,
    trace_limit: int = DEFAULT_TURN_TRACE_LIMIT,
) -> dict[str, Any]:
    current = context or empty_context(now=now)
    memory = normalize_structured_memory(
        current.get("structured_memory"),
        trace_limit=trace_limit,
        now=now,
    )
    created_at = now()
    previous_records = list(memory.get("turn_records") or [])
    turn_index = _bounded_int(
        previous_records[-1].get("turn_index") if previous_records else 0,
        default=len(previous_records),
    ) + 1
    turn_id = f"{int(created_at * 1000)}-{turn_index}"
    rewrite_fields = _minimal_rewrite_fields(rewrite_result)
    record = {
        "turn_id": turn_id,
        "turn_index": turn_index,
        "created_at": created_at,
        "user_raw": _clip_structured_text(user_input.get("content"), limit=1200),
        "merged_message_count": _bounded_int(user_input.get("merged_message_count"), default=1, minimum=1),
        "msgids": [str(item).strip() for item in user_input.get("msgids") or [] if str(item).strip()],
        **rewrite_fields,
        "assistant_sent_summary": {},
    }
    memory["current_turn_id"] = turn_id
    memory["turn_records"] = (previous_records + [_jsonable_structured_value(record)])[-trace_limit:]
    current["structured_memory"] = memory
    current["updated_at"] = created_at
    return current


def record_structured_trace_event(
    context: dict[str, Any] | None,
    section: str,
    payload: dict[str, Any],
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    # Planner/tool/selfcheck internals are runtime trace, not conversation memory.
    # Keep this as a no-op compatibility hook so older call sites do not pollute
    # the black-box memory with implementation details.
    if not context:
        return None
    context["updated_at"] = now()
    return context


def update_structured_state(
    context: dict[str, Any] | None,
    *,
    state: dict[str, Any],
    rewrite_result: dict[str, Any] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    if not context:
        return None
    memory = normalize_structured_memory(context.get("structured_memory"), now=now)
    current_turn_id = memory.get("current_turn_id")
    if current_turn_id and rewrite_result:
        records = list(memory.get("turn_records") or [])
        rewrite_fields = _minimal_rewrite_fields(rewrite_result)
        for record in reversed(records):
            if record.get("turn_id") != current_turn_id:
                continue
            record.update(_jsonable_structured_value(rewrite_fields))
            break
        memory["turn_records"] = records[-DEFAULT_TURN_TRACE_LIMIT:]
    context["structured_memory"] = memory
    context["updated_at"] = now()
    return context


def record_structured_assistant_output(
    context: dict[str, Any] | None,
    *,
    draft_reply: str = "",
    final_reply: str = "",
    sent_action: dict[str, Any] | None = None,
    blocked_action: dict[str, Any] | None = None,
    candidate_state: dict[str, Any] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    if not context:
        return None
    memory = normalize_structured_memory(context.get("structured_memory"), now=now)
    current_turn_id = memory.get("current_turn_id")
    if not current_turn_id:
        return context
    records = list(memory.get("turn_records") or [])
    for record in reversed(records):
        if record.get("turn_id") != current_turn_id:
            continue
        summary = dict(record.get("assistant_sent_summary") or {})
        if final_reply:
            summary["final_reply"] = _clip_structured_text(final_reply, limit=1200)
        if sent_action:
            action = _normalize_sent_or_blocked_action(
                {**sent_action, "created_at": sent_action.get("created_at") or now()}
            )
            if action:
                summary["sent_actions"] = (
                    list(summary.get("sent_actions") or []) + [action]
                )[-MAX_STRUCTURED_ACTION_ITEMS:]
        if candidate_state:
            summary["candidate_state"] = _jsonable_structured_value(candidate_state)
        record["assistant_sent_summary"] = _normalize_assistant_sent_summary(summary)
        break
    memory["turn_records"] = records[-DEFAULT_TURN_TRACE_LIMIT:]
    context["structured_memory"] = memory
    context["updated_at"] = now()
    return context


def structured_memory_summary(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    memory = normalize_structured_memory(context.get("structured_memory"))
    return {
        "raw_dialog_context": list(memory.get("raw_dialog_context") or [])[-DEFAULT_MESSAGE_LIMIT:],
        "recent_turn_records": list(memory.get("turn_records") or [])[-3:],
    }


def _last_assistant_output(memory: dict[str, Any]) -> dict[str, Any]:
    for record in reversed(list(memory.get("turn_records") or [])):
        output = dict(record.get("assistant_sent_summary") or {})
        if output.get("final_reply") or output.get("sent_actions"):
            return {
                "final_reply": output.get("final_reply", ""),
                "sent_actions": output.get("sent_actions", []),
            }
    return {}


def _recent_dialog_pairs(memory: dict[str, Any], *, limit: int = 6) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    pending_user = ""
    for item in memory.get("raw_dialog_context") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = _clip_structured_text(item.get("content"), limit=900)
        if not content:
            continue
        if role == "user":
            pending_user = content
            continue
        if role == "assistant" and pending_user:
            pairs.append({"user": pending_user, "assistant": content})
            pending_user = ""
    return pairs[-limit:]


def _recent_failure_summaries(memory: dict[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    return []


def rewrite_memory_view(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    memory = normalize_structured_memory(context.get("structured_memory"))
    candidate_set = normalize_last_candidate_set(context.get("last_candidate_set"))
    confirmed = normalize_confirmed_room_context(context.get("confirmed_room"))
    pending = normalize_pending_video_sends(context.get("pending_video_sends"))
    pending_media_target = normalize_pending_media_target(context.get("pending_media_target"))
    turn_records = list(memory.get("turn_records") or [])
    return {
        "raw_dialog_context": list(memory.get("raw_dialog_context") or [])[-DEFAULT_MESSAGE_LIMIT:],
        "recent_dialog_pairs": _recent_dialog_pairs(memory),
        "last_turn_record": turn_records[-1] if turn_records else {},
        "recent_turn_records": turn_records[-DEFAULT_TURN_TRACE_LIMIT:],
        "last_assistant_output": _last_assistant_output(memory),
        "confirmed_room": {
            "label": confirmed.get("label", ""),
            "row": summarize_row(confirmed.get("row")),
        } if confirmed else {},
        "last_candidate_set": {
            "intent": candidate_set.get("intent", ""),
            "query": candidate_set.get("query", ""),
            "shown_count": candidate_set.get("shown_count", 0),
            "total_count": candidate_set.get("total_count", 0),
            "candidates": summarize_rows(candidate_set.get("candidates") or []),
        } if candidate_set else {},
        "pending_video_sends": {
            "labels": pending.get("labels", []),
            "reason": pending.get("reason", ""),
            "requested_count": pending.get("requested_count", 0),
            "sent_count": pending.get("sent_count", 0),
            "attempts": pending.get("attempts", 0),
        } if pending else {},
        "pending_media_target": {
            "media_kind": pending_media_target.get("media_kind", ""),
            "candidate_labels": pending_media_target.get("candidate_labels", []),
            "reason": pending_media_target.get("reason", ""),
        } if pending_media_target else {},
    }


def planner_memory_view(context: dict[str, Any] | None) -> dict[str, Any]:
    return {}


def reply_memory_view(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    memory = normalize_structured_memory(context.get("structured_memory"))
    return {
        "raw_dialog_context": list(memory.get("raw_dialog_context") or [])[-4:],
    }


def selfcheck_memory_view(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    memory = normalize_structured_memory(context.get("structured_memory"))
    return {
        "raw_dialog_context": list(memory.get("raw_dialog_context") or [])[-DEFAULT_MESSAGE_LIMIT:],
    }


def format_dialog_context(
    context: dict[str, Any] | None,
    *,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
) -> str:
    if not context:
        return ""
    lines: list[str] = []
    for item in list(context.get("recent_messages") or [])[-message_limit:]:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def last_candidate_set(
    context: dict[str, Any] | None,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    if not context:
        return {}
    return normalize_last_candidate_set(
        context.get("last_candidate_set"),
        candidate_limit=candidate_limit,
    )


def pending_video_sends(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    return normalize_pending_video_sends(context.get("pending_video_sends"))


def pending_media_target(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    return normalize_pending_media_target(context.get("pending_media_target"))


def remember_pending_media_target(
    context: dict[str, Any] | None,
    *,
    media_kind: str,
    candidate_rows: list[dict[str, Any]] | None = None,
    candidate_labels: list[str] | None = None,
    reason: str = "pending_media_target",
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    current = context or empty_context(now=now)
    normalized = normalize_pending_media_target(
        {
            "media_kind": media_kind,
            "candidate_rows": candidate_rows or [],
            "candidate_labels": candidate_labels or [],
            "reason": reason,
            "created_at": now(),
        },
        now=now,
    )
    current["pending_media_target"] = normalized
    return current


def clear_pending_media_target(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    context["pending_media_target"] = {}
    return context


def remember_pending_video_sends(
    context: dict[str, Any] | None,
    *,
    paths: list[Path],
    labels: list[str] | None = None,
    reason: str = "send_pending",
    requested_count: int = 0,
    sent_count: int = 0,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    current = context or empty_context(now=now)
    existing = normalize_pending_video_sends(current.get("pending_video_sends"), now=now)
    existing_paths = [Path(item) for item in existing.get("paths") or [] if item]
    pending_paths = _dedupe_paths(existing_paths + paths)[:DEFAULT_CANDIDATE_LIMIT]
    existing_labels = [str(item) for item in existing.get("labels") or [] if str(item).strip()]
    pending_labels = (existing_labels + list(labels or []))[:DEFAULT_CANDIDATE_LIMIT]
    current["pending_video_sends"] = {
        "paths": pending_paths,
        "labels": pending_labels,
        "reason": reason or existing.get("reason") or "send_pending",
        "created_at": float(existing.get("created_at") or now()),
        "attempts": _bounded_int(existing.get("attempts"), default=0),
        "requested_count": max(_bounded_int(requested_count), _bounded_int(existing.get("requested_count"))),
        "sent_count": max(_bounded_int(sent_count), _bounded_int(existing.get("sent_count"))),
    }
    return current


def mark_pending_video_sends_attempted(
    context: dict[str, Any] | None,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    if not context:
        return None
    pending = normalize_pending_video_sends(context.get("pending_video_sends"), now=now)
    if not pending:
        return context
    pending["attempts"] = _bounded_int(pending.get("attempts"), default=0) + 1
    context["pending_video_sends"] = pending
    return context


def clear_pending_video_sends(
    context: dict[str, Any] | None,
    *,
    sent_paths: list[Path] | None = None,
) -> dict[str, Any] | None:
    if not context:
        return None
    pending = normalize_pending_video_sends(context.get("pending_video_sends"))
    if not pending:
        context["pending_video_sends"] = {}
        return context
    if not sent_paths:
        context["pending_video_sends"] = {}
        return context
    sent_keys = {str(path) for path in sent_paths}
    remaining_paths = [Path(path) for path in pending.get("paths") or [] if str(path) not in sent_keys]
    if not remaining_paths:
        context["pending_video_sends"] = {}
        return context
    context["pending_video_sends"] = {
        **pending,
        "paths": remaining_paths,
    }
    return context



def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped
