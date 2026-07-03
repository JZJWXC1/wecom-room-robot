# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.services.region_inventory_constants import active_area_alias_groups


RUN_ID = os.environ.get("RUN_ID") or f"server_5w50_dialog_qa_{time.strftime('%Y%m%d_%H%M%S')}"
BASE_URL = os.environ.get("QA_BASE_URL") or "http://127.0.0.1:8000/debug/message"
HEALTH_URL = os.environ.get("QA_HEALTH_URL") or "http://127.0.0.1:8000/health"
RESULT_DIR = Path(os.environ.get("QA_RESULT_DIR") or "qa_artifacts/results")
MAX_CONCURRENT_WINDOWS = int(os.environ.get("QA_MAX_CONCURRENT_WINDOWS") or "5")
REQUEST_TIMEOUT = int(os.environ.get("QA_REQUEST_TIMEOUT") or "90")


CONTACT_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
RAW_CANDIDATE_RE = re.compile(r"候选\s*\d+|候选\d+")
VIDEO_SENT_CLAIM_RE = re.compile(
    r"(?:视频.{0,12}(?:正在发送|发送中|已发|已发送|发你了|发给你了)|(?:马上|现在|这就|立即).{0,24}(?:(?:发|发送|传).{0,12}视频|视频.{0,8}(?:发|发送|传))|这是.{0,16}视频)"
)
IMAGE_SENT_CLAIM_RE = re.compile(
    r"(?:(?:图片|照片).{0,12}(?:正在发送|发送中|已发|已发送|发你了|发给你了)|(?:马上|现在|这就|立即).{0,24}(?:(?:发|发送|传).{0,12}(?:图片|照片)|(?:图片|照片).{0,8}(?:发|发送|传))|这是.{0,16}(?:图片|照片))"
)
ORIGINAL_VIDEO_SENT_CLAIM_RE = re.compile(
    r"(?:这是.{0,24}(?:原视频|高清版|高清视频)|(?:原视频|高清版|高清视频).{0,12}(?:已发|发你了|发给你了|如下))"
)

BAD_TEXT = (
    "作为AI",
    "作为 AI",
    "系统显示",
    "工具未绑定",
    "上一轮只有",
    "客户要查询",
    "客户选择了",
    "满足度",
    "满意度",
    "OUTBOUND_FORBIDDEN",
    "deterministic",
    "kf_legacy",
    "受控通道",
    "受控渠道",
    "通过系统",
    "专属联系通道",
    "稍后会通过",
    "稍后会发",
    "稍后会",
    "稍后将",
    "稍等",
    "视频视频",
    "booking",
)

SHEET_TERMS = ("房源表", "表发", "发一下表", "最新表", "表格", "房源图")
VIDEO_TERMS = ("视频", "原视频", "原片", "笔记")
IMAGE_TERMS = ("图片", "照片", "图")
VIEWING_TERMS = ("看房", "密码", "门锁", "进不去", "预约", "今天能看", "自己看")
CONTRACT_TERMS = ("合同", "预定", "订房", "定房", "定金", "联系号码", "联系谁")
DEPOSIT_TERMS = ("免押", "服务费", "芝麻", "无忧住")
PRICE_TERMS = ("价格", "租金", "多少", "水电", "押一付一", "押二付一")

SHEET_REPLY_TERMS = ("房源表", "表", "图片", "截图")
VIDEO_REPLY_TERMS = ("视频", "素材", "暂无", "没有", "没找到", "发你", "这套")
IMAGE_REPLY_TERMS = ("图片", "照片", "暂无", "没有", "没找到", "发你", "这套")
VIEWING_REPLY_TERMS = ("看房", "密码", "联系", "预约", "确认", "187", "132", "199")
CONTRACT_REPLY_TERMS = ("联系", "合同", "定金", "订房", "187", "132", "199")
DEPOSIT_REPLY_TERMS = ("无忧住", "芝麻", "服务费", "5.5", "8%", "免押", "风控")
PRICE_REPLY_TERMS = ("租金", "价格", "水电", "押", "元", "暂无", "没有", "没找到")
TARGET_CLARIFICATION_TERMS = ("没法定位", "无法定位", "具体房源", "小区+房号", "小区和房号", "重新筛", "按序号")

