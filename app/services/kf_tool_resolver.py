from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services import kf_context_memory
from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text


DEFAULT_TARGET_LIMIT = 5


@dataclass(frozen=True)
class ToolResolverInput:
    actions: list[str] = field(default_factory=list)
    content: str = ""
    understanding: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    inventory_rows: list[dict[str, Any]] = field(default_factory=list)
    pending_video: dict[str, Any] = field(default_factory=dict)
    pending_video_rows: list[dict[str, Any]] = field(default_factory=list)
    pending_video_handled: bool = False
    target_limit: int = DEFAULT_TARGET_LIMIT


@dataclass(frozen=True)
class ToolResolverResult:
    target_rows: list[dict[str, Any]]
    selection_error: dict[str, Any]
    field_target_error: dict[str, Any]
    missing_target_reason: str
    candidate_binding: dict[str, Any]
    inventory_rows_override: list[dict[str, Any]] | None = None
    clear_inventory_rows: bool = False
    clear_media_outputs: bool = False
    pending_video_context_bound: dict[str, Any] = field(default_factory=dict)
    original_video_target_binding: dict[str, Any] = field(default_factory=dict)
    selected_indices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_rows": self.target_rows,
            "selection_error": self.selection_error,
            "field_target_error": self.field_target_error,
            "missing_target_reason": self.missing_target_reason,
            "candidate_binding": self.candidate_binding,
            "clear_inventory_rows": self.clear_inventory_rows,
            "clear_media_outputs": self.clear_media_outputs,
            "pending_video_context_bound": self.pending_video_context_bound,
            "original_video_target_binding": self.original_video_target_binding,
            "selected_indices": self.selected_indices,
        }
        if self.inventory_rows_override is not None:
            payload["inventory_rows_override"] = self.inventory_rows_override
        return payload


