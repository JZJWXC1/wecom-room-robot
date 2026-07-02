# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any


RUN_ID = os.environ.get("RUN_ID") or f"server_online_dialog_qa_{time.strftime('%Y%m%d_%H%M%S')}"
BASE_URL = os.environ.get("QA_BASE_URL") or "http://127.0.0.1:8000/debug/message"
HEALTH_URL = os.environ.get("QA_HEALTH_URL") or "http://127.0.0.1:8000/health"
RESULT_PATH = Path("/tmp") / f"{RUN_ID}.json"
SUMMARY_PATH = Path("/tmp") / f"{RUN_ID}.summary.json"
PARTIAL_PATH = Path("/tmp") / f"{RUN_ID}.partial.json"
MAX_CONCURRENT_WINDOWS = int(os.environ.get("QA_MAX_CONCURRENT_WINDOWS") or "5")

CONTACT_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
BAD_TEXT = ("作为AI", "作为 AI", "系统显示", "稍后通知你", "满意度")
SHEET_TERMS = ("房源表", "表发", "发一下表", "最新表")
VIDEO_TERMS = ("视频", "原视频", "原片", "笔记", "视")
IMAGE_TERMS = ("图片", "照片", "图")
VIEWING_TERMS = (
    "密码",
    "看房方式",
    "自己看",
    "自助看",
    "门打不开",
    "锁打不开",
    "锁没反应",
    "进不去",
    "到门口",
    "能看吗",
    "能不能看",
    "今天可以看吗",
    "今天能不能看",
    "约",
    "预约",
)
CONTRACT_TERMS = ("合同", "预定", "订房", "定房", "定金", "联系号码", "联系谁")
DEPOSIT_TERMS = ("免押", "服务费", "芝麻", "无忧住")
MISSING_TERMS = ("没有", "暂无", "暂时没有", "没找到", "缺")
VIEWING_REPLY_TERMS = ("密码", "看房方式", "自己看", "联系", "预约", "确认")
DEPOSIT_REPLY_TERMS = ("无忧住", "芝麻", "服务费", "5.5", "8%", "免押")
STRICT_SHEET_TERMS = ("不要文字列表", "房源表图片", "表格图片")


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "greeting_inventory_short_media",
        "turns": [
            "你好，在吗",
            "北软件园附近1800以内的一室还有吗？",
            "独卫优先",
            "第一套视频",
            "图片也发一个",
            "上面那套视频发我",
            "这个多少水电",
            "今天能自己看吗",
            "密码多少",
            "最后房源表发我",
        ],
    },
    {
        "id": "sheet_candidate_negation_deposit_contract",
        "turns": [
            "发最新房源表图片，不要文字列表",
            "表里万达2000以下一室有哪些",
            "第1套视频",
            "不是第一套，是第二套视频",
            "这个图片",
            "不是这套，是上面那套，水电怎么算？",
            "能免押吗",
            "免押是不是免费？服务费比例是多少？",
            "客户想定，合同怎么签",
            "好",
        ],
    },
    {
        "id": "typo_community_original_video",
        "turns": [
            "星桥锦绣嘉苑2000左右的一室还有吗？名字可能打错了",
            "如果是星桥锦绣嘉苑，先查还在不在",
            "星桥锦秀嘉苑2000以内一室还有吗？",
            "原视频有吗",
            "图片也发",
            "这套押一付一多少，押二付一多少？",
            "水电怎么算",
            "看房密码多少，今天可以看吗？",
            "客户觉得视频糊，有没有原视频？",
            "免押服务费多少？",
        ],
    },
    {
        "id": "gaotang_unavailable_viewing_contract",
        "turns": [
            "皋塘东站附近2600以内的一室有没有？",
            "独立厨房或者独卫优先",
            "第一套视频发我看看",
            "这套什么时候空出",
            "还没空出能不能看",
            "密码显示错误，客户进不去怎么办？",
            "客户要订房",
            "定金联系谁",
            "高塘运都9-402B今天能看吗？",
            "嗯",
        ],
    },
    {
        "id": "shiqiao_multi_selection_correction",
        "turns": [
            "石桥4800左右有两室整租吗？",
            "筛出来的1和2视频发我",
            "这两套哪个价格低一点",
            "1号那套水电怎么收",
            "3号看房方式是什么",
            "不是3号，是第二套图片",
            "上上条那个房间还能看吗？",
            "客户看中了其中一套，找哪个号码",
            "石桥华丰房源表发我",
            "还有呢",
        ],
    },
    {
        "id": "yangjia_deposit_policy",
        "turns": [
            "杨家新雅苑6000左右三室有吗？",
            "第一套户型特点怎么样",
            "水电民用还是商用",
            "能不能免押",
            "免押是什么",
            "服务费怎么算",
            "客户芝麻分不够怎么办",
            "合同怎么签",
            "杨家新雅苑15-603图片发我",
            "可以",
        ],
    },
    {
        "id": "dongxinyuan_short_followups",
        "turns": [
            "东新园附近3500到5200的两室还有哪些？",
            "4000到5000的呢",
            "带视频的优先，先发前两套",
            "第1和第2套水电分别怎么收",
            "哪套更低",
            "第二套今天能看吗",
            "不是这个，第一套",
            "图",
            "视",
            "现在换东新园4000到5000两室，刚才的不要了",
        ],
    },
    {
        "id": "huafeng_material_partial_missing",
        "turns": [
            "华丰半山附近4500左右整租两室还有吗？",
            "最好装修好一点，能马上入住的",
            "前两套视频先发我",
            "第二套图片有没有",
            "这两套有原视频或者飞书素材链接吗",
            "价格和水电说清楚",
            "免押怎么回客户",
            "客户想约今天晚上看",
            "华丰欣苑14-2-901图片发我",
            "在吗",
        ],
    },
    {
        "id": "tangrunfu_room_specific",
        "turns": [
            "棠润府15-2-801B还在吗？客户可能把小区名写错了",
            "棠闰府15-2-801B视频发我",
            "图片",
            "这套价格和水电都说清楚",
            "这套什么时候空出，能不能自己看",
            "密码没有或者不对的话联系谁？",
            "免押和服务费怎么说？",
            "客户想定这套，合同怎么签？",
            "除了这套，棠润府1600到1800还有别的吗？",
            "好的",
        ],
    },
    {
        "id": "xintiandi_table_note_booking",
        "turns": [
            "新天地附近两室一厅预算4000到5500",
            "4500到5200的呢",
            "前三套视频粗筛一下",
            "第二套水电",
            "那套今天能看吗",
            "笔记也给我",
            "客户问定房流程",
            "免押条件",
            "刚才视频糊，能不能发原文件或者飞书原素材？",
            "谢谢，嗯",
        ],
    },
]


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def redact_text(text: str) -> str:
    return CONTACT_RE.sub("<redacted-phone>", text or "")