AREA_ALIAS_GROUPS = active_area_alias_groups()


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "window_01_inventory_media_viewing",
        "title": "房源筛选、视频、图片、看房密码、房源表",
        "turns": [
            "你好，在吗",
            "北软附近1800以内的一室还有吗？独卫优先",
            "第一套视频发我看看",
            "图片也发一个",
            "笔记发我",
            "这套水电怎么算？",
            "今天能自己看吗？",
            "密码多少？",
            "最后房源表发我",
            "还有没有同价位的？",
        ],
    },
    {
        "id": "window_02_sheet_negation_deposit_contract",
        "title": "房源表、候选纠偏、免押、合同订房",
        "turns": [
            "发最新房源表图片，不要文字列表",
            "表里万达2000以下一室有哪些？",
            "第1套视频",
            "不是第一套，是第二套视频",
            "这个图片",
            "不是这套，是上面那套，水电怎么算？",
            "能免押吗？",
            "免押是不是免费？服务费比例是多少？",
            "客户想定，合同怎么签？",
            "好，联系谁？",
        ],
    },
    {
        "id": "window_03_typo_original_video_policy",
        "title": "小区错别字、在租、原视频、费用政策",
        "turns": [
            "星桥锦绣嘉苑2000左右的一室还在吗？名字可能打错了",
            "如果是星桥锦绣嘉苑，先查还在不在",
            "有视频先发视频",
            "原视频有吗？",
            "图片也发",
            "这套押一付一多少，押二付一多少？",
            "水电怎么算？",
            "看房密码多少，今天可以看吗？",
            "客户觉得视频糊，有没有原视频？",
            "免押服务费多少？",
        ],
    },
    {
        "id": "window_04_unavailable_booking_contact",
        "title": "区域推荐、未空看房、门锁异常、订房联系人",
        "turns": [
            "皋塘东站附近2600以内的一室有没有？",
            "独立厨房或者独卫优先",
            "第一套视频发我看看",
            "这套什么时候空出？",
            "还没空出能不能看？",
            "密码显示错误，客户进不去怎么办？",
            "客户要订房",
            "定金联系谁？",
            "合同联系人给一个",
            "嗯",
        ],
    },
    {
        "id": "window_05_multi_selection_correction",
        "title": "多套推荐、序号引用、纠偏、素材绑定",
        "turns": [
            "石桥4800左右有两室整租吗？",
            "筛出来的1和3视频发我",
            "这两套哪个价格低一点？",
            "1号那套水电怎么收？",
            "3号看房方式是什么？",
            "不是3号，是第二套图片",
            "剩下的有视频吗？",
            "客户看中了其中一套，找哪个号码？",
            "石桥华丰房源表发我",
            "还有呢？",
        ],
    },
]


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def wants_price_or_utility_answer(question: str) -> bool:
    text = str(question or "")
    if not text:
        return False
    if any(term in text for term in ("水电", "水费", "电费", "价格", "租金", "押一付一", "押二付一", "多少钱", "多少一月")):
        return True
    if "多少" in text and not contains_any(text, VIEWING_TERMS):
        return True
    return False


def wants_utility_answer(question: str) -> bool:
    text = str(question or "")
    return any(term in text for term in ("水电", "水费", "电费"))


def wants_price_comparison(question: str) -> bool:
    text = str(question or "")
    if not contains_any(text, ("哪个", "哪套", "谁", "对比", "比较", "一样", "差")):
        return False
    return contains_any(text, ("价格", "租金", "便宜", "低一点", "低些", "划算"))


def is_safe_price_comparison_clarification(reply: str) -> bool:
    text = str(reply or "")
    if not contains_any(
        text,
        (
            "没法",
            "无法",
            "不能",
            "还没列出多套",
            "还没筛出",
            "还没筛到",
            "没有两套",
            "只列了",
            "只有一套",
            "哪两套",
            "对比哪两套",
        ),
    ):
        return False
    return contains_any(text, ("小区+房号", "小区和房号", "重新", "再筛", "编号", "发我", "说下"))


