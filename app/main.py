from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models import IncomingMessage
from app.services import (
    inventory_read_turn,
    inventory_sensitive_access,
    kf_agentic_rag,
    kf_context_memory,
    kf_dual_llm_production,
    kf_orchestrator_flow,
    kf_orchestrator_shadow,
    kf_send_receipts,
    kf_turn_flow,
)
from app.services.config_check import get_config_status
from app.services.feishu import FeishuClient
from app.services.fuzzy_match import (
    COMMUNITY_DISPLAY_ALIASES,
    canonical_community_display,
    fuzzy_contains_score,
    normalize_search_text,
)
from app.services.inventory import InventoryService
from app.services.inventory_image_sync import InventoryImageSyncer
from app.services.inventory_snapshot_shadow import run_inventory_snapshot_shadow
from app.services.inventory_read_models import (
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadError,
    assert_evidence_consistency,
)
from app.services.inventory_snapshot_models import is_safe_listing_id
from app.services.inventory_query import (
    parse_inventory_query,
    row_matches_hard_constraints,
    row_matches_price_range,
)
from app.services.kf_contracts import safe_artifact_payload
from app.services.llm import ReplyGenerator
from app.services.media_store import MediaStore
from app.services.region_inventory_sync import RegionInventorySyncService
from app.services.rewrite_inventory_index import (
    FIELD_SEMANTICS,
    load_rewrite_inventory_index,
    slice_rewrite_inventory_index,
    write_rewrite_inventory_index,
)
from app.services.wecom_kf import (
    WeComKfClient,
    WeComKfContextStore,
    WeComKfSendLimitError,
    extract_kf_external_userid,
    extract_kf_open_kfid,
    extract_kf_text,
    extract_kf_welcome_code,
    is_kf_enter_session_event,
    is_kf_message_event,
    kf_callback_payload_event_message,
    should_auto_reply_kf_message,
)

logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("room-robot")

app = FastAPI(title="寓你住一起客服 Agentic RAG")

Path("media").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory="media"), name="media")

inventory = InventoryService()
inventory_image_syncer = InventoryImageSyncer()
media_store = MediaStore()
reply_generator = ReplyGenerator()
agentic_rag = kf_agentic_rag.KfAgenticRagService(
    knowledge_dir=settings.kf_agentic_rag_knowledge_dir,
    enabled=settings.kf_agentic_rag_enabled,
    max_evidence=settings.kf_agentic_rag_max_evidence,
)
wecom_kf = WeComKfClient()
wecom_kf_context_store = WeComKfContextStore()

inventory_refresh_lock = asyncio.Lock()
inventory_image_refresh_lock = asyncio.Lock()
feishu_media_sync_lock = asyncio.Lock()
kf_turn_runtime_lock = asyncio.Lock()
kf_welcome_lock = asyncio.Lock()
kf_turn_tasks: dict[str, asyncio.Task[None]] = {}
kf_turn_generations: dict[str, int] = {}
kf_turn_pending_messages: dict[str, list[dict[str, Any]]] = {}

CONTACT_NUMBERS = ("18758141785", "13282125992", "19941091943")
KF_VIDEO_SEND_LIMIT = 5
KF_ON_DEMAND_MEDIA_SYNC_TIMEOUT_SECONDS = 1.2
KF_WELCOME_AUDIT_PATH = Path("data/wecom_kf_welcome_audit.jsonl")


def _mask_identifier(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _record_kf_welcome_audit(event: dict[str, Any]) -> None:
    try:
        KF_WELCOME_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            **event,
        }
        with KF_WELCOME_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("KF welcome audit write failed: %s", exc)


def _recent_kf_welcome_audits(limit: int = 30) -> list[dict[str, Any]]:
    if not KF_WELCOME_AUDIT_PATH.exists():
        return []
    try:
        lines = KF_WELCOME_AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        logger.warning("KF welcome audit read failed: %s", exc)
        return []
    result: list[dict[str, Any]] = []
    for line in lines[-max(limit, 1) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        result.append(item)
    return result


def _schedule_background_task(coro: Any, *, label: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)

    def _log_result(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.info("%s cancelled", label)
        except Exception as exc:
            logger.exception("%s failed: %s", label, exc)

    task.add_done_callback(_log_result)
    return task

AREA_ALIASES: dict[str, str] = {
    "万达": "拱墅万达\n北部软件园\n城北万象城",
    "拱墅万达": "拱墅万达\n北部软件园\n城北万象城",
    "北部软件园": "拱墅万达\n北部软件园\n城北万象城",
    "城北万象城": "拱墅万达\n北部软件园\n城北万象城",
    "新天地": "东新园\n杭氧\n新天地",
    "鑫天地": "东新园\n杭氧\n新天地",
    "新填地": "东新园\n杭氧\n新天地",
    "东新": "东新园\n杭氧\n新天地",
    "东新园": "东新园\n杭氧\n新天地",
    "杭氧": "东新园\n杭氧\n新天地",
    "石桥": "石桥街道\n华丰\n石桥\n永佳\n半山",
    "华丰": "石桥街道\n华丰\n石桥\n永佳\n半山",
    "永佳": "石桥街道\n华丰\n石桥\n永佳\n半山",
    "半山": "石桥街道\n华丰\n石桥\n永佳\n半山",
    "闸弄口": "闸弄口\n新塘\n元宝塘\n东站",
    "新塘": "闸弄口\n新塘\n元宝塘\n东站",
    "元宝塘": "闸弄口\n新塘\n元宝塘\n东站",
    "东站": "闸弄口\n新塘\n元宝塘\n东站",
}

TOOL_CATALOG: tuple[str, ...] = (
    "reference_confirmation",
    "context_tools",
    "send_contract_contact",
    "send_price_negotiation_contact",
    "send_deposit_policy",
    "send_inventory_sheet",
    "send_image",
    "send_video",
    "explain_missing_media",
    "explain_unavailable_viewing",
    "search_inventory",
    "showing_selection",
    "missing_inventory",
    "compact_listing",
    "generate_reply",
)


def _conversation_key(open_kfid: str, external_userid: str) -> str:
    return kf_context_memory.conversation_key(open_kfid, external_userid)


def _load_context(open_kfid: str, external_userid: str) -> dict[str, Any]:
    key = _conversation_key(open_kfid, external_userid)
    context = wecom_kf_context_store.get(key)
    if context:
        return context
    return kf_context_memory.empty_context()


def _save_context(open_kfid: str, external_userid: str, context: dict[str, Any]) -> None:
    wecom_kf_context_store.save(_conversation_key(open_kfid, external_userid), context)


def _kf_message_id(message: dict[str, Any]) -> str:
    return str(message.get("msgid") or message.get("msgid_v2") or "").strip()


def _kf_pending_message_item(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "msgid": _kf_message_id(message),
        "content": extract_kf_text(message),
        "created_at": time.time(),
    }


def _pending_message_ids(items: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("msgid") or "").strip()
        for item in items
        if str(item.get("msgid") or "").strip()
    ]


def _create_inventory_read_context(
    *,
    prefix: str,
    open_kfid: str,
    external_userid: str,
    content: str,
    msgids: list[str] | None = None,
    generation: int | str = "",
) -> InventoryReadContext:
    return inventory_read_turn.create_customer_inventory_read_context(
        prefix=prefix,
        open_kfid=open_kfid,
        external_userid=external_userid,
        content=content,
        inventory_service=inventory,
        rewrite_index_loader=load_rewrite_inventory_index,
        inventory_snapshot_mode=settings.inventory_snapshot_mode,
        msgids=msgids,
        generation=generation,
    )


def _local_inventory_read_context(scope: str = "local") -> InventoryReadContext:
    return inventory_read_turn.create_local_inventory_read_context(
        scope=scope,
        inventory_service=inventory,
        rewrite_index_loader=load_rewrite_inventory_index,
    )


def _remember_inventory_read_context(
    context: dict[str, Any],
    inventory_read_context: InventoryReadContext,
) -> dict[str, Any]:
    return inventory_read_turn.remember_context(context, inventory_read_context)


def _combined_pending_content(items: list[dict[str, Any]]) -> str:
    contents = [
        str(item.get("content") or "").strip()
        for item in items
        if str(item.get("content") or "").strip()
    ]
    if len(contents) <= 1:
        return contents[0] if contents else ""
    lines = [f"{index}. {content}" for index, content in enumerate(contents, start=1)]
    return "客户在机器人生成答案前连续补充了这些问题，请合并理解后一次回答：\n" + "\n".join(lines)


def _raise_if_stale_kf_turn(conversation_key: str, generation: int) -> None:
    if kf_turn_generations.get(conversation_key) != generation:
        raise asyncio.CancelledError()


async def _cleanup_kf_turn(conversation_key: str, generation: int) -> None:
    async with kf_turn_runtime_lock:
        if kf_turn_generations.get(conversation_key) != generation:
            return
        kf_turn_tasks.pop(conversation_key, None)
        kf_turn_pending_messages.pop(conversation_key, None)


async def _restart_kf_turn(
    *,
    open_kfid: str,
    external_userid: str,
    new_items: list[dict[str, Any]],
) -> None:
    conversation_key = _conversation_key(open_kfid, external_userid)
    async with kf_turn_runtime_lock:
        pending = list(kf_turn_pending_messages.get(conversation_key) or [])
        seen_msgids = {
            str(item.get("msgid") or "").strip()
            for item in pending
            if str(item.get("msgid") or "").strip()
        }
        for item in new_items:
            msgid = str(item.get("msgid") or "").strip()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if msgid and msgid in seen_msgids:
                continue
            pending.append(item)
            if msgid:
                seen_msgids.add(msgid)
        if not pending:
            return
        generation = kf_turn_generations.get(conversation_key, 0) + 1
        kf_turn_generations[conversation_key] = generation
        kf_turn_pending_messages[conversation_key] = pending
        previous_task = kf_turn_tasks.get(conversation_key)
        if previous_task and not previous_task.done():
            previous_task.cancel()
            logger.info("KF turn restarted with newer customer follow-up: %s", conversation_key)
        task = asyncio.create_task(
            _process_text_turn(
                open_kfid=open_kfid,
                external_userid=external_userid,
                pending_items=list(pending),
                generation=generation,
            )
        )
        kf_turn_tasks[conversation_key] = task

    try:
        await task
    except asyncio.CancelledError:
        return


async def _handle_text_messages_batch(messages: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for message in messages:
        if not should_auto_reply_kf_message(message):
            continue
        msgid = _kf_message_id(message)
        if msgid and wecom_kf.state_store.is_processed(msgid):
            continue
        open_kfid = str(message.get("open_kfid") or "").strip()
        external_userid = str(message.get("external_userid") or "").strip()
        item = _kf_pending_message_item(message)
        if not open_kfid or not external_userid or not item.get("content"):
            continue
        grouped.setdefault((open_kfid, external_userid), []).append(item)
    if not grouped:
        return
    tasks = [
        _restart_kf_turn(
            open_kfid=open_kfid,
            external_userid=external_userid,
            new_items=items,
        )
        for (open_kfid, external_userid), items in grouped.items()
    ]
    await asyncio.gather(*tasks)


def _conversation_text(context: dict[str, Any] | None, *, limit: int = 10) -> str:
    if not context:
        return ""
    labels = {"user": "客户", "assistant": "客服"}
    lines: list[str] = []
    for message in list(context.get("recent_messages") or [])[-limit:]:
        role = labels.get(str(message.get("role") or ""), str(message.get("role") or ""))
        content = str(message.get("content") or "").strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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


def _selection_indices_from_text(text: str) -> list[int]:
    value = str(text or "")
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
            return [index]
    ordinal_numbers = [int(item) for item in re.findall(r"第\s*([1-9])\s*(?:套|个)?", value)]
    if ordinal_numbers:
        return list(dict.fromkeys(number for number in ordinal_numbers if number > 0))[:KF_VIDEO_SEND_LIMIT]
    numbers = [
        int(item)
        for item in re.findall(r"(?<!\d)([1-9])(?:\s*(?:和|跟|、|,|，)\s*|号?和)", value)
    ]
    trailing = re.findall(r"(?:和|跟|、|,|，)\s*([1-9])(?:\s*套|\s*个|视频|图片|$)", value)
    numbers.extend(int(item) for item in trailing)
    if numbers:
        return list(dict.fromkeys(number for number in numbers if number > 0))[:KF_VIDEO_SEND_LIMIT]
    count = _candidate_selection_count_from_text(value)
    if count:
        return list(range(1, min(count, KF_VIDEO_SEND_LIMIT) + 1))
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


def _selected_indices_from_understanding(understanding: dict[str, Any], query_text: str) -> list[int]:
    proof = dict(understanding.get("constraint_proof") or {})
    if str(proof.get("pending_video_action") or "").lower() == "continue":
        return []
    text_selected = _selection_indices_from_text(query_text)
    if text_selected:
        return text_selected[:KF_VIDEO_SEND_LIMIT]
    if not _has_explicit_candidate_selection(query_text):
        return []
    selected = _int_list(understanding.get("selected_indices"))
    proof_selected = _int_list(proof.get("selected_indices"))
    structured_selected: list[int] = []
    for items in (selected, proof_selected):
        for index in items:
            if index not in structured_selected:
                structured_selected.append(index)
    if structured_selected:
        return structured_selected[:KF_VIDEO_SEND_LIMIT]
    return []


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


def _has_bound_room_field_followup(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if _room_refs_from_text(value):
        return False
    if _possible_community_mentions(value):
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


def _field_followup_needs_specific_room(content: str, understanding: dict[str, Any]) -> bool:
    if not _has_bound_room_field_followup(content):
        return False
    proof = dict(understanding.get("constraint_proof") or {})
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


def _safe_action_list(planner_result: dict[str, Any]) -> list[str]:
    actions = _string_list(planner_result.get("actions"))
    if planner_result.get("allow_all") and not actions:
        return ["search_inventory", "generate_reply"]
    return actions


def _orchestrator_tool_plan_from_understanding(understanding: dict[str, Any]) -> dict[str, Any]:
    return kf_orchestrator_flow.tool_plan_from_understanding(understanding)


def _dual_llm_production_enabled() -> bool:
    return kf_dual_llm_production.production_enabled(getattr(settings, "kf_dual_llm_mode", "shadow"))


async def _apply_llm1_production_task_packet(
    *,
    content: str,
    context: dict[str, Any],
    result: dict[str, Any],
    rewrite_view: dict[str, Any],
    inventory_index: dict[str, Any],
    inventory_read_context: InventoryReadContext,
) -> dict[str, Any]:
    if not _dual_llm_production_enabled():
        return result
    build_packet = getattr(reply_generator, "build_kf_task_packet", None)
    if not callable(build_packet):
        failure_plan = {
            "actions": [],
            "need_rewrite_clarification": True,
            "missing_evidence": "LLM1 production task packet builder is unavailable.",
            "source": "llm1_production_unavailable_gate",
            "reply_text": "",
        }
        result["tool_plan"] = failure_plan
        result.setdefault("structured_task", {})["tool_plan"] = failure_plan
        result["dual_llm_production"] = {
            "llm1": {"status": "retry", "source": "missing_llm1_builder"}
        }
        return result
    conversation_id = str(context.get("conversation_id") or inventory_read_context.request_id or "")
    turn_id = str(inventory_read_context.turn_id or "")
    candidate_set = rewrite_view.get("last_candidate_set") if isinstance(rewrite_view, dict) else {}
    candidate_set_id = str(candidate_set.get("candidate_set_id") or "") if isinstance(candidate_set, dict) else ""
    try:
        packet = await asyncio.wait_for(
            build_packet(
                content=content,
                raw_dialog_context=list(rewrite_view.get("raw_dialog_context") or []),
                structured_memory=rewrite_view,
                inventory_index=inventory_index,
                candidate_set=candidate_set if isinstance(candidate_set, dict) else {},
                legacy_rewrite=result,
                planner_feedback=result.get("planner_feedback") or {},
                conversation_id=conversation_id,
                turn_id=turn_id,
                case_id=str(inventory_read_context.decision_id or ""),
                inventory_snapshot_id=str(inventory_read_context.snapshot_id or ""),
                candidate_set_id=candidate_set_id,
                mode="production",
            ),
            timeout=8,
        )
        packet_payload = packet.to_safe_dict() if hasattr(packet, "to_safe_dict") else safe_artifact_payload(packet)
        if not isinstance(packet_payload, dict):
            packet_payload = {}
        tool_plan = kf_dual_llm_production.tool_plan_from_task_packet(packet)
    except Exception as exc:
        logger.exception("KF LLM1 production task packet failed: %s", exc)
        failure_plan = {
            "actions": [],
            "need_rewrite_clarification": True,
            "missing_evidence": "LLM1 production task packet failed; do not continue with customer-visible facts.",
            "source": "llm1_production_error_gate",
            "reply_text": "",
        }
        result["tool_plan"] = failure_plan
        result.setdefault("structured_task", {})["tool_plan"] = failure_plan
        result["dual_llm_production"] = {
            "llm1": {"status": "retry", "source": "llm1_production_error_gate", "error_type": type(exc).__name__}
        }
        return result
    result["llm1_task_packet"] = packet_payload
    result["tool_plan"] = tool_plan
    result.setdefault("structured_task", {})["llm1_task_packet"] = packet_payload
    result["structured_task"]["tool_plan"] = tool_plan
    result["dual_llm_production"] = {
        "llm1": {
            "status": "pass",
            "source": str(tool_plan.get("source") or "llm1_production_task_packet"),
            "task_count": len(packet_payload.get("tasks") or []),
            "action_count": len(tool_plan.get("actions") or []),
        }
    }
    return result


def _wants_continue_pending_video(content: str, understanding: dict[str, Any]) -> bool:
    text = str(content or "")
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    wants_video = bool(
        "视频" in text
        or proof.get("wants_video")
        or requirements.get("needs_video")
    )
    wants_continue = (
        _content_wants_pending_video_continue(text)
        or str(proof.get("pending_video_action") or "").lower() == "continue"
    )
    return wants_video and wants_continue


def _content_wants_pending_video_continue(content: str) -> bool:
    text = str(content or "")
    return any(
        word in text
        for word in (
            "继续",
            "剩下",
            "剩余",
            "补发",
            "后面的",
            "没发完",
            "发完",
            "能发的都发",
            "可以发的都发",
            "能发都发",
            "可发的都发",
            "都发",
            "发全",
            "全发",
            "不要超过",
            "不超过",
        )
    )


def _force_pending_video_continue_task(
    content: str,
    result: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    pending = kf_context_memory.pending_video_sends(context)
    if not pending or not _content_wants_pending_video_continue(content):
        return result

    normalized = dict(result)
    normalized["intent"] = "media"
    normalized["needs_clarification"] = False
    normalized["clarification_text"] = ""
    normalized["selected_indices"] = []
    pending_count = int(
        pending.get("requested_count")
        or len(pending.get("paths") or [])
        or len(pending.get("labels") or [])
        or 0
    )
    task_text = "继续发送上一轮未完成的视频素材。"
    if pending_count:
        task_text += f" 待处理数量约{pending_count}个。"
    if content.strip():
        task_text += f" 客户原话：{content.strip()}"
    normalized["rewritten_query"] = task_text
    normalized["effective_query"] = task_text

    query_state = dict(normalized.get("query_state") or {})
    query_state.pop("selected_indices", None)
    query_state.pop("room_refs", None)
    query_state.update(
        {
            "intent": "media",
            "wants_video": True,
            "pending_video_action": "continue",
        }
    )
    normalized["query_state"] = query_state

    constraint_proof = dict(normalized.get("constraint_proof") or {})
    constraint_proof.pop("selected_indices", None)
    constraint_proof.pop("room_refs", None)
    constraint_proof.update(
        {
            "intent": "media",
            "wants_video": True,
            "pending_video_action": "continue",
            "proof_status": "complete",
        }
    )
    normalized["constraint_proof"] = constraint_proof

    structured_task = dict(normalized.get("structured_task") or {})
    if structured_task:
        structured_task["intent"] = "media"
        structured_task["effective_query"] = task_text
        structured_task["query_state"] = query_state
        structured_task["constraint_proof"] = constraint_proof
        requirements = dict(structured_task.get("tool_requirements") or {})
        requirements.update(
            {
                "needs_inventory_search": False,
                "needs_video": True,
                "needs_image": False,
                "needs_viewing_policy": False,
                "needs_inventory_sheet": False,
            }
        )
        structured_task["tool_requirements"] = requirements
        structured_task["clarification"] = {
            "needed": False,
            "text": "",
            "reason": "pending_video_continue",
        }
        normalized["structured_task"] = structured_task

    return normalized


def _should_remember_candidate_set(
    *,
    content: str,
    understanding: dict[str, Any],
    rows: list[dict[str, Any]],
) -> bool:
    if not rows:
        return False
    intent = _normalize_intent(understanding.get("intent"), "inventory")
    if intent not in {"inventory", "general", "media"}:
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    text = " ".join(
        str(part).strip()
        for part in (
            content,
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
        )
        if str(part or "").strip()
    )
    if proof.get("wants_utilities"):
        return False
    if (proof.get("wants_video") or proof.get("wants_image")) and _has_single_room_context_pronoun(text):
        return False
    selected = _int_list(understanding.get("selected_indices")) or _int_list(proof.get("selected_indices"))
    if selected:
        return False
    if len(rows) > 1:
        return True
    if proof.get("room_refs") or parse_inventory_query(content).room_refs:
        return False
    if len(rows) == 1 and bool(understanding.get("context_reference")) and intent in {"inventory", "general"}:
        return True
    return any(word in text for word in ("有哪些", "推荐", "几套", "几间", "附近", "这边", "这块"))


def _should_clear_room_context_after_empty_inventory_search(
    *,
    content: str,
    understanding: dict[str, Any],
    actions: list[str],
) -> bool:
    if "search_inventory" not in actions:
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    query_text = " ".join(
        str(part).strip()
        for part in (
            content,
            task.get("original_text"),
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
            proof.get("budget_label"),
        )
        if str(part or "").strip()
    )
    if not query_text:
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    if (
        bool(understanding.get("context_reference"))
        and (proof.get("wants_video") or proof.get("wants_image"))
        and _media_request_targets_previous_candidates(str(task.get("original_text") or content))
    ):
        return False
    if _has_explicit_candidate_selection(query_text):
        return False
    if _has_bound_room_field_followup(content) or _has_single_room_context_pronoun(query_text):
        return False
    if _looks_like_new_scoped_inventory_query(query_text, proof):
        return True
    if _has_explicit_inventory_anchor(query_text):
        return True
    if requirements.get("needs_inventory_search") and any(
        word in query_text for word in ("有哪些", "还有", "有没有", "推荐", "预算", "左右", "以下", "以内")
    ):
        return True
    return False


def _fallback_actions_from_structured_task(understanding: dict[str, Any], signals: dict[str, Any]) -> list[str]:
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    actions: list[str] = []
    if signals.get("wants_inventory_sheet") or requirements.get("needs_inventory_sheet"):
        actions.append("send_inventory_sheet")
    if requirements.get("needs_deposit_policy"):
        actions.append("send_deposit_policy")
    if requirements.get("needs_viewing_policy"):
        actions.append("search_inventory")
        actions.append("context_tools")
        actions.append("explain_unavailable_viewing")
    if (
        requirements.get("needs_inventory_search")
        or requirements.get("needs_video")
        or requirements.get("needs_image")
        or requirements.get("needs_utilities")
    ):
        actions.append("search_inventory")
    if requirements.get("needs_video"):
        actions.append("context_tools")
        actions.append("send_video")
    if requirements.get("needs_image"):
        actions.append("context_tools")
        actions.append("send_image")
    if requirements.get("needs_contract_contact"):
        actions.append("send_contract_contact")
    if requirements.get("needs_price_contact"):
        actions.append("send_price_negotiation_contact")
    if "search_inventory" in actions:
        actions.append("compact_listing")
    if actions == ["send_inventory_sheet"]:
        return actions
    if (
        (signals.get("wants_deposit") or requirements.get("needs_deposit_policy"))
        and not requirements.get("needs_utilities")
        and actions == ["send_deposit_policy"]
    ):
        return ["send_deposit_policy", "generate_reply"]
    actions.append("generate_reply")
    return list(dict.fromkeys(actions))


def _ensure_required_actions(
    planner_result: dict[str, Any],
    understanding: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    result = dict(planner_result or {})
    actions = _safe_action_list(result)
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    intent = _normalize_intent(understanding.get("intent"))
    needs_video = bool(signals.get("wants_video") or proof.get("wants_video") or requirements.get("needs_video"))
    needs_image = bool(signals.get("wants_image") or proof.get("wants_image") or requirements.get("needs_image"))
    needs_viewing = bool(signals.get("wants_viewing") or requirements.get("needs_viewing_policy") or intent == "viewing")

    if signals.get("wants_inventory_sheet") or proof.get("wants_inventory_sheet") or requirements.get("needs_inventory_sheet") or intent == "inventory_sheet":
        if "send_inventory_sheet" not in actions:
            actions.insert(0, "send_inventory_sheet")
        result["need_rewrite_clarification"] = False
        result["source"] = f"{result.get('source') or 'planner'}+deterministic_inventory_sheet"
        if not (needs_video or needs_image or needs_viewing):
            result["actions"] = ["send_inventory_sheet"]
            return result

    if signals.get("wants_deposit") or requirements.get("needs_deposit_policy") or intent == "deposit":
        needs_utilities = bool(
            signals.get("wants_utilities")
            or proof.get("wants_utilities")
            or requirements.get("needs_utilities")
        )
        if needs_utilities:
            if "send_deposit_policy" not in actions:
                actions.append("send_deposit_policy")
            if "search_inventory" not in actions:
                actions.append("search_inventory")
            if "context_tools" not in actions:
                actions.append("context_tools")
            if "generate_reply" not in actions:
                actions.append("generate_reply")
            result["actions"] = list(dict.fromkeys(actions))
            result["need_rewrite_clarification"] = False
            result["source"] = f"{result.get('source') or 'planner'}+deterministic_deposit_utilities"
            return result
        result["actions"] = ["send_deposit_policy", "generate_reply"]
        result["need_rewrite_clarification"] = False
        result["source"] = f"{result.get('source') or 'planner'}+deterministic_deposit"
        return result

    if signals.get("wants_contract_contact") or requirements.get("needs_contract_contact") or intent == "contract":
        result["actions"] = ["send_contract_contact", "generate_reply"]
        result["need_rewrite_clarification"] = False
        result["source"] = f"{result.get('source') or 'planner'}+deterministic_contract"
        return result

    if needs_viewing:
        for action in ("search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"):
            if action not in actions:
                actions.append(action)

    if needs_video or needs_image:
        if "search_inventory" not in actions:
            actions.insert(0, "search_inventory")
        if "context_tools" not in actions:
            actions.append("context_tools")
        if needs_video and "send_video" not in actions:
            actions.append("send_video")
        if needs_image and "send_image" not in actions:
            actions.append("send_image")
        if "explain_missing_media" not in actions:
            actions.append("explain_missing_media")
        if "generate_reply" not in actions:
            actions.append("generate_reply")

    removable_when_unasked = {
        "send_inventory_sheet": bool(
            signals.get("wants_inventory_sheet")
            or proof.get("wants_inventory_sheet")
            or requirements.get("needs_inventory_sheet")
            or intent == "inventory_sheet"
        ),
        "send_deposit_policy": bool(signals.get("wants_deposit") or requirements.get("needs_deposit_policy") or intent == "deposit"),
        "explain_unavailable_viewing": needs_viewing,
        "send_contract_contact": bool(signals.get("wants_contract_contact") or requirements.get("needs_contract_contact") or intent == "contract"),
        "send_video": needs_video,
        "send_image": needs_image,
        "explain_missing_media": bool(needs_video or needs_image),
    }
    filtered_actions = [
        action
        for action in actions
        if action not in removable_when_unasked or removable_when_unasked[action]
    ]
    if filtered_actions != actions:
        actions = filtered_actions
        result["source"] = f"{result.get('source') or 'planner'}+unrequested_actions_removed"

    if intent == "inventory" or requirements.get("needs_inventory_search"):
        if "search_inventory" not in actions:
            actions.insert(0, "search_inventory")
        if "compact_listing" not in actions:
            actions.append("compact_listing")
        if "generate_reply" not in actions:
            actions.append("generate_reply")

    if actions != _safe_action_list(result):
        result["actions"] = list(dict.fromkeys(actions))
        result["source"] = f"{result.get('source') or 'planner'}+deterministic_actions"
    return result


def _planner_reply_text(result: dict[str, Any]) -> str:
    return str(
        result.get("reply")
        or result.get("reply_text")
        or result.get("final_reply")
        or ""
    ).strip()


def _ensure_planner_action_contract(
    planner_result: dict[str, Any],
    understanding: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    result = dict(planner_result or {})
    if result.get("need_rewrite_clarification"):
        result["reply_text"] = ""
        if not str(result.get("missing_evidence") or "").strip():
            result["missing_evidence"] = "Planner 证据不足，需要问题重写/意图分析重新绑定目标。"
        return result

    actions = _safe_action_list(result)
    if not actions:
        result["need_rewrite_clarification"] = True
        result["missing_evidence"] = "Planner 没有输出工具动作。"
        result["reply_text"] = ""
        result["source"] = f"{result.get('source') or 'planner'}+missing_action_contract"
        return result

    pre_tool_reply = _planner_reply_text(result)
    if pre_tool_reply:
        result["pre_tool_reply_text"] = pre_tool_reply
    result["reply_text"] = ""
    result.pop("reply", None)
    result.pop("final_reply", None)
    result.pop("planner_missing_reply", None)
    result["source"] = f"{result.get('source') or 'planner'}+action_contract"
    return result


def _normalize_intent(value: Any, fallback: str = "general") -> str:
    intent = str(value or "").strip()
    return intent or fallback


def _content_wants_inventory_sheet(content: str) -> bool:
    text = content.strip()
    if any(word in text for word in ("房源表", "空房表", "库存表", "在租表", "房态表")):
        return True
    return bool(
        re.search(r"表(?:发|给|看|来|传|截|拍)(?:我|一下|下|个|一份|张|份|吗|吧|哈|呗)?", text)
        or re.search(r"发(?:我|一下|下|个|一份|张|份|最新)(?:房源)?表", text)
    )


def _content_wants_deposit(content: str) -> bool:
    return any(word in content for word in ("免押", "无忧住", "芝麻", "免押金", "服务费"))


def _content_wants_utilities(content: str) -> bool:
    return any(word in content for word in ("水电", "水费", "电费", "水电费", "民用水电"))


def _content_wants_price(content: str) -> bool:
    return any(word in content for word in ("价格", "多少钱", "租金", "月租", "押一付一", "押二付一", "多少一月"))


def _content_wants_password(content: str) -> bool:
    return any(word in content for word in ("密码", "门锁码", "开门码", "门禁码"))


def _understanding_wants_utilities(understanding: dict[str, Any], *, content: str = "") -> bool:
    proof = dict(understanding.get("constraint_proof") or {})
    query_state = dict(understanding.get("query_state") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    return bool(
        _content_wants_utilities(content)
        or proof.get("wants_utilities")
        or query_state.get("wants_utilities")
        or requirements.get("needs_utilities")
    )


def _understanding_wants_price(understanding: dict[str, Any], *, content: str = "") -> bool:
    proof = dict(understanding.get("constraint_proof") or {})
    query_state = dict(understanding.get("query_state") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    return bool(
        _content_wants_price(content)
        or proof.get("wants_price")
        or query_state.get("wants_price")
        or requirements.get("needs_price")
    )


def _content_wants_viewing(content: str) -> bool:
    return any(
        word in content
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


def _deterministic_signals(content: str) -> dict[str, Any]:
    wants_original_video = any(word in content for word in ("原视频", "原片", "高清", "源文件", "下载链接", "太糊", "模糊", "保存", "转发"))
    return {
        "wants_inventory_sheet": _content_wants_inventory_sheet(content),
        "wants_video": any(word in content for word in ("视频", "实拍", "笔记")),
        "wants_original_video": wants_original_video,
        "wants_image": any(word in content for word in ("图片", "照片", "实拍图", "房间图")),
        "wants_contract_contact": any(word in content for word in ("合同", "签约", "定金", "订金", "订房", "定房")),
        "wants_price_contact": any(word in content for word in ("最低价", "优惠", "便宜点", "砍价")),
        "wants_deposit": _content_wants_deposit(content),
        "wants_utilities": _content_wants_utilities(content),
        "wants_viewing": _content_wants_viewing(content),
        "is_greeting": content.strip() in {"你好", "您好", "在吗", "在不在", "有人吗"},
    }


def _fallback_understanding(content: str, signals: dict[str, Any]) -> dict[str, Any]:
    intent = "inventory_sheet" if signals.get("wants_inventory_sheet") else "general"
    if signals.get("wants_video") or signals.get("wants_image"):
        intent = "media"
    if signals.get("is_greeting"):
        intent = "greeting"
    return {
        "rewritten_query": content,
        "effective_query": content,
        "query_state": {"intent": intent, **signals},
        "intent": intent,
        "intent_confidence": 0.5,
        "context_reference": False,
        "candidate_action": "none",
        "selected_indices": [],
        "needs_clarification": False,
        "clarification_text": "",
    }


def _state_from_understanding(understanding: dict[str, Any]) -> dict[str, Any]:
    query_state = dict(understanding.get("query_state") or {})
    state = {
        "intent": _normalize_intent(understanding.get("intent")),
        "effective_query": str(
            understanding.get("effective_query")
            or understanding.get("rewritten_query")
            or ""
        ).strip(),
        "rewritten_query": str(understanding.get("rewritten_query") or "").strip(),
        "query_state": query_state,
        "selected_indices": _int_list(understanding.get("selected_indices")),
        "needs_clarification": bool(understanding.get("needs_clarification")),
        "pending_video_action": str(query_state.get("pending_video_action") or "").strip(),
    }
    if understanding.get("structured_task"):
        state["structured_task"] = understanding["structured_task"]
    if understanding.get("entity_resolution"):
        state["entity_resolution"] = understanding["entity_resolution"]
    if understanding.get("constraint_proof"):
        state["constraint_proof"] = understanding["constraint_proof"]
    for key in ("area", "budget", "layout", "media_kind"):
        if query_state.get(key):
            state[key] = query_state[key]
    return state


async def _refresh_inventory() -> dict[str, Any]:
    async with inventory_refresh_lock:
        frame = await inventory.refresh()
    rows = frame.fillna("").to_dict(orient="records") if hasattr(frame, "fillna") else []
    index_result = _write_rewrite_inventory_index(rows)
    if index_result.get("ok"):
        shadow_result = run_inventory_snapshot_shadow(
            legacy_rows=rows,
            source_kind="admin_inventory_refresh",
            source_version=str(index_result.get("signature") or inventory.cache_meta.get("hash") or ""),
            cache_meta=inventory.cache_meta,
            legacy_rewrite_index_path=settings.rewrite_inventory_index_path,
            sync_run_id=f"admin_inventory_refresh:{time.time_ns()}",
        )
    else:
        shadow_result = {
            "ok": False,
            "mode": settings.inventory_snapshot_mode,
            "status": "skipped",
            "error_code": "legacy_rewrite_index_failed",
        }
    return {
        "ok": True,
        "rows": int(len(frame)),
        "rewrite_index": index_result,
        "inventory_snapshot_shadow": shadow_result,
    }


def _write_rewrite_inventory_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        index = write_rewrite_inventory_index(
            rows,
            area_aliases=AREA_ALIASES,
            cache_meta=_inventory_cache_meta_for_prompt(),
        )
        return {
            "ok": True,
            "path": str(settings.rewrite_inventory_index_path),
            "row_count": index.get("row_count", 0),
            "signature": index.get("signature", ""),
        }
    except Exception as exc:
        logger.exception("rewrite inventory index generation failed: %s", exc)
        return {"ok": False, "error": str(exc), "path": str(settings.rewrite_inventory_index_path)}


async def _refresh_inventory_images(*, force: bool = False) -> dict[str, Any]:
    async with inventory_image_refresh_lock:
        return await inventory_image_syncer.refresh_if_changed(force=force)


async def _sync_feishu_media(*, force: bool = False) -> dict[str, Any]:
    async with feishu_media_sync_lock:
        return await FeishuClient().sync_all_media()


def _current_inventory_images() -> list[Path]:
    paths = sorted(Path().glob(settings.inventory_image_glob))
    if not paths and settings.inventory_image_path.exists():
        paths = [settings.inventory_image_path]
    if not paths:
        paths = sorted(Path("room_database").glob("inventory_*_original.png"))
    return [path for path in paths if path.exists()]


async def _refresh_current_inventory_images_for_sheet() -> Any:
    return await _refresh_inventory_images(force=False)


def _row_value(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _row_label(row: dict[str, Any]) -> str:
    community = _row_value(row, ("小区", "小区名", "community"))
    room_no = _row_value(row, ("房号", "房间号", "room", "room_no"))
    return f"{community}{room_no}".strip() or "这套房源"


def _row_listing_id(row: dict[str, Any]) -> str:
    for key in ("listing_id", "listingId", "房源ID", "房源编号"):
        value = str(row.get(key) or "").strip()
        if value and is_safe_listing_id(value):
            return value
    listing_id = inventory_sensitive_access.legacy_listing_id_for_row(row)
    return listing_id if is_safe_listing_id(listing_id) else ""


def _row_with_listing_id(row: dict[str, Any]) -> dict[str, Any]:
    listing_id = _row_listing_id(row)
    if not listing_id:
        return row
    if str(row.get("listing_id") or "").strip() == listing_id:
        return row
    enriched = dict(row)
    enriched["listing_id"] = listing_id
    return enriched


def _rows_with_listing_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_row_with_listing_id(row) for row in rows if isinstance(row, dict)]


def _normalized_room_label(row: dict[str, Any]) -> str:
    return normalize_search_text(_row_label(row))


def _displayed_candidate_rows_from_reply(
    reply: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return rows in the same numbered order the customer actually saw."""
    if not reply or not rows:
        return []
    by_label = {
        _normalized_room_label(row): row
        for row in rows
        if _normalized_room_label(row)
    }
    by_room: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        room_no = normalize_search_text(_row_value(row, ("房号", "房间号", "room", "room_no")))
        if room_no:
            by_room.setdefault(room_no, []).append(row)

    found: list[tuple[int, int, dict[str, Any]]] = []
    seen_labels: set[str] = set()
    pattern = re.compile(r"(?m)^\s*(\d{1,2})[\.、)]\s*([^\n，,。；;]+)")
    for match in pattern.finditer(reply):
        try:
            display_index = int(match.group(1))
        except ValueError:
            continue
        text = match.group(2).strip()
        normalized_text = normalize_search_text(text)
        if not normalized_text:
            continue

        matched_row: dict[str, Any] | None = None
        matched_label = ""
        for label, row in by_label.items():
            if label and (label in normalized_text or normalized_text in label):
                matched_row = row
                matched_label = label
                break
        if matched_row is None:
            for room_no, room_rows in by_room.items():
                if room_no and room_no in normalized_text and len(room_rows) == 1:
                    matched_row = room_rows[0]
                    matched_label = _normalized_room_label(matched_row)
                    break
        if matched_row is None or not matched_label or matched_label in seen_labels:
            continue
        seen_labels.add(matched_label)
        found.append((display_index, match.start(), matched_row))

    if not found:
        return []
    found.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in found]


def _reconcile_last_candidate_set_with_visible_reply(
    context: dict[str, Any],
    final_reply: str,
    tool_evidence: dict[str, Any],
) -> dict[str, Any]:
    candidate_set = kf_context_memory.normalize_last_candidate_set(
        context.get("last_candidate_set")
    )
    if not candidate_set:
        return context
    source_rows = [
        row for row in (
            tool_evidence.get("inventory_rows")
            or candidate_set.get("candidates")
            or []
        )
        if isinstance(row, dict)
    ]
    displayed_rows = _displayed_candidate_rows_from_reply(final_reply, source_rows)
    if not displayed_rows:
        return context
    context["last_candidate_set"] = {
        **candidate_set,
        "candidates": displayed_rows[:10],
        "shown_count": len(displayed_rows[:10]),
        "total_count": len(displayed_rows),
        "displayed_summary": _normalize_customer_visible_reply_text_before_selfcheck(final_reply),
        "created_at": time.time(),
    }
    return context


def _area_alias_hits(text: str) -> list[dict[str, str]]:
    normalized = normalize_search_text(text)
    hits: list[dict[str, str]] = []
    for alias, canonical in AREA_ALIASES.items():
        if normalize_search_text(alias) in normalized:
            hits.append(
                {
                    "raw_text": alias,
                    "canonical": canonical,
                    "status": "resolved",
                    "confidence": "high",
                    "reason": "area_alias",
                }
            )
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        key = f"{hit['raw_text']}->{hit['canonical']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def _community_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = _row_value(row, ("小区", "社区", "楼盘", "小区名"))
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _community_alias_hits(text: str, communities: list[str]) -> list[dict[str, Any]]:
    normalized = normalize_search_text(text)
    community_set = set(communities)
    hits: list[dict[str, Any]] = []
    for raw, canonical in COMMUNITY_DISPLAY_ALIASES.items():
        if normalize_search_text(raw) in normalized and canonical in community_set:
            hits.append(
                {
                    "raw_text": raw,
                    "canonical": canonical,
                    "status": "resolved",
                    "confidence": "high",
                    "reason": "configured_community_alias",
                }
            )
    for community in communities:
        if normalize_search_text(community) in normalized:
            hits.append(
                {
                    "raw_text": community,
                    "canonical": community,
                    "status": "resolved",
                    "confidence": "exact",
                    "reason": "exact_community",
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        key = f"{hit['raw_text']}->{hit['canonical']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def _possible_community_mentions(text: str) -> list[str]:
    parsed = parse_inventory_query(text)
    mentions: list[str] = []
    for match in re.findall(r"[一-鿿]{2,8}(?:府|苑|园|城|湾|都|邸|庭|府邸)", text):
        match = _clean_community_mention_candidate(match)
        if not _looks_like_possible_community_mention(match):
            continue
        mentions.append(match)
    mentions.extend(
        _clean_community_mention_candidate(str(term))
        for term in parsed.anchor_terms
        if _looks_like_possible_community_mention(_clean_community_mention_candidate(str(term)))
    )
    return list(dict.fromkeys(mentions))


def _clean_community_mention_candidate(value: str) -> str:
    text = str(value or "").strip(" ，,。；;：:？?！!")
    if not text:
        return ""
    text = re.sub(
        r"^(?:客户|租客)?(?:又问|再问|问下|问一下|问|想问|咨询|说|要问|在问)",
        "",
        text,
    )
    text = re.sub(r"^(?:客户|租客)(?:又|再)?", "", text)
    text = re.sub(r"^(?:这个|那个|这边|那边|这|那)?小区", "", text)
    text = re.sub(r"^(?:你说的是|说的是|是|换成|改成)", "", text)
    return text.strip(" ，,。；;：:？?！!")


def _looks_like_area_alias_mention(mention: str, area_hits: list[dict[str, Any]]) -> bool:
    normalized = normalize_search_text(mention)
    if not normalized:
        return False
    for hit in area_hits:
        raw = normalize_search_text(str(hit.get("raw_text") or ""))
        canonical = normalize_search_text(str(hit.get("canonical") or ""))
        if normalized == raw:
            return True
        if canonical and normalized in canonical:
            return True
    return False


def _query_state_communities(query_state: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("community", "communities", "小区"):
        value = query_state.get(key)
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            values.append(value.strip())
    return list(dict.fromkeys(values))


def _strip_llm_inferred_community_for_area_alias(
    *,
    content: str,
    result: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    query_state = dict(result.get("query_state") or {})
    proof = dict(result.get("constraint_proof") or {})
    area_scope_present = bool(
        _area_alias_hits(content)
        or query_state.get("area")
        or query_state.get("areas")
        or proof.get("area")
        or proof.get("areas")
    )
    if not area_scope_present or not _area_query_context_is_clear(content):
        return result
    proof_communities = [
        str(item).strip()
        for item in proof.get("communities") or []
        if str(item).strip()
    ]
    communities = list(dict.fromkeys([*_query_state_communities(query_state), *proof_communities]))
    if not communities:
        return result
    known_communities = set(_community_names(rows))
    normalized_content = normalize_search_text(content)
    inferred = [
        community
        for community in communities
        if community in known_communities
        and normalize_search_text(community) not in normalized_content
    ]
    if not inferred:
        return result

    updated = dict(result)
    updated_query_state = dict(query_state)
    for key in ("community", "communities", "小区"):
        value = updated_query_state.get(key)
        if isinstance(value, list):
            kept = [item for item in value if str(item).strip() not in inferred]
            if kept:
                updated_query_state[key] = kept
            else:
                updated_query_state.pop(key, None)
        elif str(value or "").strip() in inferred:
            updated_query_state.pop(key, None)
    updated["query_state"] = updated_query_state

    if proof:
        updated_proof = dict(proof)
        kept_proof_communities = [
            item
            for item in proof.get("communities") or []
            if str(item).strip() not in inferred
        ]
        if kept_proof_communities:
            updated_proof["communities"] = kept_proof_communities
        else:
            updated_proof.pop("communities", None)
        hard_constraints = dict(updated_proof.get("hard_constraints") or {})
        if not kept_proof_communities and "community" in hard_constraints:
            hard_constraints["community"] = False
        if hard_constraints:
            updated_proof["hard_constraints"] = hard_constraints
        updated["constraint_proof"] = updated_proof
        structured_task = dict(updated.get("structured_task") or {})
        if structured_task:
            structured_task["constraint_proof"] = updated_proof
            updated["structured_task"] = structured_task

    for key in ("rewritten_query", "effective_query"):
        text = str(updated.get(key) or "")
        if not text:
            continue
        cleaned = text
        for community in inferred:
            cleaned = cleaned.replace(community, "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,、")
        updated[key] = cleaned or content
    updated["area_alias_community_stripped"] = {
        "reason": "area_alias_query_not_specific_community",
        "removed_communities": inferred,
    }
    return updated


def _looks_like_possible_community_mention(value: str) -> bool:
    text = _clean_community_mention_candidate(str(value or ""))
    if not 2 <= len(text) <= 8:
        return False
    if text in AREA_ALIASES:
        return False
    normalized = normalize_search_text(text)
    blocked_exact = {
        "前两套",
        "前三套",
        "第一套",
        "第二套",
        "第三套",
        "这两套",
        "这几套",
        "这个",
        "这套",
        "那套",
        "视频",
        "图片",
        "原视频",
        "高清",
        "清楚",
        "水电",
        "水电怎么收",
        "密码",
        "密码多少",
        "今天能看",
        "怎么安排",
        "怎么定",
        "带厅",
        "也算",
        "和都",
        "都发",
        "筛一下",
        "看中最低",
        "哪个更低",
        "更低",
        "最低",
        "是多少",
        "多少",
        "多少钱",
        "是民用",
        "怎么回",
        "怎么回复",
        "怎么办",
        "能不能",
        "自己看",
        "约看",
        "空出来",
        "客户问",
        "押一付一",
        "押二付一",
        "和分别",
        "和一起",
        "分别",
        "怎么算",
    }
    if normalized in {normalize_search_text(item) for item in blocked_exact}:
        return False
    blocked_fragments = (
        "前两套",
        "前三套",
        "第一个",
        "第一套",
        "第二套",
        "第三套",
        "视频",
        "图片",
        "原视频",
        "水电",
        "密码",
        "怎么收",
        "怎么安排",
        "怎么定",
        "能发",
        "可发",
        "发的都",
        "都发",
        "不要超过",
        "不超过",
        "筛一下",
        "带厅",
        "也算",
        "看中",
        "更低",
        "最低",
        "联系谁",
        "今天",
        "是多少",
        "多少",
        "多少钱",
        "是民用",
        "怎么回",
        "怎么回复",
        "怎么办",
        "能不能",
        "自己看",
        "约看",
        "空出来",
        "客户问",
        "押一付",
        "押二付",
        "分别",
        "一起",
        "怎么算",
    )
    if any(fragment in text for fragment in blocked_fragments):
        return False
    return True


def _looks_like_strong_unresolved_community_mention(value: str) -> bool:
    text = str(value or "").strip()
    if not _looks_like_possible_community_mention(text):
        return False
    if text in AREA_ALIASES:
        return False
    return bool(re.fullmatch(r"[一-鿿]{2,10}(?:府邸|花园|公寓|府|苑|园|城|湾|都|邸|庭|轩|阁|居)", text))


def _similar_community_options(raw_text: str, communities: list[str]) -> list[str]:
    raw_norm = normalize_search_text(raw_text)
    scored: list[tuple[int, str]] = []
    for community in communities:
        community_norm = normalize_search_text(community)
        if not raw_norm or not community_norm:
            continue
        if len(raw_norm) <= 3 and len(community_norm) <= 3 and raw_norm[:1] != community_norm[:1]:
            continue
        score = 0
        if raw_norm in community_norm or community_norm in raw_norm:
            score = 80
        else:
            score = fuzzy_contains_score(raw_text, community)
            common_count = len(set(raw_norm) & set(community_norm))
            if common_count >= 2:
                score = max(score, common_count * 10)
                if raw_norm[-1:] and raw_norm[-1:] == community_norm[-1:]:
                    score += 10
        if score >= 20:
            scored.append((score, community))
    scored.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(community for _, community in scored[:5]))


def _risky_similar_community_options(raw_text: str, communities: list[str]) -> list[str]:
    raw_norm = normalize_search_text(raw_text)
    if len(raw_norm) < 3:
        return []
    suffix = raw_norm[-2:]
    scored: list[tuple[int, str]] = []
    for community in communities:
        community_norm = normalize_search_text(community)
        if len(community_norm) < 3:
            continue
        score = 0
        if suffix and community_norm.endswith(suffix):
            score = 30
        common_count = len(set(raw_norm) & set(community_norm))
        if common_count >= 2 and raw_norm[-1:] == community_norm[-1:]:
            score = max(score, common_count * 10)
        if score >= 20:
            scored.append((score, community))
    scored.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(community for _, community in scored[:5]))


def _clean_community_mention_for_compare(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    for suffix in (
        "是不是",
        "有没有",
        "还在不在",
        "在不在",
        "还在吗",
        "还在",
        "有吗",
        "多少",
        "吗",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def _community_mention_can_auto_correct(raw_text: str, canonical: str) -> bool:
    raw = str(raw_text or "").strip()
    canonical = str(canonical or "").strip()
    if not raw or not canonical:
        return True
    raw = _clean_community_mention_for_compare(raw)
    if not raw:
        return True
    if canonical in raw or raw in canonical:
        return True
    if COMMUNITY_DISPLAY_ALIASES.get(raw) == canonical:
        return True
    raw_norm = normalize_search_text(raw)
    canonical_norm = normalize_search_text(canonical)
    if raw_norm and (
        canonical_norm in raw_norm
        or raw_norm in canonical_norm
        or COMMUNITY_DISPLAY_ALIASES.get(raw_norm) == canonical
    ):
        return True
    return fuzzy_contains_score(raw, canonical) >= 30


def _assistant_text_confirms_community_correction(assistant_text: str, canonical: str) -> bool:
    text = str(assistant_text or "").strip()
    canonical = str(canonical or "").strip()
    if not text or not canonical or canonical not in text:
        return False
    confirmation_phrases = (
        f"你说的应该是{canonical}",
        f"你说的是{canonical}",
        f"你刚才说的是{canonical}",
        f"刚才说的是{canonical}",
        f"已确认是{canonical}",
        f"确认是{canonical}",
        f"就是{canonical}",
    )
    if any(phrase in text for phrase in confirmation_phrases):
        return True
    if any(
        word in text
        for word in (
            "还在",
            "视频",
            "图片",
            "价格",
            "月租",
            "押一付一",
            "押二付一",
            "看房",
            "发你",
        )
    ):
        return True
    return False


async def _inventory_rows_for_resolution(
    inventory_read_context: InventoryReadContext | None = None,
) -> list[dict[str, Any]]:
    inventory_read_context = inventory_read_context or _local_inventory_read_context("resolution")
    try:
        rows, _evidence = await inventory_read_turn.all_rows_for_context(
            inventory_read_context,
            inventory_service=inventory,
            rewrite_index_loader=load_rewrite_inventory_index,
            limit=500,
            refresh_if_needed=False,
        )
        return rows
    except Exception as exc:
        logger.debug("inventory rows for resolution unavailable: %s", exc)
        return []


def _inventory_cache_meta_for_prompt() -> dict[str, Any]:
    try:
        meta = getattr(inventory, "cache_meta", {})
        if callable(meta):
            meta = meta()
        return dict(meta or {})
    except Exception:
        return {}


async def _inventory_metadata_for_read_context(
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    inventory_read_context = inventory_read_context or _local_inventory_read_context("metadata")
    try:
        return await inventory_read_turn.metadata_for_context(
            inventory_read_context,
            inventory_service=inventory,
            rewrite_index_loader=load_rewrite_inventory_index,
        )
    except Exception as exc:
        logger.debug("inventory metadata unavailable from read provider: %s", exc)
        return {}


async def _inventory_rewrite_index_for_read_context(
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    inventory_read_context = inventory_read_context or _local_inventory_read_context("rewrite")
    try:
        return await inventory_read_turn.rewrite_index_for_context(
            inventory_read_context,
            inventory_service=inventory,
            rewrite_index_loader=load_rewrite_inventory_index,
        )
    except Exception as exc:
        logger.debug("inventory rewrite index unavailable from read provider: %s", exc)
        return {}


async def _inventory_search_rows_for_context(
    inventory_read_context: InventoryReadContext,
    query_state: Any,
    *,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    return await inventory_read_turn.search_rows_for_context(
        inventory_read_context,
        query_state,
        inventory_service=inventory,
        rewrite_index_loader=load_rewrite_inventory_index,
        limit=limit,
    )


async def _inventory_all_rows_for_context(
    inventory_read_context: InventoryReadContext,
    *,
    limit: int = 500,
    refresh_if_needed: bool = True,
) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
    return await inventory_read_turn.all_rows_for_context(
        inventory_read_context,
        inventory_service=inventory,
        rewrite_index_loader=load_rewrite_inventory_index,
        limit=limit,
        refresh_if_needed=refresh_if_needed,
    )


def _area_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        area = _row_value(row, ("区域", "商圈", "板块", "位置"))
        if area:
            counts[area] = counts.get(area, 0) + 1
    return [
        {"name": name, "row_count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _community_counts(rows: list[dict[str, Any]], *, limit: int = 300) -> tuple[list[dict[str, Any]], bool]:
    counts: dict[str, int] = {}
    areas: dict[str, str] = {}
    for row in rows:
        community = _row_value(row, ("小区", "社区", "楼盘", "小区名"))
        if not community:
            continue
        counts[community] = counts.get(community, 0) + 1
        areas.setdefault(community, _row_value(row, ("区域", "商圈", "板块", "位置")))
    items = [
        {
            "name": name,
            "row_count": count,
            "area": areas.get(name, ""),
        }
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return items[:limit], len(items) > limit


def _room_ref_hits(text: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    room_refs = {_normalize_room_ref(ref) for ref in parse_inventory_query(text).room_refs}
    if not room_refs:
        return []
    hits: list[dict[str, Any]] = []
    for row in rows:
        room_no = _row_value(row, ("房号", "房间号", "room", "room_no"))
        normalized_room = _normalize_room_ref(room_no)
        community = _row_value(row, ("小区", "社区", "楼盘", "小区名"))
        normalized_community = normalize_search_text(community).lower()
        matched = normalized_room in room_refs
        if not matched and normalized_room:
            compact_room = normalized_room.replace("-", "")
            for ref in room_refs:
                compact_ref = ref.replace("-", "")
                if compact_ref.endswith(compact_room):
                    prefix = compact_ref[: -len(compact_room)]
                    if prefix and prefix in normalized_community:
                        matched = True
                        break
        if matched:
            hits.append(
                {
                    "community": community,
                    "room_no": room_no,
                    "area": _row_value(row, ("区域", "商圈", "板块", "位置")),
                }
            )
    return hits[:10]


def _build_inventory_rewrite_index(
    *,
    content: str,
    rows: list[dict[str, Any]],
    signals: dict[str, Any],
    rewrite_view: dict[str, Any] | None = None,
    persisted_index: dict[str, Any] | None = None,
    cache_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    should_build_persisted_index = persisted_index is None
    persisted_index = (
        load_rewrite_inventory_index()
        if should_build_persisted_index
        else dict(persisted_index)
    )
    cache_meta = (
        _inventory_cache_meta_for_prompt()
        if cache_meta is None
        else dict(cache_meta)
    )
    if should_build_persisted_index and not persisted_index:
        persisted_index = write_rewrite_inventory_index(
            rows,
            area_aliases=AREA_ALIASES,
            cache_meta=cache_meta,
        )
    rewrite_index_query = _rewrite_inventory_index_query(
        content=content,
        rewrite_view=rewrite_view or {},
        signals=signals,
    )
    sliced_index = slice_rewrite_inventory_index(persisted_index, query=rewrite_index_query)
    communities = _community_names(rows)
    community_items, communities_truncated = _community_counts(rows)
    area_hits = _area_alias_hits(rewrite_index_query)
    possible_mentions = [
        mention
        for mention in _possible_community_mentions(content)
        if not _looks_like_area_alias_mention(mention, area_hits)
    ]
    similar_candidates = [
        {
            "raw_text": mention,
            "options": _similar_community_options(mention, communities),
        }
        for mention in possible_mentions
    ]
    similar_candidates = [item for item in similar_candidates if item["options"]]
    result = {
        "source": "latest_inventory_rows",
        "cache_meta": cache_meta,
        "rewrite_index_query": rewrite_index_query,
        "rewrite_inventory_index": sliced_index,
        "row_count": len(rows),
        "field_catalog": list(FIELD_SEMANTICS.keys()),
        "field_semantics": FIELD_SEMANTICS,
        "area_aliases": [
            {"alias": alias, "canonical": canonical}
            for alias, canonical in AREA_ALIASES.items()
        ],
        "areas": _area_counts(rows),
        "communities": community_items,
        "communities_truncated": communities_truncated,
        "exact_area_hits": area_hits,
        "exact_community_hits": _community_alias_hits(rewrite_index_query, communities),
        "similar_community_candidates": similar_candidates[:8],
        "room_ref_hits": _room_ref_hits(rewrite_index_query, rows),
        "sheet_request": bool(signals.get("wants_inventory_sheet")),
        "rules": {
            "inventory_sheet_request": "用户要房源表/表格/总表时直接判定 inventory_sheet，不要求客户再给小区或价位。",
            "unknown_entity": "房源表索引里没有唯一命中的小区/房号时只能追问或说明未找到，不能编造。",
            "business_scope": "只服务杭州当前房源表；命中区域别名时按索引归一，不追问城市。",
            "payment_fields": "押一付一/押二付一是对应付款方式下的月租价格，不是押金金额。",
            "utility_field": "备注字段是水电费收取方式。",
            "layout_detail_field": "户型描述字段是详细户型介绍和特点。",
            "viewing_field": "看房方式密码字段是密码、空出时间、提前联系等看房方式信息。",
        },
    }
    index_area_hits = sliced_index.get("exact_area_hits") if isinstance(sliced_index, dict) else []
    if index_area_hits and not result["exact_area_hits"]:
        result["exact_area_hits"] = index_area_hits
    return result


def _rewrite_inventory_index_query(
    *,
    content: str,
    rewrite_view: dict[str, Any],
    signals: dict[str, Any],
) -> str:
    text = str(content or "").strip()
    if not text or not isinstance(rewrite_view, dict):
        return text
    if not (
        _is_contextual_condition_followup(text, signals)
        or _is_previous_clarification_followup(text, rewrite_view)
    ):
        return text
    memory_context = _memory_search_context(rewrite_view)
    if not memory_context:
        return text
    merged = _query_parts_from_contextual_followup(
        content=text,
        signals=signals,
        memory_context=memory_context,
        result={},
    )
    effective_query = str(merged.get("effective_query") or "").strip()
    if not effective_query or effective_query == text:
        return text
    return f"{text} {effective_query}".strip()


def _build_entity_resolution(text: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    communities = _community_names(rows)
    area_hits = _area_alias_hits(text)
    community_hits = _community_alias_hits(text, communities)
    community_status = "resolved" if community_hits else "none"
    community_options: list[dict[str, Any]] = []
    room_ref_hits = _room_ref_hits(text, rows)
    possible_mentions = [
        mention
        for mention in _possible_community_mentions(text)
        if not _looks_like_area_alias_mention(mention, area_hits)
    ]
    suppress_area_fuzzy_community = bool(area_hits and _area_query_context_is_clear(text))
    community_corrections: list[dict[str, str]] = []
    unique_room_communities = list(
        dict.fromkeys(
            str(item.get("community") or "").strip()
            for item in room_ref_hits
            if str(item.get("community") or "").strip()
        )
    )
    if len(unique_room_communities) == 1:
        canonical_community = unique_room_communities[0]
        if not community_hits:
            conflicting_mentions = [
                mention
                for mention in possible_mentions
                if mention
                and canonical_community not in mention
                and mention not in canonical_community
                and not _community_mention_can_auto_correct(mention, canonical_community)
            ]
            if conflicting_mentions:
                community_options.append(
                    {
                        "raw_text": conflicting_mentions[0],
                        "status": "needs_confirmation",
                        "options": [canonical_community],
                        "confidence": "low",
                        "reason": "room_ref_community_mismatch",
                    }
                )
                community_status = "needs_confirmation"
            else:
                community_hits = [
                    {
                        "raw_text": "",
                        "canonical": canonical_community,
                        "source": "unique_room_ref",
                    }
                ]
                for mention in possible_mentions:
                    if (
                        mention
                        and canonical_community not in mention
                        and mention not in canonical_community
                    ):
                        community_corrections.append(
                            {
                                "raw_text": mention,
                                "canonical": canonical_community,
                                "reason": "unique_room_ref",
                            }
                        )
                community_status = "resolved"
        else:
            community_status = "resolved"
    if not community_hits and community_status != "needs_confirmation":
        for mention in possible_mentions:
            options = _similar_community_options(mention, communities)
            risky_options = [] if options else _risky_similar_community_options(mention, communities)
            if suppress_area_fuzzy_community and (options or risky_options):
                continue
            if not options:
                if not risky_options:
                    continue
                community_options.append(
                    {
                        "raw_text": mention,
                        "status": "needs_confirmation",
                        "options": risky_options,
                        "confidence": "low",
                        "reason": "risky_similar_community",
                    }
                )
                continue
            if len(options) == 1:
                canonical_community = options[0]
                community_hits.append(
                    {
                        "raw_text": mention,
                        "canonical": canonical_community,
                        "source": "single_fuzzy_community",
                    }
                )
                community_corrections.append(
                    {
                        "raw_text": mention,
                        "canonical": canonical_community,
                        "reason": "single_fuzzy_community",
                    }
                )
                community_status = "resolved"
                continue
            community_options.append(
                {
                    "raw_text": mention,
                    "status": "ambiguous",
                    "options": options,
                    "confidence": "low",
                    "reason": "similar_community",
                }
            )
        if community_options:
            community_status = "ambiguous"
    parsed = parse_inventory_query(text)
    status = "resolved"
    if community_status in {"ambiguous", "needs_confirmation"}:
        status = community_status
    return {
        "status": status,
        "areas": area_hits,
        "communities": community_hits,
        "community_options": community_options,
        "community_corrections": community_corrections[:3],
        "room_refs": list(parsed.room_refs),
        "room_ref_hits": room_ref_hits,
        "raw_mentions": possible_mentions,
    }


def _community_from_text(text: str, communities: list[str]) -> str:
    normalized_text = normalize_search_text(text)
    compact_text = re.sub(r"\s+", "", str(text or ""))
    matches = [
        community
        for community in communities
        if community
        and (
            community in str(text or "")
            or re.sub(r"\s+", "", community) in compact_text
            or normalize_search_text(community) in normalized_text
        )
    ]
    if not matches:
        return ""
    return sorted(matches, key=len, reverse=True)[0]


def _dialog_user_assistant_pairs(raw_dialog_context: list[dict[str, Any]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    previous_user = ""
    for item in raw_dialog_context:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            previous_user = content
            continue
        if role == "assistant" and previous_user:
            pairs.append((previous_user, content))
            previous_user = ""
    return pairs[-5:]


def _rewrite_memory_user_assistant_pairs(rewrite_view: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    raw_dialog_context = [item for item in rewrite_view.get("raw_dialog_context") or [] if isinstance(item, dict)]
    pairs.extend(_dialog_user_assistant_pairs(raw_dialog_context))
    for record in rewrite_view.get("recent_turn_records") or []:
        if not isinstance(record, dict):
            continue
        user_text = str(record.get("user_raw") or record.get("rewritten_query") or "").strip()
        assistant_summary = record.get("assistant_sent_summary") or {}
        if not isinstance(assistant_summary, dict):
            continue
        assistant_text = str(assistant_summary.get("final_reply") or "").strip()
        if user_text and assistant_text:
            pairs.append((user_text, assistant_text))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        deduped.append(pair)
    return deduped[-10:]


def _contextual_community_resolution(
    *,
    content: str,
    entity_resolution: dict[str, Any],
    rewrite_view: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if entity_resolution.get("communities"):
        return entity_resolution
    mentions = [str(item).strip() for item in entity_resolution.get("raw_mentions") or [] if str(item).strip()]
    if not mentions:
        mentions = _possible_community_mentions(content)
    if not mentions:
        return entity_resolution
    communities = _community_names(rows)
    for mention in mentions:
        normalized_mention = normalize_search_text(mention)
        if not normalized_mention:
            continue
        for user_text, assistant_text in _rewrite_memory_user_assistant_pairs(rewrite_view):
            if normalized_mention not in normalize_search_text(user_text):
                continue
            if not any(word in assistant_text for word in ("应该是", "像是", "你说的")):
                continue
            canonical = _community_from_text(assistant_text, communities)
            if not canonical:
                continue
            if not _assistant_text_confirms_community_correction(assistant_text, canonical):
                continue
            corrected = dict(entity_resolution)
            corrected["status"] = "resolved"
            corrected["communities"] = [
                {"raw_text": mention, "canonical": canonical, "source": "conversation_memory"}
            ]
            corrected["community_options"] = []
            corrections = list(corrected.get("community_corrections") or [])
            corrections.append(
                {"raw_text": mention, "canonical": canonical, "reason": "conversation_memory"}
            )
            corrected["community_corrections"] = corrections[-3:]
            return corrected
    return entity_resolution


def _constraint_layout(parsed_labels: tuple[str, ...]) -> str:
    if not parsed_labels:
        return ""
    if "两室" in parsed_labels:
        return "两室"
    return parsed_labels[0]


def _budget_range_from_query_state(query_state: dict[str, Any]) -> list[int]:
    raw_range = query_state.get("budget_range")
    if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
        try:
            low, high = sorted((int(float(raw_range[0])), int(float(raw_range[1]))))
            return [low, high]
        except (TypeError, ValueError):
            pass
    for key in ("budget", "budget_label"):
        value = str(query_state.get(key) or "").strip()
        if not value:
            continue
        parsed = parse_inventory_query(value)
        if parsed.price_range:
            return list(parsed.price_range)
        cleaned_value = _strip_room_refs_for_budget_parse(value)
        numbers = [int(item) for item in re.findall(r"\d{3,5}", cleaned_value)]
        if len(numbers) >= 2:
            low, high = sorted(numbers[:2])
            return [low, high]
        if len(numbers) == 1 and any(marker in cleaned_value for marker in ("以内", "以下", "内", "以下")):
            return [0, numbers[0]]
    return []


def _strip_room_refs_for_budget_parse(value: str) -> str:
    text = str(value or "")
    for ref in parse_inventory_query(text).room_refs:
        text = text.replace(str(ref), "")
        text = text.replace(str(ref).replace("-", ""), "")
    return re.sub(r"[A-Za-z]?\d+(?:[-－—]\d+)+(?:[-－—][A-Za-z])?(?:[A-Za-z])?", "", text)


def _build_constraint_proof(
    *,
    content: str,
    effective_query: str,
    understanding: dict[str, Any],
    entity_resolution: dict[str, Any],
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed_content = parse_inventory_query(content)
    parsed_effective = parse_inventory_query(effective_query)
    query_state = dict(understanding.get("query_state") or {})
    signals = signals or {}
    area = ""
    areas = entity_resolution.get("areas") or []
    area_values: list[str] = []
    for item in areas:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip()
        if canonical and canonical not in area_values:
            area_values.append(canonical)
    if area_values:
        area = "\n".join(area_values)
    query_state_area = _normalized_area_value(query_state.get("area"))
    communities = [
        str(item.get("canonical") or "")
        for item in entity_resolution.get("communities") or []
        if str(item.get("canonical") or "").strip()
    ]
    negated_communities = _negated_community_names(content, communities)
    if negated_communities:
        communities = [
            community
            for community in communities
            if community not in negated_communities
        ]
    parsed_price_range = parsed_content.price_range or parsed_effective.price_range
    budget_range = list(parsed_price_range) if parsed_price_range else []
    if not budget_range:
        budget_range = _budget_range_from_query_state(query_state)
    layout = _constraint_layout(parsed_content.room_type_labels or parsed_effective.room_type_labels)
    features = list(dict.fromkeys([*parsed_content.feature_labels, *parsed_effective.feature_labels]))
    room_refs = list(dict.fromkeys([*parsed_content.room_refs, *parsed_effective.room_refs]))
    selected_indices = _int_list(understanding.get("selected_indices"))
    deterministic_indices = _selection_indices_from_text(content)
    if deterministic_indices:
        selected_indices = deterministic_indices
    proof = {
        "intent": _normalize_intent(understanding.get("intent")),
        "area": area or query_state_area,
        "communities": communities,
        "room_refs": room_refs,
        "budget_range": budget_range,
        "budget_label": f"{budget_range[0]}-{budget_range[1]}" if budget_range else "",
        "layout": layout or str(query_state.get("layout") or ""),
        "features": features or query_state.get("features") or [],
        "selected_indices": selected_indices,
        "wants_video": bool(query_state.get("wants_video") or signals.get("wants_video")),
        "wants_original_video": bool(query_state.get("wants_original_video") or signals.get("wants_original_video")),
        "wants_image": bool(query_state.get("wants_image") or signals.get("wants_image")),
        "wants_inventory_sheet": bool(query_state.get("wants_inventory_sheet") or signals.get("wants_inventory_sheet")),
        "wants_utilities": bool(query_state.get("wants_utilities") or signals.get("wants_utilities")),
        "field_semantics": FIELD_SEMANTICS,
        "hard_constraints": {
            "area": bool(area or query_state_area),
            "community": bool(communities),
            "room_refs": bool(room_refs),
            "budget_range": bool(budget_range),
            "layout": bool(layout or query_state.get("layout")),
            "features": bool(features or query_state.get("features")),
            "selected_indices": bool(selected_indices),
        },
        "proof_status": "needs_confirmation" if entity_resolution.get("status") in {"ambiguous", "needs_confirmation"} else "complete",
    }
    return {key: value for key, value in proof.items() if value not in ("", None, [], {})}


def _negated_community_names(content: str, communities: list[str]) -> set[str]:
    text = normalize_search_text(content)
    result: set[str] = set()
    for community in communities:
        name = normalize_search_text(community)
        if not name:
            continue
        negated_patterns = (
            f"不一定是{name}",
            f"不一定{name}",
            f"不是{name}",
            f"不是只{name}",
            f"不是只问{name}",
            f"不是只看{name}",
            f"不要{name}",
            f"不限定{name}",
            f"不只{name}",
            f"不只问{name}",
            f"不只看{name}",
            f"不只是{name}",
            f"不光{name}",
        )
        if any(pattern in text for pattern in negated_patterns):
            result.add(community)
    return result


def _should_drop_unasked_inherited_search_constraints(content: str, effective_query: str) -> bool:
    current = str(content or "").strip()
    effective = str(effective_query or "").strip()
    if not current or not effective or current == effective:
        return False
    parsed_current = parse_inventory_query(current)
    parsed_effective = parse_inventory_query(effective)
    if parsed_current.price_range or parsed_current.room_type_labels or parsed_current.feature_labels:
        return False
    if not (parsed_effective.price_range or parsed_effective.room_type_labels or parsed_effective.feature_labels):
        return False
    if any(
        word in current
        for word in (
            "刚才",
            "上面",
            "这几套",
            "这几间",
            "这些",
            "这个",
            "这个呢",
            "这套",
            "这套呢",
            "那个",
            "那个呢",
            "那套",
            "那套呢",
            "呢",
            "我说",
            "换成",
            "改成",
            "再高",
            "再低",
            "高点",
            "低点",
            "那几套",
            "那几间",
            "第一",
            "第二",
            "第三",
            "前两套",
            "前三套",
            "继续",
            "剩下",
            "发剩下",
        )
    ):
        return False
    if not _area_alias_hits(current):
        return False
    return any(
        word in current
        for word in (
            "有没有",
            "还有",
            "有吗",
            "有么",
            "马上空",
            "空出来",
            "空出",
            "比较急",
            "急着",
            "急看",
            "今天能看",
            "今天看",
            "今天想看",
        )
    )


def _drop_unasked_inherited_search_constraints(
    result: dict[str, Any],
    *,
    content: str,
    drop_area: bool = False,
) -> dict[str, Any]:
    updated = dict(result or {})
    current = str(content or "").strip()
    if current:
        updated["effective_query"] = current
        updated["rewritten_query"] = current
    query_state = dict(updated.get("query_state") or {})
    for key in (
        "budget",
        "budget_range",
        "price_range",
        "layout",
        "room_type",
        "room_type_labels",
        "features",
        "feature",
        "feature_labels",
    ):
        query_state.pop(key, None)
    if drop_area:
        for key in ("area", "areas", "area_alias", "area_aliases", "region", "regions"):
            query_state.pop(key, None)
    updated["query_state"] = query_state
    updated["dropped_inherited_constraints"] = True
    return updated


def _drop_unasked_inherited_budget(result: dict[str, Any], *, content: str) -> dict[str, Any]:
    updated = dict(result or {})
    current = str(content or "").strip()
    if current:
        updated["effective_query"] = current
        updated["rewritten_query"] = current
    query_state = dict(updated.get("query_state") or {})
    for key in ("budget", "budget_range", "price_range"):
        query_state.pop(key, None)
    updated["query_state"] = query_state
    updated["dropped_inherited_budget"] = True
    return updated


def _should_drop_unasked_inherited_budget(
    content: str,
    effective_query: str,
    result: dict[str, Any],
    *,
    rewrite_view: dict[str, Any],
) -> bool:
    current = str(content or "").strip()
    effective = str(effective_query or "").strip()
    if not current or not effective or current == effective:
        return False
    if _has_contextual_followup_marker(current) or _is_previous_clarification_followup(current, rewrite_view):
        return False
    parsed_current = parse_inventory_query(current)
    if parsed_current.price_range:
        return False
    query_state = dict((result or {}).get("query_state") or {})
    has_inherited_budget = bool(
        parse_inventory_query(effective).price_range
        or _coerce_budget_range(query_state.get("budget_range"))
        or _coerce_budget_range(query_state.get("price_range"))
        or _coerce_budget_range(query_state.get("budget"))
    )
    if not has_inherited_budget:
        return False
    if not _has_explicit_inventory_anchor(current):
        return False
    return bool(
        parsed_current.room_type_labels
        or parsed_current.feature_labels
        or any(word in current for word in ("有没有", "还有", "有吗", "有么", "在吗", "还在", "哪几套", "哪些"))
    )


def _coerce_budget_range(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            low, high = sorted((int(value[0]), int(value[1])))
            return [low, high]
        except (TypeError, ValueError):
            return []
    text = str(value or "").strip()
    if not text:
        return []
    parsed = parse_inventory_query(text)
    if parsed.price_range:
        return list(parsed.price_range)
    return []


def _budget_range_from_text(text: str) -> list[int]:
    parsed = parse_inventory_query(text)
    if parsed.price_range:
        return list(parsed.price_range)
    return []


def _layout_from_text(text: str) -> str:
    parsed = parse_inventory_query(text)
    return _constraint_layout(parsed.room_type_labels)


def _area_from_text(text: str) -> str:
    hits = _area_alias_hits(text)
    if hits:
        return str(hits[0].get("canonical") or "").strip()
    normalized = normalize_search_text(text)
    for canonical in dict.fromkeys(AREA_ALIASES.values()):
        parts = [part for part in str(canonical).splitlines() if part.strip()]
        if any(normalize_search_text(part) in normalized for part in parts):
            return canonical
    return ""


def _clean_llm_structured_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\\n", " ").replace("\r\n", " ").replace("\n", " ")
    text = re.sub(r"[\[\]'\"]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _area_values_from_any(value: Any) -> list[str]:
    raw_items = list(value) if isinstance(value, (list, tuple, set)) else [value]
    values: list[str] = []
    canonical_values = list(dict.fromkeys(str(item) for item in AREA_ALIASES.values() if str(item).strip()))

    def add(canonical: str) -> None:
        canonical = str(canonical or "").strip()
        if canonical and canonical not in values:
            values.append(canonical)

    for raw in raw_items:
        text = _clean_llm_structured_text(raw)
        if not text:
            continue
        for hit in _area_alias_hits(text):
            add(str(hit.get("canonical") or ""))
        normalized = normalize_search_text(text)
        for canonical in canonical_values:
            parts = [part.strip() for part in str(canonical).splitlines() if part.strip()]
            if any(normalize_search_text(part) in normalized for part in parts):
                add(canonical)
    return values


def _normalized_area_value(value: Any) -> str:
    return "\n".join(_area_values_from_any(value))


def _has_explicit_inventory_anchor(text: str) -> bool:
    parsed = parse_inventory_query(text)
    if parsed.room_refs or _area_alias_hits(text):
        return True
    return bool(_possible_community_mentions(text))


def _memory_search_context(rewrite_view: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for record in reversed(list(rewrite_view.get("recent_turn_records") or [])):
        if isinstance(record, dict):
            sources.append(record)
    last_record = rewrite_view.get("last_turn_record")
    if isinstance(last_record, dict):
        sources.insert(0, last_record)

    context: dict[str, Any] = {}
    for source in sources:
        query_state = dict(source.get("query_state") or {})
        texts = [
            str(source.get("rewritten_query") or ""),
            str(source.get("user_raw") or ""),
            str((source.get("assistant_sent_summary") or {}).get("final_reply") or ""),
        ]
        if not context.get("area"):
            context["area"] = str(query_state.get("area") or "").strip() or next(
                (area for area in (_area_from_text(text) for text in texts) if area),
                "",
            )
        if not context.get("budget_range"):
            context["budget_range"] = (
                _coerce_budget_range(query_state.get("budget_range"))
                or _coerce_budget_range(query_state.get("price_range"))
                or _coerce_budget_range(query_state.get("budget"))
                or next((budget for budget in (_budget_range_from_text(text) for text in texts) if budget), [])
            )
        if not context.get("layout"):
            context["layout"] = str(query_state.get("layout") or query_state.get("room_type") or "").strip() or next(
                (layout for layout in (_layout_from_text(text) for text in texts) if layout),
                "",
            )
        if not context.get("intent"):
            context["intent"] = str(source.get("intent") or query_state.get("intent") or "").strip()
        if context.get("area") and context.get("budget_range") and context.get("layout"):
            break

    if not (context.get("area") and context.get("budget_range") and context.get("layout")):
        for item in reversed(list(rewrite_view.get("raw_dialog_context") or [])[-6:]):
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or "")
            if not context.get("area"):
                context["area"] = _area_from_text(text)
            if not context.get("budget_range"):
                context["budget_range"] = _budget_range_from_text(text)
            if not context.get("layout"):
                context["layout"] = _layout_from_text(text)
            if context.get("area") and context.get("budget_range") and context.get("layout"):
                break
    return {key: value for key, value in context.items() if value not in ("", None, [], {})}


def _is_contextual_condition_followup(content: str, signals: dict[str, Any]) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if text.startswith("客户在机器人生成答案前连续补充了这些问题"):
        return False
    if signals.get("wants_inventory_sheet") or signals.get("is_greeting"):
        return False
    if _has_contextual_followup_marker(text):
        return True
    explicit_anchor = _has_explicit_inventory_anchor(text)
    if (_budget_range_from_text(text) or _layout_from_text(text)) and len(text) <= 28 and not explicit_anchor:
        return True
    if (signals.get("wants_video") or signals.get("wants_image") or signals.get("wants_viewing")) and len(text) <= 28 and not explicit_anchor:
        return True
    return False


def _has_contextual_followup_marker(text: str) -> bool:
    current = str(text or "").strip()
    return any(
        word in current
        for word in (
            "我说",
            "的呢",
            "这个呢",
            "那个呢",
            "这套",
            "那套",
            "这几套",
            "那几套",
            "这些",
            "那些",
            "上面",
            "刚才",
            "换成",
            "改成",
            "再高",
            "再低",
            "高点",
            "低点",
            "前两套",
            "前三套",
            "如果有",
            "有的话",
            "先发",
            "发给客户",
            "给客户看看",
            "第1",
            "第2",
            "第3",
            "第一",
            "第二",
            "第三",
            "继续",
            "剩下",
            "发剩下",
        )
    )


def _is_previous_clarification_followup(content: str, rewrite_view: dict[str, Any]) -> bool:
    text = str(content or "").strip()
    if not text or len(text) > 32:
        return False
    last_record = rewrite_view.get("last_turn_record") if isinstance(rewrite_view, dict) else {}
    last_output = rewrite_view.get("last_assistant_output") if isinstance(rewrite_view, dict) else {}
    final_reply = str((last_output or {}).get("final_reply") or "")
    if final_reply and _has_explicit_inventory_anchor(text):
        normalized_reply = normalize_search_text(final_reply)
        anchor_terms = _explicit_anchor_terms(text)
        if anchor_terms and not any(normalize_search_text(term) in normalized_reply for term in anchor_terms):
            return False
    if isinstance(last_record, dict) and bool(last_record.get("needs_clarification")):
        return True
    return bool(final_reply and any(word in final_reply for word in ("确认", "你说的是", "发具体小区", "回我小区")))


def _explicit_anchor_terms(text: str) -> list[str]:
    terms: list[str] = []
    for item in _area_alias_hits(text):
        raw_text = str(item.get("raw_text") or "").strip()
        canonical = str(item.get("canonical") or "").strip()
        if raw_text:
            terms.append(raw_text)
        for part in re.split(r"[\s\n/、]+", canonical):
            if part.strip():
                terms.append(part.strip())
    terms.extend(_possible_community_mentions(text))
    return list(dict.fromkeys(term for term in terms if term))


def _query_parts_from_contextual_followup(
    *,
    content: str,
    signals: dict[str, Any],
    memory_context: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    current_budget = _budget_range_from_text(content)
    current_layout = _layout_from_text(content)
    current_area = _area_from_text(content)
    query_state = dict(result.get("query_state") or {})

    # In short follow-ups like "4000-5000 的呢", the current message often only
    # replaces one constraint. Do not let an LLM-inferred query_state override the
    # last visible conversation unless the current user text explicitly says so.
    area = current_area or str(memory_context.get("area") or "").strip() or str(query_state.get("area") or "").strip()
    budget_range = (
        current_budget
        or _coerce_budget_range(memory_context.get("budget_range"))
        or _coerce_budget_range(query_state.get("budget_range"))
        or _coerce_budget_range(query_state.get("price_range"))
        or _coerce_budget_range(query_state.get("budget"))
    )
    layout = current_layout or str(memory_context.get("layout") or "").strip() or str(query_state.get("layout") or query_state.get("room_type") or "").strip()

    parts: list[str] = []
    if area:
        parts.append(area.replace("\n", "/"))
    if budget_range:
        parts.append(f"{budget_range[0]}-{budget_range[1]}")
    if layout:
        parts.append(layout)
    if signals.get("wants_video"):
        parts.append("视频")
    elif signals.get("wants_image"):
        parts.append("图片")
    elif signals.get("wants_viewing"):
        parts.append("看房方式")
    elif parts:
        parts.append("在租房源")
    return {
        "area": area,
        "budget_range": budget_range,
        "layout": layout,
        "effective_query": " ".join(part for part in parts if part).strip(),
    }


def _apply_contextual_followup_rewrite(
    *,
    content: str,
    result: dict[str, Any],
    rewrite_view: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    if not (
        _is_contextual_condition_followup(content, signals)
        or _is_previous_clarification_followup(content, rewrite_view)
    ):
        return result
    memory_context = _memory_search_context(rewrite_view)
    if not memory_context:
        return result
    current_has_anchor = _has_explicit_inventory_anchor(content)
    if not current_has_anchor and not (
        memory_context.get("area")
        or memory_context.get("layout")
        or memory_context.get("budget_range")
    ):
        return result

    merged = _query_parts_from_contextual_followup(
        content=content,
        signals=signals,
        memory_context=memory_context,
        result=result,
    )
    effective_query = str(merged.get("effective_query") or "").strip()
    if not effective_query:
        return result

    updated = dict(result or {})
    query_state = dict(updated.get("query_state") or {})
    if merged.get("area"):
        query_state["area"] = merged["area"]
    if merged.get("budget_range"):
        query_state["budget_range"] = merged["budget_range"]
        query_state["budget"] = f"{merged['budget_range'][0]}-{merged['budget_range'][1]}"
    if merged.get("layout"):
        query_state["layout"] = merged["layout"]
    if signals.get("wants_video"):
        query_state["wants_video"] = True
    if signals.get("wants_image"):
        query_state["wants_image"] = True
    if signals.get("wants_viewing"):
        query_state["wants_viewing"] = True
    if not query_state.get("intent") or query_state.get("intent") in {"general", "unclear", "context_followup"}:
        query_state["intent"] = "media" if (signals.get("wants_video") or signals.get("wants_image")) else "inventory"

    updated["query_state"] = query_state
    updated["intent"] = query_state["intent"]
    updated["rewritten_query"] = effective_query
    updated["effective_query"] = effective_query
    updated["context_reference"] = True
    updated["needs_clarification"] = False
    updated["clarification_text"] = ""
    updated["contextual_followup_resolution"] = {
        "source": "raw_dialog_context_and_turn_records",
        "inherited": memory_context,
        "current_user_input": content,
    }
    return updated


def _bound_room_label_from_rewrite_view(rewrite_view: dict[str, Any]) -> str:
    confirmed = rewrite_view.get("confirmed_room") if isinstance(rewrite_view, dict) else {}
    if isinstance(confirmed, dict):
        label = str(confirmed.get("label") or "").strip()
        if label:
            return label
        row = confirmed.get("row") if isinstance(confirmed.get("row"), dict) else {}
        key = str(row.get("key") or "").strip()
        if key:
            return key
    candidate_set = rewrite_view.get("last_candidate_set") if isinstance(rewrite_view, dict) else {}
    if not isinstance(candidate_set, dict):
        return ""
    candidates = [row for row in candidate_set.get("candidates") or [] if isinstance(row, dict)]
    if len(candidates) != 1:
        return ""
    row = candidates[0]
    key = str(row.get("key") or "").strip()
    if key:
        return key
    community = str(row.get("community") or row.get("小区") or "").strip()
    room_no = str(row.get("room_no") or row.get("房号") or "").strip()
    return f"{community}{room_no}".strip()


def _apply_bound_room_context_action_rewrite(
    *,
    content: str,
    result: dict[str, Any],
    rewrite_view: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    if not (signals.get("wants_video") or signals.get("wants_image") or signals.get("wants_viewing")):
        return result
    text = str(content or "").strip()
    if not text or len(text) > 32:
        return result
    if _has_explicit_inventory_anchor(text) or _budget_range_from_text(text) or _layout_from_text(text):
        return result
    if not _has_context_reference_word(text) and not any(word in text for word in ("发一下", "发我", "看看", "能看")):
        return result
    label = _bound_room_label_from_rewrite_view(rewrite_view)
    if not label:
        return result

    updated = dict(result or {})
    query_state = dict(updated.get("query_state") or {})
    if signals.get("wants_video"):
        query_state["wants_video"] = True
    if signals.get("wants_image"):
        query_state["wants_image"] = True
    if signals.get("wants_viewing"):
        query_state["wants_viewing"] = True
    intent = "viewing" if signals.get("wants_viewing") else "media"
    query_state["intent"] = intent
    updated["query_state"] = query_state
    updated["intent"] = intent
    media_parts = []
    if signals.get("wants_image"):
        media_parts.append("图片")
    if signals.get("wants_video"):
        media_parts.append("视频")
    if signals.get("wants_viewing"):
        media_parts.append("看房方式")
    action_label = "/".join(media_parts) if media_parts else text
    effective_query = f"{label} {action_label}".strip()
    updated["rewritten_query"] = effective_query
    updated["effective_query"] = effective_query
    updated["context_reference"] = True
    updated["needs_clarification"] = False
    updated["clarification_text"] = ""
    updated["contextual_followup_resolution"] = {
        "source": "confirmed_room_or_single_candidate",
        "bound_room": label,
        "current_user_input": text,
    }
    return updated


def _should_drop_inherited_constraints_for_explicit_community(
    content: str,
    effective_query: str,
    entity_resolution: dict[str, Any],
) -> bool:
    current = str(content or "").strip()
    effective = str(effective_query or "").strip()
    if not current or not effective or current == effective or _has_context_reference_word(current):
        return False
    raw_mentions = [
        str(item).strip()
        for item in entity_resolution.get("raw_mentions") or []
        if str(item).strip()
    ]
    for item in entity_resolution.get("communities") or []:
        if isinstance(item, dict):
            raw = str(item.get("raw_text") or "").strip()
            if raw:
                raw_mentions.append(raw)
    raw_mentions = list(dict.fromkeys(raw_mentions))
    if not raw_mentions:
        raw_mentions = _community_like_mentions(current)
    if not raw_mentions:
        return False
    parsed_current = parse_inventory_query(current)
    parsed_effective = parse_inventory_query(effective)
    inherited_area = bool(_area_alias_hits(effective)) and not bool(_area_alias_hits(current))
    inherited_layout = bool(parsed_effective.room_type_labels) and not bool(parsed_current.room_type_labels)
    inherited_features = bool(parsed_effective.feature_labels) and not bool(parsed_current.feature_labels)
    return inherited_area or inherited_layout or inherited_features


def _apply_query_state_community_resolution(
    *,
    content: str,
    result: dict[str, Any],
    entity_resolution: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if entity_resolution.get("communities"):
        return entity_resolution
    query_state = dict(result.get("query_state") or {})
    community = str(query_state.get("community") or "").strip()
    if not community:
        return entity_resolution
    known_communities = set(_community_names(rows))
    if community not in known_communities:
        return entity_resolution
    raw_mentions = _community_like_mentions(content)
    if not raw_mentions and community not in str(result.get("effective_query") or result.get("rewritten_query") or ""):
        return entity_resolution
    updated = dict(entity_resolution or {})
    updated["status"] = "resolved"
    updated["communities"] = [
        {
            "raw_text": raw_mentions[0] if raw_mentions else community,
            "canonical": community,
            "source": "query_state_community",
            "confidence": "medium",
        }
    ]
    updated["raw_mentions"] = raw_mentions or [community]
    updated["community_options"] = []
    return updated


def _should_drop_unasked_inherited_room_refs(content: str, effective_query: str) -> bool:
    current = str(content or "").strip()
    effective = str(effective_query or "").strip()
    if not current or not effective or current == effective or _has_context_reference_word(current):
        return False
    if parse_inventory_query(current).room_refs:
        return False
    return bool(parse_inventory_query(effective).room_refs)


def _drop_unasked_inherited_room_refs(
    result: dict[str, Any],
    *,
    content: str,
) -> dict[str, Any]:
    updated = dict(result or {})
    current = str(content or "").strip()
    if current:
        updated["effective_query"] = current
        updated["rewritten_query"] = current
    query_state = dict(updated.get("query_state") or {})
    for key in ("room_ref", "room_refs", "room_no", "room_number", "room"):
        query_state.pop(key, None)
    updated["query_state"] = query_state
    updated["needs_clarification"] = False
    updated["clarification_text"] = ""
    updated["dropped_inherited_room_refs"] = True
    return updated


def _has_context_reference_word(content: str) -> bool:
    return any(
        word in content
        for word in (
            "刚才",
            "上面",
            "这几套",
            "这几间",
            "这些",
            "这个",
            "这套",
            "那个",
            "那套",
            "呢",
            "我说",
            "换成",
            "改成",
            "再高",
            "再低",
            "高点",
            "低点",
            "那几套",
            "那几间",
            "第一",
            "第二",
            "第三",
            "前两套",
            "前三套",
            "如果有",
            "有的话",
            "先发",
            "发给客户",
            "给客户看看",
            "继续",
            "剩下",
            "发剩下",
        )
    )


def _should_drop_unasked_llm_inferred_layout_features(
    content: str,
    effective_query: str,
    query_state: dict[str, Any] | None = None,
) -> bool:
    current = str(content or "").strip()
    effective = str(effective_query or "").strip()
    state = query_state if isinstance(query_state, dict) else {}
    state_has_layout_or_features = any(
        state.get(key)
        for key in ("layout", "room_type", "room_type_labels", "features", "feature", "feature_labels")
    )
    if not current or not effective or _has_context_reference_word(current):
        return False
    if current == effective and not state_has_layout_or_features:
        return False
    parsed_current = parse_inventory_query(current)
    parsed_effective = parse_inventory_query(effective)
    if parsed_current.room_type_labels or parsed_current.feature_labels:
        return False
    if not (parsed_effective.room_type_labels or parsed_effective.feature_labels or state_has_layout_or_features):
        return False
    return bool(
        parsed_current.price_range
        or _area_alias_hits(current)
        or any(word in current for word in ("有没有", "还有", "有吗", "有哪些"))
    )


def _drop_unasked_llm_inferred_layout_features(
    result: dict[str, Any],
    *,
    content: str,
) -> dict[str, Any]:
    updated = dict(result or {})
    current = str(content or "").strip()
    if current:
        updated["effective_query"] = current
        updated["rewritten_query"] = current
    query_state = dict(updated.get("query_state") or {})
    for key in ("layout", "room_type", "room_type_labels", "features", "feature", "feature_labels"):
        query_state.pop(key, None)
    if not _area_alias_hits(current) and _community_like_mentions(current):
        for key in ("area", "areas", "area_alias", "area_aliases", "region", "regions"):
            query_state.pop(key, None)
        updated["dropped_inherited_constraints"] = True
    updated["query_state"] = query_state
    updated["dropped_unasked_llm_inferred_constraints"] = True
    return updated


def _community_like_mentions(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            mention
            for mention in (
                _clean_community_mention_candidate(match)
                for match in re.findall(
                    r"[一-鿿]{2,}(?:府|苑|湾|城|轩|庭|阁|寓|郡|院|邸)",
                    str(text or ""),
                )
            )
            if _looks_like_possible_community_mention(mention)
        )
    )


def _strip_room_ref_derived_budget_text(text: str, room_refs: list[str] | tuple[str, ...]) -> str:
    cleaned = str(text or "")
    if not cleaned or not room_refs:
        return cleaned
    for ref in room_refs:
        parts = [part for part in re.split(r"[-－—]", str(ref or "")) if part]
        if not parts:
            continue
        tail = re.sub(r"\D", "", parts[-1])
        if len(tail) < 3:
            continue
        cleaned = re.sub(rf"(?<!\d){re.escape(tail)}\s*(?:预算|元预算|块预算|价格|元)(?!\d)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ，,、")


def _strip_discourse_prefix_before_communities(text: str, communities: list[str] | tuple[str, ...]) -> str:
    cleaned = str(text or "")
    if not cleaned or not communities:
        return cleaned
    prefixes = r"(?:客户|租客)?(?:又问|再问|问下|问一下|问|想问|咨询|说|要问|在问)"
    for community in communities:
        community_text = str(community or "").strip()
        if not community_text:
            continue
        cleaned = re.sub(rf"{prefixes}{re.escape(community_text)}", community_text, cleaned)
        cleaned = re.sub(rf"(?:这个|那个|这边|那边|这|那)?小区{re.escape(community_text)}", community_text, cleaned)
    return cleaned.strip(" ，,、")


def _tool_requirements_from_task(
    *,
    intent: str,
    signals: dict[str, Any],
    constraint_proof: dict[str, Any],
) -> dict[str, Any]:
    return {
        "needs_inventory_search": intent in {"inventory", "media", "viewing", "context_followup", "general"} and not signals.get("wants_inventory_sheet"),
        "needs_inventory_sheet": bool(signals.get("wants_inventory_sheet") or constraint_proof.get("wants_inventory_sheet")),
        "needs_video": bool(
            signals.get("wants_video")
            or signals.get("wants_original_video")
            or constraint_proof.get("wants_video")
            or constraint_proof.get("wants_original_video")
        ),
        "needs_image": bool(signals.get("wants_image") or constraint_proof.get("wants_image")),
        "needs_contract_contact": bool(signals.get("wants_contract_contact")),
        "needs_price_contact": bool(signals.get("wants_price_contact")),
        "needs_deposit_policy": bool(signals.get("wants_deposit") or intent == "deposit"),
        "needs_viewing_policy": bool(signals.get("wants_viewing") or intent == "viewing"),
        "needs_utilities": bool(signals.get("wants_utilities") or constraint_proof.get("wants_utilities")),
    }


def _enforce_effective_query(
    *,
    content: str,
    understanding: dict[str, Any],
    constraint_proof: dict[str, Any],
) -> str:
    parts = [
        _clean_llm_structured_text(
            understanding.get("effective_query") or understanding.get("rewritten_query") or content
        )
    ]
    parts[0] = _strip_room_ref_derived_budget_text(
        parts[0],
        [str(ref) for ref in constraint_proof.get("room_refs") or [] if str(ref).strip()],
    )
    parts[0] = _strip_discourse_prefix_before_communities(
        parts[0],
        [str(community) for community in constraint_proof.get("communities") or [] if str(community).strip()],
    )
    area = str(constraint_proof.get("area") or "").strip()
    if area and area not in parts[0]:
        parts.append(area.replace("\n", " "))
    for community in constraint_proof.get("communities") or []:
        if community and community not in " ".join(parts):
            parts.append(str(community))
    for room_ref in constraint_proof.get("room_refs") or []:
        room_ref_text = str(room_ref or "").strip()
        if room_ref_text and _normalize_room_ref(room_ref_text) not in {
            _normalize_room_ref(ref) for ref in _room_refs_from_text(" ".join(parts))
        }:
            parts.append(room_ref_text)
    budget = constraint_proof.get("budget_range") or []
    if budget and not any(str(value) in " ".join(parts) for value in budget):
        parts.append(f"{budget[0]}到{budget[1]}预算")
    layout = str(constraint_proof.get("layout") or "").strip()
    if layout and layout not in " ".join(parts):
        parts.append(layout)
    return " ".join(part for part in parts if part).strip()


def _clarification_from_entity_resolution(entity_resolution: dict[str, Any]) -> str:
    options: list[str] = []
    raw = ""
    for item in entity_resolution.get("community_options") or []:
        raw = str(item.get("raw_text") or raw)
        options.extend(str(option) for option in item.get("options") or [] if str(option).strip())
    raw = _clean_clarification_raw_mention(raw)
    options = list(dict.fromkeys(options))[:5]
    if not options:
        return ""
    if len(options) == 1:
        return f"你说的是{options[0]}吗？我先确认一下小区名，确认后再按最新房源表查。"
    return f"你说的“{raw}”我这边有几个相近小区：{'、'.join(options)}。你确认下是哪一个，我再按最新房源表查。"


def _clean_clarification_raw_mention(value: str) -> str:
    text = str(value or "").strip(" 　，,。？?！!；;：:“”\"'‘’（）()[]【】")
    if not text:
        return ""
    for marker in ("还有", "还在", "还子", "有房", "有吗", "有么", "在吗", "房子"):
        index = text.find(marker)
        if index >= 2:
            text = text[:index]
            break
    suffixes = (
        "还有房子吗",
        "还有房子",
        "还有房",
        "有房子吗",
        "有房子",
        "房子吗",
        "房子",
        "还有吗",
        "还有",
        "还在吗",
        "还在",
        "有吗",
        "有么",
        "在吗",
        "还子",
        "的呢",
        "呢",
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)].strip(" 　，,。？?！!；;：:“”\"'‘’（）()[]【】")
                changed = True
                break
    return text.strip() or str(value or "").strip()


def _room_ref_parts(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def _room_ref_looks_related(requested: str, existing: str) -> bool:
    requested_parts = _room_ref_parts(requested)
    existing_parts = _room_ref_parts(existing)
    if not requested_parts or not existing_parts:
        return False
    if _normalize_room_ref(requested) == _normalize_room_ref(existing):
        return True
    return requested_parts[0] == existing_parts[0] and requested_parts[-1] == existing_parts[-1]


def _room_ref_mismatch_clarification(
    content: str,
    entity_resolution: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
) -> str:
    room_refs = list(parse_inventory_query(content).room_refs)
    if not room_refs or entity_resolution.get("room_ref_hits"):
        return ""
    options: list[str] = []
    for item in entity_resolution.get("community_options") or []:
        options.extend(str(option).strip() for option in item.get("options") or [] if str(option).strip())
    for item in entity_resolution.get("communities") or []:
        if isinstance(item, dict) and str(item.get("canonical") or "").strip():
            options.append(str(item["canonical"]).strip())
    for item in entity_resolution.get("raw_mentions") or []:
        mention = str(item).strip()
        if mention:
            options.append(mention)
    options = list(dict.fromkeys(options))[:3]
    refs = "、".join(ref.upper() for ref in room_refs[:3])
    similar_rooms: list[str] = []
    if rows and options:
        option_set = set(options)
        for row in rows:
            community = _row_value(row, ("小区", "社区", "楼盘", "小区名"))
            room_no = _row_value(row, ("房号", "房间号", "门牌"))
            if community not in option_set or not room_no:
                continue
            if any(_room_ref_looks_related(ref, room_no) for ref in room_refs):
                similar_rooms.append(_row_label(row))
        similar_rooms = list(dict.fromkeys(similar_rooms))[:3]
    if similar_rooms:
        return f"最新房源表没查到{options[0]}{refs}这套，只匹配到相近房号：{'、'.join(similar_rooms)}。你确认是不是这套？"
    if options:
        return f"你说的小区像是{options[0]}，但最新房源表没查到{refs}这套。你确认下房号，或者我按{options[0]}当前在租房源继续查。"
    return f"最新房源表没查到{refs}这套。你确认下小区+房号，我再查价格、视频或看房方式。"


def _clarification_mentions_current_inventory(
    clarification_text: str,
    rows: list[dict[str, Any]],
    entity_resolution: dict[str, Any],
) -> bool:
    text = str(clarification_text or "").strip()
    if not text:
        return True
    mentions = list(
        dict.fromkeys(
            match.strip()
            for match in re.findall(r"[一-鿿]{2,8}(?:府|苑|园|城|湾|邸|庭|府邸)", text)
            if match.strip()
        )
    )
    if not mentions:
        return True
    communities = set(_community_names(rows))
    valid_options: set[str] = set()
    raw_options: set[str] = set()
    for item in entity_resolution.get("community_options") or []:
        if str(item.get("raw_text") or "").strip():
            raw_options.add(str(item["raw_text"]).strip())
        valid_options.update(str(option).strip() for option in item.get("options") or [] if str(option).strip())
    for hit in entity_resolution.get("communities") or []:
        if isinstance(hit, dict) and str(hit.get("canonical") or "").strip():
            valid_options.add(str(hit["canonical"]).strip())
    valid = communities | valid_options
    def mention_is_allowed(mention: str) -> bool:
        canonical = canonical_community_display(mention)
        if canonical in valid or mention in valid or mention in raw_options:
            return True
        return any(option and (option in mention or mention in option) for option in valid | raw_options)

    return all(mention_is_allowed(mention) for mention in mentions)


def _safe_inventory_bound_clarification(
    *,
    content: str,
    entity_resolution: dict[str, Any],
) -> str:
    room_refs = list(parse_inventory_query(content).room_refs)
    if room_refs and not entity_resolution.get("room_ref_hits"):
        return "我这边最新房源表没查到你说的这套房源。你确认下小区+房号，我再查价格、视频或看房方式。"
    mentions = [
        str(item).strip()
        for item in entity_resolution.get("raw_mentions") or _possible_community_mentions(content)
        if _looks_like_strong_unresolved_community_mention(str(item).strip())
    ]
    if mentions:
        return f"最新房源表里暂时没查到{mentions[0]}这个小区。你确认一下小区名，或者发区域/预算我帮你重新筛。"
    return "这个小区名我在最新房源表里没稳定匹配到。你确认下标准小区名，或发区域/预算我帮你重新筛。"


def _unresolved_community_mention_clarification(
    *,
    content: str,
    entity_resolution: dict[str, Any],
) -> str:
    if entity_resolution.get("communities") or entity_resolution.get("community_options"):
        return ""
    if entity_resolution.get("areas"):
        return ""
    mentions = [
        str(item).strip()
        for item in entity_resolution.get("raw_mentions") or _possible_community_mentions(content)
        if _looks_like_strong_unresolved_community_mention(str(item).strip())
    ]
    if not mentions:
        return ""
    mention = mentions[0]
    return f"最新房源表里暂时没查到{mention}这个小区。你确认一下小区名，或者发区域/预算我帮你重新筛。"


def _build_structured_task(
    *,
    content: str,
    understanding: dict[str, Any],
    signals: dict[str, Any],
    entity_resolution: dict[str, Any],
    constraint_proof: dict[str, Any],
) -> dict[str, Any]:
    intent = _normalize_intent(understanding.get("intent"))
    return {
        "original_text": content,
        "effective_query": str(understanding.get("effective_query") or content),
        "intent": intent,
        "query_state": dict(understanding.get("query_state") or {}),
        "target_binding": {
            "context_reference": bool(understanding.get("context_reference")),
            "candidate_action": str(understanding.get("candidate_action") or "none"),
            "selected_indices": _int_list(understanding.get("selected_indices")),
            "target_rows": [_row_label(row) for row in understanding.get("target_rows") or [] if isinstance(row, dict)],
        },
        "entity_resolution": entity_resolution,
        "constraint_proof": constraint_proof,
        "field_semantics": FIELD_SEMANTICS,
        "tool_requirements": _tool_requirements_from_task(
            intent=intent,
            signals=signals,
            constraint_proof=constraint_proof,
        ),
        "clarification": {
            "needed": bool(understanding.get("needs_clarification")),
            "text": str(understanding.get("clarification_text") or ""),
            "reason": str(entity_resolution.get("status") or ""),
        },
    }


def _candidate_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_set = context.get("last_candidate_set") or {}
    return [row for row in candidate_set.get("candidates") or [] if isinstance(row, dict)]


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
            content = str(item.get("content") or "").strip()
            if content:
                texts.append(content)
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
            return sent_matched[:KF_VIDEO_SEND_LIMIT]
    texts = _recent_assistant_texts(context)
    if not texts:
        return []
    matched: list[dict[str, Any]] = []
    for text in texts:
        for label, row in label_rows:
            if label and label in text and row not in matched:
                matched.append(row)
        if matched:
            return matched[:KF_VIDEO_SEND_LIMIT]
    return []


async def _pending_video_label_rows(
    context: dict[str, Any],
    *,
    limit: int = KF_VIDEO_SEND_LIMIT,
    inventory_read_context: InventoryReadContext | None = None,
) -> list[dict[str, Any]]:
    pending_video = kf_context_memory.pending_video_sends(context)
    labels = [
        str(label).strip()
        for label in (pending_video or {}).get("labels") or []
        if str(label).strip()
    ]
    labels = list(dict.fromkeys(labels))
    if not labels:
        return []
    inventory_read_context = inventory_read_context or _local_inventory_read_context("pending_video")
    try:
        all_rows, _evidence = await _inventory_all_rows_for_context(
            inventory_read_context,
            limit=1000,
            refresh_if_needed=True,
        )
    except Exception as exc:
        logger.debug("pending video label lookup unavailable: %s", exc)
        return []
    rows_by_label = {
        normalize_search_text(_row_label(row)): row
        for row in all_rows
        if isinstance(row, dict) and normalize_search_text(_row_label(row))
    }
    matched: list[dict[str, Any]] = []
    for label in labels:
        row = rows_by_label.get(normalize_search_text(label))
        if row and row not in matched:
            matched.append(row)
        if len(matched) >= limit:
            break
    return matched


def _proof_community_norms(proof: dict[str, Any]) -> set[str]:
    return {
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if normalize_search_text(str(item))
    }


def _rows_matching_proof_communities(
    rows: list[dict[str, Any]],
    proof: dict[str, Any],
) -> list[dict[str, Any]]:
    community_norms = _proof_community_norms(proof)
    if not community_norms:
        return list(rows)
    return [
        row
        for row in rows
        if normalize_search_text(_row_value(row, ("小区", "小区名"))) in community_norms
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
        return matched_inventory[:KF_VIDEO_SEND_LIMIT]
    return matched_targets


def _last_candidate_query_from_memory(context: dict[str, Any]) -> str:
    memory = context.get("structured_memory") or {}
    records = memory.get("turn_records") if isinstance(memory, dict) else []
    if not isinstance(records, list):
        return ""
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        summary = record.get("assistant_sent_summary") or {}
        candidate_state = summary.get("candidate_state") if isinstance(summary, dict) else {}
        candidate_set = candidate_state.get("candidate_set") if isinstance(candidate_state, dict) else {}
        query = str(candidate_set.get("query") or "").strip() if isinstance(candidate_set, dict) else ""
        if query:
            return query
    return ""


def _confirmed_row(context: dict[str, Any]) -> dict[str, Any]:
    confirmed = context.get("confirmed_room") or {}
    row = confirmed.get("row") if isinstance(confirmed, dict) else {}
    return row if isinstance(row, dict) else {}


def _media_request_targets_previous_candidates(query_text: str) -> bool:
    text = str(query_text or "")
    if not text:
        return False
    if _area_alias_hits(text) or _room_refs_from_text(text):
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


def _target_rows_from_understanding(
    understanding: dict[str, Any],
    context: dict[str, Any],
    search_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    explicit_rows = [row for row in understanding.get("target_rows") or [] if isinstance(row, dict)]
    if explicit_rows:
        return explicit_rows

    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    query_text = " ".join(
        str(part).strip()
        for part in (
            task.get("original_text"),
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
        )
            if str(part or "").strip()
    )
    current_text = " ".join(
        str(part).strip()
        for part in (
            task.get("original_text"),
            understanding.get("original_query"),
        )
        if str(part or "").strip()
    )
    current_turn_has_room_refs = bool(_room_refs_from_text(current_text))
    explicit_room_refs = bool(
        proof.get("room_refs")
        or _room_refs_from_text(query_text)
    )
    wants_viewing = bool(
        requirements.get("needs_viewing_policy")
        or _normalize_intent(understanding.get("intent")) == "viewing"
        or _content_wants_viewing(query_text)
    )
    candidates = _candidate_rows(context)
    selected = _selected_indices_from_understanding(understanding, query_text)
    if not selected:
        matched_by_room_ref = _target_rows_from_room_refs(understanding, search_rows)
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

    proof_communities = {
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if normalize_search_text(str(item))
    }
    current_text_norm = normalize_search_text(current_text)
    current_mentions_proof_community = bool(
        proof_communities
        and current_text_norm
        and any(community in current_text_norm for community in proof_communities)
    )
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
        if current_search_rows and any(index > len(current_search_rows) for index in selected):
            return []
        current_selected_rows = [
            current_search_rows[index - 1]
            for index in selected
            if 1 <= index <= len(current_search_rows)
        ]
        return current_selected_rows or []

    if selected:
        if not candidates:
            return []
        if any(index > len(candidates) for index in selected):
            return []
        selected_rows = [
            candidates[index - 1]
            for index in selected
            if 1 <= index <= len(candidates)
        ]
        return selected_rows or []

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
        and bool(understanding.get("context_reference"))
        and wants_media
        and _media_request_targets_previous_candidates(str(task.get("original_text") or query_text))
    ):
        return candidates[:KF_VIDEO_SEND_LIMIT]

    if (
        candidates
        and bool(understanding.get("context_reference"))
        and wants_viewing
        and _references_unbound_room_context(query_text)
    ):
        return candidates[:10]

    if (
        confirmed
        and _should_bind_confirmed_room_context(understanding, query_text)
        and not _has_explicit_candidate_selection(query_text)
    ):
        return [confirmed]

    if search_rows and wants_media:
        media_query_text = " ".join(
            str(part).strip()
            for part in (
                task.get("original_text"),
                understanding.get("effective_query"),
                understanding.get("rewritten_query"),
                proof.get("budget_label"),
            )
            if str(part or "").strip()
        )
        requested_count = _requested_room_count_from_text(media_query_text)
        if requested_count:
            return search_rows[: min(requested_count, KF_VIDEO_SEND_LIMIT)]
        if any(word in media_query_text for word in ("最合适", "几套", "几间", "推荐")):
            return search_rows[: min(len(search_rows), KF_VIDEO_SEND_LIMIT)]

    if len(search_rows) == 1:
        return search_rows
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
    query_text = " ".join(
        str(part).strip()
        for part in (
            content,
            task.get("original_text"),
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
            proof.get("budget_label"),
        )
        if str(part or "").strip()
    )
    if proof.get("room_refs") or _room_refs_from_text(query_text):
        return {}
    if _selected_indices_from_understanding(understanding, query_text):
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

    proof_communities = {
        normalize_search_text(str(item))
        for item in proof.get("communities") or []
        if normalize_search_text(str(item))
    }
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


def _normalize_room_ref(value: str) -> str:
    text = str(value or "").lower().strip()
    text = text.replace("－", "-").replace("—", "-")
    return re.sub(r"[-\s]+", "", text)


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
    raw_mentions = [
        normalize_search_text(item)
        for item in _possible_community_mentions(text)
        if item
    ]
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


def _target_rows_from_room_refs(
    understanding: dict[str, Any],
    search_rows: list[dict[str, Any]],
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
            query_text,
            str(task.get("original_text") or ""),
            str(task.get("effective_query") or ""),
        )
        if part.strip()
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


def _has_specific_room_context_reference(query_text: str) -> bool:
    text = str(query_text or "")
    return any(
        phrase in text
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
    ) or _references_unbound_room_context(text)


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


def _should_bind_confirmed_room_context(
    understanding: dict[str, Any],
    query_text: str,
) -> bool:
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


def _clarification_claims_inventory_not_found(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    return any(
        phrase in cleaned
        for phrase in (
            "未在房源表中找到",
            "暂未在房源表中找到",
            "房源表中未找到",
            "没有在房源表中找到",
            "未找到完全匹配",
            "没有找到完全匹配",
            "未找到“",
            "没找到“",
            "未查到",
            "没查到",
            "查不到",
        )
    )


def _route_unverified_not_found_to_tools(result: dict[str, Any], *, planner_feedback: dict[str, Any] | None) -> dict[str, Any]:
    if planner_feedback:
        return result
    if not result.get("needs_clarification"):
        return result
    clarification = str(result.get("clarification_text") or "")
    if not _clarification_claims_inventory_not_found(clarification):
        return result
    intent = _normalize_intent(result.get("intent"), "general")
    if intent not in {"inventory", "media", "viewing", "context_followup", "general"}:
        return result
    result = dict(result)
    result["needs_clarification"] = False
    result["clarification_text"] = ""
    result["rewrite_layer_not_found_claim_routed_to_tools"] = True
    query_state = dict(result.get("query_state") or {})
    query_state["needs_tool_verification"] = True
    result["query_state"] = query_state
    return result


def _remove_unasked_deposit_clauses(text: str, *, fallback: str) -> str:
    cleaned = str(text or "")
    for pattern in (
        r"[，,；;。]?\s*并?隐含希望了解[^，,；;。]*(?:免押|无忧住|芝麻|押金|服务费)[^，,；;。]*",
        r"[，,；;。]?\s*同时(?:咨询|了解|询问)[^，,；;。]*(?:免押|无忧住|芝麻|押金|服务费)[^，,；;。]*",
        r"[，,；;。]?\s*并(?:咨询|了解|询问)[^，,；;。]*(?:免押|无忧住|芝麻|押金|服务费)[^，,；;。]*",
    ):
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = cleaned.strip(" ，,；;。")
    if any(word in cleaned for word in ("免押", "无忧住", "芝麻", "押金政策", "服务费")):
        return fallback.strip() or cleaned
    return cleaned or fallback.strip()


def _strip_unasked_deposit_from_understanding(
    content: str,
    result: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    if signals.get("wants_deposit"):
        return result
    combined = "\n".join(
        str(result.get(key) or "")
        for key in ("rewritten_query", "effective_query", "clarification_text")
    )
    if not any(word in combined for word in ("免押", "无忧住", "芝麻", "押金政策", "服务费")):
        return result
    result = dict(result)
    for key in ("rewritten_query", "effective_query"):
        if result.get(key):
            result[key] = _remove_unasked_deposit_clauses(str(result[key]), fallback=content)
    query_state = dict(result.get("query_state") or {})
    query_state.pop("wants_deposit", None)
    query_state.pop("deposit", None)
    query_state["unasked_deposit_context_removed"] = True
    result["query_state"] = query_state
    result["unasked_deposit_context_removed"] = True
    return result


def _remove_unasked_media_clauses(text: str, *, fallback: str) -> str:
    cleaned = str(text or "")
    if not cleaned.strip():
        return fallback.strip()
    for pattern in (
        r"[，,；;。]?\s*并?(?:优先)?发送[^，,；;。！？\n]*(?:视频|图片|照片|房间图)[^，,；;。！？\n]*",
        r"[，,；;。]?\s*先发[^，,；;。！？\n]*(?:视频|图片|照片|房间图)[^，,；;。！？\n]*",
        r"[，,；;。]?\s*需要[^，,；;。！？\n]*(?:视频|图片|照片|房间图)[^，,；;。！？\n]*",
    ):
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = cleaned.replace("供用户筛选", "").replace("供客户查看", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,；;。")
    if any(word in cleaned for word in ("视频", "图片", "照片", "房间图")):
        return fallback.strip() or cleaned
    return cleaned or fallback.strip()


def _strip_unasked_media_from_understanding(
    content: str,
    result: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    if signals.get("wants_video") or signals.get("wants_image"):
        return result
    combined = "\n".join(
        str(result.get(key) or "")
        for key in ("rewritten_query", "effective_query", "clarification_text")
    )
    query_state = dict(result.get("query_state") or {})
    has_media_state = bool(
        query_state.get("wants_video")
        or query_state.get("wants_image")
        or _normalize_intent(result.get("intent")) == "media"
    )
    if not has_media_state and not any(word in combined for word in ("视频", "图片", "照片", "房间图")):
        return result
    result = dict(result)
    for key in ("rewritten_query", "effective_query"):
        if result.get(key):
            result[key] = _remove_unasked_media_clauses(str(result[key]), fallback=content)
    query_state.pop("wants_video", None)
    query_state.pop("wants_image", None)
    query_state.pop("pending_video_action", None)
    query_state["unasked_media_context_removed"] = True
    if _normalize_intent(result.get("intent")) == "media":
        result["intent"] = "inventory"
        query_state["intent"] = "inventory"
    result["query_state"] = query_state
    result["candidate_action"] = ""
    result["selected_indices"] = []
    result["unasked_media_context_removed"] = True
    return result


def _content_has_explicit_room_query(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if parse_inventory_query(text).room_refs:
        return True
    if _area_alias_hits(text):
        return True
    if any(word in text for word in ("有没有", "还有吗", "有吗", "有哪些", "还在吗", "在不在", "价格", "多少钱", "预算", "视频", "图片", "今天看", "能看", "密码")):
        return True
    if re.search(r"\d{3,5}\s*(?:左右|以内|以下|上下)?", text):
        return True
    return False


def _guard_stale_inventory_sheet_intent(
    content: str,
    result: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    if signals.get("wants_inventory_sheet"):
        return result
    if _normalize_intent(result.get("intent")) != "inventory_sheet":
        return result
    if not _content_has_explicit_room_query(content):
        return result
    result = dict(result)
    query_state = dict(result.get("query_state") or {})
    query_state.pop("wants_inventory_sheet", None)
    query_state["intent"] = "media" if (signals.get("wants_video") or signals.get("wants_image")) else "inventory"
    result["intent"] = query_state["intent"]
    result["query_state"] = query_state
    for key in ("rewritten_query", "effective_query"):
        if "房源表" in str(result.get(key) or ""):
            result[key] = content
    result["stale_inventory_sheet_context_removed"] = True
    return result


def _force_inventory_sheet_task(content: str, result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    query_state = dict(result.get("query_state") or {})
    task = "用户明确请求发送最新房源表 PNG。"
    if content.strip():
        task = f"{task} 客户原话：{content.strip()}"
    query_state["intent"] = "inventory_sheet"
    query_state["wants_inventory_sheet"] = True
    result["intent"] = "inventory_sheet"
    result["query_state"] = query_state
    result["rewritten_query"] = task
    result["effective_query"] = task
    result["needs_clarification"] = False
    result["clarification_text"] = ""
    return result


def _force_deposit_task(content: str, result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    query_state = dict(result.get("query_state") or {})
    task = "用户咨询免押、押金或免押服务费政策。"
    if content.strip():
        task = f"{task} 客户原话：{content.strip()}"
    query_state["intent"] = "deposit"
    query_state["wants_deposit"] = True
    if _content_wants_utilities(content):
        query_state["wants_utilities"] = True
    result["intent"] = "deposit"
    result["query_state"] = query_state
    result["rewritten_query"] = task
    result["effective_query"] = task
    result["needs_clarification"] = False
    result["clarification_text"] = ""
    return result


def _force_contract_task(content: str, result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    query_state = dict(result.get("query_state") or {})
    task = "用户咨询定房、定金、签约或电子合同流程。"
    if content.strip():
        task = f"{task} 客户原话：{content.strip()}"
    query_state["intent"] = "contract"
    query_state["wants_contract_contact"] = True
    result["intent"] = "contract"
    result["query_state"] = query_state
    result["rewritten_query"] = task
    result["effective_query"] = task
    result["needs_clarification"] = False
    result["clarification_text"] = ""
    return result


def _normalize_field_lookup_understanding(content: str, result: dict[str, Any]) -> dict[str, Any]:
    wants_utilities = _content_wants_utilities(content)
    wants_price = _content_wants_price(content)
    if not (wants_utilities or wants_price) or _content_wants_viewing(content):
        return result

    normalized = dict(result)
    if _normalize_intent(normalized.get("intent")) == "viewing":
        normalized["intent"] = "inventory"

    query_state = dict(normalized.get("query_state") or {})
    if _normalize_intent(query_state.get("intent")) == "viewing":
        query_state["intent"] = "inventory"
    if wants_utilities:
        query_state["wants_utilities"] = True
    if wants_price:
        query_state["wants_price"] = True
    normalized["query_state"] = query_state

    constraint_proof = dict(normalized.get("constraint_proof") or {})
    if _normalize_intent(constraint_proof.get("intent")) == "viewing":
        constraint_proof["intent"] = "inventory"
    if wants_utilities:
        constraint_proof["wants_utilities"] = True
    if wants_price:
        constraint_proof["wants_price"] = True
    normalized["constraint_proof"] = constraint_proof

    structured_task = dict(normalized.get("structured_task") or {})
    if structured_task:
        if _normalize_intent(structured_task.get("intent")) == "viewing":
            structured_task["intent"] = "inventory"
        task_query_state = dict(structured_task.get("query_state") or {})
        if _normalize_intent(task_query_state.get("intent")) == "viewing":
            task_query_state["intent"] = "inventory"
        if wants_utilities:
            task_query_state["wants_utilities"] = True
        if wants_price:
            task_query_state["wants_price"] = True
        structured_task["query_state"] = task_query_state
        task_proof = dict(structured_task.get("constraint_proof") or {})
        if _normalize_intent(task_proof.get("intent")) == "viewing":
            task_proof["intent"] = "inventory"
        if wants_utilities:
            task_proof["wants_utilities"] = True
        if wants_price:
            task_proof["wants_price"] = True
        structured_task["constraint_proof"] = task_proof
        requirements = dict(structured_task.get("tool_requirements") or {})
        requirements["needs_inventory_search"] = True
        if wants_utilities:
            requirements["needs_utilities"] = True
        if _normalize_intent(normalized.get("intent")) != "viewing":
            requirements["needs_viewing_policy"] = False
        structured_task["tool_requirements"] = requirements
        normalized["structured_task"] = structured_task

    return normalized


def _is_bad_area_alias_clarification(text: str) -> bool:
    normalized = normalize_search_text(text)
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "哪个城市",
            "哪座城市",
            "哪个万达",
            "万达广场",
            "哪个区域",
            "哪个板块",
            "哪个商圈",
        )
    )


def _is_bad_one_room_clarification(content: str, clarification_text: str) -> bool:
    normalized_content = normalize_search_text(content)
    normalized_clarification = normalize_search_text(clarification_text)
    if not normalized_content or not normalized_clarification:
        return False
    if not any(token in normalized_content for token in ("一室", "1室", "一房")):
        return False
    if any(token in normalized_content for token in ("一室一厅", "1室1厅", "一房一厅", "带厅", "有厅", "要厅")):
        return False
    return (
        "一室一厅" in normalized_clarification
        and any(token in normalized_clarification for token in ("一室户", "独立客厅", "是否包含", "包不包含", "还是"))
    )


def _references_unbound_room_context(content: str) -> bool:
    text = str(content or "")
    return any(word in text for word in ("这几套", "这几间", "这些", "刚才", "上面", "前面", "里面"))


def _has_room_binding_context(context: dict[str, Any]) -> bool:
    return bool(_candidate_rows(context) or _confirmed_row(context))


def _original_video_followup_without_explicit_target(
    content: str,
    understanding: dict[str, Any],
) -> bool:
    proof = dict(understanding.get("constraint_proof") or {})
    if not proof.get("wants_original_video"):
        return False
    task = dict(understanding.get("structured_task") or {})
    query_text = " ".join(
        str(part).strip()
        for part in (
            content,
            task.get("original_text"),
            understanding.get("effective_query"),
            understanding.get("rewritten_query"),
        )
        if str(part or "").strip()
    )
    if proof.get("room_refs") or _room_refs_from_text(query_text):
        return False
    return any(
        word in query_text
        for word in ("原视频", "原片", "高清", "源文件", "素材源", "下载链接", "太糊", "模糊", "糊", "清楚", "保存", "转发")
    )


def _should_skip_unresolved_community_for_context_action(
    *,
    content: str,
    context: dict[str, Any],
    signals: dict[str, Any],
    understanding: dict[str, Any],
    entity_resolution: dict[str, Any],
) -> bool:
    if entity_resolution.get("community_options"):
        return False
    if entity_resolution.get("communities") or entity_resolution.get("areas"):
        return False
    if not _has_room_binding_context(context):
        return False
    if _selection_indices_from_text(content):
        return True
    if _has_context_reference_word(content):
        return True
    if any(signals.get(key) for key in ("wants_video", "wants_image", "wants_viewing", "wants_original_video")):
        return True
    text = str(content or "")
    if any(
        word in text
        for word in (
            "水电",
            "密码",
            "看房",
            "今天",
            "空出",
            "多少钱",
            "价格",
            "押一付一",
            "押二付一",
            "原视频",
            "高清",
            "清楚",
            "怎么定",
            "定房",
            "联系谁",
        )
    ):
        return True
    return bool(understanding.get("context_reference"))


def _area_query_context_is_clear(text: str, structured_memory: dict[str, Any] | None = None) -> bool:
    parsed = parse_inventory_query(text)
    if any(word in text for word in ("附近", "周边", "这边", "区域", "板块", "商圈", "一带")):
        return True
    if parsed.price_range or parsed.room_type_labels:
        return True
    if any(word in text for word in ("房源", "房子", "有什么", "有哪些", "还有", "预算")):
        return True
    memory_text = json.dumps(structured_memory or {}, ensure_ascii=False, default=str)
    return bool(
        memory_text
        and any(word in memory_text for word in ("附近", "周边", "区域", "预算", "左右", "两室", "一室", "房源"))
    )


def _area_community_clarification(
    *,
    content: str,
    clarification_text: str,
    rows: list[dict[str, Any]],
) -> str:
    normalized_clarification = normalize_search_text(clarification_text)
    if not normalized_clarification:
        return ""
    communities = _community_names(rows)
    for hit in _area_alias_hits(content):
        raw_area = str(hit.get("raw_text") or "").strip()
        canonical_area = str(hit.get("canonical") or raw_area).strip()
        raw_norm = normalize_search_text(raw_area)
        if not raw_norm:
            continue
        area_communities = [
            community
            for community in communities
            if raw_norm in normalize_search_text(community)
        ]
        mentioned = [
            community
            for community in area_communities
            if normalize_search_text(community) in normalized_clarification
        ]
        if mentioned:
            return f"你说的是{raw_area}这个区域，还是{mentioned[0]}这个小区？我先确认一下，避免房源或素材发错。"
        if area_communities and raw_norm in normalized_clarification:
            return f"你说的是{raw_area}这个区域，还是{area_communities[0]}这个小区？我先确认一下，避免房源或素材发错。"
        if canonical_area and raw_norm in normalized_clarification:
            return f"你说的是{raw_area}这个区域吗？我先确认一下，避免房源或素材发错。"
    return ""


async def _understand_message(
    *,
    content: str,
    context: dict[str, Any],
    signals: dict[str, Any],
    planner_feedback: dict[str, Any] | None = None,
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    inventory_read_context = inventory_read_context or _local_inventory_read_context("rewrite")
    rewrite_view = kf_context_memory.rewrite_memory_view(context)
    try:
        resolution_rows = await _inventory_rows_for_resolution(inventory_read_context)
    except TypeError:
        resolution_rows = await _inventory_rows_for_resolution()
    provider_rewrite_index = await _inventory_rewrite_index_for_read_context(inventory_read_context)
    inventory_cache_meta = await _inventory_metadata_for_read_context(inventory_read_context)
    inventory_index = _build_inventory_rewrite_index(
        content=content,
        rows=resolution_rows,
        signals=signals,
        rewrite_view=rewrite_view,
        persisted_index=provider_rewrite_index,
        cache_meta=inventory_cache_meta,
    )
    try:
        result = await asyncio.wait_for(
            reply_generator.rewrite_kf_message(
                content=content,
                structured_memory=rewrite_view,
                inventory_index=inventory_index,
                planner_feedback=planner_feedback or {},
            ),
            timeout=8,
        )
    except asyncio.TimeoutError:
        logger.warning("KF rewrite timed out; using structured fallback: %s", content)
        result = {}
    except Exception as exc:
        logger.exception("KF rewrite failed: %s", exc)
        result = {}
    if not isinstance(result, dict) or not result:
        result = _fallback_understanding(content, signals)
    result.setdefault("needs_clarification", False)
    result.setdefault("clarification_text", "")
    if signals.get("wants_inventory_sheet"):
        result = _force_inventory_sheet_task(content, result)
    if signals.get("wants_deposit"):
        result = _force_deposit_task(content, result)
    if signals.get("wants_contract_contact"):
        result = _force_contract_task(content, result)
    result = _strip_unasked_deposit_from_understanding(content, result, signals)
    result = _strip_unasked_media_from_understanding(content, result, signals)
    result = _guard_stale_inventory_sheet_intent(content, result, signals)
    result = _route_unverified_not_found_to_tools(result, planner_feedback=planner_feedback)
    result = _apply_contextual_followup_rewrite(
        content=content,
        result=result,
        rewrite_view=rewrite_view,
        signals=signals,
    )
    result = _apply_bound_room_context_action_rewrite(
        content=content,
        result=result,
        rewrite_view=rewrite_view,
        signals=signals,
    )
    result["effective_query"] = str(
        result.get("effective_query")
        or result.get("rewritten_query")
        or content
    ).strip()
    if _should_drop_unasked_inherited_room_refs(content, str(result.get("effective_query") or "")):
        result = _drop_unasked_inherited_room_refs(result, content=content)
    elif _should_drop_unasked_inherited_budget(
        content,
        str(result.get("effective_query") or ""),
        result,
        rewrite_view=rewrite_view,
    ):
        result = _drop_unasked_inherited_budget(result, content=content)
    elif _should_drop_unasked_inherited_search_constraints(content, str(result.get("effective_query") or "")):
        result = _drop_unasked_inherited_search_constraints(result, content=content)
    elif _should_drop_unasked_llm_inferred_layout_features(
        content,
        str(result.get("effective_query") or ""),
        dict(result.get("query_state") or {}),
    ):
        result = _drop_unasked_llm_inferred_layout_features(result, content=content)
    result = _strip_llm_inferred_community_for_area_alias(
        content=content,
        result=result,
        rows=resolution_rows,
    )
    entity_resolution = _build_entity_resolution(content, resolution_rows)
    entity_resolution = _contextual_community_resolution(
        content=content,
        entity_resolution=entity_resolution,
        rewrite_view=rewrite_view,
        rows=resolution_rows,
    )
    entity_resolution = _apply_query_state_community_resolution(
        content=content,
        result=result,
        entity_resolution=entity_resolution,
        rows=resolution_rows,
    )
    if _should_drop_inherited_constraints_for_explicit_community(
        content,
        str(result.get("effective_query") or ""),
        entity_resolution,
    ):
        result = _drop_unasked_inherited_search_constraints(
            result,
            content=content,
            drop_area=True,
        )
    constraint_proof = _build_constraint_proof(
        content=content,
        effective_query=str(result.get("effective_query") or content),
        understanding=result,
        entity_resolution=entity_resolution,
        signals=signals,
    )
    if constraint_proof.get("selected_indices"):
        result["selected_indices"] = constraint_proof["selected_indices"]
    result["effective_query"] = _enforce_effective_query(
        content=content,
        understanding=result,
        constraint_proof=constraint_proof,
    )
    result["entity_resolution"] = entity_resolution
    result["constraint_proof"] = constraint_proof
    result["structured_task"] = _build_structured_task(
        content=content,
        understanding=result,
        signals=signals,
        entity_resolution=entity_resolution,
        constraint_proof=constraint_proof,
    )
    orchestrator_tool_plan = _orchestrator_tool_plan_from_understanding(result)
    if orchestrator_tool_plan:
        result["tool_plan"] = orchestrator_tool_plan
        result["structured_task"]["tool_plan"] = orchestrator_tool_plan
    result = _normalize_field_lookup_understanding(content, result)
    result = _force_pending_video_continue_task(content, result, context)
    constraint_proof = dict(result.get("constraint_proof") or constraint_proof)
    query_state = dict(result.get("query_state") or {})
    skip_unresolved_community_clarification = bool(
        _normalize_intent(result.get("intent")) in {"deposit", "contract", "inventory_sheet", "greeting"}
        or signals.get("wants_deposit")
        or signals.get("wants_inventory_sheet")
        or query_state.get("needs_tool_verification")
        or _should_skip_unresolved_community_for_context_action(
            content=content,
            context=context,
            signals=signals,
            understanding=result,
            entity_resolution=entity_resolution,
        )
    )
    if skip_unresolved_community_clarification and result.get("needs_clarification") and not entity_resolution.get("community_options"):
        result["needs_clarification"] = False
        result["clarification_text"] = ""
        result["structured_task"]["clarification"] = {
            "needed": False,
            "text": "",
            "reason": "context_action_bound_to_existing_memory",
        }
    unresolved_community_clarification = "" if skip_unresolved_community_clarification else _unresolved_community_mention_clarification(
        content=content,
        entity_resolution=entity_resolution,
    )
    if unresolved_community_clarification:
        result["needs_clarification"] = True
        result["clarification_text"] = unresolved_community_clarification
        result["structured_task"]["clarification"] = {
            "needed": True,
            "text": unresolved_community_clarification,
            "reason": "community_not_found_in_current_inventory",
        }
    def mark_area_resolved(reason: str) -> None:
        nonlocal entity_resolution, constraint_proof
        entity_resolution = {**entity_resolution, "status": "resolved", "community_options": [], "raw_mentions": []}
        constraint_proof = {**constraint_proof, "proof_status": "complete"}
        result["entity_resolution"] = entity_resolution
        result["constraint_proof"] = constraint_proof
        result["structured_task"]["entity_resolution"] = entity_resolution
        result["structured_task"]["constraint_proof"] = constraint_proof
        result["structured_task"]["clarification"] = {"needed": False, "text": "", "reason": reason}

    trusted_community_sources = {
        str(item.get("source") or item.get("reason") or "")
        for item in entity_resolution.get("communities") or []
        if isinstance(item, dict)
    }
    trusted_community_resolution_reason = ""
    if "unique_room_ref" in trusted_community_sources:
        trusted_community_resolution_reason = "unique_room_ref_resolved"
    elif "exact_community" in trusted_community_sources:
        trusted_community_resolution_reason = "exact_community_resolved"
    elif "alias" in trusted_community_sources or "configured_community_alias" in trusted_community_sources:
        trusted_community_resolution_reason = "community_alias_resolved"
    elif "conversation_memory" in trusted_community_sources:
        trusted_community_resolution_reason = "conversation_memory_resolved"
    elif "query_state_community" in trusted_community_sources:
        trusted_community_resolution_reason = "query_state_community_resolved"
    elif "single_fuzzy_community" in trusted_community_sources:
        trusted_community_resolution_reason = "single_fuzzy_community_resolved"
    if trusted_community_resolution_reason:
        result["needs_clarification"] = False
        result["clarification_text"] = ""
        query_state = dict(result.get("query_state") or {})
        communities = [
            str(item.get("canonical") or "").strip()
            for item in entity_resolution.get("communities") or []
            if isinstance(item, dict) and str(item.get("canonical") or "").strip()
        ]
        if communities:
            query_state["community"] = communities[0]
        result["query_state"] = query_state
        result["structured_task"]["query_state"] = query_state
        result["structured_task"]["clarification"] = {
            "needed": False,
            "text": "",
            "reason": trusted_community_resolution_reason,
        }

    room_ref_mismatch = _room_ref_mismatch_clarification(content, entity_resolution, resolution_rows)
    if room_ref_mismatch:
        result["needs_clarification"] = True
        result["clarification_text"] = room_ref_mismatch
        result["structured_task"]["clarification"] = {
            "needed": True,
            "text": room_ref_mismatch,
            "reason": "room_ref_not_found_in_current_inventory",
        }

    if (
        entity_resolution.get("areas")
        and not entity_resolution.get("communities")
        and entity_resolution.get("status") == "resolved"
        and _is_bad_area_alias_clarification(str(result.get("clarification_text") or ""))
    ):
        result["needs_clarification"] = False
        result["clarification_text"] = ""
        query_state = dict(result.get("query_state") or {})
        query_state["area"] = str(constraint_proof.get("area") or query_state.get("area") or "")
        result["query_state"] = query_state
        result["structured_task"]["query_state"] = query_state
        mark_area_resolved("area_alias_resolved")
    if _is_bad_one_room_clarification(content, str(result.get("clarification_text") or "")):
        result["needs_clarification"] = False
        result["clarification_text"] = ""
        result["structured_task"]["clarification"] = {
            "needed": False,
            "text": "",
            "reason": "one_room_broad_match_includes_one_room_living",
        }
    if (
        signals.get("wants_viewing")
        and _references_unbound_room_context(content)
        and not _has_room_binding_context(context)
    ):
        clarification = "你说的这几套我这边还没绑定到具体房源。你把小区+房号发我，或者回房源序号，我马上查密码和看房注意事项。"
        result["needs_clarification"] = True
        result["clarification_text"] = clarification
        result["structured_task"]["clarification"] = {
            "needed": True,
            "text": clarification,
            "reason": "context_reference_without_bound_room",
        }
    if (
        signals.get("wants_viewing")
        and _references_unbound_room_context(content)
        and _has_room_binding_context(context)
    ):
        result["context_reference"] = True
        if result.get("needs_clarification"):
            result["needs_clarification"] = False
            result["clarification_text"] = ""
        result["structured_task"]["clarification"] = {
            "needed": False,
            "text": "",
            "reason": "bound_context_reference_for_viewing",
        }
    area_clarification_handled = False
    area_community_clarification = _area_community_clarification(
        content=content,
        clarification_text=str(result.get("clarification_text") or ""),
        rows=resolution_rows,
    )
    if (
        entity_resolution.get("areas")
        and not entity_resolution.get("communities")
        and result.get("needs_clarification")
        and area_community_clarification
    ):
        area_clarification_handled = True
        if _area_query_context_is_clear(content, rewrite_view):
            result["needs_clarification"] = False
            result["clarification_text"] = ""
            query_state = dict(result.get("query_state") or {})
            query_state["area"] = str(constraint_proof.get("area") or query_state.get("area") or "")
            result["query_state"] = query_state
            result["structured_task"]["query_state"] = query_state
            mark_area_resolved("area_context_resolved")
        else:
            result["needs_clarification"] = True
            result["clarification_text"] = area_community_clarification
            result["structured_task"]["clarification"] = {
                "needed": True,
                "text": area_community_clarification,
                "reason": "area_or_community_ambiguous",
            }
    if (
        entity_resolution.get("areas")
        and not entity_resolution.get("communities")
        and entity_resolution.get("status") in {"ambiguous", "needs_confirmation"}
        and not area_clarification_handled
    ):
        options: list[str] = []
        for item in entity_resolution.get("community_options") or []:
            options.extend(str(option) for option in item.get("options") or [] if str(option).strip())
        options = list(dict.fromkeys(options))
        raw_area = str((entity_resolution.get("areas") or [{}])[0].get("raw_text") or "").strip()
        if options and raw_area:
            area_clarification_handled = True
            if _area_query_context_is_clear(content, rewrite_view):
                result["needs_clarification"] = False
                result["clarification_text"] = ""
                query_state = dict(result.get("query_state") or {})
                query_state["area"] = str(constraint_proof.get("area") or query_state.get("area") or "")
                result["query_state"] = query_state
                result["structured_task"]["query_state"] = query_state
                mark_area_resolved("area_context_resolved")
            else:
                clarification = f"你说的是{raw_area}这个区域，还是{options[0]}这个小区？我先确认一下，避免房源或素材发错。"
                result["needs_clarification"] = True
                result["clarification_text"] = clarification
                result["structured_task"]["clarification"] = {
                    "needed": True,
                    "text": clarification,
                    "reason": "area_or_community_ambiguous",
                }
    if entity_resolution.get("status") in {"ambiguous", "needs_confirmation"} and not area_clarification_handled:
        clarification = _room_ref_mismatch_clarification(content, entity_resolution, resolution_rows) or _clarification_from_entity_resolution(entity_resolution)
        if clarification:
            result["needs_clarification"] = True
            result["clarification_text"] = clarification
            result["structured_task"]["clarification"] = {
                "needed": True,
                "text": clarification,
                "reason": str(entity_resolution.get("status") or ""),
            }
    if result.get("needs_clarification") and not _clarification_mentions_current_inventory(
        str(result.get("clarification_text") or ""),
        resolution_rows,
        entity_resolution,
    ):
        clarification = _safe_inventory_bound_clarification(
            content=content,
            entity_resolution=entity_resolution,
        )
        result["clarification_text"] = clarification
        result["structured_task"]["clarification"] = {
            "needed": True,
            "text": clarification,
            "reason": "clarification_rebased_to_current_inventory",
        }
    if planner_feedback:
        result["planner_feedback"] = planner_feedback
    result = await _apply_llm1_production_task_packet(
        content=content,
        context=context,
        result=result,
        rewrite_view=rewrite_view,
        inventory_index=inventory_index,
        inventory_read_context=inventory_read_context,
    )
    return result


async def _plan_actions(
    *,
    content: str,
    context: dict[str, Any],
    understanding: dict[str, Any],
    signals: dict[str, Any],
    retry_reason: str = "",
) -> dict[str, Any]:
    result = _orchestrator_tool_plan_from_understanding(understanding)
    if result:
        result["source"] = f"{result.get('source') or 'orchestrator_pre_tool_plan'}+from_rewrite"
    else:
        result = {
            "actions": _fallback_actions_from_structured_task(understanding, signals),
            "confidence": 0.55,
            "source": "structured_task_deterministic_plan",
            "reason": "Orchestrator 工具前阶段未返回 tool_plan，按结构化任务和确定性信号补齐工具计划。",
        }
    if retry_reason:
        result["planner_retry_reason"] = retry_reason
        result["source"] = f"{result.get('source') or 'orchestrator_pre_tool_plan'}+retry_packet"
    if not _safe_action_list(result) and not result.get("need_rewrite_clarification"):
        result["actions"] = _fallback_actions_from_structured_task(understanding, signals)
        result["source"] = f"{result.get('source') or 'planner'}+structured_task_fallback"
    result = _ensure_required_actions(result, understanding, signals)
    result = _ensure_planner_action_contract(result, understanding, signals)
    return result


async def _collect_room_media(
    rows: list[dict[str, Any]],
    *,
    media_kind: str,
    limit: int = KF_VIDEO_SEND_LIMIT,
) -> tuple[list[Path], list[dict[str, Any]], list[str], dict[str, Any]]:
    sync_result: dict[str, Any] = {}

    def list_local(label: str) -> list[Path]:
        if media_kind == "image":
            return media_store.list_room_database_images(label, limit=1)
        return media_store.list_room_database_videos(label, limit=1)

    paths: list[Path] = []
    matched_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in rows:
        label = _row_label(row)
        found = list_local(label)
        if found:
            paths.append(found[0])
            matched_rows.append(row)
            if len(paths) >= limit:
                break
        else:
            missing.append(label)

    if paths:
        return paths, matched_rows, missing, sync_result

    remaining = max(limit - len(paths), 0)
    missing_rows = [
        row
        for row in rows
        if _row_label(row) in set(missing)
    ][:remaining]
    if missing_rows:
        try:
            client = FeishuClient()
            sync_result = await asyncio.wait_for(
                client.sync_media_for_rooms(
                    missing_rows,
                    media_kind=media_kind,
                ),
                timeout=KF_ON_DEMAND_MEDIA_SYNC_TIMEOUT_SECONDS,
            )
            if settings.feishu_region_sync_target_drive_folder_token:
                region_result = await asyncio.wait_for(
                    client.sync_drive_media_for_rooms(
                        missing_rows,
                        media_kind=media_kind,
                        folder_token=settings.feishu_region_sync_target_drive_folder_token,
                    ),
                    timeout=KF_ON_DEMAND_MEDIA_SYNC_TIMEOUT_SECONDS,
                )
                sync_result["region_drive"] = region_result
        except asyncio.TimeoutError:
            logger.warning("on-demand Feishu media sync timeout: kind=%s rows=%s", media_kind, len(missing_rows))
            sync_result = {
                "failed": [
                    {
                        "source": "feishu_on_demand",
                        "reason": f"timeout_after_{KF_ON_DEMAND_MEDIA_SYNC_TIMEOUT_SECONDS}s",
                    }
                ]
            }
        except Exception as exc:
            logger.exception("on-demand Feishu media sync failed: %s", exc)
            sync_result = {"failed": [{"source": "feishu_on_demand", "reason": str(exc)}]}

        if sync_result:
            paths = []
            matched_rows = []
            missing = []
            for row in rows:
                label = _row_label(row)
                found = list_local(label)
                if found:
                    paths.append(found[0])
                    matched_rows.append(row)
                    if len(paths) >= limit:
                        break
                else:
                    missing.append(label)

    return paths, matched_rows, missing, sync_result


async def _execute_tools(
    *,
    actions: list[str],
    content: str,
    context: dict[str, Any],
    understanding: dict[str, Any],
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    inventory_read_context = inventory_read_context or _local_inventory_read_context("tools")
    context = _remember_inventory_read_context(context, inventory_read_context)
    inventory_source_metadata = await _inventory_metadata_for_read_context(inventory_read_context)
    inventory_listing_evidence: list[InventoryListingEvidence] = []
    effective_query = str(understanding.get("effective_query") or content)
    evidence: dict[str, Any] = {
        "actions": actions,
        "inventory_read_context": inventory_read_context.to_log_dict(),
        "inventory_source_metadata": inventory_source_metadata,
        "inventory_listing_evidence": [],
        "inventory_rows": [],
        "target_rows": [],
        "inventory_images": [],
        "image_paths": [],
        "video_paths": [],
        "missing_media": [],
        "media_request": {},
        "outbound_package": {},
        "rule_evidence": {},
    }
    media_request = _media_request_summary(content, understanding)
    if media_request:
        evidence["media_request"] = media_request
    wants_bound_viewing_context = _content_wants_viewing(content) and _references_unbound_room_context(content)

    pending_video_handled = False
    pending_video = kf_context_memory.pending_video_sends(context)
    if (
        "send_video" in actions
        and _wants_continue_pending_video(content, understanding)
        and pending_video
    ):
        pending_paths = [str(path) for path in pending_video.get("paths") or [] if str(path).strip()]
        pending_labels = [
            str(label).strip()
            for label in pending_video.get("labels") or []
            if str(label).strip()
        ]
        if pending_paths:
            evidence["video_paths"] = pending_paths[:KF_VIDEO_SEND_LIMIT]
        if pending_labels:
            evidence["missing_media"].extend(f"{label}:视频" for label in pending_labels[:KF_VIDEO_SEND_LIMIT])
        evidence.setdefault("media_status", {})["video"] = {
            "requested_count": int(pending_video.get("requested_count") or len(pending_paths) or len(pending_labels)),
            "sent_count": len(pending_paths[:KF_VIDEO_SEND_LIMIT]),
            "missing_rooms": pending_labels[:KF_VIDEO_SEND_LIMIT],
            "sync_status": {"source": "pending_video_sends"},
        }
        pending_video_handled = True

    if "send_inventory_sheet" in actions:
        try:
            sheet_result = await inventory_sensitive_access.sheet_artifacts_for_context(
                context=inventory_read_context,
                refresh_func=_refresh_current_inventory_images_for_sheet,
                list_paths_func=_current_inventory_images,
            )
            evidence["inventory_images"] = [str(path) for path in sheet_result.paths]
            evidence["inventory_sheet_artifact_evidence"] = [
                item.to_dict() for item in sheet_result.evidence
            ]
            if sheet_result.error:
                evidence["inventory_image_error"] = str(sheet_result.error.get("message") or sheet_result.error)
        except InventoryReadError as exc:
            logger.warning("inventory sheet artifact blocked by read context: %s", exc.to_dict())
            evidence["inventory_sheet_artifact_error"] = exc.to_dict()
            evidence["inventory_images"] = []

    proof = dict(understanding.get("constraint_proof") or {})
    if proof.get("wants_original_video"):
        evidence["original_video_paths"] = []
        evidence["original_video_urls"] = []
        evidence["material_page_urls"] = []
        evidence["original_video_request"] = {
            "requested": True,
            "has_original_source": False,
            "has_sendable_video": False,
            "sendable_video_count": 0,
            "reason": "当前素材库只提供企业微信可发送视频，没有单独的原视频/高清下载链接证据。",
        }
    task = dict(understanding.get("structured_task") or {})
    original_room_text = " ".join(
        str(part).strip()
        for part in (
            content,
            task.get("original_text"),
        )
        if str(part or "").strip()
    )
    if (
        not pending_video_handled
        and (
            "search_inventory" in actions
            or any(action in actions for action in ("send_image", "send_video", "compact_listing"))
        )
    ):
        inventory_query = _inventory_tool_search_query(
            effective_query=effective_query,
            content=content,
        )
        try:
            rows, search_evidence = await _inventory_search_rows_for_context(
                inventory_read_context,
                inventory_query,
                limit=10,
            )
            inventory_read_turn.extend_listing_evidence(inventory_listing_evidence, search_evidence)
        except InventoryReadError as exc:
            logger.warning("inventory search blocked by read router: %s", exc.to_dict())
            inventory_read_turn.clear_fact_evidence(evidence, exc)
            rows = []
        rows = _filter_rows_by_constraint_proof(
            rows,
            proof,
            query_text=inventory_query,
        )
        if _room_refs_from_text(original_room_text):
            try:
                all_rows, all_evidence = await _inventory_all_rows_for_context(
                    inventory_read_context,
                    limit=500,
                    refresh_if_needed=True,
                )
                original_ref_rows = _rows_matching_original_room_refs(original_room_text, all_rows)
                inventory_read_turn.extend_listing_evidence(
                    inventory_listing_evidence,
                    inventory_read_turn.evidence_for_rows(original_ref_rows, all_rows, all_evidence),
                )
            except Exception as exc:
                logger.debug("original room ref fallback unavailable: %s", exc)
                original_ref_rows = []
            if original_ref_rows:
                rows = original_ref_rows[:10]
        early_selected_indices = _selected_indices_from_understanding(
            understanding,
            " ".join(
                str(part).strip()
                for part in (
                    content,
                    task.get("original_text"),
                    effective_query,
                    understanding.get("rewritten_query"),
                )
                if str(part or "").strip()
            ),
        )
        selection_has_current_scope = bool(
            proof.get("communities")
            or proof.get("area")
            or proof.get("room_refs")
            or _room_refs_from_text(original_room_text)
        )
        if (
            early_selected_indices
            and not _candidate_rows(context)
            and not selection_has_current_scope
            and not proof.get("wants_original_video")
        ):
            rows = []
            evidence["selection_error"] = {
                "requested_indices": early_selected_indices,
                "candidate_count": 0,
                "candidate_labels": [],
                "reason": "missing_current_candidate_set",
            }
        if proof.get("wants_utilities") and any(word in content for word in ("这几套", "这几间", "这些", "刚才", "上面")):
            candidate_rows = _candidate_rows(context)
            if candidate_rows:
                rows = candidate_rows[:10]
        if wants_bound_viewing_context:
            candidate_rows = _candidate_rows(context)
            if not candidate_rows:
                candidate_query = _last_candidate_query_from_memory(context)
                if candidate_query:
                    try:
                        candidate_rows, candidate_evidence = await _inventory_search_rows_for_context(
                            inventory_read_context,
                            candidate_query,
                            limit=10,
                        )
                        inventory_read_turn.extend_listing_evidence(inventory_listing_evidence, candidate_evidence)
                    except InventoryReadError as exc:
                        logger.warning("candidate inventory search blocked by read router: %s", exc.to_dict())
                        inventory_read_turn.clear_fact_evidence(evidence, exc)
                        candidate_rows = []
                    candidate_rows = _filter_rows_by_constraint_proof(
                        candidate_rows,
                        {},
                        query_text=candidate_query,
                    )
                    if candidate_rows:
                        context["last_candidate_set"] = {
                            "intent": "inventory",
                            "query": candidate_query,
                            "candidates": candidate_rows[:10],
                            "created_at": time.time(),
                            "shown_count": min(len(candidate_rows), 10),
                            "total_count": len(candidate_rows),
                            "inventory_cache_meta": inventory_source_metadata,
                        }
            if candidate_rows:
                rows = candidate_rows[:10]
        evidence["inventory_rows"] = rows
        if rows and _should_remember_candidate_set(content=content, understanding=understanding, rows=rows):
            context["last_candidate_set"] = {
                "intent": _normalize_intent(understanding.get("intent"), "inventory"),
                "query": effective_query,
                "candidates": rows[:10],
                "created_at": time.time(),
                "shown_count": min(len(rows), 10),
                "total_count": len(rows),
                "inventory_cache_meta": inventory_source_metadata,
            }
        elif not rows and _should_clear_room_context_after_empty_inventory_search(
            content=content,
            understanding=understanding,
            actions=actions,
        ):
            context.pop("last_candidate_set", None)
            context.pop("confirmed_room", None)
            evidence["candidate_context_cleared"] = {
                "reason": "empty_new_scoped_inventory_search",
                "query": effective_query,
            }
    else:
        rows = []

    field_followup_requires_specific_room = _field_followup_needs_specific_room(content, understanding)
    media_target_error = (
        {}
        if pending_video_handled
        else _media_target_error_for_unclear_room(
            content=content,
            understanding=understanding,
            search_rows=rows,
        )
    )
    target_selection_query_text = " ".join(
        str(part).strip()
        for part in (
            content,
            task.get("original_text"),
            effective_query,
            understanding.get("rewritten_query"),
        )
        if str(part or "").strip()
    )
    target_selection_indices = _selected_indices_from_understanding(
        understanding,
        target_selection_query_text,
    )
    target_selection_uses_candidates = bool(
        target_selection_indices
        and _candidate_rows(context)
        and not _room_refs_from_text(original_room_text)
    )
    target_rows = (
        []
        if pending_video_handled or field_followup_requires_specific_room or media_target_error
        else _target_rows_from_understanding(understanding, context, rows)
    )
    if not target_selection_uses_candidates:
        target_rows = _enforce_target_rows_community_constraints(target_rows, rows, proof)
    if (
        not target_rows
        and not pending_video_handled
        and "send_video" in actions
        and proof.get("wants_original_video")
    ):
        pending_rows = await _pending_video_label_rows(
            context,
            inventory_read_context=inventory_read_context,
        )
        if pending_rows:
            target_rows = pending_rows
            rows = pending_rows
            evidence["inventory_rows"] = pending_rows
            evidence["pending_video_context_bound"] = {
                "reason": "original_video_followup_uses_pending_missing_video_labels",
                "labels": [_row_label(row) for row in pending_rows],
            }
            target_rows = _enforce_target_rows_community_constraints(target_rows, rows, proof)
    original_video_target_error = (
        not target_rows
        and not pending_video_handled
        and "send_video" in actions
        and _original_video_followup_without_explicit_target(content, understanding)
    )
    if not target_rows and not pending_video_handled and _has_bound_room_field_followup(content):
        confirmed_row = _confirmed_row(context)
        if confirmed_row and not _has_explicit_candidate_selection(content):
            target_rows = [confirmed_row]
            target_rows = _enforce_target_rows_community_constraints(target_rows, rows, proof)
    selection_query_text = target_selection_query_text
    candidate_rows_for_selection = _candidate_rows(context)
    selected_indices = target_selection_indices
    current_selection_text = " ".join(
        str(part).strip()
        for part in (content, task.get("original_text"))
        if str(part or "").strip()
    )
    selection_has_direct_room_ref = bool(
        _room_refs_from_text(current_selection_text)
    )
    selection_proof_communities = _proof_community_norms(proof)
    selection_current_text_norm = normalize_search_text(current_selection_text)
    selection_has_current_community_scope = bool(
        selection_proof_communities
        and selection_current_text_norm
        and any(community in selection_current_text_norm for community in selection_proof_communities)
    )
    selection_has_prior_context = bool(
        candidate_rows_for_selection or pending_video
    )
    missing_candidate_selection_context = bool(
        selected_indices
        and not selection_has_prior_context
        and not selection_has_direct_room_ref
        and not original_video_target_error
    )
    invalid_candidate_selection = bool(
        selected_indices
        and candidate_rows_for_selection
        and not target_rows
        and any(index > len(candidate_rows_for_selection) for index in selected_indices)
    )
    invalid_search_selection = bool(
        selected_indices
        and not missing_candidate_selection_context
        and not candidate_rows_for_selection
        and rows
        and any(index > len(rows) for index in selected_indices)
    )
    current_scope_selection_miss = bool(
        selected_indices
        and selection_has_current_community_scope
        and not selection_has_direct_room_ref
        and not target_rows
    )
    if current_scope_selection_miss:
        candidate_labels = [_row_label(row) for row in rows[:10]]
        evidence["selection_error"] = {
            "requested_indices": selected_indices,
            "candidate_count": len(rows),
            "candidate_labels": candidate_labels,
            "reason": "current_scope_selection_not_found",
        }
        rows = []
        target_rows = []
        evidence["inventory_rows"] = []
        evidence["image_rows"] = []
        evidence["video_rows"] = []
        evidence["image_paths"] = []
        evidence["video_paths"] = []
    elif invalid_candidate_selection or invalid_search_selection:
        selection_rows = candidate_rows_for_selection or rows
        candidate_labels = [_row_label(row) for row in selection_rows[:10]]
        evidence["selection_error"] = {
            "requested_indices": selected_indices,
            "candidate_count": len(selection_rows),
            "candidate_labels": candidate_labels,
            "reason": "requested_candidate_index_out_of_range",
        }
        rows = []
        target_rows = []
        evidence["inventory_rows"] = []
        evidence["image_rows"] = []
        evidence["video_rows"] = []
        evidence["image_paths"] = []
        evidence["video_paths"] = []
    elif missing_candidate_selection_context:
        evidence["selection_error"] = {
            "requested_indices": selected_indices,
            "candidate_count": 0,
            "candidate_labels": [],
            "reason": "missing_current_candidate_set",
        }
        rows = []
        target_rows = []
        evidence["inventory_rows"] = []
        evidence["image_rows"] = []
        evidence["video_rows"] = []
        evidence["image_paths"] = []
        evidence["video_paths"] = []
    if (
        not target_rows
        and not evidence.get("selection_error")
        and field_followup_requires_specific_room
        and not original_video_target_error
    ):
        candidate_labels = [_row_label(row) for row in candidate_rows_for_selection[:10]]
        evidence["field_target_error"] = {
            "field": _field_followup_label(content),
            "reason": "missing_specific_room_for_field_followup",
            "candidate_count": len(candidate_rows_for_selection),
            "candidate_labels": candidate_labels,
        }
        rows = []
        evidence["inventory_rows"] = []
    if (
        not target_rows
        and not evidence.get("selection_error")
        and media_target_error
    ):
        evidence["field_target_error"] = media_target_error
        rows = []
        evidence["inventory_rows"] = []
    if (
        not target_rows
        and not evidence.get("selection_error")
        and not evidence.get("field_target_error")
        and original_video_target_error
    ):
        pending_labels = [
            str(label).strip()
            for label in (pending_video or {}).get("labels") or []
            if str(label).strip()
        ]
        pending_labels = list(dict.fromkeys(pending_labels))[:KF_VIDEO_SEND_LIMIT]
        evidence["field_target_error"] = {
            "field": "原视频",
            "reason": "original_video_followup_missing_stable_video_target",
            "candidate_count": 0,
            "candidate_labels": [],
            "pending_labels": pending_labels,
        }
        original_video_request = dict(evidence.get("original_video_request") or {})
        if original_video_request.get("requested"):
            original_video_request["target_binding"] = {
                "stable": False,
                "reason": "previous_video_target_not_bound",
                "pending_labels": pending_labels,
            }
            original_video_request["reason"] = "上一轮没有稳定匹配到视频目标，不能直接给原视频/高清源。"
            evidence["original_video_request"] = original_video_request
        rows = []
        evidence["inventory_rows"] = []
    if (
        not target_rows
        and "explain_unavailable_viewing" in actions
        and wants_bound_viewing_context
        and not invalid_candidate_selection
        and not invalid_search_selection
    ):
        candidate_rows = _candidate_rows(context)
        if not candidate_rows:
            candidate_query = _last_candidate_query_from_memory(context)
            if candidate_query:
                try:
                    candidate_rows, candidate_evidence = await _inventory_search_rows_for_context(
                        inventory_read_context,
                        candidate_query,
                        limit=10,
                    )
                    inventory_read_turn.extend_listing_evidence(inventory_listing_evidence, candidate_evidence)
                except InventoryReadError as exc:
                    logger.warning("viewing inventory search blocked by read router: %s", exc.to_dict())
                    inventory_read_turn.clear_fact_evidence(evidence, exc)
                    candidate_rows = []
            if candidate_rows:
                context["last_candidate_set"] = {
                    "intent": "inventory",
                    "query": candidate_query,
                    "candidates": candidate_rows[:10],
                    "created_at": time.time(),
                    "shown_count": min(len(candidate_rows), 10),
                    "total_count": len(candidate_rows),
                    "inventory_cache_meta": inventory_source_metadata,
                }
        if candidate_rows:
            target_rows = candidate_rows[:10]
    explicit_room_refs = bool(proof.get("room_refs"))
    if (
        not target_rows
        and rows
        and not explicit_room_refs
        and not invalid_candidate_selection
        and not invalid_search_selection
        and not media_target_error
        and not original_video_target_error
        and not selected_indices
        and any(action in actions for action in ("send_image", "send_video"))
    ):
        target_rows = rows[:KF_VIDEO_SEND_LIMIT]
    rows = _rows_with_listing_ids(rows)
    target_rows = _rows_with_listing_ids(target_rows)
    evidence["inventory_rows"] = _rows_with_listing_ids(evidence.get("inventory_rows") or rows)
    evidence["target_rows"] = target_rows
    if target_rows and selected_indices:
        rows = target_rows
        evidence["inventory_rows"] = target_rows

    if evidence.get("inventory_read_error"):
        rows = []
        target_rows = []
        evidence["inventory_rows"] = []
        evidence["target_rows"] = []
        evidence["inventory_listing_evidence"] = []
    else:
        try:
            assert_evidence_consistency(inventory_read_context, inventory_listing_evidence)
            evidence["inventory_listing_evidence"] = [
                item.to_dict() for item in inventory_listing_evidence
            ]
        except InventoryReadError as exc:
            logger.warning("inventory evidence consistency blocked outbound facts: %s", exc.to_dict())
            inventory_read_turn.clear_fact_evidence(evidence, exc)
            rows = []
            target_rows = []

    media_collect_specs: list[tuple[str, Any]] = []
    if "send_image" in actions and target_rows:
        media_collect_specs.append(
            (
                "image",
                _collect_room_media(
                    target_rows,
                    media_kind="image",
                    limit=KF_VIDEO_SEND_LIMIT,
                ),
            )
        )
    if "send_video" in actions and target_rows:
        media_collect_specs.append(
            (
                "video",
                _collect_room_media(
                    target_rows,
                    media_kind="video",
                    limit=KF_VIDEO_SEND_LIMIT,
                ),
            )
        )
    if media_collect_specs:
        media_results = await asyncio.gather(
            *(spec[1] for spec in media_collect_specs),
            return_exceptions=True,
        )
        for (media_kind, _), result in zip(media_collect_specs, media_results):
            if isinstance(result, Exception):
                logger.warning("collect room media failed: kind=%s reason=%s", media_kind, result)
                paths: list[Path] = []
                matched_rows: list[dict[str, Any]] = []
                missing = [_row_label(row) for row in target_rows]
                sync_result = {"failed": [{"source": "collect_room_media", "reason": str(result)}]}
            else:
                paths, matched_rows, missing, sync_result = result

            if media_kind == "image":
                evidence["image_paths"] = [str(path) for path in paths]
                evidence["image_rows"] = _rows_with_listing_ids(matched_rows)
                if sync_result:
                    evidence.setdefault("media_sync", {})["image"] = sync_result
                evidence["missing_media"].extend(f"{label}:图片" for label in missing)
                evidence.setdefault("media_status", {})["image"] = {
                    "requested_count": media_request.get("requested_count") or len(target_rows),
                    "sent_count": len(paths),
                    "missing_rooms": missing,
                    "sync_status": sync_result,
                }
                continue

            evidence["video_paths"] = [str(path) for path in paths]
            evidence["video_rows"] = _rows_with_listing_ids(matched_rows)
            if sync_result:
                evidence.setdefault("media_sync", {})["video"] = sync_result
            evidence["missing_media"].extend(f"{label}:视频" for label in missing)
            requested_count = int(media_request.get("requested_count") or len(target_rows) or 0)
            evidence.setdefault("media_status", {})["video"] = {
                "requested_count": requested_count,
                "sent_count": len(paths),
                "missing_rooms": missing,
                "sync_status": sync_result,
            }
            if proof.get("wants_original_video"):
                source_summary = media_store.original_video_sources_for_paths(paths)
                evidence["original_video_paths"] = source_summary.get("original_video_paths") or []
                evidence["original_video_urls"] = source_summary.get("original_video_urls") or []
                evidence["material_page_urls"] = source_summary.get("material_page_urls") or []
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
                context = kf_context_memory.remember_pending_video_sends(
                    context,
                    paths=[],
                    labels=missing,
                    reason="missing_or_pending_video",
                    requested_count=requested_count,
                    sent_count=len(paths),
                )

    if "send_contract_contact" in actions:
        evidence["rule_evidence"]["contract_contact"] = list(CONTACT_NUMBERS)
    if "send_price_negotiation_contact" in actions:
        evidence["rule_evidence"]["price_contact"] = list(CONTACT_NUMBERS)
    if "send_deposit_policy" in actions:
        evidence["rule_evidence"]["deposit_policy"] = _deposit_policy_evidence()
    if "explain_unavailable_viewing" in actions:
        try:
            viewing_evidence, viewing_rule = await inventory_sensitive_access.viewing_evidence_for_rows(
                context=inventory_read_context,
                rows=target_rows,
                content=content,
                row_labeler=_row_label,
                viewing_text_getter=_viewing_text,
                contact_numbers=CONTACT_NUMBERS,
            )
            evidence["rule_evidence"]["viewing"] = viewing_rule
            evidence["viewing_instruction_evidence"] = [
                item.to_log_dict() for item in viewing_evidence
            ]
        except InventoryReadError as exc:
            logger.warning("viewing access blocked by read context: %s", exc.to_dict())
            evidence["viewing_instruction_error"] = exc.to_dict()
            evidence["rule_evidence"]["viewing"] = {"rooms": [], "contact_numbers": list(CONTACT_NUMBERS)}
        if _viewing_needs_contact(target_rows):
            evidence["rule_evidence"]["viewing_contact"] = list(CONTACT_NUMBERS)

    if target_rows:
        first = target_rows[0]
        context["confirmed_room"] = {
            "row": first,
            "label": _row_label(first),
            "intent": _normalize_intent(understanding.get("intent")),
            "created_at": time.time(),
            "inventory_cache_meta": inventory_source_metadata,
        }

    context["active_query_state"] = dict(understanding.get("query_state") or {})
    return evidence


def _row_brief(row: dict[str, Any]) -> dict[str, str]:
    return {
        "label": _row_label(row),
        "area": _row_value(row, ("区域", "area")),
        "community": _row_value(row, ("小区", "小区名", "community")),
        "room_no": _row_value(row, ("房号", "房间号", "room", "room_no")),
        "layout_description": _row_value(row, ("户型描述", "户型", "户型详情", "户型介绍")),
        "layout": _row_value(row, ("户型分类", "房型")),
        "rent_yayi": _row_value(row, ("押一付一", "押一付", "押一付一月租金")),
        "rent_yaer": _row_value(row, ("押二付一", "押二付", "押二付一月租金")),
        "has_viewing": str(bool(_row_value(row, ("看房方式密码", "看房方式", "密码")))),
        "viewing_summary": _row_viewing_summary(row),
        "utilities": _row_value(row, ("备注", "水电", "说明")),
    }


def _tool_evidence_summary(tool_evidence: dict[str, Any]) -> dict[str, Any]:
    inventory_rows = [row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]
    target_rows = [row for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)]
    image_rows = [row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict)]
    video_rows = [row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)]
    return {
        "actions": list(tool_evidence.get("actions") or []),
        "inventory_row_count": len(inventory_rows),
        "target_row_count": len(target_rows),
        "inventory_image_count": len(tool_evidence.get("inventory_images") or []),
        "image_count": len(tool_evidence.get("image_paths") or []),
        "video_count": len(tool_evidence.get("video_paths") or []),
        "missing_media": list(tool_evidence.get("missing_media") or []),
        "media_request": tool_evidence.get("media_request") or {},
        "media_status": tool_evidence.get("media_status") or {},
        "original_video_request": tool_evidence.get("original_video_request") or {},
        "original_video_url_count": len(tool_evidence.get("original_video_urls") or []),
        "material_page_url_count": len(tool_evidence.get("material_page_urls") or []),
        "inventory_image_error": tool_evidence.get("inventory_image_error") or "",
        "rule_evidence": inventory_sensitive_access.safe_rule_evidence_for_summary(
            tool_evidence.get("rule_evidence") or {}
        ),
        "media_sync": tool_evidence.get("media_sync") or {},
        "planner_reply_result": tool_evidence.get("planner_reply_result") or {},
        "outbound_package": tool_evidence.get("outbound_package") or {},
        "inventory_read_context": tool_evidence.get("inventory_read_context") or {},
        "inventory_source_metadata": tool_evidence.get("inventory_source_metadata") or {},
        "inventory_listing_evidence_count": len(tool_evidence.get("inventory_listing_evidence") or []),
        "viewing_instruction_evidence_count": len(tool_evidence.get("viewing_instruction_evidence") or []),
        "inventory_sheet_artifact_evidence_count": len(tool_evidence.get("inventory_sheet_artifact_evidence") or []),
        "inventory_read_error": tool_evidence.get("inventory_read_error") or {},
        "viewing_instruction_error": tool_evidence.get("viewing_instruction_error") or {},
        "inventory_sheet_artifact_error": tool_evidence.get("inventory_sheet_artifact_error") or {},
        "selection_error": tool_evidence.get("selection_error") or {},
        "field_target_error": tool_evidence.get("field_target_error") or {},
        "field_semantics": FIELD_SEMANTICS,
        "inventory_rows": [_row_brief(row) for row in inventory_rows[:5]],
        "target_rows": [_row_brief(row) for row in target_rows[:5]],
        "image_rows": [_row_brief(row) for row in image_rows[:5]],
        "video_rows": [_row_brief(row) for row in video_rows[:5]],
    }


def _assessment_to_dict(assessment: Any) -> dict[str, Any]:
    status = str(getattr(assessment, "status", "") or getattr(assessment, "action", "") or "pass")
    fallback = str(getattr(assessment, "fallback_text", "") or getattr(assessment, "fallback_reply", "") or "")
    return {
        "action": str(getattr(assessment, "action", "") or status),
        "status": status,
        "reason": str(getattr(assessment, "reason", "") or ""),
        "fallback_text": fallback,
        "fallback_reply": fallback,
    }


def _planner_retry_reason_payload(
    *,
    content: str,
    understanding: dict[str, Any],
    planner_result: dict[str, Any],
    tool_evidence: dict[str, Any],
    draft_reply: str,
    rule_selfcheck: dict[str, Any],
    llm_selfcheck: dict[str, Any],
    reason: str,
) -> str:
    payload = {
        "reason": reason,
        "original_content": content,
        "effective_query": str(understanding.get("effective_query") or understanding.get("rewritten_query") or content),
        "intent": understanding.get("intent"),
        "query_state": understanding.get("query_state") or {},
        "structured_task": understanding.get("structured_task") or {},
        "entity_resolution": understanding.get("entity_resolution") or {},
        "constraint_proof": understanding.get("constraint_proof") or {},
        "understanding": {
            "rewritten_query": understanding.get("rewritten_query"),
            "context_reference": understanding.get("context_reference"),
            "candidate_action": understanding.get("candidate_action"),
            "selected_indices": understanding.get("selected_indices") or [],
            "needs_clarification": understanding.get("needs_clarification"),
        },
        "planner_result": planner_result,
        "tool_evidence": _tool_evidence_summary(tool_evidence),
        "draft_reply": draft_reply,
        "rule_selfcheck": rule_selfcheck,
        "llm_selfcheck": llm_selfcheck,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _reply_mentions_any(reply_text: str, values: list[str]) -> bool:
    normalized_reply = normalize_search_text(reply_text)
    for value in values:
        normalized_value = normalize_search_text(value)
        if normalized_value and normalized_value in normalized_reply:
            return True
    return False


def _reply_segments_for_row(reply_text: str, row: dict[str, Any], *, only_when_label_mentioned: bool) -> list[str]:
    text = str(reply_text or "")
    if not text:
        return []
    if not only_when_label_mentioned:
        return [text]
    label = _row_label(row)
    community = _row_value(row, ("小区", "小区名", "community"))
    room_no = _row_value(row, ("房号", "房间号", "room", "room_no"))
    if only_when_label_mentioned and room_no:
        refs = [item for item in (label, room_no) if item]
    else:
        refs = [item for item in (label, community, room_no) if item]
    if not refs:
        return []
    segments = re.split(r"[\n。；;]", text)
    return [segment for segment in segments if _reply_mentions_any(segment, refs)]


def _payment_field_consistency_failures(reply_text: str, evidence_rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if not evidence_rows or not any(word in str(reply_text or "") for word in ("押一付一", "押二付一")):
        return failures
    only_when_label_mentioned = len(evidence_rows) > 1
    field_specs = (
        ("押一付一", ("押一付一", "押一付"), ("押一付一", "押一付", "月租", "价格")),
        ("押二付一", ("押二付一", "押二付"), ("押二付一", "押二付")),
    )
    payment_alias_tokens = ("押一付一", "押一付", "押二付一", "押二付")
    if len(evidence_rows) > 1:
        mentioned_rows = [row for row in evidence_rows[:8] if _reply_mentions_any(reply_text, [_row_label(row)])]
        if len(mentioned_rows) >= 2:
            for field_name, aliases, row_keys in field_specs:
                expected_numbers: set[str] = set()
                for row in mentioned_rows:
                    expected_numbers.update(re.findall(r"\d{3,5}", _row_value(row, row_keys)))
                if not expected_numbers:
                    continue
                actual_numbers: list[str] = []
                for alias in aliases:
                    actual_numbers.extend(
                        match.group(1)
                        for match in re.finditer(rf"{re.escape(alias)}[^\d]{{0,12}}(\d{{3,5}})", str(reply_text or ""))
                    )
                for actual in actual_numbers:
                    if actual not in expected_numbers:
                        failures.append(
                            f"多房源{field_name}只能使用目标房源真实价格{'/'.join(sorted(expected_numbers))}，回复写成{actual}"
                        )
    for row in evidence_rows[:8]:
        row_label = _row_label(row)
        for segment in _reply_segments_for_row(
            reply_text,
            row,
            only_when_label_mentioned=only_when_label_mentioned,
        ):
            for field_name, aliases, row_keys in field_specs:
                expected = _row_value(row, row_keys)
                expected_numbers = set(re.findall(r"\d{3,5}", expected))
                if not expected_numbers:
                    continue
                for alias in aliases:
                    valid_reverse_mention = False
                    for match in re.finditer(rf"(?<![-\dA-Za-z])(\d{{3,5}})[^\d]{{0,8}}{re.escape(alias)}", segment):
                        before_number = segment[: match.start()]
                        last_delimiter = max(
                            before_number.rfind(delimiter)
                            for delimiter in ("，", ",", "；", ";", "。", ".", "\n")
                        )
                        current_clause_prefix = before_number[last_delimiter + 1 :]
                        if any(token in current_clause_prefix for token in payment_alias_tokens):
                            continue
                        actual = match.group(1)
                        if actual in expected_numbers:
                            valid_reverse_mention = True
                        else:
                            failures.append(
                                f"{row_label}{field_name}应为{'/'.join(sorted(expected_numbers))}，回复写成{actual}"
                            )
                    if valid_reverse_mention:
                        continue
                    for match in re.finditer(rf"{re.escape(alias)}[^\d]{{0,8}}(\d{{3,5}})", segment):
                        actual = match.group(1)
                        if actual not in expected_numbers:
                            failures.append(
                                f"{row_label}{field_name}应为{'/'.join(sorted(expected_numbers))}，回复写成{actual}"
                            )
    return list(dict.fromkeys(failures))


def _utility_field_consistency_failures(reply_text: str, evidence_rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if not evidence_rows or not any(word in str(reply_text or "") for word in ("水电", "水费", "电费", "水", "电")):
        return failures
    only_when_label_mentioned = len(evidence_rows) > 1
    if len(evidence_rows) > 1:
        mentioned_rows = [row for row in evidence_rows[:8] if _reply_mentions_any(reply_text, [_row_label(row)])]
        utility_values = [
            _row_value(row, ("备注", "水电", "水电费", "水电备注"))
            for row in mentioned_rows
        ]
        utility_values = [value for value in utility_values if value]
        if len(set(utility_values)) > 1 and re.search(r"水电[^。；;\n]{0,12}(?:均|都|都是|均为|都为)", str(reply_text or "")):
            failures.append(
                "多套房源水电备注不同，回复不能概括说水电都一样；需要逐套说明"
            )
    for row in evidence_rows[:8]:
        row_label = _row_label(row)
        expected = _row_value(row, ("备注", "水电", "水电费", "水电备注"))
        if not expected:
            continue
        for segment in _reply_segments_for_row(
            reply_text,
            row,
            only_when_label_mentioned=only_when_label_mentioned,
        ):
            if "电1元/度" in expected and "水1元/度" in segment and "水1元/度" not in expected:
                failures.append(f"{row_label}水电备注应为{expected}，回复把电费写成了水费")
            if "水30/月" in expected and "水电费30元/月，水" in segment:
                failures.append(f"{row_label}水电备注应为{expected}，回复把水电字段说反了")
    return list(dict.fromkeys(failures))


def _missing_payment_field_answer_failures(
    *,
    reply_text: str,
    evidence_rows: list[dict[str, Any]],
    content: str,
) -> list[str]:
    original = str(content or "")
    asks_specific_yayi = "押一" in original and "押二" not in original and not any(word in original for word in ("价格", "多少钱", "租金", "多少一月"))
    asks_specific_yaer = "押二" in original and "押一" not in original and not any(word in original for word in ("价格", "多少钱", "租金", "多少一月"))
    if asks_specific_yayi or asks_specific_yaer:
        return []
    if not evidence_rows:
        return []
    if not any(word in original for word in ("价格", "多少钱", "租金", "多少一月", "押一押二", "押一付一", "押二付一")):
        return []
    failures: list[str] = []
    only_when_label_mentioned = len(evidence_rows) > 1
    for row in evidence_rows[:8]:
        row_label = _row_label(row)
        segments = _reply_segments_for_row(
            reply_text,
            row,
            only_when_label_mentioned=only_when_label_mentioned,
        )
        if not segments and len(evidence_rows) == 1:
            segments = [reply_text]
        joined_segment = "\n".join(segments)
        if not joined_segment.strip():
            continue
        rent_yayi = _row_value(row, ("押一付一", "押一付", "押一付一月租金"))
        rent_yaer = _row_value(row, ("押二付一", "押二付", "押二付一月租金"))
        if rent_yayi and not _reply_mentions_any(joined_segment, ["押一付一", "押一付", rent_yayi, *re.findall(r"\d{3,5}", rent_yayi)]):
            failures.append(f"{row_label}价格回复遗漏押一付一月租")
        if rent_yaer and not _reply_mentions_any(joined_segment, ["押二付一", "押二付", rent_yaer, *re.findall(r"\d{3,5}", rent_yaer)]):
            failures.append(f"{row_label}价格回复遗漏押二付一月租")
    return list(dict.fromkeys(failures))


def _budget_payment_scope_failures(
    *,
    reply_text: str,
    evidence_rows: list[dict[str, Any]],
    budget_range: Any,
) -> list[str]:
    if not evidence_rows or not isinstance(budget_range, list) or not budget_range:
        return []
    budget_numbers: list[int] = []
    for value in budget_range:
        try:
            budget_numbers.append(int(float(value)))
            continue
        except (TypeError, ValueError):
            pass
        budget_numbers.extend(int(match) for match in re.findall(r"\d{1,5}", str(value)))
    if not budget_numbers:
        return []
    budget_low = min(budget_numbers)
    budget_high = max(budget_numbers)
    reply = str(reply_text or "")
    has_broad_budget_claim = any(
        token in reply
        for token in (
            "符合预算",
            "满足预算",
            "预算内",
            "预算以内",
            f"{budget_high}以内",
            f"{budget_high}以下",
            f"{budget_high}元以内",
            f"{budget_high}元以下",
        )
    )
    has_payment_scope_note = any(
        token in reply
        for token in (
            "其中一种",
            "有些房源",
            "部分房源",
            "付款方式",
            "押一付一或押二付一",
            "押二付一在预算",
            "押一付一在预算",
        )
    )
    partial_labels: list[str] = []
    for row in evidence_rows[:8]:
        rents: list[int] = []
        for key in ("押一付一", "押一付", "押一付一月租金", "押二付一", "押二付", "押二付一月租金"):
            rents.extend(int(match) for match in re.findall(r"\d{3,5}", _row_value(row, (key,))))
        rents = list(dict.fromkeys(rents))
        if rents and any(value <= budget_high for value in rents) and any(value > budget_high for value in rents):
            label = _row_label(row)
            if label:
                partial_labels.append(label)
    if not partial_labels:
        partial_warning: list[str] = []
    else:
        partial_warning = []
        if has_broad_budget_claim and not has_payment_scope_note:
            partial_warning.append(
                f"{'、'.join(partial_labels[:5])}只有部分付款方式在预算内，回复不能笼统说全部符合预算"
            )
    wording_failures: list[str] = []
    out_of_scope_rows: list[str] = []
    over_budget_words = ("刚过预算", "超预算", "超过预算", "超出预算", "高于预算", "高出预算", "不在预算")
    within_budget_words = ("在预算内", "符合预算", "满足预算", "预算以内")
    for row in evidence_rows[:8]:
        row_label = _row_label(row)
        row_rents: list[int] = []
        for key in ("押一付一", "押一付", "押一付一月租金", "押二付一", "押二付", "押二付一月租金"):
            for match in re.findall(r"\d{3,5}", _row_value(row, (key,))):
                price = int(match)
                row_rents.append(price)
                if price <= budget_high and any(
                    re.search(rf"{price}[^。；;\n]{{0,16}}{word}", reply)
                    or re.search(rf"{word}[^。；;\n]{{0,16}}{price}", reply)
                    for word in over_budget_words
                ):
                    wording_failures.append(
                        f"{row_label}{key}{price}在预算上限{budget_high}以内，回复不能说超预算"
                    )
                if price > budget_high and any(
                    re.search(rf"{price}[^。；;\n]{{0,16}}{word}", reply)
                    or re.search(rf"{word}[^。；;\n]{{0,16}}{price}", reply)
                    for word in within_budget_words
                ):
                    wording_failures.append(
                        f"{row_label}{key}{price}高于预算上限{budget_high}，回复不能说在预算内"
                    )
        if row_label and row_rents and not any(budget_low <= price <= budget_high for price in row_rents):
            if _reply_mentions_any(reply, [row_label]):
                out_of_scope_rows.append(
                    f"{row_label}两种付款方式月租都不在预算{budget_low}-{budget_high}内，回复不能列入匹配结果"
                )
    return list(dict.fromkeys(partial_warning + wording_failures + out_of_scope_rows))


def _constraint_consistency_selfcheck(
    *,
    content: str,
    draft_reply: str,
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
) -> dict[str, Any]:
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    wants_utilities = _understanding_wants_utilities(understanding, content=content)
    wants_price = _content_wants_price(content)
    explicit_viewing_request = _content_wants_viewing(content)
    field_lookup_without_viewing = (wants_utilities or wants_price) and not explicit_viewing_request
    wants_viewing = bool(
        explicit_viewing_request
        or (
            not field_lookup_without_viewing
            and (
                requirements.get("needs_viewing_policy")
                or _normalize_intent(understanding.get("intent")) == "viewing"
            )
        )
    )
    wants_price_comparison = any(
        word in str(content or "")
        for word in ("价格一样", "一样吗", "一样不一样", "哪个便宜", "哪个更便宜", "哪个价格低", "哪个更划算")
    )
    if not proof and not wants_viewing and not wants_utilities and not wants_price:
        return {"status": "pass", "source": "constraint_consistency"}
    target_rows = [row for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)]
    inventory_rows = [row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]
    evidence_rows = target_rows or inventory_rows
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    reply_source = str(tool_evidence.get("deterministic_reply_source") or "")
    clarification_only = bool(actions) and all(action == "clarification" for action in actions)
    if clarification_only:
        entity_resolution = dict(understanding.get("entity_resolution") or {})
        option_labels: list[str] = []
        for item in entity_resolution.get("community_options") or []:
            if isinstance(item, dict):
                option_labels.extend(str(option).strip() for option in item.get("options") or [] if str(option).strip())
        option_labels = list(dict.fromkeys(option_labels))
        clarification_failures: list[str] = []
        if option_labels and not _reply_mentions_any(draft_reply, option_labels[:5]):
            clarification_failures.append("多义小区追问必须展示房源表里的真实候选小区")
        if not any(word in draft_reply for word in ("确认", "哪一个", "哪个", "哪套", "小区", "房号", "序号")):
            clarification_failures.append("追问必须明确说明需要客户补充或确认什么信息")
        if clarification_failures:
            return {
                "status": "retry",
                "action": "retry",
                "source": "constraint_consistency",
                "reason": "；".join(clarification_failures),
            }
        return {"status": "pass", "source": "constraint_consistency", "scope": "clarification"}
    original = str(content or "")
    pending_media_continue = bool(
        str(proof.get("pending_video_action") or "").lower() == "continue"
        and (proof.get("wants_video") or proof.get("wants_image"))
    )
    asks_inventory_existence = any(word in original for word in ("有没有", "还有吗", "有吗", "有哪些", "还在吗", "还在不在", "在不在", "在租吗"))
    inventory_actions = any(action in actions for action in ("search_inventory", "compact_listing"))
    normalized_intent = _normalize_intent(understanding.get("intent"))
    contract_contact_request = _understanding_wants_contract_contact(understanding, content=original)
    action_fulfills_primary_need = bool(
        ("send_inventory_sheet" in actions and tool_evidence.get("inventory_images"))
        or ((proof.get("wants_video") or proof.get("wants_image")) and (tool_evidence.get("video_paths") or tool_evidence.get("image_paths")))
    )
    bound_single_room_field_followup = bool(
        len(target_rows) == 1
        and (wants_utilities or wants_viewing or wants_price)
        and not asks_inventory_existence
    )
    if action_fulfills_primary_need and reply_source.endswith("_hard_rule"):
        return {"status": "pass", "source": "constraint_consistency", "scope": "action_fulfilled_hard_rule"}
    fail_reasons: list[str] = []
    fail_reasons.extend(_customer_visible_format_failures(draft_reply))
    selection_error = dict(tool_evidence.get("selection_error") or {})
    if selection_error:
        requested_indices = [
            int(index)
            for index in selection_error.get("requested_indices") or []
            if str(index).isdigit()
        ]
        requested_tokens = [f"第{index}套" for index in requested_indices]
        candidate_count = int(selection_error.get("candidate_count") or 0)
        candidate_labels = [
            str(label).strip()
            for label in selection_error.get("candidate_labels") or []
            if str(label).strip()
        ]
        if requested_tokens and not _reply_mentions_any(draft_reply, requested_tokens):
            fail_reasons.append("候选编号越界回复必须说明用户选的是哪一套")
        if candidate_count and str(candidate_count) not in draft_reply and "只列" not in draft_reply:
            fail_reasons.append("候选编号越界回复必须说明上一轮候选数量")
        if not any(word in draft_reply for word in ("没有", "没法", "不能按", "只列")):
            fail_reasons.append("候选编号越界回复必须明确说明该编号不存在")
        if candidate_labels and not _reply_mentions_any(draft_reply, candidate_labels[:3]):
            fail_reasons.append("候选编号越界回复必须提到真实候选房源，避免让客户误以为房源不存在")
        if fail_reasons:
            return {
                "status": "retry",
                "action": "retry",
                "source": "constraint_consistency",
                "reason": "；".join(fail_reasons),
            }
        return {"status": "pass", "source": "constraint_consistency", "scope": "candidate_selection_error"}
    field_target_error = dict(tool_evidence.get("field_target_error") or {})
    if field_target_error.get("reason") == "original_video_followup_missing_stable_video_target":
        field_failures: list[str] = []
        if not any(word in draft_reply for word in ("没稳定匹配到视频目标", "没有稳定匹配到视频目标", "不能直接给原视频", "不能直接给高清源")):
            field_failures.append("原视频追问目标未绑定时，回复必须明确说明上一轮没有稳定视频目标")
        if not any(word in draft_reply for word in ("回房源序号", "回我序号", "小区名+房号", "小区+房号")):
            field_failures.append("原视频追问目标未绑定时，回复必须引导客户补充序号或小区房号")
        if any(word in draft_reply for word in ("原视频已发", "高清已发", "源文件已发", "这是")):
            field_failures.append("原视频追问目标未绑定时，回复不能声称已发送或绑定到某套视频")
        if field_failures:
            return {
                "status": "retry",
                "action": "retry",
                "source": "constraint_consistency",
                "reason": "；".join(field_failures),
            }
        return {"status": "pass", "source": "constraint_consistency", "scope": "original_video_target_unbound"}
    answered_existence_words = ("有的", "查到", "找到了", "暂时没查到", "没有", "没找到", "还在")
    fulfilled_action_words = ("发你了", "发给你", "已发")
    if asks_inventory_existence and not any(word in draft_reply for word in answered_existence_words) and not (
        action_fulfills_primary_need and any(word in draft_reply for word in fulfilled_action_words)
    ):
        fail_reasons.append("用户问有没有/有哪些/还在吗，回复没有先明确回答有或没有")
    if inventory_actions and not action_fulfills_primary_need:
        found_claim_words = (
            "有的",
            "查到",
            "找到了",
            "找到",
            "匹配到",
            "还在租",
            "还有",
            "比如",
            "明细发你",
            "发你了",
            "押一付一价格",
            "押二付一价格",
            "看房方式",
            "看房密码",
        )
        no_match_words = ("没查到", "没找到", "暂时没查到", "暂无", "没有完全匹配")
        if evidence_rows:
            room_labels = [_row_label(row) for row in evidence_rows[:8] if _row_label(row)]
            mentions_room = _reply_mentions_any(draft_reply, room_labels)
            primary_inventory_reply_requires_room_mention = bool(
                (asks_inventory_existence or normalized_intent == "inventory")
                and normalized_intent not in {"contract", "deposit", "media", "viewing"}
                and not contract_contact_request
                and not wants_utilities
                and not wants_price
                and not wants_viewing
            )
            if primary_inventory_reply_requires_room_mention and not mentions_room:
                fail_reasons.append("查到房源后回复必须列出至少一个真实小区+房号，不能只说查到几套")
            mentioned_count = sum(1 for label in room_labels if _reply_mentions_any(draft_reply, [label]))
            if re.search(r"(?:两|2)\s*套(?:都|均|全部)?(?:符合|满足|可以|还在|在租)", draft_reply) and mentioned_count < 2:
                fail_reasons.append("回复声称有两套房源符合，但没有列出两套真实小区+房号")
        elif any(word in draft_reply for word in found_claim_words) and not any(word in draft_reply for word in no_match_words):
            fail_reasons.append("房源工具没有返回匹配房源，回复不能声称查到了或还有房源")
        elif any(word in draft_reply for word in no_match_words):
            no_match_positions = [draft_reply.find(word) for word in no_match_words if draft_reply.find(word) >= 0]
            text_before_no_match = draft_reply[: min(no_match_positions)] if no_match_positions else draft_reply
            if re.search(
                r"(?:有的|找到了|找到|匹配到|还有|(?<!没)(?<!未)查到|(?<!没)有)[^，。；\n]{0,24}(房源|一室|两室|三室|单间|整租|符合|预算)",
                text_before_no_match,
            ):
                fail_reasons.append("房源工具没有返回匹配房源，回复不能先说有房源再说没查到")
    if (
        inventory_actions
        and not wants_viewing
        and len(evidence_rows) > 1
        and any(word in draft_reply for word in ("看房密码", "看房方式密码", "密码一般", "一般是960615"))
    ):
        fail_reasons.append("用户未问看房/密码时，多房源推荐不能泛化看房密码；需要看哪套再按具体房源查")
    if not wants_viewing and not _understanding_wants_contract_contact(understanding, content=original):
        proactive_viewing_tokens = ("看房密码", "密码是", "自助看", "空出", "提前联系", "预约", *CONTACT_NUMBERS)
        if any(token in draft_reply for token in proactive_viewing_tokens):
            fail_reasons.append("用户未问看房/密码时，回复不能主动输出空出时间、密码或预约联系方式")
    if wants_price and evidence_rows and not action_fulfills_primary_need:
        price_tokens: list[str] = []
        for row in evidence_rows[:5]:
            for key, value in row.items():
                if any(marker in str(key) for marker in ("押", "价", "租金", "月租")):
                    price_tokens.extend(re.findall(r"\d{3,5}", str(value)))
        if price_tokens and not _reply_mentions_any(draft_reply, price_tokens):
            fail_reasons.append("用户问价格/租金，回复必须直接给出房源表里的押一付一/押二付一月租价格")
        if any(word in draft_reply for word in ("马上发", "稍等", "我查一下", "我先查一下")):
            fail_reasons.append("用户问价格/租金且已有房源证据时，不能只说马上发或稍等")
    if wants_price_comparison and len(evidence_rows) >= 2:
        comparison_words = ("一样", "不一样", "不同", "更便宜", "便宜", "价格低", "更划算", "差")
        if not any(word in draft_reply for word in comparison_words):
            fail_reasons.append("用户问价格是否一样/哪套更便宜，回复必须先给直接对比结论，不能只罗列价格")
    if wants_price_comparison:
        requested_room_refs = [str(item) for item in proof.get("room_refs") or [] if str(item).strip()]
        if requested_room_refs and len(evidence_rows) < len(requested_room_refs):
            unsupported_comparison_words = ("更低", "更便宜", "更划算", "便宜点", "价格低")
            safe_unknown_words = (
                "无法比较",
                "没法比较",
                "不能比较",
                "无法判断",
                "没法判断",
                "无法对比",
                "没法对比",
                "不能对比",
                "暂时没查到",
                "只查到",
                "确认房号",
            )
            if any(word in draft_reply for word in unsupported_comparison_words) and not any(
                word in draft_reply for word in safe_unknown_words
            ):
                fail_reasons.append("价格对比只查到部分房源时，不能直接判断哪套更低；应说明缺少未命中房源证据")
    if proof.get("wants_original_video"):
        has_original_video_evidence = bool(
            tool_evidence.get("original_video_paths")
            or tool_evidence.get("original_video_urls")
            or tool_evidence.get("material_page_urls")
        )
        original_sent_claims = ("原视频已发", "原片已发", "高清已发", "高清版已发", "源文件已发")
        if not has_original_video_evidence and any(word in draft_reply for word in original_sent_claims):
            fail_reasons.append("客户要原视频/高清素材，但工具证据没有原视频文件或下载链接，不能声称已发送原视频")
        if (
            tool_evidence.get("video_paths")
            and not has_original_video_evidence
            and not any(word in draft_reply for word in ("压缩", "下载链接", "素材页", "原视频", "高清", "源文件"))
        ):
            fail_reasons.append("客户要原视频/高清可保存素材时，普通视频发送必须说明可能压缩或需要原素材下载链接")
    payment_failures = _payment_field_consistency_failures(draft_reply, evidence_rows)
    if payment_failures:
        fail_reasons.append("；".join(payment_failures))
    utility_failures = _utility_field_consistency_failures(draft_reply, evidence_rows)
    if utility_failures:
        fail_reasons.append("；".join(utility_failures))
    entity_resolution = dict(understanding.get("entity_resolution") or {})
    community_corrections = [
        item
        for item in entity_resolution.get("community_corrections") or []
        if isinstance(item, dict)
    ]
    if community_corrections:
        correction_markers = ("你说的", "应该是", "匹配到", "按", "我这边查到")
        missing_corrections: list[str] = []
        for item in community_corrections:
            canonical = str(item.get("canonical") or "").strip()
            raw_text = str(item.get("raw_text") or "").strip()
            if not canonical:
                continue
            if canonical in draft_reply and any(marker in draft_reply for marker in correction_markers):
                continue
            missing_corrections.append(f"{raw_text}->{canonical}" if raw_text else canonical)
        if missing_corrections:
            fail_reasons.append(
                "房号唯一命中但小区名被纠正时，回复必须透明说明“你说的应该是标准小区名”："
                + "、".join(missing_corrections[:3])
            )
    missing_payment_answers = _missing_payment_field_answer_failures(
        reply_text=draft_reply,
        evidence_rows=evidence_rows,
        content=content,
    )
    if missing_payment_answers:
        fail_reasons.append("；".join(missing_payment_answers))
    area = str(proof.get("area") or "").strip()
    communities = [str(item) for item in proof.get("communities") or [] if str(item).strip()]
    exact_room_bound = bool(proof.get("room_refs"))
    constraint_scope_requires_search_terms = bool(
        inventory_actions
        and not pending_media_continue
        and not action_fulfills_primary_need
        and not bound_single_room_field_followup
        and not wants_utilities
        and not wants_viewing
        and not contract_contact_request
        and normalized_intent not in {"media", "viewing", "deposit", "contract"}
        and not proof.get("wants_video")
        and not proof.get("wants_original_video")
        and not proof.get("wants_image")
    )
    if (
        area
        and constraint_scope_requires_search_terms
        and not exact_room_bound
        and not communities
        and not bound_single_room_field_followup
    ):
        area_tokens = [token for token in re.split(r"[\s\n/、]+", area) if token]
        if not _reply_mentions_any(draft_reply, area_tokens) and len(evidence_rows) != 1:
            fail_reasons.append(f"回复遗漏区域约束：{area.replace(chr(10), '/')}")
    budget_range = proof.get("budget_range") or []
    if budget_range and constraint_scope_requires_search_terms:
        price_tokens = [str(value) for value in budget_range]
        price_tokens.extend(re.findall(r"\d{3,5}", original))
        row_price_tokens: list[str] = []
        for row in evidence_rows[:5]:
            for key, value in row.items():
                if any(marker in str(key) for marker in ("押", "价", "租金")):
                    row_price_tokens.extend(re.findall(r"\d{3,5}", str(value)))
        if not _reply_mentions_any(draft_reply, price_tokens + row_price_tokens):
            fail_reasons.append(f"回复遗漏预算约束：{budget_range}")
        budget_scope_failures = _budget_payment_scope_failures(
            reply_text=draft_reply,
            evidence_rows=evidence_rows,
            budget_range=budget_range,
        )
        if budget_scope_failures:
            fail_reasons.append("；".join(budget_scope_failures))
    layout = str(proof.get("layout") or "").strip()
    original_requests_layout = any(word in original for word in ("一室", "两室", "二室", "三室", "四室", "单间", "开间", "一厅", "两厅"))
    if (
        layout
        and constraint_scope_requires_search_terms
        and (not exact_room_bound or original_requests_layout)
        and not _reply_mentions_any(draft_reply, [layout, layout.replace("两", "二"), layout.replace("二", "两")])
    ):
        fail_reasons.append(f"回复遗漏户型约束：{layout}")
    features = [str(item).strip() for item in proof.get("features") or [] if str(item).strip()]
    if features and constraint_scope_requires_search_terms and not _reply_mentions_any(draft_reply, features):
        fail_reasons.append(f"回复遗漏特征约束：{'、'.join(features[:3])}")
    if communities and constraint_scope_requires_search_terms and not _reply_mentions_any(draft_reply, communities):
        fail_reasons.append(f"回复遗漏已归一小区：{'、'.join(communities[:3])}")
    if wants_utilities:
        utility_values = [
            _row_value(row, ("备注", "水电", "水电费", "水电备注"))
            for row in evidence_rows[:5]
        ]
        utility_values = [value for value in utility_values if value]
        if utility_values:
            utility_tokens: list[str] = ["水电", "水费", "电费"]
            for value in utility_values:
                utility_tokens.extend(re.findall(r"\d+(?:\.\d+)?", value))
                utility_tokens.append(value)
            if not _reply_mentions_any(draft_reply, utility_tokens):
                fail_reasons.append("用户问水电收取方式，回复遗漏房源备注里的水电证据")
        elif not any(word in draft_reply for word in ("小区", "房号", "哪套", "具体房源", "具体哪套")):
            fail_reasons.append("用户问水电收取方式但没有绑定房源，回复应先追问具体小区和房号")
    if wants_viewing:
        rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
        viewing = rule_evidence.get("viewing") if isinstance(rule_evidence.get("viewing"), dict) else {}
        viewing_rooms = [room for room in viewing.get("rooms") or [] if isinstance(room, dict)] if viewing else []
        asks_bound_context = _references_unbound_room_context(content)
        needs_specific_viewing_target = asks_bound_context or any(
            word in original
            for word in (
                "密码",
                "自己看",
                "自助",
                "开门",
                "打不开",
                "怎么去",
                "怎么进",
                "怎么自己看",
            )
        )
        if asks_bound_context and not target_rows:
            fail_reasons.append("用户问这几套/刚才房源的看房密码，但 Planner 没有绑定候选房源")
        target_rows_have_viewing_value = any(_viewing_text(row) for row in target_rows)
        if target_rows and not viewing_rooms and not target_rows_have_viewing_value:
            fail_reasons.append("看房/密码请求已有目标房源，但工具证据缺少看房方式/密码结果")
        if (
            not target_rows
            and not viewing_rooms
            and (needs_specific_viewing_target or not evidence_rows)
            and not action_fulfills_primary_need
            and not any(word in draft_reply for word in ("小区+房号", "房号", "哪套", "具体房源", "序号"))
        ):
            fail_reasons.append("看房/密码请求未绑定房源时，回复应追问具体房源或序号")
        if not viewing_rooms and any(word in draft_reply for word in ("稍后给您准确回复", "我先帮您确认一下最新房态", "稍后确认")):
            fail_reasons.append("看房/密码请求缺少工具证据时，不能用稍后确认替代 Planner 重新规划")
        if target_rows and not any(
            word in draft_reply
            for word in ("看房", "密码", "空出", "提前联系", "预约", "18758141785", "13282125992", "19941091943")
        ):
            fail_reasons.append("用户问今天看/看房方式时，回复必须包含看房方式、空出时间或预约联系方式")
        if evidence_rows and not target_rows and not any(
            word in draft_reply
            for word in ("空出", "提前联系", "预约", "18758141785", "13282125992", "19941091943")
        ):
            fail_reasons.append("用户问空出/急看/看房方式时，多房源列表也必须包含空出时间、提前联系要求或预约联系方式")
        if target_rows and not any(
            word in draft_reply
            for word in ("看房", "密码", "空出", "提前联系", "预约", "18758141785", "13282125992", "19941091943")
        ):
            fail_reasons.append("用户问今天看/看房方式时，回复必须包含看房方式、空出时间或预约联系方式")
        if not target_rows and len(evidence_rows) > 1 and ("密码" in draft_reply or re.search(r"\b\d{4,8}#", draft_reply)):
            fail_reasons.append("多房源看房列表不能直接给看房密码，应让用户选定具体小区+房号后再查")
        viewing_values = [
            _row_value(row, ("看房方式密码", "看房方式", "密码", "看房密码"))
            for row in evidence_rows[:8]
        ]
        viewing_values = [value for value in viewing_values if value]
        if target_rows and viewing_values:
            viewing_tokens: list[str] = []
            for value in viewing_values:
                viewing_tokens.append(value)
                viewing_tokens.extend(re.findall(r"\d{1,8}(?:#)?", value))
                viewing_tokens.extend(
                    token
                    for token in ("空出", "提前联系", "预约", "转租", "联系", "密码")
                    if token in value
                )
            if not _reply_mentions_any(draft_reply, viewing_tokens + list(CONTACT_NUMBERS)):
                fail_reasons.append("看房/密码回复必须使用目标房源看房方式密码字段，或给出预约核对联系方式")
        has_specific_empty_time = any(
            "空出" in value and re.search(r"\d{1,2}(?:[./月]\d{1,2})?", value)
            for value in viewing_values
        )
        if has_specific_empty_time and any(phrase in draft_reply for phrase in ("都已空出", "全部已空出", "已经空出", "已空出")):
            fail_reasons.append("看房方式里有具体空出时间时，回复不能泛称都已空出，必须按房源说明空出时间或提前联系")
    if not fail_reasons:
        return {"status": "pass", "source": "constraint_consistency"}
    return {
        "status": "retry",
        "action": "retry",
        "reason": "；".join(fail_reasons),
        "fallback_text": "",
        "fallback_reply": "",
        "source": "constraint_consistency",
    }


def _sanitize_rule_selfcheck_for_intent(
    rule_selfcheck: dict[str, Any],
    *,
    content: str,
    understanding: dict[str, Any],
) -> dict[str, Any]:
    status = str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower()
    if status == "pass":
        return rule_selfcheck
    intent = _normalize_intent(understanding.get("intent"))
    reason = str(rule_selfcheck.get("reason") or "")
    if (
        intent != "deposit"
        and not _content_wants_deposit(content)
        and any(marker in reason for marker in ("deposit", "免押", "押金", "无忧住", "芝麻"))
    ):
        return {
            "status": "pass",
            "action": "pass",
            "reason": "ignored_stale_deposit_selfcheck_for_current_intent",
            "source": "intent_guard",
        }
    return rule_selfcheck


def _safe_fallback_for_intent(understanding: dict[str, Any], fallback: str) -> str:
    intent = _normalize_intent(understanding.get("intent"))
    fallback = str(fallback or "").strip()
    if intent == "deposit":
        task = dict(understanding.get("structured_task") or {})
        requirements = dict(task.get("tool_requirements") or {})
        original_text = " ".join(
            str(part).strip()
            for part in (
                task.get("original_text"),
                understanding.get("effective_query"),
                understanding.get("rewritten_query"),
            )
            if str(part or "").strip()
        )
        needs_utilities = bool(
            requirements.get("needs_utilities")
            or dict(understanding.get("constraint_proof") or {}).get("wants_utilities")
            or dict(understanding.get("query_state") or {}).get("wants_utilities")
        )
        if needs_utilities and not _content_wants_deposit(original_text):
            return fallback or "水电要按具体房源备注查，你把小区+房号发我，我马上按那套核对。"
        required = ("芝麻", "服务费")
        fee_tokens = ("5.5", "7%", "8%", "5.5%-8%")
        if not fallback or not all(token in fallback for token in required) or not any(token in fallback for token in fee_tokens):
            fallback = "免押走支付宝无忧住芝麻信用评估，不是完全免费免押。通过后可以不直接付押金，但要按租期支付免押服务费：3个月约5.5%，3-6个月约7%，6-12个月约8%。具体是否支持和最终金额以签约页为准。"
        if needs_utilities and "水电" not in fallback:
            fallback += " 水电要按具体房源备注查，你把小区+房号发我，我再按那套确认。"
        return fallback
    if intent == "contract":
        if (
            not fallback
            or not _reply_mentions_any(fallback, list(CONTACT_NUMBERS))
            or any(word in fallback for word in ("稍后给您准确回复", "我先帮您确认一下最新房态", "稍后确认"))
        ):
            return "客户看中了就直接联系 18758141785 / 13282125992 / 19941091943 预定和签电子合同。联系时把小区名和房号发过去确认房态；定金一般至少半个月房租，个人原因退定不退。"
    if intent == "inventory_sheet":
        if (
            not fallback
            or any(word in fallback for word in ("免押", "无忧住", "芝麻", "押金服务费"))
            or any(word in fallback for word in ("稍后给您准确回复", "我先帮您确认一下最新房态", "稍后确认"))
        ):
            return "房源表图片这边暂时没生成成功，我先不乱发。你也可以先发小区、预算或户型，我按文字先帮你筛。"
    if intent == "media":
        if (
            not fallback
            or any(word in fallback for word in ("免押", "无忧住", "芝麻"))
            or any(word in fallback for word in ("稍后给您准确回复", "我先帮您确认一下最新房态", "稍后确认"))
        ):
            return "我这边暂时没稳定匹配到对应素材，不能乱发视频或图片。你回我序号，或者直接发小区名+房号，我马上按那套查图片/视频。"
    return fallback or settings.default_fallback_reply


def _media_request_summary(content: str, understanding: dict[str, Any]) -> dict[str, Any]:
    proof = dict(understanding.get("constraint_proof") or {})
    wants_video = bool(proof.get("wants_video"))
    wants_image = bool(proof.get("wants_image"))
    if not wants_video and not wants_image:
        return {}
    selected = _int_list(proof.get("selected_indices") or understanding.get("selected_indices"))
    requested_count = len(selected) if selected else 0
    text = str(content or "")
    if not requested_count:
        requested_count = _requested_room_count_from_text(text)
    return {
        "wants_video": wants_video,
        "wants_image": wants_image,
        "requested_count": requested_count,
        "selected_indices": selected,
    }


def _deposit_policy_evidence() -> dict[str, Any]:
    return {
        "name": "支付宝无忧住信用免押",
        "conditions": [
            "芝麻信用需要符合风控要求，通常芝麻分大于等于550分。",
            "合同周期3-12个月。",
            "必须签电子合同。",
            "合同起始时间要在当天及之后。",
            "芝麻信用不能有到期未守约记录。",
            "部分收款卡或房源可能不支持，最终以签约系统校验为准。",
            "目前仅新签合同支持免押。",
        ],
        "service_fee": {
            "3个月": "免押金额5.5%",
            "3-6个月": "免押金额7%",
            "6-12个月": "免押金额8%",
        },
    }


def _viewing_text(row: dict[str, Any]) -> str:
    return _row_value(row, ("看房方式密码", "密码", "看房方式", "看房密码"))


def _viewing_needs_contact(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    return any(item.get("needs_contact") for item in _viewing_evidence(rows).get("rooms") or [])


def _viewing_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    for row in rows:
        viewing = _viewing_text(row)
        normalized = viewing.replace(" ", "")
        has_password = bool(re.search(r"\d{3,8}#?", viewing))
        future_or_unavailable = bool(re.search(r"\d{1,2}\.\d{1,2}\s*空出|空出|未空|未入住", viewing))
        needs_contact = (
            not has_password
            or any(word in viewing for word in ("提前联系", "预约", "转租", "联系", "密码不对", "打不开"))
            or future_or_unavailable
        )
        result.append(
            {
                "room": _row_label(row),
                "viewing": viewing,
                "has_password": has_password,
                "needs_contact": needs_contact,
                "future_or_unavailable": future_or_unavailable,
                "contact_numbers": list(CONTACT_NUMBERS) if needs_contact else [],
                "reason": "需联系确认/预约或还未空出" if needs_contact else "可按看房方式密码自助查看",
                "normalized": normalized,
            }
        )
    return {"rooms": result, "contact_numbers": list(CONTACT_NUMBERS)}


def _reply_for_missing_media(understanding: dict[str, Any], tool_evidence: dict[str, Any]) -> str:
    proof = dict(understanding.get("constraint_proof") or {})
    missing = [str(item) for item in tool_evidence.get("missing_media") or [] if str(item).strip()]
    wants_video = bool(proof.get("wants_video"))
    wants_image = bool(proof.get("wants_image"))
    wants_original_video = bool(proof.get("wants_original_video"))
    pending_continue = str(proof.get("pending_video_action") or "").lower() == "continue"
    if not wants_video and not wants_image:
        return ""
    media_name = "图片和视频" if wants_video and wants_image else ("视频" if wants_video else "图片")
    has_sendable_media = bool(tool_evidence.get("video_paths") or tool_evidence.get("image_paths"))
    sent_rows: list[dict[str, Any]] = []
    if wants_video:
        sent_rows.extend(row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict))
    if wants_image:
        sent_rows.extend(row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict))
    sent_labels = []
    for row in sent_rows[:5]:
        label = _row_label(row)
        if label and label not in sent_labels:
            sent_labels.append(label)
    if wants_original_video and has_sendable_media and sent_labels:
        suffix = (
            f"已找到这些企业微信可发送视频，我先正常发你：{'、'.join(sent_labels)}。"
            "但平台可能会压缩；目前没有单独的原视频/高清下载链接证据，我不冒充原片。"
        )
    elif wants_original_video and has_sendable_media:
        suffix = (
            "已找到的企业微信可发送视频我先正常发你。"
            "但平台可能会压缩；目前没有单独的原视频/高清下载链接证据，我不冒充原片。"
        )
    elif wants_original_video:
        suffix = "这次没有可发送的视频，也没找到原视频/高清下载链接证据。你也可以换一套小区+房号，我再按那套查。"
    elif has_sendable_media and sent_labels:
        suffix = f"已找到这些{media_name}，我先正常发你：{'、'.join(sent_labels)}。"
    elif has_sendable_media:
        suffix = f"已找到的{media_name}我先正常发你。"
    elif pending_continue:
        suffix = f"剩下这些暂时没有可发送的{media_name}，我不乱发。"
    else:
        suffix = f"这次没有可发送的{media_name}。你也可以换一套小区+房号，我再按那套查。"
    if not missing:
        if has_sendable_media:
            return suffix
        requested_rows = [
            row
            for row in (tool_evidence.get("target_rows") or tool_evidence.get("inventory_rows") or [])
            if isinstance(row, dict)
        ]
        labels = [_row_label(row) for row in requested_rows[:5] if _row_label(row)]
        if labels:
            if has_sendable_media:
                return f"有的，房源我查到了，但这几套本地暂时没找到{media_name}：{'、'.join(labels)}。{suffix}"
            return f"房源我查到了，但这几套本地暂时没找到{media_name}：{'、'.join(labels)}。{suffix}"
        condition = "、".join(
            part
            for part in (_constraint_area_text(proof), _constraint_budget_text(proof), _constraint_layout_text(proof))
            if part
        )
        if condition:
            return f"我这边暂时没查到{condition}对应的在租房源，所以这次没有可发的{media_name}。你可以放宽预算、区域或户型，我再帮你筛。"
        return f"我这边还没绑定到具体房源，暂时没法发{media_name}。你回我序号，或者直接发小区名+房号，我马上按那套查。"
    labels = []
    for item in missing[:5]:
        label = item.split(":", 1)[0].strip()
        if label and label not in labels:
            labels.append(label)
    if labels:
        if has_sendable_media:
            return f"有的，这几套房源我查到了；其中本地暂时没找到{media_name}：{'、'.join(labels)}。{suffix}"
        return f"这几套房源我查到了，但本地暂时没找到{media_name}：{'、'.join(labels)}。{suffix}"
    if has_sendable_media:
        return f"有的，房源我查到了，但本地暂时没找到{media_name}。{suffix}"
    return f"房源我查到了，但本地暂时没找到{media_name}。{suffix}"


def _reply_for_deposit_and_utilities(
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    *,
    content: str = "",
) -> str:
    if not _understanding_wants_utilities(understanding, content=content):
        return ""
    rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
    wants_deposit = bool(rule_evidence.get("deposit_policy")) and (
        not str(content or "").strip()
        or _content_wants_deposit(content)
    )
    wants_price = _understanding_wants_price(understanding, content=content)
    source_rows = tool_evidence.get("target_rows") or tool_evidence.get("inventory_rows") or []
    if any(word in content for word in ("这几套", "这几间", "这些", "上面", "刚才")):
        source_rows = tool_evidence.get("inventory_rows") or source_rows
    rows = [row for row in source_rows if isinstance(row, dict)]
    utility_lines: list[str] = []
    utility_price_lines: list[str] = []
    for row in rows[:5]:
        utilities = _row_value(row, ("备注", "水电", "水电费", "水电备注"))
        if utilities:
            utility_lines.append(f"{_row_label(row)}：{utilities}")
        if wants_price:
            price_parts = []
            pay_one = _row_value(row, ("押一付一", "押1付1", "押一", "押一付"))
            pay_two = _row_value(row, ("押二付一", "押2付1", "押二", "押二付"))
            if pay_one:
                price_parts.append(f"押一付一{pay_one}")
            if pay_two:
                price_parts.append(f"押二付一{pay_two}")
            detail_parts = []
            if price_parts:
                detail_parts.append("，".join(price_parts))
            if utilities:
                detail_parts.append(f"水电{utilities}")
            if detail_parts:
                utility_price_lines.append(f"{_row_label(row)}：" + "；".join(detail_parts))

    deposit_text = (
        "免押走支付宝无忧住，通常芝麻信用要符合风控（一般芝麻分550以上），需要签电子合同；"
        "服务费按租期大概是免押金额的5.5%-8%。"
    )
    if wants_price and utility_price_lines:
        prefix = "这几套价格和水电我按房源表给你对比一下："
        if len(utility_price_lines) == 1:
            reply = f"{utility_price_lines[0]}。"
        else:
            reply = prefix + "\n" + "\n".join(f"{index}. {line}" for index, line in enumerate(utility_price_lines, 1))
        if wants_deposit:
            return f"{deposit_text}\n\n{reply}"
        return reply
    if not utility_lines:
        if wants_deposit:
            return (
                f"{deposit_text}\n"
                "水电要按具体房源备注查，你把小区+房号发我，我马上按那套核对。"
            )
        return "水电要按具体房源备注查，你把小区+房号发我，我马上按那套核对。"
    if not wants_deposit:
        if len(utility_lines) == 1:
            return f"{utility_lines[0]}。"
        return "这几套水电我按房源备注给你列一下：\n" + "\n".join(
            f"{index}. {line}" for index, line in enumerate(utility_lines, 1)
        )
    return (
        f"{deposit_text}\n\n"
        "这几套水电我按房源备注给你列一下：\n"
        + "\n".join(f"{index}. {line}" for index, line in enumerate(utility_lines, 1))
    )


def _customer_visible_viewing_text(viewing_text: str, *, allow_password: bool = False) -> str:
    text = str(viewing_text or "").strip()
    if not text:
        return ""
    if allow_password:
        return text
    text = re.sub(r"\b\d{3,8}#?\b", "", text)
    text = re.sub(r"(?:密码|门锁码|开门码|门禁码)(?:不对|错误|失效)?[、，,；; ]*", "", text)
    text = text.replace("看房方式/密码", "看房方式")
    text = text.strip(" #，,；;。")
    return text


def _reply_for_utilities_and_viewing(
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    *,
    content: str = "",
) -> str:
    if not (
        _understanding_wants_utilities(understanding, content=content)
        and _content_wants_viewing(content)
    ):
        return ""
    source_rows = tool_evidence.get("target_rows") or tool_evidence.get("inventory_rows") or []
    rows = [row for row in source_rows if isinstance(row, dict)]
    if not rows:
        return "水电和看房方式都要按具体房源查。你把小区+房号发我，我马上按那套核对。"

    viewing_evidence = _viewing_evidence(rows)
    viewing_by_label = {
        str(item.get("room") or "").strip(): item
        for item in viewing_evidence.get("rooms") or []
        if isinstance(item, dict)
    }
    lines: list[str] = []
    contact_needed = False
    allow_password = _content_wants_password(content)
    for row in rows[:5]:
        label = _row_label(row)
        utilities = _row_value(row, ("备注", "水电", "水电费", "水电备注")) or "水电备注暂时没写"
        viewing_item = viewing_by_label.get(label) or {}
        raw_viewing_text = str(viewing_item.get("viewing") or _row_viewing_summary(row, allow_password=allow_password)).strip()
        viewing_text = _customer_visible_viewing_text(raw_viewing_text, allow_password=allow_password)
        if not viewing_text:
            viewing_text = "看房方式需要联系确认"
        if bool(viewing_item.get("needs_contact")):
            contact_needed = True
        viewing_label = "看房方式/密码" if allow_password else "看房方式"
        lines.append(f"{label}：水电是{utilities}；{viewing_label}是{viewing_text}。")
    reply = "\n".join(lines)
    if contact_needed:
        if allow_password:
            reply += "\n如果密码不对、打不开门，或者还没空出，直接联系 18758141785 / 13282125992 / 19941091943 预约或核对。"
        else:
            reply += "\n如果打不开门、现场情况不一致，或者还没空出，直接联系 18758141785 / 13282125992 / 19941091943 预约或核对。"
    return reply


def _understanding_wants_contract_contact(understanding: dict[str, Any], *, content: str = "") -> bool:
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    query_state = dict(understanding.get("query_state") or {})
    return bool(
        _deterministic_signals(content).get("wants_contract_contact")
        or _normalize_intent(understanding.get("intent")) == "contract"
        or requirements.get("needs_contract_contact")
        or query_state.get("wants_contract_contact")
    )


def _reply_for_contract_contact(
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    *,
    content: str = "",
) -> str:
    if not _understanding_wants_contract_contact(understanding, content=content):
        return ""
    rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
    if not rule_evidence.get("contract_contact"):
        return ""
    return (
        "客户看中了就让他联系 18758141785 / 13282125992 / 19941091943 定房，"
        "定金、签电子合同和具体入住时间都让这几个号码确认。"
        "如果是从房源表里挑的，最好同时带上小区+房号，避免定错房。"
    )


def _reply_for_viewing(tool_evidence: dict[str, Any], *, allow_password: bool = False) -> str:
    rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
    viewing = rule_evidence.get("viewing")
    if not isinstance(viewing, dict):
        return ""
    rooms = [room for room in viewing.get("rooms") or [] if isinstance(room, dict)]
    if not rooms:
        return ""
    lines: list[str] = []
    for item in rooms[:5]:
        room = str(item.get("room") or "这套房源").strip()
        viewing_text = _customer_visible_viewing_text(str(item.get("viewing") or ""), allow_password=allow_password)
        has_password = bool(item.get("has_password"))
        needs_contact = bool(item.get("needs_contact"))
        if has_password and allow_password and viewing_text:
            lines.append(f"{room}：看房方式是 {viewing_text}。")
        elif viewing_text:
            lines.append(f"{room}：{viewing_text}。")
        else:
            lines.append(f"{room}：看房方式需要联系确认。")
        if needs_contact:
            if allow_password:
                lines.append("如果密码不对、打不开门，或者还没空出，直接联系 18758141785 / 13282125992 / 19941091943 预约或核对。")
            else:
                lines.append("如果打不开门、现场情况不一致，或者还没空出，直接联系 18758141785 / 13282125992 / 19941091943 预约或核对。")
    return "\n".join(lines)


def _is_generic_waiting_reply(text: str) -> bool:
    normalized = str(text or "").strip()
    return any(
        phrase in normalized
        for phrase in (
            "我先帮您确认一下最新房态",
            "稍后给您准确回复",
            "我先确认一下最新房态",
            "需要再确认",
            "稍后回复",
        )
    )


def _constraint_area_text(proof: dict[str, Any]) -> str:
    area = str(proof.get("area") or "").strip()
    if not area:
        return ""
    parts = [part.strip() for part in re.split(r"[\n/]+", area) if part.strip()]
    return "、".join(dict.fromkeys(parts))


def _constraint_community_text(proof: dict[str, Any]) -> str:
    communities = [
        str(item or "").strip()
        for item in proof.get("communities") or []
        if str(item or "").strip()
    ]
    return "、".join(dict.fromkeys(communities))


def _constraint_budget_text(proof: dict[str, Any]) -> str:
    budget_range = proof.get("budget_range") or []
    if isinstance(budget_range, list) and len(budget_range) == 2:
        low, high = budget_range
        if low in (0, "0", None, ""):
            return f"{high}以下"
        return f"{low}-{high}左右"
    return str(proof.get("budget_label") or "").strip()


def _constraint_layout_text(proof: dict[str, Any]) -> str:
    layout = str(proof.get("layout") or "").strip()
    if layout.lower() in {"any", "all", "unknown", "none", "null", ""}:
        return ""
    if layout in {"未明确", "不明确", "不限", "无", "任意", "无要求"}:
        return ""
    return layout


def _constraint_feature_text(proof: dict[str, Any]) -> str:
    raw_features = proof.get("features") or proof.get("feature_labels") or []
    if isinstance(raw_features, str):
        raw_features = re.split(r"[,，/、\s]+", raw_features)
    features = [
        str(item).strip()
        for item in raw_features
        if str(item).strip()
        and str(item).strip().lower() not in {"any", "all", "unknown", "none", "null"}
        and str(item).strip() not in {"未明确", "不明确", "不限", "无", "任意", "无要求"}
    ]
    return "、".join(dict.fromkeys(features))


def _constraint_condition_text(proof: dict[str, Any]) -> str:
    community_text = _constraint_community_text(proof)
    area_text = _constraint_area_text(proof)
    return "、".join(
        part
        for part in (
            community_text or area_text,
            _constraint_budget_text(proof),
            _constraint_layout_text(proof),
            _constraint_feature_text(proof),
        )
        if part
    )


def _constraint_preserving_inventory_fallback(
    understanding: dict[str, Any],
    fallback: str,
    tool_evidence: dict[str, Any] | None = None,
) -> str:
    if _normalize_intent(understanding.get("intent")) != "inventory":
        return fallback
    evidence = tool_evidence if isinstance(tool_evidence, dict) else {}
    has_inventory_evidence = bool(
        evidence.get("inventory_rows")
        or evidence.get("target_rows")
        or evidence.get("video_rows")
        or evidence.get("image_rows")
        or evidence.get("video_paths")
        or evidence.get("image_paths")
        or evidence.get("inventory_images")
    )
    if has_inventory_evidence:
        return fallback
    proof = dict(understanding.get("constraint_proof") or {})
    condition = _constraint_condition_text(proof)
    if not condition:
        return fallback
    return f"我这边暂时没查到{condition}完全匹配的在租房源。你可以放宽一点预算、户型或区域，我再帮你筛一轮。"


def _room_facts_text(row: dict[str, Any]) -> str:
    layout = _row_value(row, ("户型分类", "户型", "户型描述", "房型"))
    rent_one = _row_value(row, ("押一付一", "押一付", "月租", "价格"))
    rent_two = _row_value(row, ("押二付一", "押二付"))
    remark = _row_value(row, ("备注", "水电", "水电费", "水电备注"))
    parts = [_row_label(row)]
    if layout:
        parts.append(layout)
    if rent_one:
        parts.append(f"押一付一{rent_one}")
    if rent_two:
        parts.append(f"押二付一{rent_two}")
    if remark:
        parts.append(remark)
    return "，".join(parts)


def _room_line(row: dict[str, Any], index: int) -> str:
    return f"{index}. {_room_facts_text(row)}"


def _row_viewing_summary(row: dict[str, Any], *, allow_password: bool = False) -> str:
    viewing = _row_value(row, ("看房方式密码", "看房方式", "密码", "看房密码"))
    if not viewing:
        return "看房方式需要联系确认"
    if allow_password:
        return viewing
    safe = re.sub(r"\b\d{4,8}#?\b", "", viewing).strip(" #，,；;。")
    if safe:
        return safe
    return "可自助看房，具体看房方式需联系确认"


def _room_line_with_viewing(row: dict[str, Any], index: int, *, allow_password: bool = False) -> str:
    return f"{_room_line(row, index)}，{_row_viewing_summary(row, allow_password=allow_password)}"


def _row_payment_prices(row: dict[str, Any]) -> dict[str, list[int]]:
    prices: dict[str, list[int]] = {}
    for label, keys in (
        ("押一付一", ("押一付一", "押一付", "月租", "价格")),
        ("押二付一", ("押二付一", "押二付")),
    ):
        value = _row_value(row, keys)
        if not value:
            continue
        found = [int(item) for item in re.findall(r"\d{3,5}", value)]
        if found:
            prices[label] = found
    return prices


def _budget_payment_note(rows: list[dict[str, Any]], proof: dict[str, Any]) -> str:
    budget_range = proof.get("budget_range") or []
    if not isinstance(budget_range, list) or len(budget_range) != 2:
        return ""
    try:
        low = int(budget_range[0] or 0)
        high = int(budget_range[1])
    except (TypeError, ValueError):
        return ""
    has_mixed_payment_match = False
    for row in rows:
        all_prices: list[int] = []
        matched_prices: list[int] = []
        for values in _row_payment_prices(row).values():
            all_prices.extend(values)
            matched_prices.extend(price for price in values if low <= price <= high)
        if matched_prices and any(price < low or price > high for price in all_prices):
            has_mixed_payment_match = True
            break
    if not has_mixed_payment_match:
        return ""
    return "有些房源是押一付一或押二付一其中一种月租在预算内，我把两种付款方式下的月租都列出来。"


def _exact_room_refs_from_understanding(understanding: dict[str, Any], proof: dict[str, Any]) -> tuple[str, ...]:
    proof_refs = tuple(str(ref).strip() for ref in proof.get("room_refs") or [] if str(ref).strip())
    if proof_refs:
        return proof_refs
    query = str(
        understanding.get("effective_query")
        or understanding.get("rewritten_query")
        or ""
    )
    if not query:
        return ()
    return parse_inventory_query(query).room_refs


def _reply_for_inventory_search(understanding: dict[str, Any], tool_evidence: dict[str, Any]) -> str:
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    if "search_inventory" not in actions and "compact_listing" not in actions:
        return ""
    rows = [row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    wants_viewing = bool(
        requirements.get("needs_viewing_policy")
        or _normalize_intent(understanding.get("intent")) == "viewing"
        or _content_wants_viewing(str(task.get("original_text") or ""))
        or _content_wants_viewing(str(understanding.get("effective_query") or understanding.get("rewritten_query") or ""))
    )
    condition = _constraint_condition_text(proof)
    if rows:
        exact_room_refs = _exact_room_refs_from_understanding(understanding, proof)
        if len(rows) == 1 and exact_room_refs:
            if wants_viewing:
                viewing_query = " ".join(
                    str(part).strip()
                    for part in (
                        task.get("original_text"),
                        understanding.get("effective_query"),
                        understanding.get("rewritten_query"),
                    )
                    if str(part or "").strip()
                )
                allow_password = _content_wants_password(viewing_query)
                return (
                    f"还在，{_room_facts_text(rows[0])}，{_row_viewing_summary(rows[0], allow_password=allow_password)}。\n"
                    "今天想看的话，建议先联系18758141785 / 13282125992 / 19941091943确认时间。"
                )
            return f"还在，{_room_facts_text(rows[0])}。\n要视频、图片或者看房方式的话，直接说这套就行。"
        heading = f"有的，{condition}我查到这些还在租：" if condition else "有的，我查到这些还在租："
        budget_note = _budget_payment_note(rows, proof)
        if budget_note:
            heading += f"\n{budget_note}"
        if wants_viewing:
            heading = f"有的，{condition}我查到这些看房时间比较明确的在租房源：" if condition else "有的，我查到这些看房时间比较明确的在租房源："
            viewing_rows = [
                row
                for row in rows
                if any(word in _row_viewing_summary(row) for word in ("空出", "提前联系", "预约", "联系"))
            ] or rows
            lines = [_room_line_with_viewing(row, index) for index, row in enumerate(viewing_rows[:8], 1)]
        else:
            lines = [_room_line(row, index) for index, row in enumerate(rows[:8], 1)]
        tail = ""
        if wants_viewing:
            tail = "\n如果客户今天比较急，先联系18758141785 / 13282125992 / 19941091943确认哪套能安排。"
        elif len(rows) > len(lines):
            tail = f"\n还有 {len(rows) - len(lines)} 套没列完，你要视频/图片可以直接回序号或小区+房号。"
        elif len(rows) == 1:
            tail = "\n要视频、图片或者看房方式的话，直接说这套就行。"
        else:
            tail = "\n你要视频、图片或者看房方式的话，直接回序号或小区+房号就行。"
        return heading + "\n" + "\n".join(lines) + tail
    if condition:
        return f"我这边暂时没查到{condition}完全匹配的在租房源。你可以放宽一点预算、户型或区域，我再帮你筛一轮。"
    return "我这边暂时没查到完全匹配的在租房源。你把区域、预算或户型再补一下，我继续帮你筛。"


def _reply_for_candidate_selection_error(tool_evidence: dict[str, Any]) -> str:
    error = dict(tool_evidence.get("selection_error") or {})
    if not error:
        return ""
    requested_indices: list[int] = []
    for index in error.get("requested_indices") or []:
        try:
            number = int(index)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in requested_indices:
            requested_indices.append(number)
    candidate_count = int(error.get("candidate_count") or 0)
    candidate_labels = [str(label).strip() for label in error.get("candidate_labels") or [] if str(label).strip()]
    if not requested_indices:
        return ""
    requested_text = "、".join(f"第{index}套" for index in requested_indices)
    if candidate_count <= 0:
        return f"刚才没有可选候选房源，所以没法按{requested_text}查。你把小区、房号或区域预算再发我，我重新筛。"
    prefix = f"上一轮我只列了{candidate_count}套，没有{requested_text}。"
    if candidate_labels:
        labels_text = "、".join(candidate_labels[:5])
        return f"{prefix}上一轮候选是：{labels_text}。你可以直接说第1套，或者换区域/预算我重新筛。"
    return f"{prefix}你可以重新说小区+房号，或者换区域/预算我重新筛。"


def _reply_for_field_target_error(tool_evidence: dict[str, Any]) -> str:
    error = dict(tool_evidence.get("field_target_error") or {})
    if not error:
        return ""
    if error.get("reason") == "original_video_followup_missing_stable_video_target":
        return (
            "上一轮没稳定匹配到视频目标，所以我不能直接给原视频/高清源，避免发错房源。"
            "你回房源序号，或者直接发小区名+房号，我马上按那套查普通视频和原视频链接。"
        )
    field = str(error.get("field") or "这个信息").strip()
    candidate_labels = [str(label).strip() for label in error.get("candidate_labels") or [] if str(label).strip()]
    if field in {"素材", "视频", "图片"}:
        prefix = f"{field}要按具体房源查，不能乱发。"
    else:
        prefix = f"{field}要按具体房源查。"
    if candidate_labels:
        labels_text = "、".join(f"{index}. {label}" for index, label in enumerate(candidate_labels[:5], 1))
        if error.get("reason") == "community_media_request_missing_room_ref":
            return f"{prefix}这个小区有多套在租：{labels_text}。你回序号或小区+房号，我按那套给你查准。"
        return f"{prefix}刚才候选是：{labels_text}。你回序号或小区+房号，我按那套给你查准。"
    return f"{prefix}你把小区+房号发我，我马上按最新房源表查准。"


def _reply_for_prepared_media(understanding: dict[str, Any], tool_evidence: dict[str, Any]) -> str:
    video_paths = [str(path) for path in tool_evidence.get("video_paths") or [] if str(path).strip()]
    image_paths = [str(path) for path in tool_evidence.get("image_paths") or [] if str(path).strip()]
    if not video_paths and not image_paths:
        return ""
    proof = dict(understanding.get("constraint_proof") or {})
    condition = "、".join(
        part
        for part in (_constraint_area_text(proof), _constraint_budget_text(proof), _constraint_layout_text(proof))
        if part
    )
    video_rows = [row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)]
    image_rows = [row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict)]
    parts: list[str] = []
    if video_paths:
        labels = [_row_label(row) for row in video_rows[: len(video_paths)] if _row_label(row)]
        prefix = f"{condition}的" if condition else ""
        if labels:
            parts.append(f"找到了，{prefix}视频先发你 {len(video_paths)} 套：{'、'.join(labels)}。")
        else:
            parts.append(f"找到了，{prefix}视频先发你 {len(video_paths)} 套。")
    if image_paths:
        labels = [_row_label(row) for row in image_rows[: len(image_paths)] if _row_label(row)]
        prefix = f"{condition}的" if condition else ""
        if labels:
            parts.append(f"{prefix}图片也找到了：{'、'.join(labels)}。")
        else:
            parts.append(f"{prefix}图片也找到了，共 {len(image_paths)} 张。")
    missing_reply = _reply_for_missing_media(understanding, tool_evidence) if tool_evidence.get("missing_media") else ""
    if missing_reply:
        parts.append(missing_reply)
    original_notice = _reply_for_original_video_request(understanding, tool_evidence)
    if original_notice:
        parts.append(original_notice)
    return "\n".join(parts)


def _reply_for_original_video_request(understanding: dict[str, Any], tool_evidence: dict[str, Any]) -> str:
    proof = dict(understanding.get("constraint_proof") or {})
    if not proof.get("wants_original_video"):
        return ""
    original_paths = [str(path) for path in tool_evidence.get("original_video_paths") or [] if str(path).strip()]
    original_urls = [str(url) for url in tool_evidence.get("original_video_urls") or [] if str(url).strip()]
    material_urls = [str(url) for url in tool_evidence.get("material_page_urls") or [] if str(url).strip()]
    has_original_source = bool(original_paths or original_urls or material_urls)
    video_count = len([path for path in tool_evidence.get("video_paths") or [] if str(path).strip()])
    if has_original_source:
        links = list(dict.fromkeys([*original_urls, *material_urls]))[:3]
        if links:
            return "原视频/高清源文件链接也找到了：\n" + "\n".join(links)
        return "素材库记录了原视频源文件，但没有可直接转发的下载链接；我先发微信可发送版。"
    if video_count:
        return "先说明一下：我这边发的是企业微信可发送视频，平台可能会压缩；目前没有单独的原视频/高清下载链接证据，我不冒充原片。"
    return "这套原视频/高清源和普通视频暂时都没找到，我不乱发。"


def _build_outbound_package(reply_text: str, tool_evidence: dict[str, Any]) -> dict[str, Any]:
    inventory_images = [str(path) for path in tool_evidence.get("inventory_images") or []]
    image_paths = [str(path) for path in tool_evidence.get("image_paths") or []]
    video_paths = [str(path) for path in tool_evidence.get("video_paths") or []]
    image_rows = [row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict)]
    video_rows = [row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)]
    return {
        "text": reply_text,
        "inventory_images": inventory_images,
        "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。" if inventory_images else "",
        "image_paths": image_paths,
        "image_explanations": [f"这是{_row_label(row)}的图片。" for row in image_rows[: len(image_paths)]],
        "video_paths": video_paths,
        "video_explanations": [f"这是{_row_label(row)}的视频。" for row in video_rows[: len(video_paths)]],
        "missing_media": list(tool_evidence.get("missing_media") or []),
        "media_request": tool_evidence.get("media_request") or {},
        "media_status": tool_evidence.get("media_status") or {},
        "original_video_request": tool_evidence.get("original_video_request") or {},
        "original_video_urls": list(tool_evidence.get("original_video_urls") or []),
        "material_page_urls": list(tool_evidence.get("material_page_urls") or []),
        "rule_evidence": tool_evidence.get("rule_evidence") or {},
        "reply_source": str(tool_evidence.get("deterministic_reply_source") or ""),
        "target_rooms": [_row_brief(row) for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)],
    }


def _normalize_inventory_sheet_reply_before_selfcheck(
    *,
    draft_reply: str,
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
) -> str:
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    inventory_images = tool_evidence.get("inventory_images") or []
    if "send_inventory_sheet" not in actions or not inventory_images:
        return draft_reply
    constraint_proof = dict(understanding.get("constraint_proof") or {})
    structured_task = dict(understanding.get("structured_task") or {})
    tool_requirements = dict(structured_task.get("tool_requirements") or {})
    if (
        constraint_proof.get("wants_video")
        or constraint_proof.get("wants_image")
        or tool_requirements.get("needs_video")
        or tool_requirements.get("needs_image")
    ):
        return draft_reply
    normalized = "房源表发你了，你可以让客户先整体看一下。"
    area = str(constraint_proof.get("area") or "").strip()
    if area:
        area_label = re.split(r"[\n/、，,]+", area, maxsplit=1)[0].strip()
        if area_label:
            normalized = f"{area_label}附近的房源表发你了，你可以让客户先整体看一下。"
    return normalized


def _normalize_unasked_viewing_tail_before_selfcheck(
    *,
    content: str,
    draft_reply: str,
    understanding: dict[str, Any],
) -> str:
    if _content_wants_viewing(content):
        return draft_reply
    if _normalize_intent(understanding.get("intent")) == "viewing":
        return draft_reply
    text = str(draft_reply or "")
    replacements = {
        "你可以先看视频，选中想了解的再告诉我房号，我来帮你查具体看房方式或预约。": (
            "你可以先看视频，选中想了解的直接告诉我房号，我再帮你查图片或其他细节。"
        ),
        "选中想了解的再告诉我房号，我来帮你查具体看房方式或预约。": (
            "选中想了解的直接告诉我房号，我再帮你查图片或其他细节。"
        ),
        "我来帮你查具体看房方式或预约": "我再帮你查图片或其他细节",
        "我来帮您查具体看房方式或预约": "我再帮您查图片或其他细节",
        "查视频或预约看房": "查视频",
        "具体视频或预约看房": "具体视频",
        "或预约看房": "",
        "或预约": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?:如果|要是)?想约看房[^。！？\n]*(?:。|$)", "", text)
    text = re.sub(r"(?:如果|要是)?想看房[^。！？\n]*(?:。|$)", "", text)
    text = re.sub(r"可以联系\s*\d{6,}[^。！？\n]*(?:预约|看房)[^。！？\n]*(?:。|$)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _normalize_reply_alias_separators_before_selfcheck(draft_reply: str) -> str:
    text = str(draft_reply or "")
    text = re.sub(
        r"(?<=[\u4e00-\u9fffA-Za-z0-9])\|(?=[\u4e00-\u9fffA-Za-z0-9])",
        "、",
        text,
    )
    text = re.sub(r"、{2,}", "、", text)
    return text.strip()


def _normalize_customer_visible_reply_text_before_selfcheck(draft_reply: str) -> str:
    text = _normalize_reply_alias_separators_before_selfcheck(draft_reply)
    text = re.sub(r"\{'min':\s*\d+\s*,\s*'max':\s*\d+\}", "预算范围内", text)
    text = re.sub(r'\{"min":\s*\d+\s*,\s*"max":\s*\d+\}', "预算范围内", text)
    text = re.sub(r"(\d+(?:-\d+)+)-([A-Za-z])(?![A-Za-z0-9])", r"\1\2", text)
    area_names = [
        "拱墅万达",
        "北部软件园",
        "城北万象城",
        "石桥街道",
        "华丰",
        "石桥",
        "永佳",
        "半山",
        "东新园",
        "杭氧",
        "新天地",
        "闸弄口",
        "新塘",
        "元宝塘",
        "东站",
    ]
    for index, left in enumerate(area_names):
        for right in area_names[index + 1 :]:
            text = text.replace(f"{left}\n{right}", f"{left}、{right}")
            text = text.replace(f"{left}\r\n{right}", f"{left}、{right}")
    text = re.sub(r"、{2,}", "、", text)
    return text.strip()


def _customer_visible_format_failures(reply_text: str) -> list[str]:
    text = str(reply_text or "")
    failures: list[str] = []
    if re.search(r"\{[^{}]*(?:'min'|\"min\")[^{}]*(?:'max'|\"max\")[^{}]*\}", text):
        failures.append("回复泄漏了内部预算结构，不能把 {'min': ..., 'max': ...} 这类内容发给客户")
    if "|" in text:
        failures.append("回复泄漏了内部户型别名分隔符 |，需要改成自然中文顿号")
    area_names = {
        "拱墅万达",
        "北部软件园",
        "城北万象城",
        "石桥街道",
        "华丰",
        "石桥",
        "永佳",
        "半山",
        "东新园",
        "杭氧",
        "新天地",
        "闸弄口",
        "新塘",
        "元宝塘",
        "东站",
    }
    visible_lines = [
        line.strip(" ，。、:：")
        for line in re.split(r"\r?\n", text)
        if line.strip()
    ]
    if any(left in area_names and right in area_names for left, right in zip(visible_lines, visible_lines[1:])):
        failures.append("回复把区域名按换行输出，客户可见文本要用顿号或自然短语连接")
    if re.search(r"\d+(?:-\d+)+-[A-Za-z](?![A-Za-z0-9])", text):
        failures.append("回复把房号字母前多加了横杠，应按房源表标准房号展示")
    if "您" in text:
        failures.append("回复口吻过正式，客服话术要更像真人中介助手，统一用“你”自然接话")
    return failures


def _inventory_tool_search_query(*, effective_query: str, content: str) -> str:
    parts = [
        str(effective_query or "").strip(),
        str(content or "").strip(),
    ]
    return " ".join(dict.fromkeys(part for part in parts if part))


def _has_sendable_actions(tool_evidence: dict[str, Any]) -> bool:
    return bool(
        tool_evidence.get("video_paths")
        or tool_evidence.get("image_paths")
        or tool_evidence.get("inventory_images")
    )


def _reply_for_sendable_action_fallback(*, content: str, tool_evidence: dict[str, Any]) -> str:
    """Build a customer-visible action reply from already verified tool evidence."""
    lines: list[str] = []
    video_rows = [row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)]
    image_rows = [row for row in tool_evidence.get("image_rows") or [] if isinstance(row, dict)]
    video_count = len([path for path in tool_evidence.get("video_paths") or [] if str(path).strip()])
    image_count = len([path for path in tool_evidence.get("image_paths") or [] if str(path).strip()])
    inventory_image_count = len([path for path in tool_evidence.get("inventory_images") or [] if str(path).strip()])

    def labels_for(rows: list[dict[str, Any]], limit: int) -> list[str]:
        labels: list[str] = []
        for row in rows[:limit]:
            label = _row_label(row)
            if label and label not in labels:
                labels.append(label)
        return labels

    if video_count:
        labels = labels_for(video_rows, video_count)
        if len(labels) == 1:
            lines.append(f"有的，这套视频我发你：{labels[0]}。")
        elif labels:
            lines.append(f"有的，按你说的条件先发这{len(labels)}套视频，你给客户筛一下：")
            lines.extend(f"{index}. 这是{label}的视频。" for index, label in enumerate(labels, start=1))
        else:
            lines.append(f"有的，视频我先发你，共{video_count}个。")
        original_video_request = dict(tool_evidence.get("original_video_request") or {})
        if original_video_request.get("requested") and not original_video_request.get("has_original_source"):
            lines.append("先说明一下：我这边发的是企业微信可发送视频，平台可能会压缩；目前没有单独的原视频/高清下载链接证据，我不冒充原片。")

    if image_count:
        labels = labels_for(image_rows, image_count)
        if len(labels) == 1:
            lines.append(f"图片也有，这套图片我发你：{labels[0]}。")
        elif labels:
            lines.append(f"图片我也一起发这{len(labels)}套：")
            lines.extend(f"{index}. 这是{label}的图片。" for index, label in enumerate(labels, start=1))
        else:
            lines.append(f"图片我也一起发你，共{image_count}张。")

    if inventory_image_count:
        lines.append("房源表发你了，你可以让客户先整体看一下。")

    missing_items = [str(item).strip() for item in tool_evidence.get("missing_media") or [] if str(item).strip()]
    if missing_items:
        missing_labels = []
        for item in missing_items:
            label = item.split(":", 1)[0].strip()
            media_type = item.split(":", 1)[1].strip() if ":" in item else "素材"
            text = f"{label}{media_type}" if label else item
            if text and text not in missing_labels:
                missing_labels.append(text)
        if missing_labels:
            lines.append("另外，" + "、".join(missing_labels[:5]) + "暂时没找到，我就不乱发。")
    original_video_request = dict(tool_evidence.get("original_video_request") or {})
    if (
        original_video_request.get("requested")
        and not original_video_request.get("has_original_source")
        and not video_count
    ):
        lines.append("这次要的是原视频/高清源，但素材库里普通视频和原片下载链接都没稳定找到，我先不乱发。")

    if not lines:
        return ""
    return _normalize_customer_visible_reply_text_before_selfcheck("\n".join(lines))


def _merge_preserved_sendable_evidence(
    current: dict[str, Any],
    preserved: dict[str, Any],
) -> dict[str, Any]:
    if not preserved or _has_sendable_actions(current):
        return current
    merged = dict(current)
    for key in ("video_paths", "video_rows", "image_paths", "image_rows", "inventory_images"):
        if preserved.get(key) and not merged.get(key):
            merged[key] = preserved.get(key)
    if preserved.get("missing_media"):
        merged["missing_media"] = list(
            dict.fromkeys([*(merged.get("missing_media") or []), *(preserved.get("missing_media") or [])])
        )
    for key in ("media_request", "media_status"):
        if preserved.get(key) and not merged.get(key):
            merged[key] = preserved.get(key)
    actions = [str(action) for action in (preserved.get("actions") or []) + (merged.get("actions") or []) if str(action).strip()]
    if actions:
        merged["actions"] = list(dict.fromkeys(actions))
    return merged


def _outbound_package_selfcheck(
    *,
    draft_reply: str,
    tool_evidence: dict[str, Any],
    outbound_package: dict[str, Any],
) -> dict[str, Any]:
    fail_reasons: list[str] = []
    text = str(draft_reply or "")
    forbidden = ("此处触发", "动作证据", "真实匹配项", "列出真实", "系统生成房源表图片时出现", "[发送房源表图片]")
    if any(word in text for word in forbidden):
        fail_reasons.append("回复泄漏了内部模板词或动作说明")
    placeholder_room_words = (
        "XX小区",
        "xx小区",
        "XX房号",
        "xx房号",
        "某小区",
        "某房号",
        "某某小区",
        "某某房号",
        "比如某",
    )
    if any(word in text for word in placeholder_room_words):
        fail_reasons.append("回复包含占位符或未由工具证据证明的泛称房源")
    video_paths = [str(path) for path in outbound_package.get("video_paths") or [] if str(path).strip()]
    image_paths = [str(path) for path in outbound_package.get("image_paths") or [] if str(path).strip()]
    inventory_images = [str(path) for path in outbound_package.get("inventory_images") or [] if str(path).strip()]
    missing_video_paths = [path for path in video_paths if not Path(path).exists()]
    missing_image_paths = [path for path in image_paths if not Path(path).exists()]
    missing_inventory_images = [path for path in inventory_images if not Path(path).exists()]
    if missing_video_paths:
        fail_reasons.append("视频动作包含不存在的本地文件")
    if missing_image_paths:
        fail_reasons.append("图片动作包含不存在的本地文件")
    if missing_inventory_images:
        fail_reasons.append("房源表动作包含不存在的本地图片")
    if outbound_package.get("inventory_images"):
        if any(
            word in text
            for word in (
                "没法直接发",
                "无法直接发送",
                "暂时无法生成",
                "不能发房源表",
                "暂时没查到",
                "暂未查到",
                "没查到",
                "房源表暂未更新",
                "暂未更新",
                "没生成成功",
                "暂时没生成成功",
                "暂时无法发送",
                "稍后再试",
                "记录需求",
                "第一时间发",
                "有新房源会",
                "先不乱发",
            )
        ):
            fail_reasons.append("房源表图片动作存在，但文本说不能发送房源表")
        if not outbound_package.get("inventory_explanation"):
            fail_reasons.append("房源表动作缺少客户可见解释")
    if outbound_package.get("image_paths") and not outbound_package.get("image_explanations"):
        fail_reasons.append("图片动作缺少小区+房号解释")
    if outbound_package.get("video_paths") and not outbound_package.get("video_explanations"):
        fail_reasons.append("视频动作缺少小区+房号解释")
    if outbound_package.get("video_paths") and not outbound_package.get("missing_media"):
        if any(
            phrase in text
            for phrase in (
                "视频暂时没同步",
                "视频还没同步",
                "暂时没找到视频",
                "没找到视频",
                "暂无视频",
                "没有视频",
                "暂时没有视频",
                "暂无其他视频素材",
                "正在补同步",
                "稍后会发",
                "稍后发您",
                "稍后发你",
                "等补全后",
                "补全后我再发",
            )
        ):
            fail_reasons.append("视频已准备发送时，回复不能再暗示该视频未同步或稍后再发")
    if outbound_package.get("image_paths") and not outbound_package.get("missing_media"):
        if any(
            phrase in text
            for phrase in (
                "图片暂时没同步",
                "图片还没同步",
                "照片暂时没同步",
                "暂时没找到图片",
                "暂时没找到照片",
                "没找到图片",
                "没找到照片",
                "暂无图片",
                "暂无照片",
                "没有图片",
                "没有照片",
                "暂时没有图片",
                "暂时没有照片",
                "暂无其他图片素材",
                "暂无其他照片素材",
                "正在补同步",
                "稍后会发",
                "稍后发您",
                "稍后发你",
                "等补全后",
                "补全后我再发",
            )
        ):
            fail_reasons.append("图片已准备发送时，回复不能再暗示该图片未同步或稍后再发")
    if outbound_package.get("missing_media") and not any(word in text for word in ("没找到", "暂无", "暂时没有", "只找到", "缺", "补素材", "正在")):
        fail_reasons.append("素材缺失时回复没有说明缺哪类素材或下一步")
    missing_labels: list[str] = []
    for item in outbound_package.get("missing_media") or []:
        label = str(item).split(":", 1)[0].strip()
        if label and label not in missing_labels:
            missing_labels.append(label)
    if missing_labels and not _reply_mentions_any(text, missing_labels[:5]):
        fail_reasons.append("素材缺失时回复必须点名缺素材的小区+房号，不能只说有些房源缺素材")
    sent_video_labels: list[str] = []
    for row in tool_evidence.get("video_rows") or []:
        if isinstance(row, dict):
            label = _row_label(row)
            if label and label not in sent_video_labels:
                sent_video_labels.append(label)
    if not sent_video_labels:
        for explanation in outbound_package.get("video_explanations") or []:
            label = str(explanation).strip()
            label = label.removeprefix("这是").removesuffix("的视频。").removesuffix("的视频")
            if label and label not in sent_video_labels:
                sent_video_labels.append(label)
    sent_image_labels: list[str] = []
    for row in tool_evidence.get("image_rows") or []:
        if isinstance(row, dict):
            label = _row_label(row)
            if label and label not in sent_image_labels:
                sent_image_labels.append(label)
    if not sent_image_labels:
        for explanation in outbound_package.get("image_explanations") or []:
            label = str(explanation).strip()
            label = label.removeprefix("这是").removesuffix("的图片。").removesuffix("的图片")
            if label and label not in sent_image_labels:
                sent_image_labels.append(label)
    duplicate_video_missing = [label for label in sent_video_labels if label in missing_labels]
    duplicate_image_missing = [label for label in sent_image_labels if label in missing_labels]
    if duplicate_video_missing:
        fail_reasons.append("同一房源既准备发送视频，又被标记为缺视频素材")
    if duplicate_image_missing:
        fail_reasons.append("同一房源既准备发送图片，又被标记为缺图片素材")
    def label_local_segment_at(label: str, label_index: int, labels: list[str]) -> str:
        if label_index < 0:
            return ""
        end_index = len(text)
        for other in labels:
            if other == label:
                continue
            other_index = text.find(other, label_index + len(label))
            if other_index >= 0:
                end_index = min(end_index, other_index)
        punctuation_indexes = [
            index
            for index in (text.find(mark, label_index + len(label)) for mark in ("。", "；", ";", "\n"))
            if index >= 0
        ]
        if punctuation_indexes:
            end_index = min(end_index, min(punctuation_indexes))
        return text[label_index:end_index]

    def label_local_segments(label: str, labels: list[str]) -> list[str]:
        return [
            label_local_segment_at(label, match.start(), labels)
            for match in re.finditer(re.escape(label), text)
        ]

    if len(sent_video_labels) >= 2:
        for label in sent_video_labels:
            label_index = text.find(label)
            if label_index < 0:
                fail_reasons.append("多套视频动作回复必须逐套点名小区+房号")
                break
            if not any("视频" in segment for segment in label_local_segments(label, sent_video_labels)):
                fail_reasons.append("多套视频动作回复必须逐套说明“这是小区+房号的视频”")
                break
    if len(sent_image_labels) >= 2:
        for label in sent_image_labels:
            label_index = text.find(label)
            if label_index < 0:
                fail_reasons.append("多套图片动作回复必须逐套点名小区+房号")
                break
            if not any(
                any(word in segment for word in ("图片", "照片"))
                for segment in label_local_segments(label, sent_image_labels)
            ):
                fail_reasons.append("多套图片动作回复必须逐套说明“这是小区+房号的图片”")
                break
    negative_video_phrases = ("暂时没找到视频", "没找到视频", "暂无视频", "没有视频", "暂时没有视频")
    negative_image_phrases = ("暂时没找到图片", "暂时没找到照片", "没找到图片", "没找到照片", "暂无图片", "暂无照片", "没有图片", "没有照片", "暂时没有图片", "暂时没有照片")
    for label in sent_video_labels:
        label_index = text.find(label)
        if label_index < 0:
            continue
        window = text[max(0, label_index - 24) : min(len(text), label_index + len(label) + 24)]
        if any(phrase in window for phrase in negative_video_phrases):
            fail_reasons.append("视频动作已准备发送，但文本说同一房源没有视频")
            break
    for label in sent_image_labels:
        label_index = text.find(label)
        if label_index < 0:
            continue
        window = text[max(0, label_index - 24) : min(len(text), label_index + len(label) + 24)]
        if any(phrase in window for phrase in negative_image_phrases):
            fail_reasons.append("图片动作已准备发送，但文本说同一房源没有图片")
            break
    if not video_paths and not image_paths and any(phrase in text for phrase in ("前面已经有的素材", "先看前面已经有", "先看前面已发")):
        fail_reasons.append("没有待发送图片/视频动作时，回复不能让客户先看前面素材")
    rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
    if rule_evidence.get("contract_contact") and not _reply_mentions_any(text, list(CONTACT_NUMBERS)):
        fail_reasons.append("定房/合同链路回复遗漏联系电话")
    if rule_evidence.get("viewing_contact") and not _reply_mentions_any(text, list(CONTACT_NUMBERS)):
        fail_reasons.append("看房密码/预约异常回复遗漏联系电话")
    if rule_evidence.get("deposit_policy"):
        if "芝麻" not in text or not any(token in text for token in ("5.5", "7%", "8%", "服务费")):
            fail_reasons.append("免押链路回复遗漏芝麻信用或服务费规则")
    if any(word in text for word in ("押一付一押金", "押二付一押金", "押一付一的押金", "押二付一的押金")):
        fail_reasons.append("把押一付一/押二付一误说成押金金额")
    if any(
        phrase in text
        for phrase in (
            "我这边直接电话",
            "我直接电话",
            "我给你打电话",
            "我给您打电话",
            "电话跟你核对",
            "电话跟您核对",
            "直接电话跟",
            "电话我",
            "打我电话",
            "直接打我",
            "可以电话我",
        )
    ):
        fail_reasons.append("客服机器人不能声称自己会打电话核对，只能让用户联系号码或提供具体房号继续查")
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    media_request = dict(tool_evidence.get("media_request") or {})
    original_video_request = dict(tool_evidence.get("original_video_request") or {})
    if original_video_request.get("requested"):
        has_original_source = bool(
            original_video_request.get("has_original_source")
            or outbound_package.get("original_video_urls")
            or outbound_package.get("material_page_urls")
        )
        original_claim_words = ("原视频已发", "原片已发", "高清已发", "高清版已发", "源文件已发")
        if not has_original_source and any(word in text for word in original_claim_words):
            fail_reasons.append("客户要原视频/高清时，没有原片证据不能声称已发送原视频")
        if outbound_package.get("video_paths") and not has_original_source:
            if not any(word in text for word in ("压缩", "原视频", "高清", "源文件", "下载链接", "素材页")):
                fail_reasons.append("客户要原视频/高清时，普通视频动作必须说明企微视频可能压缩或没有原素材下载链接")
        if not outbound_package.get("video_paths") and not has_original_source:
            if not any(word in text for word in ("原视频", "高清", "源文件", "下载链接", "没找到", "暂无", "暂时没有", "不乱发")):
                fail_reasons.append("客户要原视频/高清但没有视频动作时，回复必须说明没有稳定找到原素材或普通视频")
    if media_request.get("wants_video") and not outbound_package.get("video_paths"):
        if not any(
            word in text
            for word in (
                "没找到",
                "没匹配到",
                "暂无",
                "暂时没有",
                "还没稳定绑定",
                "回我序号",
                "小区名+房号",
                "小区+房号",
                "具体房源",
            )
        ):
            fail_reasons.append("视频请求没有发出视频时，回复没有说明缺视频或目标未绑定")
    if "视频" in text and not outbound_package.get("video_paths"):
        claimed_video_send = False
        for match in re.finditer("视频", text):
            window = text[max(0, match.start() - 20) : min(len(text), match.end() + 20)]
            if any(word in window for word in ("没找到", "没匹配到", "暂无", "暂时没有", "不能发", "无法发")):
                continue
            if any(word in window for word in ("已找到", "已发", "发出", "发你", "发您", "发给", "稍后发", "马上发", "补发")):
                claimed_video_send = True
                break
        if claimed_video_send:
            fail_reasons.append("回复声称发送或即将发送视频，但待发送包没有视频动作")
    if media_request.get("wants_image") and not outbound_package.get("image_paths"):
        if not any(word in text for word in ("没找到", "暂无", "暂时没有", "还没稳定绑定", "回我序号", "小区名+房号")):
            fail_reasons.append("图片请求没有发出图片时，回复没有说明缺图片或目标未绑定")
    if any(word in text for word in ("图片", "照片")) and not outbound_package.get("image_paths") and not outbound_package.get("inventory_images"):
        if any(word in text for word in ("已找到", "已发", "发出", "发你", "发您", "发给", "稍后发", "马上发", "补发")):
            fail_reasons.append("回复声称发送或即将发送图片，但待发送包没有图片动作")
    if "send_inventory_sheet" in actions and not outbound_package.get("inventory_images"):
        if "房源表" in text and any(word in text for word in ("发你", "发给你", "查收", "发送")):
            fail_reasons.append("房源表图片未准备好，但回复说已经发送或即将发送")
    if fail_reasons:
        return {
            "status": "retry",
            "action": "retry",
            "reason": "；".join(fail_reasons),
            "fallback_text": "",
            "fallback_reply": "",
            "source": "outbound_package_selfcheck",
        }
    return {"status": "pass", "source": "outbound_package_selfcheck"}


def _local_human_context_selfcheck(
    *,
    content: str,
    draft_reply: str,
    tool_evidence: dict[str, Any],
    deterministic_reply_source: str = "",
) -> dict[str, Any]:
    text = str(draft_reply or "").strip()
    fail_reasons: list[str] = []
    if deterministic_reply_source == "planner_missing_reply_text":
        fail_reasons.append("Planner 没有输出 reply_text，不能进入发送阶段")
    if not text:
        fail_reasons.append("回复为空")
    if len(text) > 900 and not tool_evidence.get("inventory_images"):
        fail_reasons.append("回复过长，不适合企业微信客服即时对话")
    if text.count("请问") >= 2 and any(word in content for word in ("视频", "图片", "密码", "房源表", "有没有", "还有")):
        fail_reasons.append("回复连续追问，未贴着客户当前需求接话")
    if any(word in text for word in ("作为AI", "作为一个AI", "根据上下文", "系统显示", "无法完成该请求")):
        fail_reasons.append("回复有系统模板感，不像真人客服")
    if any(
        word in text
        for word in (
            "马上通知你",
            "马上通知您",
            "有合适的马上通知",
            "帮你留意着",
            "帮您留意着",
            "后面通知你",
            "后面通知您",
            "稍后会通知",
            "稍后通知",
            "稍后会发",
            "后面会发",
            "补同步中，稍后",
            "补齐后发",
            "后面素材补齐",
            "素材补齐再处理",
            "素材补齐后",
            "稍后发你",
            "稍后发您",
            "有新资源会第一时间通知",
            "有新房源会第一时间通知",
            "有新资源第一时间通知",
            "有新房源第一时间通知",
            "持续更新房源信息",
            "持续更新房源",
            "稍后会为您推送",
            "稍后会为你推送",
            "为您推送最新",
            "为你推送最新",
            "推送最新信息",
        )
    ):
        fail_reasons.append("机器人不能承诺后续主动通知客户，只能说明可继续按条件查询或让客户再发需求")
    generic_waiting_phrases = (
        "我先帮您确认一下最新房态",
        "稍后给您准确回复",
        "稍后确认",
        "我查一下",
        "我先查一下",
        "我这边查一下",
        "帮你查一下",
        "帮您查一下",
        "我帮你查一下",
        "我帮您查一下",
        "稍等",
        "稍等一下",
        "马上查",
        "我马上查",
        "马上给你查",
        "马上给您查",
        "马上列出来",
        "马上给你列",
        "马上给您列",
        "列出来给你",
        "列出来给您",
    )
    has_tool_evidence = bool(
        tool_evidence.get("inventory_rows")
        or tool_evidence.get("target_rows")
        or tool_evidence.get("image_paths")
        or tool_evidence.get("video_paths")
        or tool_evidence.get("inventory_images")
        or tool_evidence.get("rule_evidence")
    )
    if any(word in text for word in generic_waiting_phrases):
        if any(word in content for word in ("有没有", "还有", "有", "还在", "在不在", "价格", "多少钱", "视频", "图片", "密码", "看房", "房源表", "哪些")):
            fail_reasons.append("客户问明确房源/素材/看房问题时，不能只回复稍后确认")
    asks_list_or_area_inventory = any(word in content for word in ("有哪些", "哪几套", "几套", "附近", "这边有没有", "有没有")) and not any(
        word in content for word in ("还在", "在不在")
    )
    first_sentence = re.split(r"[。！？\n]", text, maxsplit=1)[0]
    if asks_list_or_area_inventory and (
        re.match(r"^\s*(在的|还在|还在的)", text)
        or (first_sentence.strip().startswith("在") and "还在" in first_sentence)
    ):
        fail_reasons.append("列表/区域查询要先说有的或暂时没查到，不能用还在/在的开头")
    asks_viewing_or_password = _content_wants_viewing(content) or any(
        word in content
        for word in (
            "能看",
            "自助",
            "怎么去",
            "预约",
            "门",
            "打不通",
        )
    )
    if not asks_viewing_or_password and ("密码" in text or re.search(r"\b\d{4,8}#", text)):
        fail_reasons.append("客户没有问看房或密码时，回复不能主动给看房密码")
    needs_contact_in_reply = any(
        word in text
        for word in (
            "提前联系",
            "提前预约",
            "预约看房",
            "安排看房",
            "确认时间",
            "看房需联系",
            "需要联系",
            "未空出",
            "没空出",
            "还没空出",
            "不能直接看",
            "暂时不能看",
        )
    )
    if needs_contact_in_reply and not any(number in text for number in CONTACT_NUMBERS):
        fail_reasons.append("看房需要提前联系、预约或未空出时，必须给三个联系电话")
    media_request = dict(tool_evidence.get("media_request") or {})
    asks_video_or_image = (
        any(word in content for word in ("视频", "图片", "照片", "素材", "实拍"))
        or bool(media_request.get("wants_video") or media_request.get("wants_image"))
        or deterministic_reply_source in {"prepared_media_reply", "missing_media_reply"}
    )
    asks_inventory_sheet = "房源表" in content or "表格" in content or re.search(r"(^|[，。；\s])表(发|给|看|来|传)", content)
    if not asks_video_or_image and (
        any(
            word in text
            for word in (
                "视频素材",
                "图片素材",
                "照片素材",
                "暂时没找到视频",
                "暂时没匹配到可直接发视频",
                "暂未匹配到可直接发视频",
                "暂未匹配到可直接发送视频",
                "暂未查到可直接发视频",
                "暂未查到可直接发送视频",
                "暂时没查到可直接发视频",
                "暂时没查到可直接发送视频",
                "没匹配到可直接发送视频",
                "没查到可直接发视频",
                "没查到可直接发送视频",
                "暂未匹配到可直接发送视频或图片",
                "暂未查到可直接发送视频或图片",
                "暂未匹配到可直接发视频或图片",
                "暂未查到可直接发视频或图片",
                "没匹配到可直接发视频",
                "没匹配到可直接发送视频或图片",
                "暂时没找到图片",
                "暂时没找到照片",
                "视频暂未找到",
                "图片暂未找到",
                "照片暂未找到",
                "视频和图片暂未找到",
            )
        )
        or re.search(r"(视频|图片|照片)[^。；\n]{0,12}(暂未|暂无|没找到|未找到|没匹配到)", text)
        or re.search(r"(暂未|暂无|没找到|未找到|没匹配到|没查到|暂时没查到)[^。；\n]{0,18}(视频|图片|照片)", text)
    ):
        fail_reasons.append("客户没有问图片/视频时，回复不能主动提素材缺失")
    if not asks_inventory_sheet and "房源表" in text and any(word in text for word in ("可以先看", "先看", "发你", "发您")):
        fail_reasons.append("客户没有要房源表时，回复不能主动让客户看房源表")
    if "回复序号" in text:
        numbered = bool(re.search(r"(?:^|\n)\s*[1-9][0-9]?[.、]", text))
        if not numbered:
            fail_reasons.append("回复要求客户回序号，但正文没有编号候选")
    if deterministic_reply_source and deterministic_reply_source.endswith("_hard_rule"):
        if any(word in text for word in ("我帮您", "我帮你", "我这边", "发你", "联系", "看房", "客户", "这套", "这几套")):
            pass
        elif not tool_evidence.get("inventory_images"):
            fail_reasons.append("确定性规则回复口吻过硬，缺少客服接话感")
    if fail_reasons:
        return {
            "status": "retry",
            "action": "retry",
            "reason": "；".join(fail_reasons),
            "source": "local_human_context_selfcheck",
            "fallback_reply": "",
        }
    return {
        "status": "pass",
        "source": "local_human_context_selfcheck",
        "checked": ["context_fit", "human_tone_baseline"],
    }


def _needs_llm_final_selfcheck(
    *,
    content: str,
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    draft_reply: str,
    rule_selfcheck: dict[str, Any],
    deterministic_reply_source: str,
    retry_reason: str,
) -> bool:
    rule_status = str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower()
    if rule_status != "pass":
        return False
    planner_stage_selfcheck = kf_orchestrator_flow.planner_reply_selfcheck(
        tool_evidence.get("planner_reply_result") or {}
    )
    if (
        deterministic_reply_source == "planner_reply_text"
        and str(planner_stage_selfcheck.get("status") or "").lower() == "pass"
    ):
        return False
    if retry_reason:
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    text = str(draft_reply or "")
    if any(word in text for word in ("稍后给您准确回复", "我先帮您确认一下最新房态", "系统显示", "作为AI")):
        return True
    if len(text) > 700:
        return True
    if any(word in content for word in ("为什么", "怎么处理", "不对", "打不开", "投诉", "纠纷")):
        return True
    if not deterministic_reply_source:
        return True
    if proof.get("wants_video") or proof.get("wants_image"):
        return False
    if any(action in actions for action in ("send_video", "send_image", "send_inventory_sheet")):
        return False
    routine_tool_actions = {
        "search_inventory",
        "compact_listing",
        "send_deposit_policy",
        "send_contract_contact",
        "explain_missing_media",
        "explain_unavailable_viewing",
        "generate_reply",
    }
    grounded_by_tools = bool(
        any(action != "generate_reply" for action in actions)
        or tool_evidence.get("inventory_rows")
        or tool_evidence.get("target_rows")
        or tool_evidence.get("rule_evidence")
        or tool_evidence.get("missing_media")
    )
    if deterministic_reply_source == "planner_reply_text" and grounded_by_tools and set(actions).issubset(routine_tool_actions):
        return False
    if deterministic_reply_source == "tool_grounded_reply":
        return False
    if deterministic_reply_source == "planner_reply_text":
        return True
    return False


def _has_tool_grounded_reply_fallback(tool_evidence: dict[str, Any]) -> bool:
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    if tool_evidence.get("inventory_rows") or tool_evidence.get("target_rows"):
        return any(action in actions for action in ("search_inventory", "compact_listing", "generate_reply"))
    return bool(
        tool_evidence.get("rule_evidence")
        or tool_evidence.get("missing_media")
        or tool_evidence.get("inventory_images")
    )


def _can_use_inventory_reply_when_planner_missing(
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
) -> bool:
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    if not any(action in actions for action in ("search_inventory", "compact_listing")):
        return False
    if not tool_evidence.get("inventory_rows"):
        return False
    action_blockers = {
        "send_video",
        "send_image",
        "send_inventory_sheet",
        "send_inventory_image",
        "explain_missing_media",
    }
    if any(action in action_blockers for action in actions):
        return False
    proof = dict(understanding.get("constraint_proof") or {})
    task = dict(understanding.get("structured_task") or {})
    requirements = dict(task.get("tool_requirements") or {})
    if (
        proof.get("wants_video")
        or proof.get("wants_image")
        or requirements.get("needs_video")
        or requirements.get("needs_image")
        or requirements.get("needs_inventory_sheet")
    ):
        return False
    return True


def _planner_reply_conflicts_with_inventory_rows(reply: str, tool_evidence: dict[str, Any]) -> bool:
    reply_text = str(reply or "").strip()
    if not reply_text or not tool_evidence.get("inventory_rows"):
        return False
    unsafe_clarification_patterns = (
        "避免发错",
        "先不乱发",
        "小区+房号",
        "更具体条件",
        "重新按最新房源表查准",
        "重新查准",
        "确认小区",
        "确认一下小区",
    )
    if not any(pattern in reply_text for pattern in unsafe_clarification_patterns):
        return False
    rows = [row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]
    for row in rows[:5]:
        community = _row_value(row, ("小区", "小区名", "楼盘"))
        room_no = _row_value(row, ("房号", "房间号", "房源编号"))
        if community and community in reply_text:
            return False
        if room_no and room_no in reply_text:
            return False
    return True


def _final_inventory_evidence_fallback(
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
) -> str:
    rows = [
        row
        for row in (
            tool_evidence.get("target_rows")
            or tool_evidence.get("inventory_rows")
            or []
        )
        if isinstance(row, dict)
    ]
    if not rows:
        return ""
    proof = dict(understanding.get("constraint_proof") or {})
    condition = _constraint_condition_text(proof)
    heading = f"有的，{condition}我查到这些还在租：" if condition else "有的，我查到这些还在租："
    budget_note = _budget_payment_note(rows, proof)
    if budget_note:
        heading += f"\n{budget_note}"
    lines = [_room_line(row, index) for index, row in enumerate(rows[:5], 1)]
    if len(rows) == 1:
        tail = "要视频、图片或者看房方式的话，直接说这套就行。"
    elif len(rows) > len(lines):
        tail = f"还有 {len(rows) - len(lines)} 套没列完，你要视频、图片或者看房方式的话，直接回序号或小区+房号就行。"
    else:
        tail = "你要视频、图片或者看房方式的话，直接回序号或小区+房号就行。"
    return "\n".join([heading, *lines, tail])


def _row_area_matches(area: str, row: dict[str, Any]) -> bool:
    if not area:
        return True
    row_area = normalize_search_text(_row_value(row, ("区域", "商圈", "板块", "位置")))
    if not row_area:
        return False
    tokens = [token for token in re.split(r"[\s\n/、]+", area) if token]
    return any(normalize_search_text(token) in row_area for token in tokens)


def _filter_rows_by_constraint_proof(
    rows: list[dict[str, Any]],
    proof: dict[str, Any],
    *,
    query_text: str,
) -> list[dict[str, Any]]:
    if not rows or not proof:
        return rows
    filtered = rows
    area = str(proof.get("area") or "").strip()
    if area:
        area_rows = [row for row in filtered if _row_area_matches(area, row)]
        filtered = area_rows
    communities = {str(item).strip() for item in proof.get("communities") or [] if str(item).strip()}
    if communities:
        community_rows = [
            row
            for row in filtered
            if _row_value(row, ("小区", "社区", "楼盘", "小区名")) in communities
        ]
        filtered = community_rows
    room_refs = {
        _normalize_room_ref(ref)
        for ref in [*(proof.get("room_refs") or []), *_room_refs_from_text(query_text)]
        if str(ref).strip()
    }
    if room_refs:
        room_rows = [
            row
            for row in filtered
            if _normalize_room_ref(_row_value(row, ("房号", "房间号", "门牌"))) in room_refs
        ]
        return room_rows
    budget_range = proof.get("budget_range") or proof.get("price_range") or []
    if isinstance(budget_range, (list, tuple)) and len(budget_range) >= 2:
        try:
            low, high = sorted((int(float(budget_range[0])), int(float(budget_range[1]))))
        except (TypeError, ValueError):
            low = high = None
        if low is not None and high is not None:
            filtered = [row for row in filtered if row_matches_price_range(row, (low, high))]
    layout = str(proof.get("layout") or proof.get("room_type") or "").strip()
    if layout:
        layout_query = parse_inventory_query(layout)
        if layout_query.room_type_aliases:
            filtered = [row for row in filtered if row_matches_hard_constraints(row, layout_query)]
    parsed = parse_inventory_query(query_text)
    if parsed.has_hard_constraints:
        hard_rows = [row for row in filtered if row_matches_hard_constraints(row, parsed)]
        filtered = hard_rows
    return filtered


async def _generate_reply_result(
    *,
    content: str,
    context: dict[str, Any],
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    planner_result: dict[str, Any] | None = None,
    retry_reason: str = "",
    timer: kf_turn_flow.RagStageTimer | None = None,
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    if inventory_read_context is not None:
        tool_evidence.setdefault("inventory_read_context", inventory_read_context.to_log_dict())
    if tool_evidence.get("inventory_read_error"):
        tool_evidence["inventory_rows"] = []
        tool_evidence["target_rows"] = []
        tool_evidence["image_rows"] = []
        tool_evidence["video_rows"] = []
        tool_evidence["image_paths"] = []
        tool_evidence["video_paths"] = []
    effective_query = str(understanding.get("effective_query") or content)
    deterministic_signals = _deterministic_signals(content)
    if deterministic_signals.get("wants_deposit"):
        rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
        rule_evidence.setdefault("deposit_policy", _deposit_policy_evidence())
        tool_evidence["rule_evidence"] = rule_evidence
    if deterministic_signals.get("wants_contract_contact") or _normalize_intent(understanding.get("intent")) == "contract":
        rule_evidence = dict(tool_evidence.get("rule_evidence") or {})
        rule_evidence.setdefault("contract_contact", CONTACT_NUMBERS)
        tool_evidence["rule_evidence"] = rule_evidence
    rows = [row for row in tool_evidence.get("inventory_rows") or [] if isinstance(row, dict)]
    target_rows = [row for row in tool_evidence.get("target_rows") or [] if isinstance(row, dict)]
    evidence_rows = target_rows or rows
    planner_reply = str(
        (planner_result or {}).get("reply")
        or (planner_result or {}).get("reply_text")
        or (planner_result or {}).get("final_reply")
        or ""
    ).strip()
    planner_requires_reply = bool(planner_result) and not (planner_result or {}).get("need_rewrite_clarification")
    if planner_requires_reply and not planner_reply:
        post_tool_reply_result: dict[str, Any] = {}
        planner_reply_func = getattr(reply_generator, "plan_kf_reply_text", None)
        if callable(planner_reply_func):
            try:
                stage = timer.stage("planner_reply_text") if timer else nullcontext()
                with stage:
                    post_tool_reply_result = await asyncio.wait_for(
                        planner_reply_func(
                            content=effective_query,
                            structured_task=understanding.get("structured_task") or {},
                            entity_resolution=understanding.get("entity_resolution") or {},
                            constraint_proof=understanding.get("constraint_proof") or {},
                            planner_result=planner_result or {},
                            tool_evidence=_tool_evidence_summary(tool_evidence),
                            planner_retry_reason=retry_reason,
                        ),
                        timeout=8,
                    )
            except TimeoutError as exc:
                logger.warning("KF planner post-tool reply generation timed out: %s", exc)
                post_tool_reply_result = {"reply_text": "", "source": "planner_reply_timeout", "error": "timeout"}
            except Exception as exc:
                logger.exception("KF planner reply generation failed: %s", exc)
                post_tool_reply_result = {"reply_text": "", "source": "planner_reply_exception", "error": str(exc)}
        planner_result = dict(planner_result or {})
        planner_result["post_tool_reply_result"] = post_tool_reply_result
        if post_tool_reply_result.get("need_rewrite_clarification"):
            planner_result["need_rewrite_clarification"] = True
            planner_result["missing_evidence"] = str(
                post_tool_reply_result.get("missing_evidence")
                or post_tool_reply_result.get("reason")
                or "工具结果不足，Planner 第二阶段无法生成回复。"
            )
        planner_reply = str(
            post_tool_reply_result.get("reply")
            or post_tool_reply_result.get("reply_text")
            or post_tool_reply_result.get("final_reply")
            or ""
        ).strip()
        if planner_reply:
            planner_result["reply_text"] = planner_reply
            planner_result["reply_source"] = "post_tool_planner"
            tool_evidence["planner_reply_result"] = post_tool_reply_result
    planner_reply_result = (planner_result or {}).get("post_tool_reply_result") or {}
    inventory_search_reply = _reply_for_inventory_search(understanding, tool_evidence)
    planner_missing_reply = planner_requires_reply and not planner_reply
    if (
        planner_missing_reply
        and str(planner_reply_result.get("source") or "") == "planner_reply_timeout"
        and _has_tool_grounded_reply_fallback(tool_evidence)
    ):
        planner_missing_reply = False
        tool_evidence["planner_reply_timeout_tool_grounded_fallback"] = True
    if (
        planner_missing_reply
        and retry_reason
        and inventory_search_reply
        and _can_use_inventory_reply_when_planner_missing(understanding, tool_evidence)
    ):
        planner_missing_reply = False
        planner_reply = inventory_search_reply
        planner_result = dict(planner_result or {})
        planner_result["reply_text"] = planner_reply
        planner_result["reply_source"] = "tool_grounded_inventory_reply_after_planner_retry"
        planner_reply_result = {
            "reply_text": planner_reply,
            "source": "tool_grounded_inventory_reply_after_planner_retry",
            "reason": "Planner 重试后仍未生成文本，但房源工具已返回纯房源列表证据，使用工具证据生成待自检回复。",
        }
        planner_result["post_tool_reply_result"] = planner_reply_result
        tool_evidence["planner_reply_result"] = planner_reply_result
        tool_evidence["planner_missing_reply_tool_grounded_fallback"] = True
    if (
        planner_reply
        and inventory_search_reply
        and _can_use_inventory_reply_when_planner_missing(understanding, tool_evidence)
        and _planner_reply_conflicts_with_inventory_rows(planner_reply, tool_evidence)
    ):
        planner_reply = inventory_search_reply
        planner_result = dict(planner_result or {})
        planner_result["reply_text"] = planner_reply
        planner_result["reply_source"] = "tool_grounded_inventory_reply_replaced_invalid_planner_reply"
        planner_reply_result = {
            "reply_text": planner_reply,
            "source": "tool_grounded_inventory_reply_replaced_invalid_planner_reply",
            "reason": "Planner 已拿到房源列表却生成要求客户重复信息的兜底话术，改用工具证据生成待自检回复。",
        }
        planner_result["post_tool_reply_result"] = planner_reply_result
        tool_evidence["planner_reply_result"] = planner_reply_result
        tool_evidence["planner_invalid_inventory_reply_replaced"] = True
    pre_gate_field_target_error = dict(tool_evidence.get("field_target_error") or {})
    if (
        planner_missing_reply
        and pre_gate_field_target_error.get("reason") == "original_video_followup_missing_stable_video_target"
    ):
        planner_reply = _reply_for_field_target_error(tool_evidence)
        if planner_reply:
            planner_missing_reply = False
            planner_result = dict(planner_result or {})
            planner_result["reply_text"] = planner_reply
            planner_result["reply_source"] = "tool_grounded_original_video_target_error"
            planner_reply_result = {
                "reply_text": planner_reply,
                "source": "tool_grounded_original_video_target_error",
                "selfcheck": {"status": "pass", "source": "field_target_error_contract"},
                "reason": "原视频追问没有稳定上一轮视频目标，使用工具证据生成目标未绑定说明。",
            }
            planner_result["post_tool_reply_result"] = planner_reply_result
            tool_evidence["planner_reply_result"] = planner_reply_result
            tool_evidence["planner_missing_original_video_target_fallback"] = True
    if planner_missing_reply and not retry_reason:
        gate_selfcheck = {
            "status": "retry",
            "action": "retry",
            "source": "planner_output_gate",
            "reason": "Planner 没有生成客户可见 reply_text，不能进入最终自检；必须先回 Planner 重规划并补齐回复。",
            "fallback_text": "",
            "fallback_reply": "",
        }
        gate_llm = {
            "status": "skipped",
            "source": "planner_output_gate",
            "reason": "Planner 输出门禁在最终自检前拦截空 reply_text，自检不替 Planner 生成回复。",
        }
        retry_payload = _planner_retry_reason_payload(
            content=content,
            understanding=understanding,
            planner_result=planner_result or {},
            tool_evidence=tool_evidence,
            draft_reply="",
            rule_selfcheck=gate_selfcheck,
            llm_selfcheck=gate_llm,
            reason=str(gate_selfcheck["reason"]),
        )
        return {
            "reply": "",
            "draft_reply": "",
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {"status": "retry", "rule": gate_selfcheck, "llm": gate_llm},
            "needs_planner_retry": True,
            "planner_retry_reason": retry_payload,
        }
    if planner_missing_reply:
        fallback = _safe_fallback_for_intent(
            understanding,
            "",
        )
        fallback = _normalize_customer_visible_reply_text_before_selfcheck(fallback)
        tool_evidence["suppress_actions"] = True
        tool_evidence["deterministic_reply_source"] = "planner_missing_reply_text"
        fallback_evidence = {"actions": [], "rule_evidence": {}}
        fallback_package = _build_outbound_package(fallback, fallback_evidence)
        fallback_rule_selfcheck = _outbound_package_selfcheck(
            draft_reply=fallback,
            tool_evidence=fallback_evidence,
            outbound_package=fallback_package,
        )
        fallback_llm_selfcheck = {
            "status": "pass",
            "source": "planner_missing_reply_safe_fallback",
            "reason": "Planner 重试后仍没有生成 reply_text，不能由自检或兜底话术替 Planner 生成动作说明。",
        }
        tool_evidence["outbound_package"] = fallback_package
        retry_payload = _planner_retry_reason_payload(
            content=content,
            understanding=understanding,
            planner_result=planner_result or {},
            tool_evidence=tool_evidence,
            draft_reply="",
            rule_selfcheck={
                "status": "retry",
                "source": "planner_output_gate",
                "reason": "Planner 重试后仍没有生成客户可见 reply_text。",
            },
            llm_selfcheck=fallback_llm_selfcheck,
            reason="Planner 重试后仍没有生成客户可见 reply_text。",
        )
        return {
            "reply": fallback,
            "draft_reply": "",
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {
                "status": "retry",
                "rule": {"status": "retry", "source": "planner_output_gate", "reason": "Planner 重试后仍没有生成客户可见 reply_text。"},
                "llm": {"status": "skipped", "source": "planner_output_gate"},
                "fallback": {"rule": fallback_rule_selfcheck, "llm": fallback_llm_selfcheck},
            },
            "needs_planner_retry": False,
            "planner_retry_reason": retry_payload,
        }
    planner_stage_selfcheck = kf_orchestrator_flow.planner_reply_selfcheck(planner_reply_result)
    planner_stage_status = kf_orchestrator_flow.planner_reply_selfcheck_status(planner_reply_result)
    if planner_stage_status in {"retry", "fallback"} and not retry_reason:
        stage_reason = str(
            planner_stage_selfcheck.get("planner_retry_reason")
            or planner_stage_selfcheck.get("reason")
            or "Planner 工具后阶段自检未通过。"
        )
        retry_payload = _planner_retry_reason_payload(
            content=content,
            understanding=understanding,
            planner_result=planner_result or {},
            tool_evidence=tool_evidence,
            draft_reply=planner_reply,
            rule_selfcheck={
                "status": planner_stage_status,
                "source": "planner_reply_text_selfcheck",
                "reason": stage_reason,
            },
            llm_selfcheck=planner_stage_selfcheck,
            reason=stage_reason,
        )
        return {
            "reply": "",
            "draft_reply": planner_reply,
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {
                "status": planner_stage_status,
                "rule": {
                    "status": planner_stage_status,
                    "source": "planner_reply_text_selfcheck",
                    "reason": stage_reason,
                },
                "llm": planner_stage_selfcheck,
            },
            "needs_planner_retry": True,
            "planner_retry_reason": retry_payload,
        }
    if planner_stage_selfcheck:
        tool_evidence["planner_reply_stage_selfcheck"] = planner_stage_selfcheck
    actions = [str(action) for action in tool_evidence.get("actions") or []]
    clarification_only = bool(actions) and all(action == "clarification" for action in actions)
    inventory_text = inventory.format_rows(rows, limit=10) if rows else ""
    if not inventory_text and not clarification_only:
        inventory_snapshot = getattr(inventory, "snapshot", None)
        if callable(inventory_snapshot):
            inventory_text = await inventory_snapshot(limit=20)
    image_paths = [Path(path) for path in tool_evidence.get("image_paths") or []]
    video_paths = [Path(path) for path in tool_evidence.get("video_paths") or []]
    reply_memory = kf_context_memory.reply_memory_view(context)

    rag_result = await agentic_rag.retrieve_for_reply(
        content=effective_query,
        conversation_context=json.dumps(reply_memory, ensure_ascii=False, default=str),
        rooms=evidence_rows,
        inventory_snapshot=inventory_text,
        media_images=[str(path) for path in image_paths],
        media_videos=[str(path) for path in video_paths],
        row_video_paths=video_paths,
        row_image_paths=image_paths,
        recent_context=reply_memory,
        inventory_rows=rows,
        retry_reason=retry_reason,
        original_content=content,
    )
    knowledge_context = rag_result.context_text
    if tool_evidence.get("rule_evidence"):
        knowledge_context += "\n\n确定性规则证据：\n" + str(
            inventory_sensitive_access.safe_rule_evidence_for_summary(
                tool_evidence["rule_evidence"]
            )
        )
    if tool_evidence.get("inventory_images"):
        knowledge_context += "\n\n动作摘要：\n" + json.dumps(
            {"will_send_inventory_sheet": True, "image_count": len(tool_evidence.get("inventory_images") or [])},
            ensure_ascii=False,
        )
    if tool_evidence.get("missing_media"):
        knowledge_context += "\n\n缺失素材摘要：\n" + json.dumps(
            tool_evidence.get("media_status") or {"missing_media": tool_evidence["missing_media"]},
            ensure_ascii=False,
            default=str,
        )

    deterministic_reply_source = ""
    candidate_selection_error_reply = _reply_for_candidate_selection_error(tool_evidence)
    field_target_error_reply = _reply_for_field_target_error(tool_evidence)
    prepared_media_reply = _reply_for_prepared_media(understanding, tool_evidence)
    missing_media_reply = _reply_for_missing_media(understanding, tool_evidence)
    utilities_viewing_reply = _reply_for_utilities_and_viewing(understanding, tool_evidence, content=content)
    deposit_utilities_reply = _reply_for_deposit_and_utilities(understanding, tool_evidence, content=content)
    contract_contact_reply = _reply_for_contract_contact(understanding, tool_evidence, content=content)
    structured_task = dict(understanding.get("structured_task") or {})
    tool_requirements = dict(structured_task.get("tool_requirements") or {})
    explicit_viewing_request = bool(deterministic_signals.get("wants_viewing"))
    viewing_reply = ""
    if (
        explicit_viewing_request
        or (
            not inventory_search_reply
            and (
                tool_requirements.get("needs_viewing_policy")
                or _normalize_intent(understanding.get("intent")) == "viewing"
            )
        )
    ):
        viewing_reply = _reply_for_viewing(tool_evidence, allow_password=_content_wants_password(content))
    if planner_missing_reply:
        draft_reply = ""
        deterministic_reply_source = "planner_missing_reply_text"
    elif clarification_only and planner_reply:
        draft_reply = planner_reply
        deterministic_reply_source = "rewrite_clarification"
    elif candidate_selection_error_reply:
        draft_reply = candidate_selection_error_reply
        deterministic_reply_source = "candidate_selection_error_reply"
    elif field_target_error_reply:
        draft_reply = field_target_error_reply
        deterministic_reply_source = "field_target_error_reply"
    elif prepared_media_reply:
        draft_reply = prepared_media_reply
        deterministic_reply_source = "prepared_media_reply"
    elif missing_media_reply:
        draft_reply = missing_media_reply
        deterministic_reply_source = "missing_media_reply"
    elif utilities_viewing_reply:
        draft_reply = utilities_viewing_reply
        deterministic_reply_source = "utilities_viewing_field_reply"
    elif deposit_utilities_reply and deterministic_signals.get("wants_utilities"):
        draft_reply = deposit_utilities_reply
        deterministic_reply_source = "utilities_field_reply"
    elif inventory_search_reply and (
        tool_evidence.get("planner_reply_timeout_tool_grounded_fallback")
        or tool_evidence.get("planner_missing_reply_tool_grounded_fallback")
        or tool_evidence.get("planner_invalid_inventory_reply_replaced")
    ):
        draft_reply = inventory_search_reply
        deterministic_reply_source = "tool_grounded_reply"
    elif planner_reply:
        draft_reply = _safe_fallback_for_intent(understanding, planner_reply)
        deterministic_reply_source = "planner_reply_text"
    else:
        draft_reply = ""
        deterministic_reply_source = "planner_missing_reply_text"
    normalized_reply = _normalize_inventory_sheet_reply_before_selfcheck(
        draft_reply=draft_reply,
        understanding=understanding,
        tool_evidence=tool_evidence,
    )
    if normalized_reply != draft_reply:
        tool_evidence["reply_normalized_for_inventory_sheet"] = True
        draft_reply = normalized_reply
    normalized_reply = _normalize_unasked_viewing_tail_before_selfcheck(
        content=content,
        draft_reply=draft_reply,
        understanding=understanding,
    )
    if normalized_reply != draft_reply:
        tool_evidence["reply_normalized_for_unasked_viewing_tail"] = True
        draft_reply = normalized_reply
    original_video_notice = _reply_for_original_video_request(understanding, tool_evidence)
    if draft_reply and original_video_notice and not any(word in draft_reply for word in ("压缩", "原视频", "高清", "源文件", "下载链接", "素材页")):
        draft_reply = (draft_reply.rstrip("。") + "。\n" + original_video_notice).strip("。\n") + "。"
        tool_evidence["reply_normalized_for_original_video_request"] = True
    normalized_reply = _normalize_customer_visible_reply_text_before_selfcheck(draft_reply)
    if normalized_reply != draft_reply:
        tool_evidence["reply_normalized_for_customer_visible_text"] = True
        draft_reply = normalized_reply
    if deterministic_reply_source:
        tool_evidence["deterministic_reply_source"] = deterministic_reply_source
    if _dual_llm_production_enabled():
        llm2_retry_reason = ""
        task_packet = understanding.get("llm1_task_packet")
        production_package_payload: dict[str, Any] = {}
        if not isinstance(task_packet, dict) or not task_packet:
            llm2_retry_reason = "LLM1 production task packet is missing; LLM2 production cannot compose customer reply."
            production_package_payload = {
                "self_review": {"status": "retry", "reason": llm2_retry_reason},
                "reply_text_present": False,
            }
        else:
            try:
                production_package = await asyncio.wait_for(
                    kf_dual_llm_production.compose_production_outbound_package(
                        reply_generator=reply_generator,
                        task_packet=task_packet,
                        tool_evidence=tool_evidence,
                        draft_reply=draft_reply,
                        planner_result=planner_result or {},
                        reply_result={"reply": draft_reply, "reply_source": deterministic_reply_source},
                        retry_reason=retry_reason,
                    ),
                    timeout=8,
                )
                production_package_payload = kf_dual_llm_production.package_log_payload(production_package)
                if (
                    kf_dual_llm_production.package_passed(production_package)
                    and str(production_package.reply_text or "").strip()
                ):
                    draft_reply = _normalize_customer_visible_reply_text_before_selfcheck(
                        str(production_package.reply_text or "")
                    )
                    deterministic_reply_source = "kf_llm2_outbound_production"
                    tool_evidence["deterministic_reply_source"] = deterministic_reply_source
                    outbound_dict = production_package.to_legacy_dict()
                    tool_evidence["llm2_production_outbound_package"] = outbound_dict
                else:
                    llm2_retry_reason = kf_dual_llm_production.package_retry_reason(production_package)
            except Exception as exc:
                logger.exception("KF LLM2 production outbound failed: %s", exc)
                llm2_retry_reason = "LLM2 production outbound failed; do not continue with customer-visible facts."
                production_package_payload = {
                    "self_review": {
                        "status": "retry",
                        "source": "llm2_production_error_gate",
                        "error_type": type(exc).__name__,
                    },
                    "reply_text_present": False,
                }
        dual_meta = dict(tool_evidence.get("dual_llm_production") or understanding.get("dual_llm_production") or {})
        dual_meta["llm2"] = production_package_payload
        tool_evidence["dual_llm_production"] = safe_artifact_payload(dual_meta)
        if llm2_retry_reason and not retry_reason:
            gate_selfcheck = {
                "status": "retry",
                "source": "llm2_production_output_gate",
                "reason": llm2_retry_reason,
            }
            retry_payload = _planner_retry_reason_payload(
                content=content,
                understanding=understanding,
                planner_result=planner_result or {},
                tool_evidence=tool_evidence,
                draft_reply=draft_reply,
                rule_selfcheck=gate_selfcheck,
                llm_selfcheck=production_package_payload.get("self_review") or gate_selfcheck,
                reason=llm2_retry_reason,
            )
            return {
                "reply": "",
                "draft_reply": draft_reply,
                "planner_reply_result": planner_reply_result,
                "context": context,
                "selfcheck": {
                    "status": "retry",
                    "rule": gate_selfcheck,
                    "llm": production_package_payload.get("self_review") or gate_selfcheck,
                },
                "needs_planner_retry": True,
                "planner_retry_reason": retry_payload,
            }
        if llm2_retry_reason:
            draft_reply = _normalize_customer_visible_reply_text_before_selfcheck(
                _safe_fallback_for_intent(understanding, settings.default_fallback_reply)
            )
            deterministic_reply_source = "llm2_production_safe_fallback"
            tool_evidence["deterministic_reply_source"] = deterministic_reply_source
            tool_evidence["suppress_actions"] = True
    outbound_package = _build_outbound_package(draft_reply, tool_evidence)
    tool_evidence["outbound_package"] = outbound_package
    selfcheck_stage = timer.stage("final_selfcheck") if timer else nullcontext()
    with selfcheck_stage:
        assessment = agentic_rag.assess_reply(
            content=effective_query,
            reply_text=draft_reply,
            rag_result=rag_result,
            retry_attempted=bool(retry_reason),
        )
        rule_selfcheck = _assessment_to_dict(assessment)
        rule_selfcheck = _sanitize_rule_selfcheck_for_intent(
            rule_selfcheck,
            content=content,
            understanding=understanding,
        )
        constraint_selfcheck = _constraint_consistency_selfcheck(
            content=content,
            draft_reply=draft_reply,
            understanding=understanding,
            tool_evidence=tool_evidence,
        )
        if str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower() == "pass" and constraint_selfcheck.get("status") != "pass":
            rule_selfcheck = constraint_selfcheck
        package_selfcheck = _outbound_package_selfcheck(
            draft_reply=draft_reply,
            tool_evidence=tool_evidence,
            outbound_package=outbound_package,
        )
        if str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower() == "pass" and package_selfcheck.get("status") != "pass":
            rule_selfcheck = package_selfcheck
        human_context_selfcheck = _local_human_context_selfcheck(
            content=content,
            draft_reply=draft_reply,
            tool_evidence=tool_evidence,
            deterministic_reply_source=deterministic_reply_source,
        )
        if str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower() == "pass" and human_context_selfcheck.get("status") != "pass":
            rule_selfcheck = human_context_selfcheck
        rule_status = str(rule_selfcheck.get("status") or rule_selfcheck.get("action") or "pass").lower()
        if _needs_llm_final_selfcheck(
            content=content,
            understanding=understanding,
            tool_evidence=tool_evidence,
            draft_reply=draft_reply,
            rule_selfcheck=rule_selfcheck,
            deterministic_reply_source=deterministic_reply_source,
            retry_reason=retry_reason,
        ):
            try:
                llm_selfcheck = await asyncio.wait_for(
                    reply_generator.assess_kf_final_reply(
                        content=content,
                        raw_dialog_context=kf_context_memory.selfcheck_memory_view(context).get("raw_dialog_context", []),
                        structured_task=understanding.get("structured_task") or {},
                        constraint_proof=understanding.get("constraint_proof") or {},
                        tool_evidence=_tool_evidence_summary(tool_evidence),
                        outbound_package=outbound_package,
                        draft_reply=draft_reply,
                        rule_selfcheck=rule_selfcheck,
                    ),
                    timeout=3,
                )
            except Exception as exc:
                logger.exception("KF final LLM selfcheck failed: %s", exc)
                llm_selfcheck = {"status": "pass", "source": "llm_selfcheck_error_or_timeout", "error": str(exc)}
        else:
            llm_selfcheck = {
                "status": "pass",
                "source": "llm_selfcheck_skipped_by_tiered_final_selfcheck",
                "reason": "已完成本地事实一致、动作一致、上下文连贯和拟人化基线自检；该回复无需阻塞式 LLM 终检。",
            }
    llm_status = str(llm_selfcheck.get("status") or "pass").lower()
    final_status = rule_status if rule_status != "pass" else llm_status
    reason = str(
        (rule_selfcheck.get("reason") if rule_status != "pass" else "")
        or llm_selfcheck.get("reason")
        or llm_selfcheck.get("planner_retry_reason")
        or "final_selfcheck_failed"
    )
    planner_retry_reason = ""
    if final_status != "pass":
        planner_retry_reason = _planner_retry_reason_payload(
            content=content,
            understanding=understanding,
            planner_result=planner_result or {},
            tool_evidence=tool_evidence,
            draft_reply=draft_reply,
            rule_selfcheck=rule_selfcheck,
            llm_selfcheck=llm_selfcheck,
            reason=reason,
        )
    if final_status == "pass":
        return {
            "reply": draft_reply,
            "draft_reply": draft_reply,
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {"status": final_status, "rule": rule_selfcheck, "llm": llm_selfcheck},
            "needs_planner_retry": False,
            "planner_retry_reason": "",
        }
    if not retry_reason:
        return {
            "reply": "",
            "draft_reply": draft_reply,
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {"status": final_status, "rule": rule_selfcheck, "llm": llm_selfcheck},
            "needs_planner_retry": True,
            "planner_retry_reason": planner_retry_reason,
        }
    if planner_missing_reply:
        fallback = _safe_fallback_for_intent(
            understanding,
            "",
        )
        fallback = _normalize_customer_visible_reply_text_before_selfcheck(fallback)
        tool_evidence["suppress_actions"] = True
        fallback_evidence = {"actions": [], "rule_evidence": {}}
        fallback_package = _build_outbound_package(fallback, fallback_evidence)
        fallback_rule_selfcheck = _outbound_package_selfcheck(
            draft_reply=fallback,
            tool_evidence=fallback_evidence,
            outbound_package=fallback_package,
        )
        fallback_llm_selfcheck = {
            "status": "pass",
            "source": "planner_missing_reply_safe_fallback",
            "reason": "Planner 重试后仍没有生成 reply_text，不能由自检或兜底话术替 Planner 生成动作说明。",
        }
        tool_evidence["outbound_package"] = fallback_package
        return {
            "reply": fallback,
            "draft_reply": draft_reply,
            "planner_reply_result": planner_reply_result,
            "context": context,
            "selfcheck": {
                "status": final_status,
                "rule": rule_selfcheck,
                "llm": llm_selfcheck,
                "fallback": {"rule": fallback_rule_selfcheck, "llm": fallback_llm_selfcheck},
            },
            "needs_planner_retry": False,
            "planner_retry_reason": planner_retry_reason,
        }
    preserve_sendable_actions = _has_sendable_actions(tool_evidence)
    sendable_action_fallback = (
        _reply_for_sendable_action_fallback(content=content, tool_evidence=tool_evidence)
        if preserve_sendable_actions
        else ""
    )
    prefer_sendable_action_fallback = bool(sendable_action_fallback) and (
        reason == "robotic_template_reply"
        or "多套视频动作" in reason
        or "多套图片动作" in reason
        or "逐套说明" in reason
    )
    fallback = str(
        contract_contact_reply
        or field_target_error_reply
        or utilities_viewing_reply
        or deposit_utilities_reply
        or viewing_reply
        or (sendable_action_fallback if prefer_sendable_action_fallback else "")
        or missing_media_reply
        or inventory_search_reply
        or llm_selfcheck.get("fallback_reply")
        or rule_selfcheck.get("fallback_reply")
        or rule_selfcheck.get("fallback_text")
        or sendable_action_fallback
        or settings.default_fallback_reply
    ).strip()
    fallback = _constraint_preserving_inventory_fallback(
        understanding,
        _safe_fallback_for_intent(understanding, fallback),
        tool_evidence,
    )
    fallback = _normalize_customer_visible_reply_text_before_selfcheck(fallback)
    if preserve_sendable_actions:
        tool_evidence.pop("suppress_actions", None)
        fallback_evidence = tool_evidence
    else:
        tool_evidence["suppress_actions"] = True
        fallback_evidence = {"actions": [], "rule_evidence": tool_evidence.get("rule_evidence") or {}}
    fallback_package = _build_outbound_package(fallback, fallback_evidence)
    fallback_rule_selfcheck = _outbound_package_selfcheck(
        draft_reply=fallback,
        tool_evidence=fallback_evidence,
        outbound_package=fallback_package,
    )
    fallback_human_selfcheck = _local_human_context_selfcheck(
        content=content,
        draft_reply=fallback,
        tool_evidence=fallback_evidence,
        deterministic_reply_source="selfcheck_fallback_reply",
    )
    fallback_llm_selfcheck = {
        "status": "pass",
        "source": "fallback_llm_selfcheck_skipped_by_tiered_final_selfcheck",
        "reason": "兜底待发送包已完成本地动作一致性和本地拟人化自检，通过后不再阻塞式调用 LLM。",
    }
    if str(fallback_rule_selfcheck.get("status") or fallback_rule_selfcheck.get("action") or "pass").lower() == "pass":
        fallback_human_status = str(
            fallback_human_selfcheck.get("status") or fallback_human_selfcheck.get("action") or "pass"
        ).lower()
        if fallback_human_status != "pass":
            fallback_rule_selfcheck = fallback_human_selfcheck
    fallback_rule_status = str(
        fallback_rule_selfcheck.get("status") or fallback_rule_selfcheck.get("action") or "pass"
    ).lower()
    fallback_llm_status = str(fallback_llm_selfcheck.get("status") or "pass").lower()
    if fallback_rule_status != "pass":
        inventory_evidence_fallback = _final_inventory_evidence_fallback(understanding, tool_evidence)
        if inventory_evidence_fallback:
            tool_evidence.pop("suppress_actions", None)
            fallback = _normalize_customer_visible_reply_text_before_selfcheck(inventory_evidence_fallback)
            fallback_evidence = tool_evidence
            fallback_package = _build_outbound_package(fallback, fallback_evidence)
            fallback_rule_selfcheck = {
                "status": "pass",
                "source": "tool_grounded_inventory_final_fallback",
                "reason": (
                    "最终自检回流阶段已有房源表证据，禁止退回要求客户重复提供小区/房号的兜底话术，"
                    "改用工具证据生成房源列表。"
                ),
            }
        else:
            tool_evidence["suppress_actions"] = True
            fallback = _constraint_preserving_inventory_fallback(
                understanding,
                _safe_fallback_for_intent(
                    understanding,
                    "我这边为了避免发错，先不乱发。你把小区+房号或更具体条件发我一下，我重新按最新房源表查准。",
                ),
                tool_evidence,
            )
            fallback = _normalize_customer_visible_reply_text_before_selfcheck(fallback)
            fallback_evidence = {"actions": [], "rule_evidence": {}}
            fallback_package = _build_outbound_package(fallback, fallback_evidence)
    elif preserve_sendable_actions:
        tool_evidence.pop("suppress_actions", None)
    tool_evidence["outbound_package"] = fallback_package
    return {
        "reply": fallback,
        "draft_reply": draft_reply,
        "planner_reply_result": planner_reply_result,
        "context": context,
            "selfcheck": {
                "status": final_status,
                "rule": rule_selfcheck,
                "llm": llm_selfcheck,
                "fallback": {
                    "rule": fallback_rule_selfcheck,
                    "human": fallback_human_selfcheck,
                    "llm": fallback_llm_selfcheck,
                },
            },
        "needs_planner_retry": False,
        "planner_retry_reason": planner_retry_reason,
    }


async def _generate_reply(
    *,
    content: str,
    context: dict[str, Any],
    understanding: dict[str, Any],
    tool_evidence: dict[str, Any],
    retry_reason: str = "",
) -> str:
    result = await _generate_reply_result(
        content=content,
        context=context,
        understanding=understanding,
        tool_evidence=tool_evidence,
        planner_result={},
        retry_reason=retry_reason,
    )
    return str(result.get("reply") or settings.default_fallback_reply)


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _send_text(open_kfid: str, external_userid: str, text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    return await _await_if_needed(wecom_kf.send_text(open_kfid, external_userid, text))


async def _execute_send_action_once(
    *,
    context: dict[str, Any],
    action: Any,
    send_call: Callable[[], Any],
    receipt_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    idempotency_key = kf_send_receipts.build_idempotency_key(action)
    existing = kf_send_receipts.find_successful_receipt(context, idempotency_key)
    if existing:
        duplicate = kf_send_receipts.build_duplicate_receipt(action, existing, idempotency_key=idempotency_key)
        context = kf_send_receipts.append_receipt(context, duplicate)
        return context, False, duplicate.to_safe_dict()
    try:
        provider_result = await _await_if_needed(send_call())
    except Exception as exc:
        failed = kf_send_receipts.build_failed_receipt(
            action,
            idempotency_key=idempotency_key,
            error=exc,
            metadata=receipt_metadata,
        )
        context = kf_send_receipts.append_receipt(context, failed)
        raise
    sent = kf_send_receipts.build_sent_receipt(
        action,
        idempotency_key=idempotency_key,
        provider_result=provider_result if isinstance(provider_result, dict) else {},
        metadata=receipt_metadata,
    )
    context = kf_send_receipts.append_receipt(context, sent)
    return context, True, sent.to_safe_dict()


def _send_action_for_text(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    text: str,
    action_id: str,
    text_role: str,
    msgids: list[str] | None = None,
) -> Any:
    normalized = text.strip()
    digest = kf_send_receipts.text_hash(normalized)
    return kf_send_receipts.build_send_action(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        msgids=msgids,
        action_id=action_id,
        action_type="text",
        payload={"text_hash": digest, "text_role": text_role, "text_length": len(normalized)},
        metadata={"text_hash": digest, "text_role": text_role},
    )


def _send_action_for_path(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    path: Path,
    action_id: str,
    action_type: str,
    msgids: list[str] | None = None,
    extra_payload: dict[str, Any] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Any:
    digest = kf_send_receipts.material_hash(path)
    return kf_send_receipts.build_send_action(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        msgids=msgids,
        action_id=action_id,
        action_type=action_type,
        payload={
            "material_hash": digest,
            "file_name": path.name,
            **dict(extra_payload or {}),
        },
        metadata={
            "material_hash": digest,
            "file_name": path.name,
            **dict(extra_metadata or {}),
        },
    )


async def _send_text_with_receipt(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    text: str,
    action_id: str,
    text_role: str,
    msgids: list[str] | None = None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    text = text.strip()
    if not text:
        return context, False, {}
    action = _send_action_for_text(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        text=text,
        action_id=action_id,
        text_role=text_role,
        msgids=msgids,
    )
    return await _execute_send_action_once(
        context=context,
        action=action,
        send_call=lambda: _send_text(open_kfid, external_userid, text),
        receipt_metadata={"text_role": text_role},
    )


def _build_orchestrator_shadow_artifact(
    *,
    content: str,
    open_kfid: str,
    external_userid: str,
    msgids: list[str],
    generation: int | str,
    inventory_read_context: InventoryReadContext | None,
    understanding: dict[str, Any],
    planner_result: dict[str, Any] | None = None,
    tool_evidence: dict[str, Any] | None = None,
    reply_result: dict[str, Any] | None = None,
    final_reply: str = "",
) -> dict[str, Any]:
    try:
        artifact = kf_orchestrator_shadow.build_shadow_artifact(
            content=content,
            open_kfid=open_kfid,
            external_userid=external_userid,
            msgids=msgids,
            generation=generation,
            inventory_read_context=inventory_read_context,
            understanding=understanding,
            planner_result=planner_result or {},
            tool_evidence=tool_evidence or {},
            reply_result=reply_result or {},
            final_reply=final_reply,
        )
        logger.info("KF orchestrator shadow artifact: %s", json.dumps(artifact, ensure_ascii=False, default=str))
        return artifact
    except Exception as exc:
        logger.warning("KF orchestrator shadow artifact failed: %s", exc)
        return {}


async def _send_images(open_kfid: str, external_userid: str, paths: list[str]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            await _await_if_needed(wecom_kf.send_image(open_kfid, external_userid, path))
            sent.append({"type": "image", "path": str(path), "count": 1})
        except WeComKfSendLimitError:
            raise
        except Exception as exc:
            logger.exception("send image failed: %s", exc)
            sent.append({"type": "image_failed", "path": str(path), "reason": kf_send_receipts.safe_failure_reason(exc)})
    return sent


async def _send_videos(
    open_kfid: str,
    external_userid: str,
    paths: list[str],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    for index, raw_path in enumerate(paths[:KF_VIDEO_SEND_LIMIT]):
        path = Path(raw_path)
        if not path.exists():
            continue
        label = _normalize_customer_visible_reply_text_before_selfcheck(
            _row_label(rows[index]) if index < len(rows) else path.stem
        )
        await _send_text(open_kfid, external_userid, f"这是{label}的视频。")
        try:
            await _await_if_needed(wecom_kf.send_video(open_kfid, external_userid, path))
            sent.append({"type": "video", "path": str(path), "room": label, "count": 1})
        except WeComKfSendLimitError:
            raise
        except Exception as exc:
            logger.exception("send video failed: %s", exc)
            sent.append({"type": "video_failed", "path": str(path), "room": label, "reason": kf_send_receipts.safe_failure_reason(exc)})
    return sent


async def _send_images_with_receipts(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    paths: list[str],
    msgids: list[str] | None = None,
    action_prefix: str = "send-image",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        if not path.exists():
            continue
        action = _send_action_for_path(
            open_kfid=open_kfid,
            external_userid=external_userid,
            context=context,
            path=path,
            action_id=f"{action_prefix}-{index}-{kf_send_receipts.material_hash(path)[:12]}",
            action_type="image",
            msgids=msgids,
            extra_payload={"position": index},
            extra_metadata={"position": index},
        )
        try:
            context, did_send, _receipt = await _execute_send_action_once(
                context=context,
                action=action,
                send_call=lambda path=path: wecom_kf.send_image(open_kfid, external_userid, path),
                receipt_metadata={"position": index},
            )
            if did_send:
                sent.append({"type": "image", "path": str(path), "count": 1})
        except WeComKfSendLimitError:
            raise
        except Exception as exc:
            logger.exception("send image failed: %s", exc)
            sent.append({"type": "image_failed", "path": str(path), "reason": kf_send_receipts.safe_failure_reason(exc)})
    return sent, context


async def _send_videos_with_receipts(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    paths: list[str],
    rows: list[dict[str, Any]],
    msgids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    for index, raw_path in enumerate(paths[:KF_VIDEO_SEND_LIMIT], start=1):
        path = Path(raw_path)
        if not path.exists():
            continue
        label = _normalize_customer_visible_reply_text_before_selfcheck(
            _row_label(rows[index - 1]) if index <= len(rows) else path.stem
        )
        caption = f"这是{label}的视频。"
        action = _send_action_for_path(
            open_kfid=open_kfid,
            external_userid=external_userid,
            context=context,
            path=path,
            action_id=f"send-video-{index}-{kf_send_receipts.material_hash(path)[:12]}",
            action_type="video",
            msgids=msgids,
            extra_payload={
                "position": index,
                "caption_hash": kf_send_receipts.text_hash(caption),
            },
            extra_metadata={
                "position": index,
                "caption_hash": kf_send_receipts.text_hash(caption),
                "transaction": "caption_then_video",
            },
        )
        idempotency_key = kf_send_receipts.build_idempotency_key(action)
        existing = kf_send_receipts.find_successful_receipt(context, idempotency_key)
        if existing:
            duplicate = kf_send_receipts.build_duplicate_receipt(action, existing, idempotency_key=idempotency_key)
            context = kf_send_receipts.append_receipt(context, duplicate)
            continue
        caption_sent = False
        try:
            await _send_text(open_kfid, external_userid, caption)
            caption_sent = True
            provider_result = await _await_if_needed(wecom_kf.send_video(open_kfid, external_userid, path))
            receipt = kf_send_receipts.build_sent_receipt(
                action,
                idempotency_key=idempotency_key,
                provider_result=provider_result if isinstance(provider_result, dict) else {},
                metadata={"position": index, "caption_sent": caption_sent},
            )
            context = kf_send_receipts.append_receipt(context, receipt)
            sent.append({"type": "video", "path": str(path), "room": label, "count": 1})
        except WeComKfSendLimitError as exc:
            receipt = kf_send_receipts.build_failed_receipt(
                action,
                idempotency_key=idempotency_key,
                error=exc,
                metadata={"position": index, "caption_sent": caption_sent},
            )
            context = kf_send_receipts.append_receipt(context, receipt)
            raise
        except Exception as exc:
            receipt = kf_send_receipts.build_failed_receipt(
                action,
                idempotency_key=idempotency_key,
                error=exc,
                metadata={"position": index, "caption_sent": caption_sent},
            )
            context = kf_send_receipts.append_receipt(context, receipt)
            logger.exception("send video failed: %s", exc)
            sent.append({"type": "video_failed", "path": str(path), "room": label, "reason": kf_send_receipts.safe_failure_reason(exc)})
    return sent, context


def _candidate_state_summary(context: dict[str, Any]) -> dict[str, Any]:
    candidate_set = kf_context_memory.normalize_last_candidate_set(context.get("last_candidate_set"))
    confirmed = kf_context_memory.normalize_confirmed_room_context(context.get("confirmed_room"))
    pending = kf_context_memory.normalize_pending_video_sends(context.get("pending_video_sends"))
    summary: dict[str, Any] = {}
    if candidate_set:
        summary["candidate_set"] = {
            "query": candidate_set.get("query", ""),
            "shown_count": candidate_set.get("shown_count", 0),
            "total_count": candidate_set.get("total_count", 0),
        }
    if confirmed:
        summary["confirmed_room"] = {
            "label": confirmed.get("label", ""),
            "row": kf_context_memory.summarize_row(confirmed.get("row")),
        }
    if pending:
        summary["pending_video_sends"] = {
            "requested_count": pending.get("requested_count", 0),
            "sent_count": pending.get("sent_count", 0),
            "labels": pending.get("labels", []),
        }
    inventory_read_context = context.get("inventory_read_context")
    if isinstance(inventory_read_context, dict):
        summary["inventory_read_context"] = inventory_read_turn.context_summary(inventory_read_context)
    return summary


async def _send_final_actions(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any],
    final_reply: str,
    tool_evidence: dict[str, Any],
    msgids: list[str] | None = None,
) -> dict[str, Any]:
    sent_actions: list[dict[str, Any]] = []
    final_reply = _normalize_customer_visible_reply_text_before_selfcheck(final_reply)
    outbound_package = tool_evidence.get("outbound_package") or _build_outbound_package(final_reply, tool_evidence)
    suppress_actions = bool(tool_evidence.get("suppress_actions"))
    context = _reconcile_last_candidate_set_with_visible_reply(
        context,
        final_reply,
        tool_evidence,
    )
    context, did_send_text, _receipt = await _send_text_with_receipt(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        text=final_reply,
        action_id="send-text-final-reply",
        text_role="final_reply",
        msgids=msgids,
    )
    if final_reply and did_send_text:
        sent_actions.append({"type": "text", "count": 1})

    if suppress_actions:
        for action in sent_actions:
            context = kf_context_memory.record_structured_assistant_output(
                context,
                final_reply=final_reply if action.get("type") == "text" else "",
                sent_action=action,
                candidate_state=_candidate_state_summary(context),
            ) or context
        return {"sent_actions": sent_actions, "context": context}

    inventory_explanation = str(outbound_package.get("inventory_explanation") or "").strip()
    if (
        tool_evidence.get("inventory_images")
        and inventory_explanation
        and inventory_explanation not in final_reply
        and "房源表发" not in final_reply
    ):
        context, did_send_inventory_text, _receipt = await _send_text_with_receipt(
            open_kfid=open_kfid,
            external_userid=external_userid,
            context=context,
            text=inventory_explanation,
            action_id="send-text-inventory-explanation",
            text_role="inventory_explanation",
            msgids=msgids,
        )
        if did_send_inventory_text:
            sent_actions.append({"type": "text", "subtype": "inventory_explanation", "count": 1})

    image_actions, context = await _send_images_with_receipts(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        paths=list(tool_evidence.get("inventory_images") or []),
        msgids=msgids,
        action_prefix="send-inventory-image",
    )
    sent_actions.extend(image_actions)

    for index, explanation in enumerate(outbound_package.get("image_explanations") or [], start=1):
        context, did_send_image_text, _receipt = await _send_text_with_receipt(
            open_kfid=open_kfid,
            external_userid=external_userid,
            context=context,
            text=str(explanation),
            action_id=f"send-text-image-explanation-{index}",
            text_role="image_explanation",
            msgids=msgids,
        )
        if did_send_image_text:
            sent_actions.append({"type": "text", "subtype": "image_explanation", "count": 1})
    image_actions, context = await _send_images_with_receipts(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        paths=list(tool_evidence.get("image_paths") or []),
        msgids=msgids,
        action_prefix="send-room-image",
    )
    sent_actions.extend(image_actions)

    video_actions, context = await _send_videos_with_receipts(
        open_kfid=open_kfid,
        external_userid=external_userid,
        context=context,
        paths=list(tool_evidence.get("video_paths") or []),
        rows=[row for row in tool_evidence.get("video_rows") or [] if isinstance(row, dict)],
        msgids=msgids,
    )
    sent_actions.extend(video_actions)

    for action in sent_actions:
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply=final_reply if action.get("type") == "text" else "",
            sent_action=action,
            candidate_state=_candidate_state_summary(context),
        ) or context
    return {"sent_actions": sent_actions, "context": context}


async def _finalize_clarification_reply(
    *,
    content: str,
    context: dict[str, Any],
    understanding: dict[str, Any],
    reply: str,
    timer: kf_turn_flow.RagStageTimer | None = None,
    inventory_read_context: InventoryReadContext | None = None,
) -> dict[str, Any]:
    draft = _normalize_customer_visible_reply_text_before_selfcheck(reply)
    tool_evidence = {
        "actions": ["clarification"],
        "clarification": dict((understanding.get("structured_task") or {}).get("clarification") or {}),
        "deterministic_reply_source": "rewrite_clarification",
    }
    planner_result = {
        "actions": ["clarification"],
        "reply_text": draft,
        "reply_source": "rewrite_clarification",
    }
    first = await _generate_reply_result(
        content=content,
        context=context,
        understanding=understanding,
        tool_evidence=dict(tool_evidence),
        planner_result=planner_result,
        timer=timer,
        inventory_read_context=inventory_read_context,
    )
    if not first.get("needs_planner_retry") and str(first.get("reply") or "").strip():
        return first
    retry_reason = str(first.get("planner_retry_reason") or "clarification_selfcheck_retry")
    second = await _generate_reply_result(
        content=content,
        context=context,
        understanding=understanding,
        tool_evidence=dict(tool_evidence),
        planner_result=planner_result,
        retry_reason=retry_reason,
        timer=timer,
        inventory_read_context=inventory_read_context,
    )
    if str(second.get("reply") or "").strip():
        return second
    return {
        "reply": draft,
        "draft_reply": str(first.get("draft_reply") or draft),
        "context": context,
        "selfcheck": first.get("selfcheck") or {},
        "needs_planner_retry": False,
        "planner_retry_reason": retry_reason,
    }


async def _handle_text_message(message: dict[str, Any]) -> None:
    await _handle_text_messages_batch([message])


async def _process_text_turn(
    *,
    open_kfid: str,
    external_userid: str,
    pending_items: list[dict[str, Any]],
    generation: int,
) -> None:
    conversation_key = _conversation_key(open_kfid, external_userid)
    content = _combined_pending_content(pending_items)
    msgids = _pending_message_ids(pending_items)
    if not content:
        await _cleanup_kf_turn(conversation_key, generation)
        return
    timer = kf_turn_flow.RagStageTimer()
    try:
        inventory_read_context = _create_inventory_read_context(
            prefix="kf",
            open_kfid=open_kfid,
            external_userid=external_userid,
            content=content,
            msgids=msgids,
            generation=generation,
        )
        context = _load_context(open_kfid, external_userid)
        context = _remember_inventory_read_context(context, inventory_read_context)
        context = kf_context_memory.append_dialog_message(context, role="user", content=content) or context
        signals = _deterministic_signals(content)

        with timer.stage("rewrite_intent"):
            understanding = await _understand_message(
                content=content,
                context=context,
                signals=signals,
                inventory_read_context=inventory_read_context,
            )
        _raise_if_stale_kf_turn(conversation_key, generation)
        state = _state_from_understanding(understanding)
        context = kf_context_memory.start_structured_turn(
            context,
            state=state,
            user_input={
                "content": content,
                "created_at": time.time(),
                "merged_message_count": len([item for item in pending_items if item.get("content")]),
                "msgids": msgids,
            },
            rewrite_result=understanding,
        )
        context["active_query_state"] = dict(understanding.get("query_state") or {})
        _save_context(open_kfid, external_userid, context)

        if understanding.get("needs_clarification"):
            reply = str(understanding.get("clarification_text") or "").strip()
            if not reply:
                reply = "你把具体小区、房号或预算发我一下，我按最新房源表帮你查准。"
            clarification_result = await _finalize_clarification_reply(
                content=content,
                context=context,
                understanding=understanding,
                reply=reply,
                timer=timer,
                inventory_read_context=inventory_read_context,
            )
            reply = _normalize_customer_visible_reply_text_before_selfcheck(
                str(clarification_result.get("reply") or reply)
            )
            context = clarification_result.get("context") or context
            _raise_if_stale_kf_turn(conversation_key, generation)
            _build_orchestrator_shadow_artifact(
                content=content,
                open_kfid=open_kfid,
                external_userid=external_userid,
                msgids=msgids,
                generation=generation,
                inventory_read_context=inventory_read_context,
                understanding=understanding,
                planner_result={"actions": ["clarification"], "reply_source": "rewrite_clarification"},
                tool_evidence={"actions": ["clarification"], "deterministic_reply_source": "rewrite_clarification"},
                reply_result=clarification_result,
                final_reply=reply,
            )
            with timer.stage("send"):
                context, did_send_clarification, _receipt = await _send_text_with_receipt(
                    open_kfid=open_kfid,
                    external_userid=external_userid,
                    context=context,
                    text=reply,
                    action_id="send-text-clarification",
                    text_role="clarification",
                    msgids=msgids,
                )
            if did_send_clarification:
                context = kf_context_memory.append_dialog_message(context, role="assistant", content=reply) or context
            context = kf_context_memory.record_structured_assistant_output(
                context,
                draft_reply=str(clarification_result.get("draft_reply") or reply),
                final_reply=reply,
                sent_action={"type": "text", "count": 1} if did_send_clarification else None,
                candidate_state=_candidate_state_summary(context),
            ) or context
            _save_context(open_kfid, external_userid, context)
            for msgid in msgids:
                wecom_kf.state_store.mark_processed(msgid)
            return

        retry_reason = ""
        planner_result: dict[str, Any] = {}
        tool_evidence: dict[str, Any] = {}
        reply_result: dict[str, Any] = {}
        preserved_sendable_evidence: dict[str, Any] = {}
        final_reply = settings.default_fallback_reply
        final_draft_reply = settings.default_fallback_reply
        for attempt in range(2):
            with timer.stage("planner_tools"):
                planner_result = await _plan_actions(
                    content=content,
                    context=context,
                    understanding=understanding,
                    signals=signals,
                    retry_reason=retry_reason,
                )
            _raise_if_stale_kf_turn(conversation_key, generation)
            if planner_result.get("need_rewrite_clarification"):
                retry_reason = str(planner_result.get("missing_evidence") or "planner_missing_evidence")
                if attempt == 0:
                    planner_feedback = {
                        "need_rewrite_clarification": True,
                        "missing_evidence": retry_reason,
                        "planner_result": planner_result,
                    }
                    with timer.stage("rewrite_intent"):
                        understanding = await _understand_message(
                            content=content,
                            context=context,
                            signals=signals,
                            planner_feedback=planner_feedback,
                            inventory_read_context=inventory_read_context,
                        )
                    _raise_if_stale_kf_turn(conversation_key, generation)
                    context["active_query_state"] = dict(understanding.get("query_state") or {})
                    context = kf_context_memory.update_structured_state(
                        context,
                        state=_state_from_understanding(understanding),
                        rewrite_result=understanding,
                    ) or context
                    _save_context(open_kfid, external_userid, context)
                    if not understanding.get("needs_clarification"):
                        continue
                    reply = str(understanding.get("clarification_text") or "").strip()
                    if not reply:
                        reply = "你把具体小区、房号或预算发我一下，我按最新房源表帮你查准。"
                    clarification_result = await _finalize_clarification_reply(
                        content=content,
                        context=context,
                        understanding=understanding,
                        reply=reply,
                        timer=timer,
                        inventory_read_context=inventory_read_context,
                    )
                    reply = _normalize_customer_visible_reply_text_before_selfcheck(
                        str(clarification_result.get("reply") or reply)
                    )
                    context = clarification_result.get("context") or context
                    _raise_if_stale_kf_turn(conversation_key, generation)
                    _build_orchestrator_shadow_artifact(
                        content=content,
                        open_kfid=open_kfid,
                        external_userid=external_userid,
                        msgids=msgids,
                        generation=generation,
                        inventory_read_context=inventory_read_context,
                        understanding=understanding,
                        planner_result={"actions": ["clarification"], "reply_source": "rewrite_clarification"},
                        tool_evidence={"actions": ["clarification"], "deterministic_reply_source": "rewrite_clarification"},
                        reply_result=clarification_result,
                        final_reply=reply,
                    )
                    with timer.stage("send"):
                        context, did_send_clarification, _receipt = await _send_text_with_receipt(
                            open_kfid=open_kfid,
                            external_userid=external_userid,
                            context=context,
                            text=reply,
                            action_id="send-text-clarification",
                            text_role="clarification",
                            msgids=msgids,
                        )
                    if did_send_clarification:
                        context = kf_context_memory.append_dialog_message(context, role="assistant", content=reply) or context
                    context = kf_context_memory.record_structured_assistant_output(
                        context,
                        draft_reply=str(clarification_result.get("draft_reply") or reply),
                        final_reply=reply,
                        sent_action={"type": "text", "count": 1} if did_send_clarification else None,
                        candidate_state=_candidate_state_summary(context),
                    ) or context
                    _save_context(open_kfid, external_userid, context)
                    for msgid in msgids:
                        wecom_kf.state_store.mark_processed(msgid)
                    return
                final_reply = str(understanding.get("clarification_text") or settings.default_fallback_reply)
                final_reply = _normalize_customer_visible_reply_text_before_selfcheck(final_reply)
                tool_evidence = {"actions": [], "planner_missing_evidence": retry_reason}
                break

            actions = _safe_action_list(planner_result)
            with timer.stage("tool_execution"):
                tool_evidence = await _execute_tools(
                    actions=actions,
                    content=content,
                    context=context,
                    understanding=understanding,
                    inventory_read_context=inventory_read_context,
                )
            if preserved_sendable_evidence:
                tool_evidence = _merge_preserved_sendable_evidence(tool_evidence, preserved_sendable_evidence)
            if _has_sendable_actions(tool_evidence):
                preserved_sendable_evidence = dict(tool_evidence)
            _raise_if_stale_kf_turn(conversation_key, generation)
            reply_result = await _generate_reply_result(
                content=content,
                context=context,
                understanding=understanding,
                tool_evidence=tool_evidence,
                planner_result=planner_result,
                retry_reason=retry_reason,
                timer=timer,
                inventory_read_context=inventory_read_context,
            )
            _raise_if_stale_kf_turn(conversation_key, generation)
            context = reply_result["context"]
            if reply_result.get("needs_planner_retry") and attempt == 0:
                final_draft_reply = str(reply_result.get("draft_reply") or "")
                retry_reason = str(reply_result.get("planner_retry_reason") or "final_selfcheck_retry")
                planner_feedback = {
                    "planner_retry_reason": retry_reason,
                    "selfcheck_result": reply_result.get("selfcheck") or {},
                    "planner_result": planner_result,
                    "tool_evidence_summary": _tool_evidence_summary(tool_evidence),
                }
                with timer.stage("rewrite_intent"):
                    understanding = await _understand_message(
                        content=content,
                        context=context,
                        signals=signals,
                        planner_feedback=planner_feedback,
                        inventory_read_context=inventory_read_context,
                    )
                _raise_if_stale_kf_turn(conversation_key, generation)
                context["active_query_state"] = dict(understanding.get("query_state") or {})
                context = kf_context_memory.update_structured_state(
                    context,
                    state=_state_from_understanding(understanding),
                    rewrite_result=understanding,
                ) or context
                _save_context(open_kfid, external_userid, context)
                if understanding.get("needs_clarification"):
                    reply = str(understanding.get("clarification_text") or "").strip()
                    if not reply:
                        reply = "你把具体小区、房号或预算发我一下，我按最新房源表帮你查准。"
                    clarification_result = await _finalize_clarification_reply(
                        content=content,
                        context=context,
                        understanding=understanding,
                        reply=reply,
                        timer=timer,
                        inventory_read_context=inventory_read_context,
                    )
                    final_reply = _normalize_customer_visible_reply_text_before_selfcheck(
                        str(clarification_result.get("reply") or reply)
                    )
                    final_draft_reply = str(clarification_result.get("draft_reply") or final_reply)
                    context = clarification_result.get("context") or context
                    planner_result = {"actions": ["clarification"], "reply_source": "rewrite_clarification"}
                    tool_evidence = {
                        "actions": ["clarification"],
                        "deterministic_reply_source": "rewrite_clarification",
                    }
                    reply_result = clarification_result
                    break
                continue
            final_reply = str(reply_result.get("reply") or settings.default_fallback_reply)
            final_draft_reply = str(reply_result.get("draft_reply") or final_reply)
            final_reply = _normalize_customer_visible_reply_text_before_selfcheck(final_reply)
            final_draft_reply = _normalize_customer_visible_reply_text_before_selfcheck(final_draft_reply)
            break

        _build_orchestrator_shadow_artifact(
            content=content,
            open_kfid=open_kfid,
            external_userid=external_userid,
            msgids=msgids,
            generation=generation,
            inventory_read_context=inventory_read_context,
            understanding=understanding,
            planner_result=planner_result,
            tool_evidence=tool_evidence,
            reply_result=reply_result,
            final_reply=final_reply,
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            draft_reply=final_draft_reply,
            final_reply=final_reply,
            candidate_state=_candidate_state_summary(context),
        ) or context
        _raise_if_stale_kf_turn(conversation_key, generation)
        with timer.stage("send"):
            send_result = await _send_final_actions(
                open_kfid=open_kfid,
                external_userid=external_userid,
                context=context,
                final_reply=final_reply,
                tool_evidence=tool_evidence,
                msgids=msgids,
            )
        context = send_result["context"]
        context = kf_context_memory.append_dialog_message(context, role="assistant", content=final_reply) or context
        _save_context(open_kfid, external_userid, context)
        for msgid in msgids:
            wecom_kf.state_store.mark_processed(msgid)
    except asyncio.CancelledError:
        logger.info("KF turn cancelled before reply was sent: %s", conversation_key)
        raise
    finally:
        try:
            logger.info(
                "KF RAG timing: %s",
                json.dumps(
                    {
                        "conversation": _mask_identifier(conversation_key),
                        "message_count": len(msgids),
                        **timer.snapshot(),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        except Exception as exc:
            logger.warning("KF RAG timing log failed: %s", exc)
        await _cleanup_kf_turn(conversation_key, generation)


async def _handle_enter_session(message: dict[str, Any]) -> None:
    welcome_code = extract_kf_welcome_code(message)
    open_kfid = extract_kf_open_kfid(message)
    external_userid = extract_kf_external_userid(message)
    key = _conversation_key(open_kfid or "unknown", external_userid or welcome_code)
    audit_base = {
        "key": _mask_identifier(key),
        "open_kfid": _mask_identifier(open_kfid),
        "external_userid": _mask_identifier(external_userid),
        "has_welcome_code": bool(welcome_code),
    }
    async with kf_welcome_lock:
        now = time.time()
        last_sent = wecom_kf.state_store.last_welcome_sent_at(key)
        if last_sent and now - last_sent < settings.wecom_kf_welcome_interval_seconds:
            logger.info("KF welcome skipped by interval: key=%s", key)
            _record_kf_welcome_audit(
                {
                    **audit_base,
                    "status": "skipped",
                    "reason": "interval",
                    "seconds_since_last": round(now - last_sent, 3),
                }
            )
            return

        event_error = ""
        if welcome_code:
            try:
                await _await_if_needed(
                    wecom_kf.send_welcome_text_on_event(
                        welcome_code,
                        settings.wecom_kf_welcome_text,
                    )
                )
                wecom_kf.state_store.mark_welcome_sent(key, now)
                logger.info("KF welcome sent by welcome_code: key=%s", key)
                _record_kf_welcome_audit(
                    {**audit_base, "status": "sent", "method": "send_msg_on_event"}
                )
                return
            except WeComKfSendLimitError:
                raise
            except Exception as exc:
                event_error = str(exc)
                logger.warning("KF welcome_code send failed, trying text fallback: %s", exc)
        else:
            event_error = "missing welcome_code"
            logger.info("KF enter_session has no welcome_code, trying text fallback")

        if open_kfid and external_userid:
            try:
                await _await_if_needed(
                    wecom_kf.send_text(
                        open_kfid,
                        external_userid,
                        settings.wecom_kf_welcome_text,
                    )
                )
                wecom_kf.state_store.mark_welcome_sent(key, now)
                logger.info("KF welcome sent by text fallback: key=%s", key)
                _record_kf_welcome_audit(
                    {
                        **audit_base,
                        "status": "sent",
                        "method": "send_text_fallback",
                        "fallback_reason": event_error,
                    }
                )
                return
            except WeComKfSendLimitError:
                raise
            except Exception as exc:
                logger.warning("KF welcome text fallback failed: %s", exc)
                _record_kf_welcome_audit(
                    {
                        **audit_base,
                        "status": "failed",
                        "method": "send_text_fallback",
                        "fallback_reason": event_error,
                        "error": str(exc),
                    }
                )
                return

        _record_kf_welcome_audit(
            {
                **audit_base,
                "status": "failed",
                "method": "none",
                "reason": event_error or "missing_target",
            }
        )
        logger.info("KF enter_session ignored: missing welcome target")


async def _handle_kf_event(payload: dict[str, str]) -> None:
    open_kfid = str(payload.get("OpenKfId") or payload.get("open_kfid") or "").strip()
    token = str(payload.get("Token") or payload.get("token") or "").strip()
    if not open_kfid or not token:
        return
    messages = await _await_if_needed(wecom_kf.sync_messages(open_kfid, token))
    text_messages: list[dict[str, Any]] = []
    for message in messages:
        try:
            if is_kf_enter_session_event(message):
                await _handle_enter_session(message)
                continue
            if should_auto_reply_kf_message(message):
                text_messages.append(message)
        except WeComKfSendLimitError:
            logger.warning("WeCom KF send limit hit for message: %s", message.get("msgid"))
            raise
        except Exception as exc:
            logger.exception("KF message handling failed: %s", exc)
    if text_messages:
        try:
            await _handle_text_messages_batch(text_messages)
        except WeComKfSendLimitError:
            logger.warning("WeCom KF send limit hit for text message batch")
            raise
        except Exception as exc:
            logger.exception("KF text message batch handling failed: %s", exc)


@app.on_event("startup")
async def startup() -> None:
    try:
        await inventory.refresh()
    except Exception as exc:
        logger.warning("initial inventory refresh failed: %s", exc)
    if settings.feishu_inventory_sheet_sync_on_startup:
        try:
            await _refresh_inventory_images(force=False)
        except Exception as exc:
            logger.warning("initial inventory image refresh failed: %s", exc)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "wecom-room-robot-agentic-rag",
        "inventory_cache_meta": _inventory_cache_meta_for_prompt(),
    }


@app.get("/admin/config/check")
async def config_check() -> dict[str, Any]:
    return get_config_status()


@app.get("/admin/wecom/kf/status")
async def wecom_kf_status() -> dict[str, Any]:
    state = wecom_kf.state_store.load()
    welcome_sent_at = state.get("welcome_sent_at") or {}
    recent_welcome_sent = [
        {
            "key": _mask_identifier(str(key)),
            "sent_at": float(sent_at or 0),
        }
        for key, sent_at in sorted(
            welcome_sent_at.items(),
            key=lambda item: float(item[1] or 0),
        )[-10:]
    ]
    return {
        "ok": True,
        "welcome_interval_seconds": settings.wecom_kf_welcome_interval_seconds,
        "cursor_present": bool(state.get("cursor")),
        "processed_msgid_count": len(state.get("processed_msgids") or []),
        "welcome_sent_count": len(welcome_sent_at),
        "recent_welcome_sent": recent_welcome_sent,
        "last_next_cursor_present": bool(getattr(wecom_kf, "last_next_cursor", "")),
        "recent_welcome_audits": _recent_kf_welcome_audits(30),
    }


@app.post("/admin/inventory/refresh")
async def refresh_inventory() -> dict[str, Any]:
    return await _refresh_inventory()


@app.post("/admin/feishu/sync-media")
async def sync_feishu_media(force: bool = True) -> dict[str, Any]:
    return await _sync_feishu_media(force=force)


@app.post("/admin/feishu/sync-inventory-image")
async def sync_feishu_inventory_image(force: bool = True) -> dict[str, Any]:
    return await _refresh_inventory_images(force=force)


@app.post("/admin/feishu/sync-region-inventory")
async def sync_feishu_region_inventory(
    dry_run: bool = False,
    sync_media: bool = True,
) -> dict[str, Any]:
    result = await RegionInventorySyncService().sync(dry_run=dry_run, sync_media=sync_media)
    if result.get("ok") and not dry_run:
        try:
            refreshed = await _refresh_inventory()
            result["rewrite_index"] = refreshed.get("rewrite_index", {})
        except Exception as exc:
            logger.exception("rewrite_inventory_index_after_region_sync_failed")
            result["rewrite_index"] = {"ok": False, "error": str(exc)}
    return result


@app.post("/feishu/events")
async def feishu_events(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if settings.feishu_event_verify_token:
        token = str(payload.get("token") or payload.get("header", {}).get("token") or "")
        if token and token != settings.feishu_event_verify_token:
            raise HTTPException(status_code=403, detail="invalid feishu token")
    challenge = payload.get("challenge")
    if challenge:
        return {"challenge": challenge}
    asyncio.create_task(_refresh_inventory_images(force=True))
    return {"ok": True}


@app.get("/wecom/kf/callback", response_class=PlainTextResponse)
async def verify_wecom_kf_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> str:
    return wecom_kf.verify_url(msg_signature, timestamp, nonce, echostr)


@app.post("/wecom/kf/callback", response_class=PlainTextResponse)
async def receive_wecom_kf_callback(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> str:
    body = (await request.body()).decode("utf-8")
    payload = wecom_kf.parse_callback_event(body, msg_signature, timestamp, nonce)
    if is_kf_message_event(payload):
        _schedule_background_task(_handle_kf_event(payload), label="KF callback message event")
    else:
        direct_event = kf_callback_payload_event_message(payload)
        if direct_event and is_kf_enter_session_event(direct_event):
            _schedule_background_task(
                _handle_enter_session(direct_event),
                label="KF callback enter_session event",
            )
    return "success"


@app.post("/debug/message")
async def debug_message(message: IncomingMessage) -> dict[str, Any]:
    content = message.content or ""
    inventory_read_context = _create_inventory_read_context(
        prefix="debug",
        open_kfid="debug",
        external_userid=message.user_id or "debug-user",
        content=content,
        msgids=[],
        generation="debug",
    )
    context = _load_context("debug", message.user_id or "debug-user")
    context = _remember_inventory_read_context(context, inventory_read_context)
    context = kf_context_memory.append_dialog_message(context, role="user", content=content) or context
    signals = _deterministic_signals(content)
    understanding = await _understand_message(
        content=content,
        context=context,
        signals=signals,
        inventory_read_context=inventory_read_context,
    )
    retry_reason = ""
    planner_result: dict[str, Any] = {}
    tool_evidence: dict[str, Any] = {}
    reply_result: dict[str, Any] = {}
    for attempt in range(2):
        planner_result = await _plan_actions(
            content=content,
            context=context,
            understanding=understanding,
            signals=signals,
            retry_reason=retry_reason,
        )
        if planner_result.get("need_rewrite_clarification") and attempt == 0:
            planner_feedback = {
                "need_rewrite_clarification": True,
                "missing_evidence": str(planner_result.get("missing_evidence") or "planner_missing_evidence"),
                "planner_result": planner_result,
            }
            understanding = await _understand_message(
                content=content,
                context=context,
                signals=signals,
                planner_feedback=planner_feedback,
                inventory_read_context=inventory_read_context,
            )
            if not understanding.get("needs_clarification"):
                retry_reason = str(planner_feedback["missing_evidence"])
                continue
        actions = _safe_action_list(planner_result)
        tool_evidence = await _execute_tools(
            actions=actions,
            content=content,
            context=context,
            understanding=understanding,
            inventory_read_context=inventory_read_context,
        )
        reply_result = await _generate_reply_result(
            content=content,
            context=context,
            understanding=understanding,
            tool_evidence=tool_evidence,
            planner_result=planner_result,
            retry_reason=retry_reason,
            inventory_read_context=inventory_read_context,
        )
        if reply_result.get("needs_planner_retry") and attempt == 0:
            retry_reason = str(reply_result.get("planner_retry_reason") or "final_selfcheck_retry")
            continue
        break
    actions = _safe_action_list(planner_result)
    reply = str(reply_result.get("reply") or "")
    orchestrator_shadow = _build_orchestrator_shadow_artifact(
        content=content,
        open_kfid="debug",
        external_userid=message.user_id or "debug-user",
        msgids=[],
        generation="debug",
        inventory_read_context=inventory_read_context,
        understanding=understanding,
        planner_result=planner_result,
        tool_evidence=tool_evidence,
        reply_result=reply_result,
        final_reply=reply,
    )
    return {
        "understanding": understanding,
        "planner_result": planner_result,
        "tool_evidence": {
            "actions": actions,
            "inventory_row_count": len(tool_evidence.get("inventory_rows") or []),
            "target_row_count": len(tool_evidence.get("target_rows") or []),
            "inventory_image_count": len(tool_evidence.get("inventory_images") or []),
            "image_count": len(tool_evidence.get("image_paths") or []),
            "video_count": len(tool_evidence.get("video_paths") or []),
            "missing_media": tool_evidence.get("missing_media") or [],
            "inventory_read_context": tool_evidence.get("inventory_read_context") or {},
            "inventory_source_metadata": tool_evidence.get("inventory_source_metadata") or {},
            "inventory_listing_evidence_count": len(tool_evidence.get("inventory_listing_evidence") or []),
            "inventory_read_error": tool_evidence.get("inventory_read_error") or {},
        },
        "reply": reply,
        "selfcheck": reply_result.get("selfcheck") or {},
        "orchestrator_shadow": orchestrator_shadow,
    }