def read_json_url(url: str, timeout: int = 30) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_message(user_id: str, content: str) -> dict[str, Any]:
    payload = json.dumps(
        {"source": "debug", "user_id": user_id, "msg_type": "text", "content": content},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


def actions_from(data: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for source in (data.get("planner_result"), data.get("tool_evidence"), data.get("understanding")):
        if not isinstance(source, dict):
            continue
        for raw in source.get("actions") or []:
            if isinstance(raw, dict):
                result.append(str(raw.get("action_type") or raw.get("type") or raw.get("action") or raw.get("name") or ""))
            else:
                result.append(str(raw))
    tool = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    for key in ("send_actions", "outbound_send_actions"):
        for raw in tool.get(key) or []:
            if isinstance(raw, dict):
                result.append(str(raw.get("action_type") or raw.get("type") or ""))
    package = tool.get("outbound_package") if isinstance(tool.get("outbound_package"), dict) else {}
    for raw in package.get("send_actions") or []:
        if isinstance(raw, dict):
            result.append(str(raw.get("action_type") or raw.get("type") or ""))
    return [item for item in result if item]


def count_value(tool: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = tool.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            return len(value)
        try:
            return int(value)
        except Exception:
            continue
    return 0


def has_missing_media(tool: dict[str, Any], reply: str, kind: str) -> bool:
    missing = tool.get("missing_media") or []
    if missing:
        text = json.dumps(missing, ensure_ascii=False)
        return kind in text or "media" in text or contains_any(text, MISSING_TERMS)
    return contains_any(reply, MISSING_TERMS) and (kind == "video" or contains_any(reply, IMAGE_TERMS))


def validation_status(data: dict[str, Any]) -> str:
    candidates: list[dict[str, Any]] = []
    if isinstance(data.get("llm2_production"), dict):
        candidates.append(data["llm2_production"])
    tool = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    prod = tool.get("dual_llm_production") if isinstance(tool.get("dual_llm_production"), dict) else {}
    if isinstance(prod.get("llm2"), dict):
        candidates.append(prod["llm2"])
    if isinstance(tool.get("llm2_production_outbound_validation"), dict):
        candidates.append({"outbound_validation": tool["llm2_production_outbound_validation"]})
    for item in candidates:
        validation = item.get("validation") or item.get("outbound_validation") or item
        if isinstance(validation, dict) and validation.get("status"):
            return str(validation.get("status"))
    return ""


def selfcheck_status(data: dict[str, Any]) -> str:
    selfcheck = data.get("selfcheck") if isinstance(data.get("selfcheck"), dict) else {}
    return str(selfcheck.get("status") or "")


def evaluate(question: str, data: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    reply = str(data.get("reply") or "")
    tool = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    action_text = " ".join(actions_from(data))
    reply_action_text = reply + " " + action_text
    if not reply.strip():
        failures.append("empty_reply")
    if "\ufffd" in reply or "\ufffd" in question:
        failures.append("mojibake")
    status = selfcheck_status(data)
    if status and status != "pass":
        failures.append("selfcheck_not_pass:" + status)
    v_status = validation_status(data).lower()
    if v_status in {"blocked", "rewrite_required", "retry"}:
        failures.append("llm2_validation_not_pass:" + v_status)
    for bad in BAD_TEXT:
        if bad in reply:
            failures.append("banned_visible_phrase:" + bad)
    if contains_any(question, SHEET_TERMS):
        strict_sheet = contains_any(question, STRICT_SHEET_TERMS)
        if count_value(tool, "inventory_image_count") <= 0 and "send_inventory_sheet" not in action_text and (
            strict_sheet or not contains_any(reply, ("房源表", "表"))
        ):
            failures.append("expected_inventory_table")
    if contains_any(question, VIDEO_TERMS):
        if count_value(tool, "video_count", "videos_count") <= 0 and "send_video" not in action_text and not has_missing_media(tool, reply, "video") and not contains_any(reply, ("视频", "详细", "房间")):
            failures.append("expected_video_or_missing_media")
    if contains_any(question, IMAGE_TERMS):
        if count_value(tool, "image_count", "images_count") <= 0 and "send_image" not in action_text and not has_missing_media(tool, reply, "image") and not contains_any(reply, ("图片", "照片")):
            failures.append("expected_image_or_missing_media")
    if contains_any(question, VIEWING_TERMS):
        if not contains_any(reply_action_text, VIEWING_REPLY_TERMS) and not any(a in action_text for a in ("viewing_password", "viewing_contact", "contract_contact")):
            failures.append("expected_viewing_or_password_answer")
    if contains_any(question, CONTRACT_TERMS):
        if not CONTACT_RE.search(reply) and "contract_contact" not in action_text and not contains_any(reply, ("联系", "号码")):
            failures.append("expected_contract_contact")
    if contains_any(question, DEPOSIT_TERMS):
        if not contains_any(reply, DEPOSIT_REPLY_TERMS):
            failures.append("expected_deposit_policy")
    evidence_text = json.dumps(data.get("tool_evidence") or {}, ensure_ascii=False)
    if "tool_grounded_reply" in evidence_text:
        failures.append("tool_grounded_reply_fallback_visible")
    if "legacy_planner" in evidence_text.lower() and "non-production" not in evidence_text.lower():
        failures.append("legacy_planner_marker_visible")
    return failures


def safe_shell(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT, timeout=20).strip()
    except Exception as exc:
        return "error:" + exc.__class__.__name__


def collect_environment() -> dict[str, Any]:
    try:
        health = read_json_url(HEALTH_URL, timeout=10)
    except Exception as exc:
        health = {"error": exc.__class__.__name__}
    manifest = Path("/opt/wecom-room-robot/current/room_database/media_manifest.json")
    if not manifest.exists():
        manifest = Path("/opt/wecom-room-robot/room_database/media_manifest.json")
    return {
        "current_realpath": safe_shell("readlink -f /opt/wecom-room-robot/current"),
        "service": safe_shell("systemctl is-active wecom-room-robot"),
        "timer_feishu": safe_shell("systemctl is-active wecom-room-robot-feishu-region-sync.timer"),
        "timer_rag": safe_shell("systemctl is-active wecom-room-robot-rag-cache-sync.timer"),
        "health": health,
        "media_manifest_exists": manifest.exists(),
        "media_manifest_sha256": safe_shell(f"sha256sum {manifest} | cut -d' ' -f1") if manifest.exists() else "",
    }


def compact_record(
    window_index: int,
    window_name: str,
    turn_index: int,
    question: str,
    data: dict[str, Any],
    elapsed: float,
    error: str = "",
) -> dict[str, Any]:
    tool = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    understanding = data.get("understanding") if isinstance(data.get("understanding"), dict) else {}
    planner = data.get("planner_result") if isinstance(data.get("planner_result"), dict) else {}
    failures = [error] if error else evaluate(question, data)
    return {
        "window_index": window_index,
        "window_name": window_name,
        "turn_index": turn_index,
        "question": question,
        "ok": not failures,
        "failures": failures,
        "elapsed_seconds": round(elapsed, 2),
        "reply_redacted": redact_text(str(data.get("reply") or ""))[:500],
        "intent": understanding.get("intent"),
        "actions": actions_from(data),
        "planner_source": planner.get("source"),
        "selfcheck_status": selfcheck_status(data),
        "llm2_validation_status": validation_status(data),
        "deterministic_reply_source": tool.get("deterministic_reply_source"),
        "tool_counts": {
            "inventory_row_count": tool.get("inventory_row_count"),
            "target_row_count": tool.get("target_row_count"),
            "inventory_image_count": tool.get("inventory_image_count"),
            "image_count": tool.get("image_count"),
            "video_count": tool.get("video_count"),
            "missing_media_count": len(tool.get("missing_media") or []),
            "send_action_count": len(tool.get("send_actions") or []),
        },
    }


def write_summary(records: list[dict[str, Any]], started: float, final: bool, environment: dict[str, Any]) -> None:
    failed = [record for record in records if not record.get("ok")]
    reasons: dict[str, int] = {}
    for record in failed:
        for reason in record.get("failures") or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    payload = {
        "summary": {
            "run_id": RUN_ID,
            "final": final,
            "window_count": len(SCENARIOS),
            "expected_turn_count": sum(len(item["turns"]) for item in SCENARIOS),
            "completed_turn_count": len(records),
            "passed_turn_count": len(records) - len(failed),
            "failed_turn_count": len(failed),
            "failure_reasons": reasons,
            "elapsed_seconds": round(time.time() - started, 2),
            "result_path": str(RESULT_PATH),
            "environment": environment,
        },
        "failures": failed,
    }
    SUMMARY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    PARTIAL_PATH.write_text(json.dumps({"summary": payload["summary"], "turns": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    if final:
        RESULT_PATH.write_text(json.dumps({"summary": payload["summary"], "turns": records}, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_window(
    index: int,
    scenario: dict[str, Any],
    records: list[dict[str, Any]],
    lock: asyncio.Lock,
    started: float,
    environment: dict[str, Any],
) -> None:
    user_id = f"codex-qa-{RUN_ID}-w{index:02d}"
    for turn_index, question in enumerate(scenario["turns"], start=1):
        t0 = time.time()
        try:
            data = await asyncio.to_thread(post_message, user_id, question)
            record = compact_record(index, scenario["id"], turn_index, question, data, time.time() - t0)
        except Exception as exc:
            record = compact_record(index, scenario["id"], turn_index, question, {}, time.time() - t0, "exception:" + exc.__class__.__name__)
            record["traceback"] = traceback.format_exc()[-1600:]
        async with lock:
            records.append(record)
            write_summary(records, started, False, environment)
            print(
                json.dumps(
                    {
                        "event": "turn",
                        "done": len(records),
                        "window": index,
                        "turn": turn_index,
                        "ok": record["ok"],
                        "failures": record["failures"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


async def main() -> None:
    started = time.time()
    environment = collect_environment()
    records: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)

    async def guarded(index: int, scenario: dict[str, Any]) -> None:
        async with semaphore:
            await run_window(index, scenario, records, lock, started, environment)

    await asyncio.gather(*(guarded(index, scenario) for index, scenario in enumerate(SCENARIOS, start=1)))
    write_summary(records, started, True, environment)
    print(SUMMARY_PATH)


if __name__ == "__main__":
    asyncio.run(main())