def resolve_tool_targets(
    *,
    actions: list[str],
    content: str,
    understanding: dict[str, Any],
    context: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    pending_video: dict[str, Any] | None = None,
    pending_video_rows: list[dict[str, Any]] | None = None,
    pending_video_handled: bool = False,
    target_limit: int = DEFAULT_TARGET_LIMIT,
) -> ToolResolverResult:
    """Bind tool evidence rows to the LLM1 task without generating visible text."""

    safe_understanding = understanding if isinstance(understanding, dict) else {}
    safe_context = context if isinstance(context, dict) else {}
    rows = [row for row in inventory_rows or [] if isinstance(row, dict)]
    pending_video = pending_video if isinstance(pending_video, dict) else {}
    pending_video_rows = [row for row in pending_video_rows or [] if isinstance(row, dict)]
    proof = dict(safe_understanding.get("constraint_proof") or {})
    task = dict(safe_understanding.get("structured_task") or {})

    current_text = _joined_text(content, task.get("original_text"), safe_understanding.get("original_query"))
    query_text = _joined_text(
        content,
        task.get("original_text"),
        safe_understanding.get("effective_query"),
        safe_understanding.get("rewritten_query"),
        proof.get("budget_label"),
    )
    original_room_text = _joined_text(content, task.get("original_text"))
    selected_indices = _selected_indices_from_understanding(
        safe_understanding,
        current_text or query_text,
        limit=target_limit,
    )
    candidate_rows = _candidate_rows(safe_context)
    field_followup_requires_specific_room = _field_followup_needs_specific_room(
        content,
        safe_understanding,
    )
    media_target_error = (
        {}
        if pending_video_handled
        else _media_target_error_for_unclear_room(
            content=content,
            understanding=safe_understanding,
            search_rows=rows,
        )
    )
    target_selection_uses_candidates = bool(
        selected_indices
        and candidate_rows
        and not _room_refs_from_text(original_room_text)
    )

    binding: dict[str, Any] = {
        "status": "unbound",
        "selected_indices": selected_indices,
        "candidate_count": len(candidate_rows),
        "candidate_labels": [_row_label(row) for row in candidate_rows[:10]],
        "inventory_row_count": len(rows),
        "inventory_labels": [_row_label(row) for row in rows[:10]],
    }

    target_rows: list[dict[str, Any]] = []
    if not (pending_video_handled or field_followup_requires_specific_room or media_target_error):
        target_rows = _target_rows_from_understanding(
            safe_understanding,
            safe_context,
            rows,
            content=content,
            target_limit=target_limit,
        )
        if target_rows:
            binding["status"] = "bound"
            binding["source"] = _target_binding_source(
                target_rows=target_rows,
                candidate_rows=candidate_rows,
                inventory_rows=rows,
                selected_indices=selected_indices,
            )
            binding["target_labels"] = [_row_label(row) for row in target_rows]

    if not target_selection_uses_candidates:
        target_rows = _enforce_target_rows_community_constraints(target_rows, rows, proof)

    inventory_rows_override: list[dict[str, Any]] | None = None
    pending_video_context_bound: dict[str, Any] = {}
    if (
        not target_rows
        and not pending_video_handled
        and "send_video" in actions
        and proof.get("wants_original_video")
        and pending_video_rows
    ):
        target_rows = _enforce_target_rows_community_constraints(pending_video_rows, pending_video_rows, proof)
        inventory_rows_override = list(target_rows)
        pending_video_context_bound = {
            "reason": "original_video_followup_uses_pending_missing_video_labels",
            "labels": [_row_label(row) for row in target_rows],
        }
        binding.update(
            {
                "status": "bound",
                "source": "pending_video_labels",
                "target_labels": [_row_label(row) for row in target_rows],
            }
        )

    original_video_target_error = bool(
        not target_rows
        and not pending_video_handled
        and "send_video" in actions
        and _original_video_followup_without_explicit_target(content, safe_understanding)
    )

    if not target_rows and not pending_video_handled and _has_bound_room_field_followup(content):
        confirmed = _confirmed_row(safe_context)
        if confirmed and not _has_explicit_candidate_selection(content):
            target_rows = _enforce_target_rows_community_constraints([confirmed], rows, proof)
            if target_rows:
                binding.update(
                    {
                        "status": "bound",
                        "source": "confirmed_room",
                        "target_labels": [_row_label(row) for row in target_rows],
                    }
                )

    selection_error = _selection_error(
        selected_indices=selected_indices,
        target_rows=target_rows,
        candidate_rows=candidate_rows,
        inventory_rows=rows,
        pending_video=pending_video,
        current_selection_text=original_room_text,
        proof=proof,
        original_video_target_error=original_video_target_error,
    )
    if selection_error:
        binding.update(
            {
                "status": "error",
                "error_type": "selection_error",
                "reason": selection_error.get("reason") or "",
            }
        )
        return ToolResolverResult(
            target_rows=[],
            selection_error=selection_error,
            field_target_error={},
            missing_target_reason=str(selection_error.get("reason") or "selection_error"),
            candidate_binding=binding,
            inventory_rows_override=[],
            clear_inventory_rows=True,
            clear_media_outputs=True,
            selected_indices=selected_indices,
        )

    field_target_error: dict[str, Any] = {}
    original_video_target_binding: dict[str, Any] = {}
    if not target_rows and field_followup_requires_specific_room and not original_video_target_error:
        field_target_error = {
            "field": _field_followup_label(content),
            "reason": "missing_specific_room_for_field_followup",
            "candidate_count": len(candidate_rows),
            "candidate_labels": [_row_label(row) for row in candidate_rows[:10]],
        }
    elif not target_rows and media_target_error:
        field_target_error = media_target_error
    elif not target_rows and original_video_target_error:
        pending_labels = [
            str(label).strip()
            for label in (pending_video or {}).get("labels") or []
            if str(label).strip()
        ]
        pending_labels = list(dict.fromkeys(pending_labels))[:target_limit]
        field_target_error = {
            "field": "原视频",
            "reason": "original_video_followup_missing_stable_video_target",
            "candidate_count": 0,
            "candidate_labels": [],
            "pending_labels": pending_labels,
        }
        original_video_target_binding = {
            "stable": False,
            "reason": "previous_video_target_not_bound",
            "pending_labels": pending_labels,
        }

    if field_target_error:
        binding.update(
            {
                "status": "error",
                "error_type": "field_target_error",
                "reason": field_target_error.get("reason") or "",
            }
        )
        return ToolResolverResult(
            target_rows=[],
            selection_error={},
            field_target_error=field_target_error,
            missing_target_reason=str(field_target_error.get("reason") or "field_target_error"),
            candidate_binding=binding,
            inventory_rows_override=[],
            clear_inventory_rows=True,
            clear_media_outputs=False,
            pending_video_context_bound=pending_video_context_bound,
            original_video_target_binding=original_video_target_binding,
            selected_indices=selected_indices,
        )

    wants_bound_viewing_context = (
        "explain_unavailable_viewing" in actions
        and _content_wants_viewing(content)
        and _references_unbound_room_context(content)
    )
    if not target_rows and wants_bound_viewing_context:
        if candidate_rows:
            target_rows = candidate_rows[:10]
            binding.update(
                {
                    "status": "bound",
                    "source": "viewing_candidate_context",
                    "target_labels": [_row_label(row) for row in target_rows],
                }
            )

    explicit_room_refs = bool(proof.get("room_refs"))
    if (
        not target_rows
        and rows
        and not explicit_room_refs
        and not original_video_target_error
        and not selected_indices
        and any(action in actions for action in ("send_image", "send_video"))
    ):
        target_rows = rows[:target_limit]
        binding.update(
            {
                "status": "bound",
                "source": "media_inventory_rows",
                "target_labels": [_row_label(row) for row in target_rows],
            }
        )

    if target_rows and selected_indices:
        inventory_rows_override = list(target_rows)

    missing_target_reason = ""
    if not target_rows and any(action in actions for action in ("send_image", "send_video")):
        missing_target_reason = "media_target_unbound"
        binding.setdefault("reason", missing_target_reason)

    return ToolResolverResult(
        target_rows=target_rows,
        selection_error={},
        field_target_error={},
        missing_target_reason=missing_target_reason,
        candidate_binding=binding,
        inventory_rows_override=inventory_rows_override,
        clear_inventory_rows=False,
        clear_media_outputs=False,
        pending_video_context_bound=pending_video_context_bound,
        original_video_target_binding=original_video_target_binding,
        selected_indices=selected_indices,
    )


def resolve_target_rows(
    understanding: dict[str, Any],
    context: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    *,
    content: str = "",
    target_limit: int = DEFAULT_TARGET_LIMIT,
) -> list[dict[str, Any]]:
    return _target_rows_from_understanding(
        understanding if isinstance(understanding, dict) else {},
        context if isinstance(context, dict) else {},
        [row for row in inventory_rows or [] if isinstance(row, dict)],
        content=content,
        target_limit=target_limit,
    )


def selected_indices_from_understanding(
    understanding: dict[str, Any],
    query_text: str,
    *,
    target_limit: int = DEFAULT_TARGET_LIMIT,
) -> list[int]:
    return _selected_indices_from_understanding(
        understanding if isinstance(understanding, dict) else {},
        query_text,
        limit=target_limit,
    )


def has_explicit_candidate_selection(text: str) -> bool:
    return _has_explicit_candidate_selection(text)


def room_refs_from_text(text: str) -> list[str]:
    return _room_refs_from_text(text)


def _joined_text(*parts: Any) -> str:
    return " ".join(str(part).strip() for part in parts if str(part or "").strip())


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.append(number)
    return list(dict.fromkeys(numbers))