def area_scope_from_text(*texts: str) -> tuple[str, tuple[str, ...]]:
    combined = " ".join(str(text or "") for text in texts)
    for alias, group in AREA_ALIAS_GROUPS.items():
        if alias in combined:
            return alias, group
    return "", ()


def row_area_matches_group(row: dict[str, Any], group: tuple[str, ...]) -> bool:
    area = str(row.get("area") or row.get("区域") or "")
    return bool(area and any(part in area for part in group))


def evidence_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key in ("tool_candidates", "inventory_rows", "target_rows"):
        for row in evidence.get(key) or []:
            if isinstance(row, dict):
                rows.append(row)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        label = str(row.get("label") or row.get("community") or row)
        if label in seen:
            continue
        seen.add(label)
        deduped.append(row)
    return deduped


def redact_text(text: str) -> str:
    return CONTACT_RE.sub("<redacted-phone>", text or "")


def request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_action_types(data: dict[str, Any]) -> list[str]:
    actions = ((data.get("tool_evidence") or {}).get("actions") or []) if isinstance(data, dict) else []
    action_types: list[str] = []
    for action in actions:
        if isinstance(action, dict):
            value = action.get("type") or action.get("action") or action.get("name")
            if value:
                action_types.append(str(value))
        else:
            action_types.append(str(action))
    return action_types


def validation_status(data: dict[str, Any]) -> tuple[str, str]:
    selfcheck = data.get("selfcheck") if isinstance(data.get("selfcheck"), dict) else {}
    status = str(
        selfcheck.get("status")
        or selfcheck.get("result")
        or selfcheck.get("decision")
        or selfcheck.get("verdict")
        or ""
    ).lower()
    reason = str(selfcheck.get("reason") or selfcheck.get("block_reason") or "")
    return status, reason


def shadow_side_effect_count(data: dict[str, Any]) -> int:
    artifact = data.get("orchestrator_shadow") if isinstance(data.get("orchestrator_shadow"), dict) else {}
    shadow_a = artifact.get("shadow_a") if isinstance(artifact.get("shadow_a"), dict) else {}
    diff = shadow_a.get("diff") if isinstance(shadow_a.get("diff"), dict) else {}
    return sum(
        1
        for key in ("customer_visible_reply_changed", "send_actions_changed", "fact_source_changed")
        if bool(diff.get(key))
    )