def _requested_room_count_from_text(text: str) -> int:
    value = str(text or "")
    for word, count in (
        ("前两套", 2),
        ("这两套", 2),
        ("那两套", 2),
        ("两套", 2),
        ("前2套", 2),
        ("这2套", 2),
        ("2套", 2),
        ("前3套", 3),
        ("前三套", 3),
        ("这三套", 3),
        ("三套", 3),
        ("3套", 3),
        ("四套", 4),
        ("4套", 4),
        ("五套", 5),
        ("5套", 5),
    ):
        if word in value:
            return count
    return 0


def _candidate_selection_count_from_text(text: str) -> int:
    value = str(text or "")
    for word, count in (
        ("前两套", 2),
        ("这两套", 2),
        ("那两套", 2),
        ("前2套", 2),
        ("这2套", 2),
        ("那2套", 2),
        ("前三套", 3),
        ("前3套", 3),
        ("这三套", 3),
        ("那三套", 3),
        ("这3套", 3),
        ("那3套", 3),
        ("前四套", 4),
        ("前4套", 4),
        ("这四套", 4),
        ("那四套", 4),
        ("这4套", 4),
        ("那4套", 4),
        ("前五套", 5),
        ("前5套", 5),
        ("这五套", 5),
        ("那五套", 5),
        ("这5套", 5),
        ("那5套", 5),
    ):
        if word in value:
            return count
    return 0


def _selection_indices_from_text(text: str, *, limit: int = DEFAULT_TARGET_LIMIT) -> list[int]:
    value = str(text or "")
    word_matches = []
    for word, index in (
        ("第一套", 1),
        ("第一个", 1),
        ("第二套", 2),
        ("第二个", 2),
        ("第三套", 3),
        ("第三个", 3),
        ("第四套", 4),
        ("第四个", 4),
        ("第五套", 5),
        ("第五个", 5),
    ):
        if word in value:
            word_matches.append(index)
    if word_matches:
        return list(dict.fromkeys(word_matches))[:limit]
    ordinal_numbers = [int(item) for item in re.findall(r"第\s*([1-9])\s*(?:套|个)?", value)]
    if ordinal_numbers:
        return list(dict.fromkeys(number for number in ordinal_numbers if number > 0))[:limit]
    numbers = [
        int(item)
        for item in re.findall(r"(?<!\d)([1-9])(?:\s*(?:和|跟|、|,|，)\s*|号?和)", value)
    ]
    trailing = re.findall(
        r"(?:和|跟|、|,|，)\s*([1-9])(?:\s*套|\s*个|\s*的?\s*(?:视频|图片|照片|素材)|$)",
        value,
    )
    numbers.extend(int(item) for item in trailing)
    if numbers:
        return list(dict.fromkeys(number for number in numbers if number > 0))[:limit]
    count = _candidate_selection_count_from_text(value)
    if count:
        return list(range(1, min(count, limit) + 1))
    return []


def _has_explicit_candidate_selection(text: str) -> bool:
    value = str(text or "")
    if _selection_indices_from_text(value):
        return True
    return any(
        word in value
        for word in (
            "第一套",
            "第二套",
            "第三套",
            "第四套",
            "第五套",
            "第一个",
            "第二个",
            "第三个",
            "第四个",
            "第五个",
            "前两套",
            "前三套",
            "这两套",
            "这三套",
            "那两套",
            "那三套",
            "1和",
            "2和",
            "3和",
            "1、",
            "2、",
            "3、",
        )
    )


def _candidate_numbers_from_llm1_packet(packet: Any, *, limit: int) -> list[int]:
    if not isinstance(packet, dict):
        return []
    trusted_candidates: list[int] = []
    metadata = dict(packet.get("legacy_unknown_fields") or {})
    for key in ("llm1_production", "llm1_shadow"):
        llm1_meta = metadata.get(key)
        if not isinstance(llm1_meta, dict):
            continue
        binding = llm1_meta.get("candidate_binding")
        if not isinstance(binding, dict):
            continue
        dropped = _int_list(binding.get("dropped_candidate_numbers"))
        status = str(binding.get("status") or "").strip().lower()
        selected = _int_list(binding.get("selected_candidate_numbers"))
        try:
            candidate_count = int(binding.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
        if status != "bound" or dropped or not selected or candidate_count <= 0:
            continue
        trusted_candidates.extend(selected)
    return list(dict.fromkeys(number for number in trusted_candidates if number > 0))[:limit]


def _llm1_selected_indices_from_understanding(understanding: dict[str, Any], *, limit: int) -> list[int]:
    structured_task = dict(understanding.get("structured_task") or {})
    for packet in (
        understanding.get("llm1_task_packet"),
        structured_task.get("llm1_task_packet"),
    ):
        candidates = _candidate_numbers_from_llm1_packet(packet, limit=limit)
        if candidates:
            return candidates
    return []


def _structured_selected_indices_from_understanding(understanding: dict[str, Any], *, limit: int) -> list[int]:
    proof = dict(understanding.get("constraint_proof") or {})
    structured_task = dict(understanding.get("structured_task") or {})
    candidates: list[int] = []
    for source in (structured_task, proof, understanding):
        for key in ("candidate_numbers", "selected_candidate_numbers", "selected_indices"):
            candidates.extend(_int_list(source.get(key)))
    return list(dict.fromkeys(number for number in candidates if number > 0))[:limit]


def _selection_text_allows_structured_expansion(query_text: str) -> bool:
    value = str(query_text or "")
    return bool(
        re.search(
            r"(?:第\s*)?[1-9]\s*(?:套|个)?\s*(?:和|跟|、|,|，)\s*(?:第\s*)?[1-9]",
            value,
        )
    )


def _merge_text_and_structured_selected_indices(
    *,
    query_text: str = "",
    text_selected: list[int],
    structured_selected: list[int],
    limit: int,
) -> list[int]:
    text_selected = list(dict.fromkeys(text_selected))[:limit]
    structured_selected = list(dict.fromkeys(structured_selected))[:limit]
    if not structured_selected:
        return text_selected
    if not text_selected:
        return structured_selected
    if text_selected == structured_selected:
        return structured_selected
    if all(index in structured_selected for index in text_selected):
        if _selection_text_allows_structured_expansion(query_text):
            return structured_selected
        return text_selected
    if all(index in text_selected for index in structured_selected):
        return text_selected
    return text_selected


def _selected_indices_from_understanding(
    understanding: dict[str, Any],
    query_text: str,
    *,
    limit: int,
) -> list[int]:
    proof = dict(understanding.get("constraint_proof") or {})
    if str(proof.get("pending_video_action") or "").lower() == "continue":
        return []
    llm1_selected = _llm1_selected_indices_from_understanding(understanding, limit=limit)
    text_selected = _selection_indices_from_text(query_text, limit=limit)
    if text_selected:
        return _merge_text_and_structured_selected_indices(
            query_text=query_text,
            text_selected=text_selected,
            structured_selected=(
                llm1_selected
                or _structured_selected_indices_from_understanding(understanding, limit=limit)
            ),
            limit=limit,
        )
    if llm1_selected:
        return llm1_selected
    if not _has_explicit_candidate_selection(query_text):
        return []
    return _merge_text_and_structured_selected_indices(
        query_text=query_text,
        text_selected=text_selected,
        structured_selected=_structured_selected_indices_from_understanding(understanding, limit=limit),
        limit=limit,
    )


def _explicit_selected_indices_from_understanding(
    understanding: dict[str, Any],
    query_text: str,
    *,
    limit: int,
) -> list[int]:
    text_selected = _selection_indices_from_text(query_text, limit=limit)
    if text_selected:
        return _merge_text_and_structured_selected_indices(
            query_text=query_text,
            text_selected=text_selected,
            structured_selected=(
                _llm1_selected_indices_from_understanding(understanding, limit=limit)
                or _structured_selected_indices_from_understanding(understanding, limit=limit)
            ),
            limit=limit,
        )
    return _llm1_selected_indices_from_understanding(understanding, limit=limit)


def _normalize_room_ref(value: str) -> str:
    text = str(value or "").lower().strip()
    text = text.replace("－", "-").replace("—", "-")
    return re.sub(r"[-\s]+", "", text)


def _room_refs_from_text(text: str) -> list[str]:
    refs = re.findall(r"\d+(?:[-－—][a-zA-Z0-9]+)+", str(text or ""))
    return list(
        dict.fromkeys(
            _normalize_room_ref(ref)
            for ref in refs
            if ref and not _looks_like_price_range_room_ref(ref)
        )
    )


def _looks_like_price_range_room_ref(ref: str) -> bool:
    parts = re.split(r"[-－—]", str(ref or "").strip())
    if len(parts) != 2:
        return False
    left, right = parts
    if not (left.isdigit() and right.isdigit()):
        return False
    if left == "0" and len(right) >= 3:
        return True
    return len(left) >= 3 and len(right) >= 3


def _row_value(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _row_label(row: dict[str, Any]) -> str:
    community = _row_value(row, ("小区", "小区名", "community", "社区", "楼盘"))
    room_no = _row_value(row, ("房号", "房间号", "room", "room_no", "门牌"))
    return f"{community}{room_no}".strip() or "这套房源"


def _candidate_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_set = context.get("last_candidate_set") or {}
    return [row for row in candidate_set.get("candidates") or [] if isinstance(row, dict)]


def _confirmed_row(context: dict[str, Any]) -> dict[str, Any]:
    confirmed = context.get("confirmed_room") or {}
    row = confirmed.get("row") if isinstance(confirmed, dict) else {}
    return row if isinstance(row, dict) else {}


def _normalize_intent(value: Any, fallback: str = "general") -> str:
    intent = str(value or "").strip()
    return intent or fallback


def _has_single_room_context_pronoun(text: str) -> bool:
    value = str(text or "")
    return any(
        phrase in value
        for phrase in (
            "这套",
            "这间",
            "这个房",
            "这个房源",
            "该房",
            "该房源",
            "那套",
            "上一套",
            "上一个",
            "上个",
            "上套",
            "刚才那套",
            "刚刚那套",
            "就这个",
            "就这套",
            "那个",
            "刚发的",
            "刚才发的",
            "刚刚发的",
            "它",
        )
    )


def _references_unbound_room_context(content: str) -> bool:
    text = str(content or "")
    return any(word in text for word in ("这几套", "这几间", "这些", "刚才", "上面", "前面", "里面"))


def _has_specific_room_context_reference(query_text: str) -> bool:
    text = str(query_text or "")
    return _has_single_room_context_pronoun(text) or _references_unbound_room_context(text)


def _content_wants_viewing(content: str) -> bool:
    return any(
        word in str(content or "")
        for word in (
            "密码",
            "看房",
            "今天看",
            "今天想看",
            "今天能看",
            "现在看",
            "自己看",
            "能自己看",
            "自助看",
            "直接看",
            "去看",
            "门口",
            "去门口",
            "怎么安排",
            "开门",
            "打不开",
            "门打不开",
            "空出",
            "空出来",
            "马上空出",
            "马上空出来",
            "比较急",
            "急着看",
            "急看",
            "什么时候能看",
            "能看吗",
        )
    )


def _field_followup_label(text: str) -> str:
    value = str(text or "")
    if any(word in value for word in ("水电", "水费", "电费")):
        return "水电"
    if any(word in value for word in ("密码", "看房", "今天看", "自己看", "自助", "开门", "打不开")):
        return "看房方式/密码"
    if any(word in value for word in ("押一付一", "押二付一", "多少钱", "价格", "租金")):
        return "价格"
    if any(word in value for word in ("户型", "装修", "特点")):
        return "户型"
    if any(word in value for word in ("图片", "照片", "视频")):
        return "素材"
    return "这个信息"


def _has_bound_room_field_followup(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if _room_refs_from_text(value):
        return False
    return any(
        word in value
        for word in (
            "密码",
            "看房",
            "今天看",
            "今天能看",
            "自己看",
            "自助",
            "开门",
            "打不开",
            "空出",
            "空出来",
            "水电",
            "水费",
            "电费",
            "押一付一",
            "押二付一",
            "多少钱",
            "价格",
            "租金",
            "户型",
            "装修",
            "特点",
            "图片",
            "照片",
            "视频",
        )
    )


def _field_followup_needs_specific_room(content: str, understanding: dict[str, Any]) -> bool:
    if not _has_bound_room_field_followup(content):
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    if _references_unbound_room_context(content):
        return False
    hard_constraints = dict(proof.get("hard_constraints") or {})
    if any(
        proof.get(key)
        for key in ("area", "communities", "room_refs", "budget_range", "layout", "selected_indices")
    ):
        return False
    return not any(
        bool(hard_constraints.get(key))
        for key in ("area", "community", "room_refs", "budget_range", "layout", "selected_indices")
    )


def _original_video_followup_without_explicit_target(content: str, understanding: dict[str, Any]) -> bool:
    proof = dict(understanding.get("constraint_proof") or {})
    if not proof.get("wants_original_video"):
        return False
    task = dict(understanding.get("structured_task") or {})
    query_text = _joined_text(
        content,
        task.get("original_text"),
        understanding.get("effective_query"),
        understanding.get("rewritten_query"),
    )
    if proof.get("room_refs") or _room_refs_from_text(query_text):
        return False
    return any(
        word in query_text
        for word in ("原视频", "原片", "高清", "源文件", "素材源", "下载链接", "太糊", "模糊", "糊", "清楚", "保存", "转发")
    )


def _looks_like_new_scoped_inventory_query(query_text: str, proof: dict[str, Any]) -> bool:
    text = str(query_text or "")
    if _has_specific_room_context_reference(text):
        return False
    if proof.get("room_refs") or _room_refs_from_text(text):
        return False
    has_new_scope = bool(
        proof.get("area")
        or proof.get("communities")
        or proof.get("budget_range")
        or proof.get("layout")
    )
    return has_new_scope and any(
        word in text
        for word in (
            "附近",
            "这边",
            "这块",
            "区域",
            "板块",
            "有哪些",
            "有没有",
            "还有哪些",
            "还有吗",
            "推荐",
            "预算",
            "左右",
            "上下",
            "以内",
            "以下",
            "两室",
            "一室",
            "单间",
            "整租",
        )
    )


def _should_bind_confirmed_room_context(understanding: dict[str, Any], query_text: str) -> bool:
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    original_text = str(task.get("original_text") or "")
    is_bound_field_followup = _has_bound_room_field_followup(original_text)
    if not is_bound_field_followup and _looks_like_new_scoped_inventory_query(query_text, proof):
        return False
    if not bool(understanding.get("context_reference")) and not is_bound_field_followup:
        return False
    if _has_specific_room_context_reference(query_text):
        return True
    requirements = dict(task.get("tool_requirements") or {})
    intent = _normalize_intent(understanding.get("intent"))
    if intent in {"media", "viewing"}:
        return True
    if is_bound_field_followup:
        return True
    return bool(
        proof.get("wants_video")
        or proof.get("wants_image")
        or requirements.get("needs_video")
        or requirements.get("needs_image")
        or requirements.get("needs_viewing_policy")
        or _content_wants_viewing(query_text)
    )


def _proof_community_norms(proof: dict[str, Any]) -> set[str]:
    return {
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if normalize_search_text(str(item))
    }


def _rows_matching_proof_communities(rows: list[dict[str, Any]], proof: dict[str, Any]) -> list[dict[str, Any]]:
    community_norms = _proof_community_norms(proof)
    if not community_norms:
        return list(rows)
    return [
        row
        for row in rows
        if normalize_search_text(_row_value(row, ("小区", "小区名", "社区", "楼盘"))) in community_norms
    ]


def _enforce_target_rows_community_constraints(
    target_rows: list[dict[str, Any]],
    inventory_rows: list[dict[str, Any]],
    proof: dict[str, Any],
) -> list[dict[str, Any]]:
    community_norms = _proof_community_norms(proof)
    if not target_rows or not community_norms:
        return target_rows
    matched_targets = _rows_matching_proof_communities(target_rows, proof)
    if len(matched_targets) == len(target_rows):
        return target_rows
    matched_inventory = _rows_matching_proof_communities(inventory_rows, proof)
    if matched_inventory:
        return matched_inventory[:DEFAULT_TARGET_LIMIT]
    return matched_targets


def _target_rows_from_understanding(
    understanding: dict[str, Any],
    context: dict[str, Any],
    search_rows: list[dict[str, Any]],
    *,
    content: str,
    target_limit: int,
) -> list[dict[str, Any]]:
    explicit_rows = [row for row in understanding.get("target_rows") or [] if isinstance(row, dict)]
    if explicit_rows:
        return explicit_rows

    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    query_text = _joined_text(
        content,
        task.get("original_text"),
        understanding.get("effective_query"),
        understanding.get("rewritten_query"),
    )
    current_text = _joined_text(content, task.get("original_text"), understanding.get("original_query"))
    current_turn_has_room_refs = bool(_room_refs_from_text(current_text))
    explicit_room_refs = bool(proof.get("room_refs") or _room_refs_from_text(query_text))
    wants_viewing = bool(
        requirements.get("needs_viewing_policy")
        or _normalize_intent(understanding.get("intent")) == "viewing"
        or _content_wants_viewing(query_text)
    )
    candidates = _candidate_rows(context)
    selected = _selected_indices_from_understanding(understanding, current_text or query_text, limit=target_limit)
    if not selected:
        matched_by_room_ref = _target_rows_from_room_refs(understanding, search_rows, content=content)
        if matched_by_room_ref:
            return matched_by_room_ref
        if explicit_room_refs:
            candidate_room_ref_rows = _candidate_rows_from_room_ref_hint(
                candidates=candidates,
                query_text=query_text,
                proof=proof,
            )
            if candidate_room_ref_rows:
                return candidate_room_ref_rows
            return []
    elif current_turn_has_room_refs:
        matched_by_room_ref = _rows_matching_original_room_refs(current_text, search_rows)
        if matched_by_room_ref:
            return matched_by_room_ref

    confirmed = _confirmed_row(context)
    if (
        confirmed
        and _should_bind_confirmed_room_context(understanding, query_text)
        and (
            _has_single_room_context_pronoun(query_text)
            or _has_bound_room_field_followup(str(task.get("original_text") or ""))
        )
        and not _has_explicit_candidate_selection(query_text)
    ):
        return [confirmed]

    proof_communities = _proof_community_norms(proof)
    current_text_norm = normalize_search_text(current_text)
    current_mentions_proof_community = bool(
        proof_communities
        and current_text_norm
        and any(community in current_text_norm for community in proof_communities)
    )
    if not selected and current_mentions_proof_community:
        explicit_selected = _explicit_selected_indices_from_understanding(
            understanding,
            current_text or query_text,
            limit=target_limit,
        )
        if explicit_selected:
            selected = explicit_selected
        elif _has_single_room_context_pronoun(current_text):
            selected = _structured_selected_indices_from_understanding(understanding, limit=target_limit)
    if selected and current_mentions_proof_community:
        if not search_rows:
            return []
        current_search_rows = [
            row
            for row in search_rows
            if normalize_search_text(_row_value(row, ("小区", "小区名"))) in proof_communities
        ]
        if not current_search_rows:
            return []
        if any(index > len(current_search_rows) for index in selected):
            return []
        return [
            current_search_rows[index - 1]
            for index in selected
            if 1 <= index <= len(current_search_rows)
        ]

    if selected:
        if not candidates:
            return []
        if any(index > len(candidates) for index in selected):
            return []
        return [
            candidates[index - 1]
            for index in selected
            if 1 <= index <= len(candidates)
        ]

    wants_media = bool(
        proof.get("wants_video")
        or proof.get("wants_image")
        or proof.get("wants_original_video")
    )
    if wants_media:
        recent_media_rows = _recent_assistant_mentioned_rows(
            context,
            [*search_rows, *candidates],
            query_text=query_text,
            target_limit=target_limit,
        )
        if recent_media_rows:
            return recent_media_rows

    candidate_hint_rows = _candidate_rows_from_context_hint(
        candidates=candidates,
        query_text=query_text,
        proof=proof,
        context_reference=bool(understanding.get("context_reference")),
    )
    if candidate_hint_rows:
        return candidate_hint_rows

    if (
        candidates
        and wants_media
        and (
            _references_unbound_room_context(str(task.get("original_text") or query_text))
            or (
                bool(understanding.get("context_reference"))
                and _media_request_targets_previous_candidates(str(task.get("original_text") or query_text))
            )
        )
    ):
        return candidates[:target_limit]

    if (
        candidates
        and bool(understanding.get("context_reference"))
        and wants_viewing
        and _references_unbound_room_context(query_text)
    ):
        return candidates[:10]

    wants_context_field_rows = bool(
        proof.get("wants_utilities")
        or requirements.get("needs_utilities")
        or wants_viewing
    )
    if candidates and wants_context_field_rows and _references_unbound_room_context(query_text):
        return candidates[:10]

    if (
        confirmed
        and _should_bind_confirmed_room_context(understanding, query_text)
        and not _has_explicit_candidate_selection(query_text)
    ):
        return [confirmed]

    if search_rows and wants_media:
        media_query_text = _joined_text(
            content,
            task.get("original_text"),
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
            proof.get("budget_label"),
        )
        requested_count = _requested_room_count_from_text(media_query_text)
        if requested_count:
            return search_rows[: min(requested_count, target_limit)]
        if any(word in media_query_text for word in ("最合适", "几套", "几间", "推荐")):
            return search_rows[: min(len(search_rows), target_limit)]

    if len(search_rows) == 1:
        return search_rows
    return []


def _media_request_targets_previous_candidates(query_text: str) -> bool:
    text = str(query_text or "")
    if not text:
        return False
    if _room_refs_from_text(text):
        return False
    return any(
        word in text
        for word in (
            "如果有",
            "有的话",
            "先发",
            "发给客户",
            "给客户看看",
            "客户看看",
            "发客户",
            "给客户筛",
        )
    )


def _target_rows_from_room_refs(
    understanding: dict[str, Any],
    search_rows: list[dict[str, Any]],
    *,
    content: str,
) -> list[dict[str, Any]]:
    task = dict(understanding.get("structured_task") or {})
    proof = dict(understanding.get("constraint_proof") or {})
    query_text = "\n".join(
        str(understanding.get(key) or "")
        for key in ("effective_query", "rewritten_query", "original_query")
    )
    query_text = "\n".join(
        part
        for part in (
            content,
            query_text,
            str(task.get("original_text") or ""),
            str(task.get("effective_query") or ""),
        )
        if str(part).strip()
    )
    refs = set(_room_refs_from_text(query_text))
    refs.update(_normalize_room_ref(ref) for ref in proof.get("room_refs") or [] if str(ref).strip())
    if not refs:
        return []
    matched: list[dict[str, Any]] = []
    for row in search_rows:
        room_no = _normalize_room_ref(_row_value(row, ("房号", "房间号", "门牌")))
        if room_no and room_no in refs:
            matched.append(row)
    return matched


def _rows_matching_original_room_refs(text: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = set(_room_refs_from_text(text))
    if not refs:
        return []
    matched: list[dict[str, Any]] = []
    for row in rows:
        room_no = _normalize_room_ref(_row_value(row, ("房号", "房间号", "门牌")))
        if room_no and room_no in refs:
            matched.append(row)
    if not matched:
        return []
    normalized_text = normalize_search_text(text)
    community_matched = [
        row
        for row in matched
        if normalize_search_text(_row_value(row, ("小区", "小区名", "社区", "楼盘")))
        and normalize_search_text(_row_value(row, ("小区", "小区名", "社区", "楼盘"))) in normalized_text
    ]
    return community_matched or matched


def _candidate_rows_from_context_hint(
    *,
    candidates: list[dict[str, Any]],
    query_text: str,
    proof: dict[str, Any],
    context_reference: bool,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    text = str(query_text or "")
    if not context_reference and not any(word in text for word in ("那个", "那套", "这套", "这个", "上一个", "上一套")):
        return []
    normalized_text = normalize_search_text(text)
    wants_all_candidates = any(word in text for word in ("都发", "全部", "都要", "全发", "都给我", "都发我"))
    proof_communities = [
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if str(item).strip()
    ]
    raw_mentions = _community_mentions(text)
    matches: list[dict[str, Any]] = []
    for row in candidates:
        community = _row_value(row, ("小区", "小区名", "社区", "楼盘"))
        normalized_community = normalize_search_text(community)
        if not normalized_community:
            continue
        matched = normalized_community in proof_communities
        if not matched and normalized_community in normalized_text:
            matched = True
        if not matched:
            matched = any(
                mention
                and (
                    mention in normalized_community
                    or normalized_community in mention
                    or fuzzy_contains_score(mention, community) >= 20
                )
                for mention in raw_mentions
            )
        if matched:
            matches.append(row)
    if not matches:
        return candidates[:10] if context_reference and wants_all_candidates else []
    matched_communities = {
        normalize_search_text(_row_value(row, ("小区", "小区名", "社区", "楼盘")))
        for row in matches
        if _row_value(row, ("小区", "小区名", "社区", "楼盘"))
    }
    if len(matches) == 1 or len(matched_communities) == 1 or wants_all_candidates:
        return matches
    return []


def _candidate_rows_from_room_ref_hint(
    *,
    candidates: list[dict[str, Any]],
    query_text: str,
    proof: dict[str, Any],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    refs = set(_room_refs_from_text(query_text))
    refs.update(_normalize_room_ref(ref) for ref in proof.get("room_refs") or [] if str(ref).strip())
    if not refs:
        return []
    normalized_text = normalize_search_text(query_text)
    proof_communities = [
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if str(item).strip()
    ]
    matches: list[dict[str, Any]] = []
    for row in candidates:
        room_no = _normalize_room_ref(_row_value(row, ("房号", "房间号", "门牌")))
        if not room_no or room_no not in refs:
            continue
        community = _row_value(row, ("小区", "小区名", "社区", "楼盘"))
        normalized_community = normalize_search_text(community)
        if proof_communities and normalized_community not in proof_communities:
            continue
        if not proof_communities and normalized_community and normalized_community not in normalized_text:
            same_room_candidates = [
                item
                for item in candidates
                if _normalize_room_ref(_row_value(item, ("房号", "房间号", "门牌"))) == room_no
            ]
            if len(same_room_candidates) > 1:
                continue
        matches.append(row)
    return matches if len(matches) == 1 else []


def _community_mentions(text: str) -> list[str]:
    candidates = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}(?:府|苑|园|湾|轩|居|寓|城|庭|里|庄|舍|阁|郡|府邸|家园)", str(text or ""))
    return [normalize_search_text(item) for item in candidates if normalize_search_text(item)]


def _recent_assistant_texts(context: dict[str, Any], *, limit: int = 10) -> list[str]:
    texts: list[str] = []
    raw_memory = context.get("structured_memory") if isinstance(context.get("structured_memory"), dict) else {}
    memory = kf_context_memory.normalize_structured_memory(context.get("structured_memory"))
    records = list(raw_memory.get("turn_records") or []) or list(memory.get("turn_records") or [])
    raw_dialog_context = list(raw_memory.get("raw_dialog_context") or []) or list(memory.get("raw_dialog_context") or [])
    for record in reversed(records):
        summary = dict(record.get("assistant_sent_summary") or {})
        final_reply = str(summary.get("final_reply") or "").strip()
        if final_reply:
            texts.append(final_reply)
        sent_rooms = [
            str(action.get("room") or "").strip()
            for action in summary.get("sent_actions") or []
            if isinstance(action, dict) and str(action.get("room") or "").strip()
        ]
        if sent_rooms:
            texts.append(" ".join(sent_rooms))
        if len(texts) >= limit:
            break
    if len(texts) < limit:
        for item in reversed(raw_dialog_context):
            if str(item.get("role") or "") != "assistant":
                continue
            item_content = str(item.get("content") or "").strip()
            if item_content:
                texts.append(item_content)
            if len(texts) >= limit:
                break
    return texts[:limit]


def _recent_sent_media_room_labels(
    context: dict[str, Any],
    *,
    media_type: str = "video",
    limit: int = 10,
) -> list[str]:
    labels: list[str] = []
    raw_memory = context.get("structured_memory") if isinstance(context.get("structured_memory"), dict) else {}
    memory = kf_context_memory.normalize_structured_memory(context.get("structured_memory"))
    records = list(raw_memory.get("turn_records") or []) or list(memory.get("turn_records") or [])
    for record in reversed(records):
        summary = dict(record.get("assistant_sent_summary") or {})
        for action in reversed(list(summary.get("sent_actions") or [])):
            if not isinstance(action, dict):
                continue
            if str(action.get("type") or "") != media_type:
                continue
            room = str(action.get("room") or "").strip()
            if room and room not in labels:
                labels.append(room)
        if len(labels) >= limit:
            break
    return labels[:limit]


def _recent_assistant_mentioned_rows(
    context: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    query_text: str,
    target_limit: int,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    query = str(query_text or "")
    if not any(word in query for word in ("视频", "图片", "照片", "素材", "原视频", "高清", "糊", "清楚", "源文件", "保存", "转发")):
        return []
    label_rows = [(_row_label(row), row) for row in rows if _row_label(row)]
    if any(word in query for word in ("原视频", "高清", "糊", "清楚", "源文件", "保存", "转发")):
        sent_video_labels = _recent_sent_media_room_labels(context, media_type="video")
        sent_matched = [
            row
            for label, row in label_rows
            if any(sent_label == label for sent_label in sent_video_labels)
        ]
        if sent_matched:
            return sent_matched[:target_limit]
    texts = _recent_assistant_texts(context)
    if not texts:
        return []
    matched: list[dict[str, Any]] = []
    for text in texts:
        for label, row in label_rows:
            if label and label in text and row not in matched:
                matched.append(row)
        if matched:
            return matched[:target_limit]
    return []


def _media_target_error_for_unclear_room(
    *,
    content: str,
    understanding: dict[str, Any],
    search_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    proof = dict(understanding.get("constraint_proof") or {})
    wants_media = bool(
        proof.get("wants_video")
        or proof.get("wants_image")
        or proof.get("wants_original_video")
    )
    if not wants_media or not search_rows:
        return {}

    task = dict(understanding.get("structured_task") or {})
    query_text = _joined_text(
        content,
        task.get("original_text"),
        understanding.get("effective_query"),
        understanding.get("rewritten_query"),
        proof.get("budget_label"),
    )
    if proof.get("room_refs") or _room_refs_from_text(query_text):
        return {}
    if _selected_indices_from_understanding(understanding, query_text, limit=DEFAULT_TARGET_LIMIT):
        return {}
    if _has_explicit_candidate_selection(query_text):
        return {}
    if _has_single_room_context_pronoun(query_text):
        return {}
    if _media_request_targets_previous_candidates(str(task.get("original_text") or query_text)):
        return {}
    requested_count = _requested_room_count_from_text(query_text)
    if requested_count or any(word in query_text for word in ("最合适", "推荐", "几套", "几间", "都发", "全部", "都要", "全发")):
        return {}

    proof_communities = _proof_community_norms(proof)
    if not proof_communities:
        return {}

    matched_rows = [
        row
        for row in search_rows
        if normalize_search_text(_row_value(row, ("小区", "小区名", "社区", "楼盘"))) in proof_communities
    ]
    if len(matched_rows) <= 1:
        return {}

    field = "视频" if proof.get("wants_video") or proof.get("wants_original_video") else "图片"
    return {
        "field": field,
        "reason": "community_media_request_missing_room_ref",
        "candidate_count": len(matched_rows),
        "candidate_labels": [_row_label(row) for row in matched_rows[:10]],
    }


def _selection_error(
    *,
    selected_indices: list[int],
    target_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    inventory_rows: list[dict[str, Any]],
    pending_video: dict[str, Any],
    current_selection_text: str,
    proof: dict[str, Any],
    original_video_target_error: bool,
) -> dict[str, Any]:
    if not selected_indices:
        return {}
    selection_has_direct_room_ref = bool(_room_refs_from_text(current_selection_text))
    selection_proof_communities = _proof_community_norms(proof)
    selection_current_text_norm = normalize_search_text(current_selection_text)
    selection_has_current_community_scope = bool(
        selection_proof_communities
        and selection_current_text_norm
        and any(community in selection_current_text_norm for community in selection_proof_communities)
    )
    selection_has_prior_context = bool(candidate_rows or pending_video)
    missing_candidate_selection_context = bool(
        selected_indices
        and not selection_has_prior_context
        and not selection_has_direct_room_ref
        and not original_video_target_error
    )
    invalid_candidate_selection = bool(
        selected_indices
        and candidate_rows
        and not target_rows
        and any(index > len(candidate_rows) for index in selected_indices)
    )
    invalid_search_selection = bool(
        selected_indices
        and not missing_candidate_selection_context
        and not candidate_rows
        and inventory_rows
        and any(index > len(inventory_rows) for index in selected_indices)
    )
    current_scope_selection_miss = bool(
        selected_indices
        and selection_has_current_community_scope
        and not selection_has_direct_room_ref
        and not target_rows
    )
    if current_scope_selection_miss:
        return {
            "requested_indices": selected_indices,
            "candidate_count": len(inventory_rows),
            "candidate_labels": [_row_label(row) for row in inventory_rows[:10]],
            "reason": "current_scope_selection_not_found",
        }
    if invalid_candidate_selection or invalid_search_selection:
        selection_rows = candidate_rows or inventory_rows
        return {
            "requested_indices": selected_indices,
            "candidate_count": len(selection_rows),
            "candidate_labels": [_row_label(row) for row in selection_rows[:10]],
            "reason": "requested_candidate_index_out_of_range",
        }
    if missing_candidate_selection_context:
        return {
            "requested_indices": selected_indices,
            "candidate_count": 0,
            "candidate_labels": [],
            "reason": "missing_current_candidate_set",
        }
    return {}


def _target_binding_source(
    *,
    target_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    inventory_rows: list[dict[str, Any]],
    selected_indices: list[int],
) -> str:
    if selected_indices and candidate_rows and all(row in candidate_rows for row in target_rows):
        return "candidate_set_selection"
    if selected_indices and inventory_rows and all(row in inventory_rows for row in target_rows):
        return "current_inventory_selection"
    if candidate_rows and all(row in candidate_rows for row in target_rows):
        return "candidate_context"
    if inventory_rows and all(row in inventory_rows for row in target_rows):
        return "current_inventory_rows"
    return "explicit_target_rows"