def evaluate_turn(question: str, data: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    reply = str(data.get("reply") or "")
    reply_compact = re.sub(r"\s+", "", reply)
    evidence = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
    actions = extract_action_types(data)
    actions_text = " ".join(actions)
    selfcheck_status, selfcheck_reason = validation_status(data)

    if not reply.strip() and not evidence.get("inventory_image_count") and not evidence.get("image_count") and not evidence.get("video_count"):
        failures.append("回复和素材动作都为空")
    if "�" in reply:
        failures.append("回复存在乱码替换字符")
    for term in BAD_TEXT:
        if term in reply:
            failures.append(f"客户可见回复含禁用内部词: {term}")
    if RAW_CANDIDATE_RE.search(reply_compact) and "租金" in reply:
        failures.append("客户可见回复暴露原始候选模板")
    if re.search(r"(?:匹配到|查到|有)\s*(?:这|以下|共|约)?\s*[\d一二三四五六七八九十]+\s*套", reply) and any(term in reply for term in ("不满足", "已剔除", "剔除")):
        failures.append("回复把已剔除房源计入匹配数量")
    if selfcheck_status in {"fail", "failed", "blocked", "reject", "rejected"}:
        failures.append(f"自检失败: {selfcheck_status} {selfcheck_reason}".strip())
    shadow_effects = shadow_side_effect_count(data)
    if shadow_effects:
        failures.append(f"shadow 出站副作用计数非零: {shadow_effects}")

    has_missing_media = bool(evidence.get("missing_media"))
    if contains_any(question, SHEET_TERMS):
        if not evidence.get("inventory_image_count") and not contains_any(reply, SHEET_REPLY_TERMS):
            failures.append("房源表请求未发送/说明房源表")
    if contains_any(question, VIDEO_TERMS):
        if "笔记" in question and "笔记" in reply:
            failures.append("笔记请求回复不应复述“笔记”，应按视频或房间信息处理")
        if VIDEO_SENT_CLAIM_RE.search(reply) and not evidence.get("video_count") and not has_missing_media:
            failures.append("视频回复声称已发送/发送中，但工具没有视频动作或缺失素材证据")
        wants_original = contains_any(question, ("原视频", "原片", "高清", "源文件", "下载链接", "太糊", "模糊", "保存", "转发"))
        has_original = bool(
            evidence.get("original_video_url_count")
            or evidence.get("material_page_url_count")
            or evidence.get("original_video_media_manifest_evidence_count")
        )
        explains_no_original = "没有原视频/高清下载链接" in reply or "没有单独的原视频" in reply
        if wants_original and not has_original and ORIGINAL_VIDEO_SENT_CLAIM_RE.search(reply) and not explains_no_original:
            failures.append("原视频/高清请求没有原始来源证据但回复声称发送原视频")
        if (
            not evidence.get("video_count")
            and not has_missing_media
            and not contains_any(reply, VIDEO_REPLY_TERMS)
            and not contains_any(reply, TARGET_CLARIFICATION_TERMS)
        ):
            failures.append("视频/笔记请求未发送视频，也未明确说明素材状态")
    if contains_any(question, IMAGE_TERMS) and "房源表" not in question:
        if IMAGE_SENT_CLAIM_RE.search(reply) and not evidence.get("image_count") and not has_missing_media:
            failures.append("图片回复声称已发送/发送中，但工具没有图片动作或缺失素材证据")
        if (
            not evidence.get("image_count")
            and not has_missing_media
            and not contains_any(reply, IMAGE_REPLY_TERMS)
            and not contains_any(reply, TARGET_CLARIFICATION_TERMS)
        ):
            failures.append("图片请求未发送图片，也未明确说明素材状态")
    if contains_any(question, VIEWING_TERMS):
        if not contains_any(reply, VIEWING_REPLY_TERMS) and not any("view" in item or "contact" in item for item in actions):
            failures.append("看房/密码/门锁问题未给出看房或联系处理")
    if contains_any(question, CONTRACT_TERMS):
        if not contains_any(reply, CONTRACT_REPLY_TERMS) and "contact" not in actions_text:
            failures.append("合同/订房/定金问题未引导联系")
    if contains_any(question, DEPOSIT_TERMS):
        if not contains_any(reply, DEPOSIT_REPLY_TERMS):
            failures.append("免押问题未覆盖无忧住/芝麻/服务费/风控边界")
        if any(term in question for term in ("能免押", "能不能免押", "可以免押", "支不支持免押")) and not contains_any(
            reply,
            ("自查", "信用额度", "租房板块", "支付宝查", "支付宝：我的"),
        ):
            failures.append("免押条件问法缺少支付宝自查方式")
    if wants_utility_answer(question):
        if not contains_any(reply, ("水", "电", "水电", "备注", "具体房源", "暂时没有", "没找到")):
            failures.append("水电问题未回答水/电字段")
    if wants_price_or_utility_answer(question) and not contains_any(question, DEPOSIT_TERMS):
        if (
            not contains_any(reply, PRICE_REPLY_TERMS)
            and not evidence.get("target_row_count")
            and not (wants_price_comparison(question) and is_safe_price_comparison_clarification(reply))
        ):
            failures.append("价格/水电问题未给出事实口径或缺失说明")
        has_price_rows = bool(evidence.get("target_row_count") or evidence.get("inventory_row_count"))
        if has_price_rows and "押一" in question and "押二" in question and ("押一" not in reply or "押二" not in reply):
            failures.append("双档价格问题缺少押一或押二")
    area_alias, area_group = area_scope_from_text(question, reply)
    scoped_rows = evidence_rows(data)
    if area_group and scoped_rows:
        leaked = [
            str(row.get("label") or row.get("community") or row)
            for row in scoped_rows
            if not row_area_matches_group(row, area_group)
        ]
        if leaked:
            failures.append(f"区域一致性失败: {area_alias} 范围内混入非本区域房源 {', '.join(leaked[:3])}")

    return failures


async def post_turn(window_id: str, turn_index: int, question: str) -> dict[str, Any]:
    payload = {
        "source": "debug",
        "user_id": window_id,
        "msg_type": "text",
        "content": question,
    }
    started = time.time()
    try:
        data = await asyncio.to_thread(request_json, BASE_URL, payload)
        elapsed_ms = int((time.time() - started) * 1000)
        failures = evaluate_turn(question, data)
        evidence = data.get("tool_evidence") if isinstance(data.get("tool_evidence"), dict) else {}
        understanding = data.get("understanding") if isinstance(data.get("understanding"), dict) else {}
        record = {
            "turn": turn_index,
            "question": question,
            "reply": redact_text(str(data.get("reply") or "")),
            "ok": not failures,
            "failures": failures,
            "elapsed_ms": elapsed_ms,
            "intent": understanding.get("intent") or understanding.get("primary_intent") or "",
            "needs_clarification": bool(understanding.get("needs_clarification")),
            "actions": extract_action_types(data),
            "tool_counts": {
                "inventory_rows": evidence.get("inventory_row_count") or 0,
                "target_rows": evidence.get("target_row_count") or 0,
                "inventory_images": evidence.get("inventory_image_count") or 0,
                "images": evidence.get("image_count") or 0,
                "videos": evidence.get("video_count") or 0,
                "original_video_urls": evidence.get("original_video_url_count") or 0,
                "material_page_urls": evidence.get("material_page_url_count") or 0,
                "missing_media": len(evidence.get("missing_media") or []),
            },
            "tool_candidates": evidence.get("tool_candidates") or [],
            "inventory_rows": evidence.get("inventory_rows") or [],
            "target_rows": evidence.get("target_rows") or [],
            "region_whitelist": evidence.get("region_whitelist") or {},
            "refine_within_candidates": evidence.get("refine_within_candidates") or {},
            "selfcheck": data.get("selfcheck") or {},
            "context_memory": data.get("context_memory") or {},
            "shadow_side_effect_count": shadow_side_effect_count(data),
        }
        print(
            json.dumps(
                {
                    "event": "turn",
                    "window": window_id,
                    "turn": turn_index,
                    "ok": record["ok"],
                    "elapsed_ms": elapsed_ms,
                    "failures": failures,
                    "reply": record["reply"][:120],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return record
    except (urllib.error.URLError, TimeoutError, Exception) as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        failure = f"请求异常: {exc.__class__.__name__}: {exc}"
        print(
            json.dumps(
                {"event": "turn_exception", "window": window_id, "turn": turn_index, "elapsed_ms": elapsed_ms, "failure": failure},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return {
            "turn": turn_index,
            "question": question,
            "reply": "",
            "ok": False,
            "failures": [failure],
            "elapsed_ms": elapsed_ms,
            "exception_trace": traceback.format_exc(),
        }


async def run_window(scenario: dict[str, Any], stop_event: asyncio.Event, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    window_id = f"{RUN_ID}:{scenario['id']}"
    records: list[dict[str, Any]] = []
    for turn_index, question in enumerate(scenario["turns"], start=1):
        if stop_event.is_set():
            break
        async with semaphore:
            if stop_event.is_set():
                break
            record = await post_turn(window_id, turn_index, question)
        records.append(record)
        if not record.get("ok"):
            stop_event.set()
            break
    return {
        "id": scenario["id"],
        "title": scenario["title"],
        "window_user_id": window_id,
        "ok": len(records) == len(scenario["turns"]) and all(item.get("ok") for item in records),
        "completed_turns": len(records),
        "records": records,
    }


def write_outputs(result: dict[str, Any]) -> tuple[Path, Path]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULT_DIR / f"{RUN_ID}.json"
    markdown_path = RESULT_DIR / f"{RUN_ID}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append(f"# {RUN_ID}")
    lines.append("")
    lines.append(f"- 结果：{'通过' if result['ok'] else '失败'}")
    lines.append(f"- 窗口：{result['completed_windows']}/{result['total_windows']}")
    lines.append(f"- 轮次：{result['completed_turns']}/{result['total_turns']}")
    lines.append(f"- shadow 出站副作用计数：{result.get('shadow_side_effect_count', 0)}")
    lines.append(f"- 服务：{BASE_URL}")
    lines.append(f"- 健康：{result.get('health', {})}")
    lines.append("")
    for window in result["windows"]:
        lines.append(f"## {window['title']}（{window['id']}）")
        lines.append("")
        for record in window["records"]:
            status = "通过" if record.get("ok") else "失败"
            lines.append(f"### 第 {record['turn']} 轮：{status}")
            lines.append("")
            lines.append(f"- 用户：{record['question']}")
            reply = str(record.get("reply") or "").replace("\n", " ")
            lines.append(f"- 回复：{reply}")
            lines.append(f"- 动作：{', '.join(record.get('actions') or []) or '无'}")
            lines.append(f"- 工具计数：{record.get('tool_counts') or {}}")
            lines.append(f"- shadow 出站副作用计数：{record.get('shadow_side_effect_count', 0)}")
            candidates = [item for item in record.get("tool_candidates") or [] if isinstance(item, dict)]
            if candidates:
                lines.append("- 工具候选明细：")
                for item in candidates[:12]:
                    stage = str(item.get("来源阶段") or item.get("source_stage") or "")
                    area_group = item.get("区域组") or item.get("area_group") or []
                    if isinstance(area_group, list):
                        area_group_text = "/".join(str(part) for part in area_group if str(part).strip())
                    else:
                        area_group_text = str(area_group or "")
                    community = str(item.get("小区") or item.get("community") or "")
                    room_no = str(item.get("房号") or item.get("room_no") or "")
                    rent_pay1 = str(item.get("押一付一") or item.get("rent_pay1") or item.get("rent_yayi") or "")
                    rent_pay2 = str(item.get("押二付一") or item.get("rent_pay2") or item.get("rent_yaer") or "")
                    lines.append(
                        f"  - 来源={stage or 'unknown'}｜区域组={area_group_text or 'unknown'}｜"
                        f"小区={community or '-'}｜房号={room_no or '-'}｜押一付一={rent_pay1 or '-'}｜押二付一={rent_pay2 or '-'}"
                    )
            if record.get("region_whitelist"):
                lines.append(f"- 区域白名单：{record.get('region_whitelist')}")
            if record.get("refine_within_candidates"):
                lines.append(f"- 候选内二筛：{record.get('refine_within_candidates')}")
            failures = record.get("failures") or []
            if failures:
                lines.append(f"- 失败点：{'; '.join(failures)}")
            lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


async def main() -> int:
    health = await asyncio.to_thread(request_json, HEALTH_URL, None, 20)
    print(json.dumps({"event": "health", "health": health}, ensure_ascii=False), flush=True)

    stop_event = asyncio.Event()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)
    started = time.time()
    windows = await asyncio.gather(*(run_window(item, stop_event, semaphore) for item in SCENARIOS))
    completed_turns = sum(window["completed_turns"] for window in windows)
    total_turns = sum(len(item["turns"]) for item in SCENARIOS)
    ok = completed_turns == total_turns and all(window["ok"] for window in windows)
    shadow_side_effect_count_total = sum(
        int(record.get("shadow_side_effect_count") or 0)
        for window in windows
        for record in window.get("records") or []
    )
    if shadow_side_effect_count_total:
        ok = False
    failures = [
        {
            "window": window["id"],
            "turn": record["turn"],
            "question": record["question"],
            "failures": record.get("failures") or [],
        }
        for window in windows
        for record in window["records"]
        if not record.get("ok")
    ]
    result = {
        "run_id": RUN_ID,
        "ok": ok,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 3),
        "base_url": BASE_URL,
        "health": health,
        "total_windows": len(SCENARIOS),
        "completed_windows": sum(1 for window in windows if window["completed_turns"] == 10),
        "total_turns": total_turns,
        "completed_turns": completed_turns,
        "shadow_side_effect_count": shadow_side_effect_count_total,
        "failures": failures,
        "windows": windows,
    }
    json_path, markdown_path = write_outputs(result)
    print(
        json.dumps(
            {
                "event": "summary",
                "ok": ok,
                "completed_turns": completed_turns,
                "total_turns": total_turns,
                "shadow_side_effect_count": shadow_side_effect_count_total,
                "failures": failures[:5],
                "json_path": str(json_path),
                "markdown_path": str(markdown_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
