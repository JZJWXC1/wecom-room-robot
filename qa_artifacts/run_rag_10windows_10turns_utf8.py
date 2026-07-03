from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

from tests.offline_guard import activate_offline_test_mode, offline_guard_status, repo_root


activate_offline_test_mode()

import app.main as main
from qa_artifacts import run_rag_test_text_window_utf8 as base


SCRIPT_PATH = Path(__file__)
ARTIFACT_DIR = Path("qa_artifacts")
CONVERSATION_PREFIX = "conv_10w_10t"
_ARTIFACT_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_ARTIFACT_SEQUENCE = count()
_CANONICAL_SKIP_KEYS = {
    "canonical_result_hash",
    "created_at",
    "elapsed_sec",
    "stage_timings",
    "timing_summary",
}


class ArtifactWriteError(RuntimeError):
    def __init__(self, artifact_path: Path, original: BaseException):
        super().__init__(f"failed to write QA artifact: {artifact_path}")
        self.artifact_path = artifact_path
        self.original = original


def _safe_artifact_stem(prefix: str) -> str:
    safe = "".join(char if char in _ARTIFACT_SAFE_CHARS else "_" for char in prefix)
    safe = "_".join(part for part in safe.split("_") if part)
    return safe.strip("._-") or "qa_artifact"


def artifact_path_for(prefix: str, *, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if now is None:
        suffix = f"_p{os.getpid()}_{time.time_ns()}_{next(_ARTIFACT_SEQUENCE):04d}"
    return ARTIFACT_DIR / f"{_safe_artifact_stem(prefix)}_{timestamp}{suffix}.json"


def _canonicalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_for_hash(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
            if key not in _CANONICAL_SKIP_KEYS
        }
    if isinstance(value, list):
        return [_canonicalize_for_hash(item) for item in value]
    if isinstance(value, Path):
        return _display_path(value)
    return value


def canonical_result_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        _canonicalize_for_hash(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _artifact_write_failure_payload(
    payload: dict[str, Any],
    error: BaseException,
    target_path: Path,
) -> dict[str, Any]:
    failure = copy.deepcopy(payload)
    quality = dict(failure.get("quality_status") or {})
    infrastructure_errors = list(quality.get("infrastructure_errors") or [])
    infrastructure_errors.append(
        {
            "stage": "artifact_write",
            "type": type(error).__name__,
            "errno": getattr(error, "errno", None),
            "winerror": getattr(error, "winerror", None),
            "target_path": target_path.name,
            "target_path_length": len(str(target_path)),
        }
    )
    quality.update(
        {
            "completed": False,
            "passed": False,
            "infrastructure_error": True,
            "business_failure": False,
            "exit_code": 2,
            "infrastructure_errors": infrastructure_errors,
        }
    )
    failure.update(
        {
            "completed": False,
            "full_suite_completed": False,
            "quality_status": quality,
        }
    )
    failure["summary"] = build_machine_summary(failure)
    failure["canonical_result_hash"] = canonical_result_hash(failure)
    return failure

WINDOWS: list[dict[str, Any]] = [
    {
        "id": "xintiandi_budget_replace",
        "turns": [
            "新天地这边有没有4000左右的两室一厅？",
            "4000-5000的呢，还是新天地附近。",
            "前两套视频先发我，客户想先看一下感觉。",
            "有原视频或者清楚一点的吗？客户嫌转发后有点糊。",
            "第一套水电怎么收？",
            "这套今天能看吗，密码多少？",
            "如果密码不对或者门打不开怎么办？",
            "客户看中了怎么定房，合同怎么弄？",
            "免押金要什么条件，服务费怎么算？",
            "房源表也发我一份，客户想自己再筛。",
        ],
    },
    {
        "id": "wanda_low_budget_candidates",
        "turns": [
            "万达附近1500左右还有哪些？客户想今天先看两套。",
            "拱墅万达附近就行，便宜的优先。",
            "前两套视频发我。",
            "这两套图片也有吗？有的话一起发。",
            "第一套还在吗，押一付一多少钱？",
            "第二套水电费怎么收？",
            "这个能今天自己看吗？",
            "如果客户说视频糊，有没有原视频链接？",
            "这两套客户更想要带厅的，有没有更合适的？",
            "都不合适的话，万达2000以下一室一厅再推荐几套。",
        ],
    },
    {
        "id": "yangjiafu_fuzzy_selection",
        "turns": [
            "杨家府还有房子吗？客户说名字可能没记准。",
            "兴业杨家府的呢，预算4500左右。",
            "如果有的话先发视频和图片给客户看看。",
            "第一套多少钱，押一付一和押二付一分别多少？",
            "这套水电怎么算？",
            "这套今天能不能看，密码多少？",
            "如果要定房联系谁？",
            "免押可以做吗，芝麻分要多少？",
            "客户又问杨家新雅苑有没有三室的。",
            "杨家新雅苑那套也发视频，最好清楚一点。",
        ],
    },
    {
        "id": "shiqiao_whole_rent",
        "turns": [
            "石桥附近5000左右有两室吗？最好整租。",
            "石桥区域就行，不是只问石桥铭苑。",
            "前两套视频发我。",
            "这两套哪套水电更划算？",
            "客户今天想看，这两套看房方式分别是什么？",
            "如果还没空出来，还能约看吗？",
            "1和2的图片也发我。",
            "这两套有没有原视频或者高清点的？",
            "客户看中了其中一套，怎么定房？",
            "房源表发我，客户还想看石桥华丰其他房。",
        ],
    },
    {
        "id": "typo_community_and_bound_room",
        "turns": [
            "棠闰府有没有1600左右的一室一厅？",
            "你说的是棠润府的话，15-2-801B还在吗？",
            "这套视频发我。",
            "视频有点糊，有原视频吗？",
            "这套图片也发我一下。",
            "这套水电怎么收？",
            "押一付一和押二付一分别多少？",
            "这套什么时候空出，能自己看吗？",
            "密码不对的话找谁？",
            "客户看中了怎么签合同？",
        ],
    },
    {
        "id": "dongzhan_gaotang_followups",
        "turns": [
            "皋塘还有房子吗？东站附近也可以。",
            "预算2600以内的一室优先。",
            "有带独厨卫的吗？",
            "第一套视频发我。",
            "这个图片也发一下。",
            "这套今天可以看吗，密码多少？",
            "4000左右的两室东站附近有没有？",
            "前两套都发视频给客户筛一下。",
            "房源表发我一份。",
            "如果客户想定其中一套，怎么操作？",
        ],
    },
    {
        "id": "multi_area_compare",
        "turns": [
            "万达、东新园两边都可以，3000以内有什么能住的？",
            "那东新园这边两室有没有便宜点的？",
            "4000-5000的呢？",
            "第1和第3套视频发我。",
            "这两套水电和价格帮我对比一下。",
            "第一套看房密码多少？",
            "如果客户今天到门口了打不开门怎么办？",
            "有没有原视频或者飞书素材源链接能直接转发？",
            "客户问免押服务费怎么算。",
            "最后把房源表也发给我。",
        ],
    },
    {
        "id": "inventory_sheet_then_detail",
        "turns": [
            "先把最新房源表发我，客户要自己看。",
            "表里面新天地4000左右两室是哪几套？",
            "4000-5000的呢？",
            "前两套视频发我。",
            "这个呢，第一套图片有没有？",
            "这套水电费怎么收？",
            "这套能今天看吗？",
            "客户看完视频想定，怎么定？",
            "免押金能不能做？",
            "如果换成万达2000以下一室，还有哪些？",
        ],
    },
    {
        "id": "batch_video_pending",
        "turns": [
            "石桥和华丰附近5000左右整租视频都发我几套。",
            "能发的都发，先不要超过5套。",
            "剩下的继续发。",
            "第1和第5套水电怎么收？",
            "第5套如果没有视频，那就发图片。",
            "这几套有没有原视频？",
            "客户今天想看其中两套，密码怎么给？",
            "如果没有密码或者还没空出来怎么处理？",
            "客户看中了怎么定房？",
            "换成东新园4000左右两室再推荐几套。",
        ],
    },
    {
        "id": "room_number_and_new_topic",
        "turns": [
            "荣润府1600到1800有没有押一付一的？",
            "如果你说的是棠润府，就查15-2-801B。",
            "这套视频和图片都发我。",
            "这套价格和水电说一下。",
            "这套今天能不能自己看？",
            "客户又问新天地附近4000左右两室一厅。",
            "4000-5000的呢？",
            "前两套视频发我。",
            "第一个原视频有没有？",
            "最后说下免押和定房流程。",
        ],
    },
]


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root()).as_posix()
    except ValueError:
        return resolved.name

REQUIRED_TOKENS = (
    "万达",
    "新天地",
    "石桥",
    "杨家府",
    "棠润府",
    "视频",
    "图片",
    "房源表",
    "免押",
    "原视频",
    "水电",
    "密码",
    "定房",
)
BAD_TOKENS = ("???", "�", "锟", "涓", "鑽", "鐭", "鎴")
CONFUSABLE_COMMUNITY_GROUPS = (
    ("兴业杨家府", "杨乐府", "杨家新雅苑"),
    ("棠润府", "荣润府"),
)
NEGATION_PREFIXES = (
    "不是只问",
    "不只是问",
    "不只问",
    "不是问",
    "不是非要",
    "不一定是",
    "不限定",
    "不限于",
    "别只看",
    "不要只看",
    "不是",
)
PASSWORD_VALUE_RE = re.compile(r"(?:看房)?密码(?:是|为|[:：])?\s*[A-Za-z0-9]{4,}#|[A-Za-z0-9]{4,}#")
SENT_MARKERS = ("已发送", "已经发送", "已经发", "已发给", "已发你", "发给你了", "都发你了")
PREPARED_MARKERS = ("已准备好", "准备发送", "准备发")
FAILED_MARKERS = ("发送失败", "上传失败", "发不出去", "没发出去")


def chinese_integrity_report(
    windows: list[dict[str, Any]] | None = None,
    *,
    required_tokens: tuple[str, ...] | list[str] | None = REQUIRED_TOKENS,
    expected_window_count: int | None = 10,
    min_window_count: int = 1,
    min_turn_count: int = 100,
) -> dict[str, Any]:
    source_windows = windows if windows is not None else WINDOWS
    turns = [turn for window in source_windows for turn in window["turns"]]
    joined = "\n".join(turns)
    chinese_count = sum(1 for char in joined if "\u4e00" <= char <= "\u9fff")
    total_count = max(len(joined), 1)
    required = required_tokens or ()
    missing = [token for token in required if token not in joined]
    bad = [token for token in BAD_TOKENS if token in joined]
    window_count_ok = (
        len(source_windows) >= min_window_count
        and (expected_window_count is None or len(source_windows) == expected_window_count)
    )
    turn_count_ok = len(turns) >= min_turn_count
    return {
        "script_path": _display_path(SCRIPT_PATH),
        "encoding": "utf-8",
        "window_count": len(source_windows),
        "turn_count": len(turns),
        "expected_window_count": expected_window_count,
        "min_window_count": min_window_count,
        "min_turn_count": min_turn_count,
        "chinese_ratio": round(chinese_count / total_count, 4),
        "missing_required_tokens": missing,
        "bad_tokens": bad,
        "passed": window_count_ok and turn_count_ok and not missing and not bad and chinese_count / total_count > 0.35,
    }


def _serialize_context_store(store: base.MemoryContextStore) -> dict[str, Any]:
    data = getattr(store, "data", {})
    summary: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        candidate_set = value.get("last_candidate_set") or {}
        confirmed = value.get("confirmed_room") or {}
        summary[key] = {
            "raw_dialog_context_count": len((value.get("structured_memory") or {}).get("raw_dialog_context") or []),
            "turn_record_count": len((value.get("structured_memory") or {}).get("turn_records") or []),
            "last_candidate_count": len(candidate_set.get("candidates") or []),
            "last_candidate_query": candidate_set.get("query"),
            "confirmed_room": confirmed.get("label"),
            "pending_video_sends": value.get("pending_video_sends"),
        }
    return summary


def _turn_problem(turn: dict[str, Any]) -> dict[str, Any]:
    chain = turn.get("chain_judgment") or {}
    bot = turn.get("bot") or {}
    rewrite = turn.get("rewrite") or {}
    tool = turn.get("tool") or {}
    problem = {
        "status": chain.get("status"),
        "likely_link": chain.get("likely_link"),
        "severity": "info",
        "reason": chain.get("reason") or "",
    }
    if turn.get("error"):
        problem["severity"] = "high"
        return problem
    if chain.get("status") in {"error", "no_output", "retry_needed"}:
        problem["severity"] = "high"
        return problem
    if chain.get("status") == "clarification" and (
        _has_real_entity_clarification_options(turn)
        or _is_safe_missing_detail_clarification(turn)
    ):
        problem["severity"] = "info"
        problem["reason"] = "基于安全证据边界的澄清追问，不计入人工复核失败。"
        return problem
    if chain.get("status") in {"clarification", "planner_feedback"}:
        problem["severity"] = "medium"
        return problem
    texts = "\n".join(bot.get("texts") or [])
    proof = rewrite.get("constraint_proof") or {}
    blackbox = turn.get("blackbox") or {}
    proof_communities = [
        str(item).strip()
        for item in proof.get("communities") or []
        if str(item).strip()
    ]
    target_rows = [
        str(item).strip()
        for item in tool.get("target_rows") or []
        if str(item).strip()
    ]
    user_text = str(turn.get("user") or "")
    normalized_user = main.normalize_search_text(user_text)
    area_alias_hit = any(
        main.normalize_search_text(alias) in normalized_user
        for alias in main.AREA_ALIASES
    )
    user_mentions_exact_community = any(
        main.normalize_search_text(community) in normalized_user
        for community in proof_communities
    )
    negated_communities = _negated_communities(user_text, proof_communities)
    if negated_communities:
        problem["severity"] = "high"
        problem["likely_link"] = "问题重写/实体归一"
        problem["reason"] = (
            "用户明确否定的小区被重写成正向小区约束："
            f"negated={negated_communities} communities={proof_communities}"
        )
        return problem
    if proof.get("area") and proof_communities and area_alias_hit and not user_mentions_exact_community:
        problem["severity"] = "high"
        problem["likely_link"] = "问题重写/实体归一"
        problem["reason"] = (
            "区域查询被误收窄成具体小区："
            f"user={user_text!r} area={proof.get('area')!r} communities={proof_communities}"
        )
        return problem
    if proof_communities and target_rows:
        wrong_targets = [
            label
            for label in target_rows
            if not any(community in label for community in proof_communities)
        ]
        if wrong_targets:
            problem["severity"] = "high"
            problem["likely_link"] = "Planner/工具目标绑定"
            problem["reason"] = (
                "目标房源违背问题重写的标准小区约束："
                f"communities={proof_communities} target_rows={wrong_targets[:3]}"
            )
            return problem
    confusable_pollution = _confusable_community_pollution(
        user_text,
        proof_communities=proof_communities,
        target_rows=target_rows,
    )
    if confusable_pollution:
        problem["severity"] = "high"
        problem["likely_link"] = "问题重写/实体归一"
        problem["reason"] = (
            "相似小区或别名互相污染："
            f"allowed={confusable_pollution['allowed']} polluted={confusable_pollution['polluted']}"
        )
        return problem
    selected_indices = proof.get("selected_indices") or []
    if (
        selected_indices
        and target_rows
        and not _selected_binding_has_prior_context(selected_indices, target_rows, blackbox)
    ):
        problem["severity"] = "high"
        problem["likely_link"] = "Planner/工具目标绑定"
        problem["reason"] = (
            "序号请求没有当前候选或上一轮待发素材上下文，却绑定到了房源："
            f"selected_indices={selected_indices} target_rows={target_rows[:3]}"
        )
        return problem
    media_listing_problem = _media_request_without_stable_listing_id(user_text, proof, bot, tool)
    if media_listing_problem:
        problem["severity"] = "high"
        problem["likely_link"] = "素材目标绑定"
        problem["reason"] = media_listing_problem
        return problem
    password_problem = _password_boundary_problem(user_text, texts, proof)
    if password_problem:
        problem["severity"] = "high"
        problem["likely_link"] = "看房密码边界"
        problem["reason"] = password_problem
        return problem
    action_tense_problem = _action_tense_problem(texts, bot, tool, turn.get("send") or {})
    if action_tense_problem:
        problem["severity"] = "high"
        problem["likely_link"] = "发送阶段"
        problem["reason"] = action_tense_problem
        return problem
    asks_original_followup = any(word in user_text for word in ("原视频", "高清", "视频糊", "有点糊", "太糊", "清楚一点", "保存转发", "源文件"))
    explicit_batch = any(word in user_text for word in ("这几套", "这些", "都发", "全部", "前两套", "前三套", "两套", "三套", "1和", "1 和"))
    if asks_original_followup and not explicit_batch:
        media_target_count = len(target_rows)
        video_count = int(bot.get("video_count") or tool.get("video_count") or 0)
        if not target_rows and any(word in texts for word in ("没绑定到具体房源", "暂时没法发视频", "回我序号")):
            problem["severity"] = "high"
            problem["likely_link"] = "素材目标绑定"
            problem["reason"] = "原视频/清楚一点追问没有绑定上一轮视频或缺视频房源，错误要求用户重新指定。"
            return problem
        if media_target_count > 2 or video_count > 2:
            problem["severity"] = "high"
            problem["likely_link"] = "素材目标绑定"
            problem["reason"] = (
                "原视频/视频糊这类后续追问没有明确批量要求，不能扩成多套素材任务："
                f"target_count={media_target_count} video_count={video_count}"
            )
            return problem
    if any(word in texts for word in ("哪个城市", "确认一下区域", "重新发小区+房号")):
        problem["severity"] = "medium"
        problem["reason"] = "回复可能要求重复已给信息，需要人工复核上下文是否已足够。"
    return problem


def _selected_binding_has_prior_context(
    selected_indices: Any,
    target_rows: list[str],
    blackbox: dict[str, Any],
) -> bool:
    try:
        if isinstance(selected_indices, (str, int)):
            selected_values = [selected_indices]
        else:
            selected_values = list(selected_indices or [])
        selected_numbers = [int(index) for index in selected_values]
        max_selected = max(selected_numbers)
    except (TypeError, ValueError):
        return False
    if max_selected <= 0:
        return False
    required_target_count = len({index for index in selected_numbers if index > 0})
    if len(target_rows) < required_target_count:
        return False
    if int(blackbox.get("last_candidate_count_before_turn") or 0) >= max_selected:
        return True
    return _pending_labels_cover_targets(
        blackbox.get("pending_video_sends_before_turn"),
        target_rows,
    )


def _pending_labels_cover_targets(pending: Any, target_rows: list[str]) -> bool:
    if not isinstance(pending, dict):
        return False
    labels = [
        str(label).strip()
        for label in pending.get("labels") or []
        if str(label).strip()
    ]
    if not labels:
        return False
    return all(any(target == label for label in labels) for target in target_rows)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _negated_communities(user_text: str, communities: list[str]) -> list[str]:
    compact = _compact_text(user_text)
    negated: list[str] = []
    for community in communities:
        community_text = _compact_text(community)
        if not community_text:
            continue
        if any(f"{prefix}{community_text}" in compact for prefix in NEGATION_PREFIXES):
            negated.append(community)
    return negated


def _mentioned_confusable_communities(user_text: str) -> dict[tuple[str, ...], set[str]]:
    normalized_user = main.normalize_search_text(user_text)
    mentioned: dict[tuple[str, ...], set[str]] = {}
    for group in CONFUSABLE_COMMUNITY_GROUPS:
        hits = {
            community
            for community in group
            if main.normalize_search_text(community) in normalized_user
        }
        if hits:
            mentioned[group] = hits
    return mentioned


def _confusable_community_pollution(
    user_text: str,
    *,
    proof_communities: list[str],
    target_rows: list[str],
) -> dict[str, Any] | None:
    mentioned = _mentioned_confusable_communities(user_text)
    if not mentioned:
        return None
    polluted: list[str] = []
    for group, allowed in mentioned.items():
        negated = set(_negated_communities(user_text, list(group)))
        allowed = {item for item in allowed if item not in negated}
        if not allowed:
            continue
        group_set = set(group)
        for community in proof_communities:
            if community in group_set and community not in allowed:
                polluted.append(f"rewrite:{community}")
        for label in target_rows:
            row_hits = [community for community in group if community in label]
            polluted.extend(f"target:{community}" for community in row_hits if community not in allowed)
        if polluted:
            return {"allowed": sorted(allowed), "polluted": polluted}
    return None


def _int_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _media_request_without_stable_listing_id(
    user_text: str,
    proof: dict[str, Any],
    bot: dict[str, Any],
    tool: dict[str, Any],
) -> str:
    wants_video = bool(proof.get("wants_video") or proof.get("wants_original_video")) or any(
        word in user_text for word in ("视频", "原视频", "高清", "清楚一点", "源文件")
    )
    wants_inventory_sheet = bool(proof.get("wants_inventory_sheet")) or "房源表" in user_text
    wants_image = bool(proof.get("wants_image")) or any(word in user_text for word in ("图片", "图也发", "照片"))
    video_count = _int_count(tool.get("video_count") or bot.get("video_count"))
    image_count = _int_count(tool.get("image_count"))
    if wants_image and not image_count:
        bot_image_count = _int_count(bot.get("image_count"))
        inventory_sheet_count = _int_count(tool.get("inventory_image_count"))
        actions = {str(item).strip() for item in tool.get("actions") or []}
        if not (wants_inventory_sheet and inventory_sheet_count > 0 and "send_inventory_sheet" in actions):
            image_count = bot_image_count
    video_listing_ids = [str(item).strip() for item in tool.get("video_listing_ids") or [] if str(item).strip()]
    image_listing_ids = [str(item).strip() for item in tool.get("image_listing_ids") or [] if str(item).strip()]
    if wants_video and video_count > 0 and len(video_listing_ids) < video_count:
        return f"视频请求发送了素材但缺少稳定 listing_id：video_count={video_count} listing_ids={len(video_listing_ids)}"
    if wants_image and image_count > 0 and len(image_listing_ids) < image_count:
        return f"图片请求发送了素材但缺少稳定 listing_id：image_count={image_count} listing_ids={len(image_listing_ids)}"
    return ""


def _password_boundary_problem(user_text: str, reply_text: str, proof: dict[str, Any]) -> str:
    if "密码" in user_text or bool(proof.get("wants_password")):
        return ""
    if "密码" in reply_text or PASSWORD_VALUE_RE.search(reply_text):
        return "用户没有询问密码，但回复出现密码字样或密码格式。"
    return ""


def _sent_action_count(send: dict[str, Any], *types: str) -> int:
    total = 0
    for action in send.get("sent_actions") or []:
        if not isinstance(action, dict) or action.get("type") not in types:
            continue
        total += _int_count(action.get("count") or 1)
    return total


def _action_tense_problem(texts: str, bot: dict[str, Any], tool: dict[str, Any], send: dict[str, Any]) -> str:
    sent_text = any(marker in texts for marker in SENT_MARKERS)
    prepared_text = any(marker in texts for marker in PREPARED_MARKERS)
    failed_text = any(marker in texts for marker in FAILED_MARKERS)
    if sum(bool(item) for item in (sent_text, prepared_text, failed_text)) > 1:
        return "回复同时混用准备发送、已发送或发送失败状态。"
    sent_media_count = (
        _sent_action_count(send, "image", "video")
        or _int_count(bot.get("image_count"))
        + _int_count(bot.get("video_count"))
        or _int_count(tool.get("image_count"))
        + _int_count(tool.get("video_count"))
    )
    failed_media_count = _sent_action_count(send, "image_failed", "video_failed")
    if sent_text and sent_media_count == 0:
        return "回复宣称素材已发送，但发送阶段没有成功素材动作。"
    if prepared_text and sent_media_count > 0:
        return "回复宣称素材只是准备发送，但发送阶段已经成功发送素材。"
    if failed_text and sent_media_count > 0:
        return "回复宣称素材发送失败，但发送阶段已有成功素材动作。"
    if sent_text and failed_media_count > 0 and sent_media_count == 0:
        return "回复宣称素材已发送，但发送阶段只有失败动作。"
    return ""


def _has_real_entity_clarification_options(turn: dict[str, Any]) -> bool:
    rewrite = turn.get("rewrite") or {}
    clarification = "\n".join(
        [
            str(rewrite.get("clarification_text") or ""),
            *[str(text) for text in (turn.get("bot") or {}).get("texts") or []],
        ]
    )
    if not clarification:
        return False
    if "暂时没查到" in clarification and "你说的是" not in clarification and "相近小区" not in clarification:
        return False
    entity_resolution = _last_stage_summary(turn, "rewrite_intent").get("entity_resolution") or {}
    options: list[str] = []
    for item in entity_resolution.get("community_options") or []:
        if not isinstance(item, dict):
            continue
        for option in item.get("options") or []:
            text = str(option).strip()
            if text and text not in options:
                options.append(text)
    if not options:
        return _looks_like_confirmation_clarification(clarification)
    if not any(option in clarification for option in options):
        return False
    return any(word in clarification for word in ("确认", "哪一个", "哪个", "你说的是", "相近小区"))


def _looks_like_confirmation_clarification(clarification: str) -> bool:
    if "暂时没查到" in clarification:
        return False
    if not any(word in clarification for word in ("确认", "你说的是", "是不是", "是指", "指的是")):
        return False
    return any(
        community in clarification
        for group in CONFUSABLE_COMMUNITY_GROUPS
        for community in group
    )


def _is_safe_missing_detail_clarification(turn: dict[str, Any]) -> bool:
    rewrite = turn.get("rewrite") or {}
    bot = turn.get("bot") or {}
    tool = turn.get("tool") or {}
    send = turn.get("send") or {}
    proof = rewrite.get("constraint_proof") or {}
    query_state = rewrite.get("query_state") if isinstance(rewrite.get("query_state"), dict) else {}
    structured_task = rewrite.get("structured_task")
    if not isinstance(structured_task, dict):
        structured_task = turn.get("structured_task") if isinstance(turn.get("structured_task"), dict) else {}
    if isinstance(rewrite.get("tool_requirements"), dict):
        tool_requirements = rewrite.get("tool_requirements") or {}
    else:
        tool_requirements = (
            structured_task.get("tool_requirements")
            if isinstance(structured_task.get("tool_requirements"), dict)
            else {}
        )
    user_text = str(turn.get("user") or "")
    clarification = "\n".join(
        [
            str(rewrite.get("clarification_text") or ""),
            *[str(text) for text in bot.get("texts") or []],
        ]
    )
    if not clarification:
        return False
    risky_terms = (
        "暂时没查到",
        "没查到",
        "没有房源",
        "没有合适",
        "已发送",
        "发给你",
        "密码",
        "门锁",
        "合同",
        "定金",
        "押金",
        "免押",
        "价格",
        "租金",
        "水电",
        "空出",
        "在租",
        "可看",
        "预约",
    )
    if any(term in clarification for term in risky_terms):
        return False
    user_risky_terms = (
        "多少钱",
        "一个月多少",
        "每月多少",
        "多少一个月",
        "一月多少钱",
        "价格",
        "房源表",
        "空房表",
        "库存表",
        "在租表",
        "房态表",
        "表发",
        "发表",
        "租金",
        "房租",
        "押一付一",
        "押二付一",
        "押金",
        "免押",
        "服务费",
        "水电",
        "电费",
        "水费",
        "合同",
        "签约",
        "签合同",
        "定金",
        "订房",
        "预定",
        "密码",
        "门锁",
        "看房",
        "能看",
        "可看",
        "可以看",
        "去看",
        "约看",
        "看下",
        "看一下",
        "看一看",
        "方便看",
        "上门看",
        "带我看",
        "带看",
        "现场看",
        "实地看",
        "预约",
        "自己看",
        "自助",
        "自助看",
        "钥匙",
        "门禁",
        "门禁码",
        "开门",
        "怎么进",
        "进去",
        "进得去",
        "进门",
        "进屋",
        "进房",
        "空吗",
        "空房",
        "空不空",
        "空着",
        "空了",
        "空出",
        "空出来",
        "什么时候空",
        "哪天空",
        "还在吗",
        "还在",
        "在不在",
        "在租吗",
        "有吗",
        "有没有",
        "有哪些",
        "有房",
        "还有吗",
        "还有房",
        "什么时候入住",
        "什么时候可以入住",
        "什么时候能入住",
        "能入住",
        "入住",
        "入住时间",
        "能住",
        "搬",
        "现房",
        "空置",
        "能租",
        "可租",
        "能不能租",
        "可以租",
        "租掉",
        "租出去",
        "租出",
        "租了没",
        "租没租",
        "已租",
        "出租",
        "被租",
        "租了吗",
        "定掉",
        "定出去",
        "定了",
        "定了吗",
        "定了没",
        "被定",
        "订掉",
        "订出去",
        "订了",
        "订了吗",
        "订了没",
        "被订",
        "预定",
        "预订",
        "被预定",
        "被预订",
        "退租",
        "可以住",
        "住进去",
        "空的",
        "退房",
    )
    if any(term in user_text for term in user_risky_terms):
        return False
    content_wants_viewing = getattr(main, "_content_wants_viewing", None)
    if callable(content_wants_viewing):
        try:
            if content_wants_viewing(user_text):
                return False
        except Exception:
            pass
    for signal_func_name in ("_content_wants_price", "_content_wants_deposit"):
        signal_func = getattr(main, signal_func_name, None)
        if callable(signal_func):
            try:
                if signal_func(user_text):
                    return False
            except Exception:
                pass
    deterministic_signals = getattr(main, "_deterministic_signals", None)
    if callable(deterministic_signals):
        try:
            signals = deterministic_signals(user_text)
        except Exception:
            signals = {}
        if isinstance(signals, dict) and any(
            signals.get(key)
            for key in (
                "wants_access",
                "wants_contract_contact",
                "wants_deposit",
                "wants_inventory_sheet",
                "wants_password",
                "wants_price_contact",
                "wants_viewing",
            )
        ):
            return False
    access_markers = ("门", "门禁", "门锁", "钥匙", "密码", "进门", "进屋", "进房", "进去")
    access_actions = ("开", "怎么", "进", "发", "拿", "给", "能", "可以")
    if any(marker in user_text for marker in access_markers) and any(action in user_text for action in access_actions):
        return False
    onsite_viewing_markers = (
        "过去",
        "上门",
        "现场",
        "有人带",
        "带我",
        "带看",
        "安排",
        "方便",
        "提前联系",
        "自助",
    )
    if any(term in user_text for term in onsite_viewing_markers):
        return False
    risky_intents = {
        "access",
        "contract",
        "contract_contact",
        "deposit",
        "inventory_sheet",
        "password",
        "price",
        "rent",
        "utilities",
        "viewing",
    }
    if str(rewrite.get("intent") or query_state.get("intent") or "").strip().lower() in risky_intents:
        return False
    risky_proof_keys = (
        "asks_access",
        "asks_password",
        "wants_access",
        "wants_contract",
        "wants_contract_contact",
        "wants_deposit",
        "wants_inventory_sheet",
        "wants_password",
        "wants_price",
        "wants_rent",
        "wants_utilities",
        "wants_viewing",
    )
    if any(
        proof.get(key)
        for key in risky_proof_keys
    ):
        return False
    risky_state_keys = (
        "access",
        "asks_password",
        "budget_range",
        "contract",
        "deposit",
        "password",
        "price",
        "price_range",
        "rent",
        "room_status",
        "status",
        "utilities",
        "viewing",
        "wants_contract_contact",
        "wants_inventory_sheet",
        "wants_password",
        "wants_price",
        "wants_utilities",
        "wants_viewing",
    )
    if any(query_state.get(key) for key in risky_state_keys):
        return False
    risky_requirement_keys = (
        "needs_access",
        "needs_contract",
        "needs_contract_contact",
        "needs_deposit",
        "needs_deposit_policy",
        "needs_inventory_sheet",
        "needs_password",
        "needs_price",
        "needs_price_contact",
        "needs_rent",
        "needs_room_status",
        "needs_utilities",
        "needs_viewing",
        "needs_viewing_policy",
    )
    if any(tool_requirements.get(key) for key in risky_requirement_keys):
        return False
    if any(tool.get(key) for key in ("inventory_rows", "target_rows")):
        return False
    if any(_int_count(tool.get(key)) > 0 for key in ("image_count", "video_count")):
        return False
    if any(_int_count(bot.get(key)) > 0 for key in ("image_count", "video_count")):
        return False
    if send.get("actions"):
        return False
    if any(_int_count(send.get(key)) > 0 for key in ("image_count", "video_count")):
        return False
    return True


def _stage_entries(turn: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    return [
        item
        for item in turn.get("stage_timings") or []
        if isinstance(item, dict) and item.get("stage") == stage
    ]


def build_completion_status(
    *,
    selected_completed: bool,
    selected_window_count: int,
    expected_full_window_count: int,
    expected_selected_turn_count: int,
    actual_window_count: int,
    actual_turn_count: int,
    expected_full_turn_count: int,
    full_suite_requested: bool,
) -> dict[str, Any]:
    full_suite_completed = bool(
        selected_completed
        and full_suite_requested
        and selected_window_count == expected_full_window_count
        and actual_window_count == expected_full_window_count
        and actual_turn_count == expected_full_turn_count
    )
    return {
        "completed": bool(selected_completed),
        "full_suite_completed": full_suite_completed,
        "full_suite_requested": bool(full_suite_requested),
        "selected_window_count": selected_window_count,
        "expected_full_window_count": expected_full_window_count,
        "expected_selected_turn_count": expected_selected_turn_count,
        "expected_case_count": expected_selected_turn_count,
        "actual_window_count": actual_window_count,
        "actual_turn_count": actual_turn_count,
        "actual_case_count": actual_turn_count,
    }


def build_machine_summary(payload: dict[str, Any]) -> dict[str, Any]:
    quality = payload.get("quality_status") or {}
    passed = bool(quality.get("passed"))
    artifact_role = "pass_transcript" if passed else "failure_log"
    actual_case_count = int(payload.get("actual_case_count") or payload.get("actual_turn_count") or 0)
    expected_case_count = int(payload.get("expected_case_count") or payload.get("expected_selected_turn_count") or 0)
    full_suite_completed = bool(payload.get("full_suite_completed"))
    usable_for_release = (
        passed
        and full_suite_completed
        and expected_case_count > 0
        and actual_case_count == expected_case_count
    )
    return {
        "schema": "rag_qa_artifact_summary.v1",
        "artifact_role": artifact_role,
        "contains_pass_transcript": artifact_role == "pass_transcript",
        "contains_failure_log": artifact_role == "failure_log",
        "usable_for_release": usable_for_release,
        "passed": passed,
        "exit_code": int(quality.get("exit_code") or (0 if passed else 2)),
        "actual_case_count": actual_case_count,
        "expected_case_count": expected_case_count,
        "actual_window_count": int(payload.get("actual_window_count") or 0),
        "selected_window_count": int(payload.get("selected_window_count") or payload.get("window_count") or 0),
        "full_suite_completed": full_suite_completed,
        "business_failure": bool(quality.get("business_failure")),
        "infrastructure_error": bool(quality.get("infrastructure_error")),
    }


def build_quality_status(
    windows: list[dict[str, Any]],
    *,
    completed: bool,
    medium_threshold: int = 0,
) -> dict[str, Any]:
    infrastructure_errors: list[dict[str, Any]] = []
    business_failures: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    high_count = 0
    medium_count = 0
    fallback_count = 0
    llm_call_count = 0
    for window in windows:
        for turn in window.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            problem = turn.get("problem") or {}
            severity = str(problem.get("severity") or "info")
            if severity == "high":
                high_count += 1
            if severity == "medium":
                medium_count += 1
            if turn.get("error"):
                infrastructure_errors.append(
                    {
                        "window_id": window.get("window_id"),
                        "turn": turn.get("turn"),
                        "reason": turn.get("error"),
                    }
                )
            if severity in {"high", "medium"}:
                item = {
                    "window_id": window.get("window_id"),
                    "turn": turn.get("turn"),
                    "user": turn.get("user"),
                    "actual_reply": (turn.get("bot") or {}).get("texts", []),
                    "expected_constraints": (turn.get("rewrite") or {}).get("constraint_proof", {}),
                    "severity": severity,
                    "rule": problem.get("likely_link") or "",
                    "reason": problem.get("reason") or "",
                    "needs_human_review": severity == "medium",
                    "evidence_refs": {
                        "rewrite": bool(turn.get("rewrite")),
                        "tool": bool(turn.get("tool")),
                        "selfcheck": bool(turn.get("selfcheck")),
                    },
                    "fallback": "我先帮您确认一下最新房态" in "\n".join((turn.get("bot") or {}).get("texts", [])),
                    "llm_called": any(
                        entry.get("stage") in {"rewrite_intent", "final_selfcheck"}
                        and (entry.get("summary") or {}).get("llm_source")
                        for entry in turn.get("stage_timings") or []
                        if isinstance(entry, dict)
                    ),
                    "network_calls": 0,
                }
                business_failures.append(item)
            if severity == "medium":
                needs_review.append(
                    {
                        "window_id": window.get("window_id"),
                        "turn": turn.get("turn"),
                        "reason": problem.get("reason") or "",
                    }
                )
            texts = "\n".join((turn.get("bot") or {}).get("texts", []))
            if "我先帮您确认一下最新房态" in texts:
                fallback_count += 1
            for entry in turn.get("stage_timings") or []:
                if not isinstance(entry, dict):
                    continue
                summary = entry.get("summary") or {}
                if summary.get("llm_source") or summary.get("planner_reply_result"):
                    llm_call_count += 1
    infrastructure_error = bool(infrastructure_errors)
    business_failure = bool(high_count or medium_count > medium_threshold)
    passed = bool(completed and not infrastructure_error and not business_failure)
    if infrastructure_error:
        exit_code = 2
    elif high_count:
        exit_code = 3
    elif medium_count > medium_threshold:
        exit_code = 4
    else:
        exit_code = 0 if completed else 2
    return {
        "completed": bool(completed),
        "passed": passed,
        "infrastructure_error": infrastructure_error,
        "business_failure": business_failure,
        "exit_code": exit_code,
        "high_count": high_count,
        "medium_count": medium_count,
        "medium_threshold": medium_threshold,
        "fallback_count": fallback_count,
        "llm_call_count": llm_call_count,
        "network_call_count": offline_guard_status().get("blocked_network_call_count", 0),
        "infrastructure_errors": infrastructure_errors,
        "business_failures": business_failures,
        "needs_review": needs_review,
    }


def _last_stage_summary(turn: dict[str, Any], stage: str) -> dict[str, Any]:
    entries = _stage_entries(turn, stage)
    if not entries:
        return {}
    summary = entries[-1].get("summary")
    return summary if isinstance(summary, dict) else {}


def _first_context_summary(store: base.MemoryContextStore) -> dict[str, Any]:
    summary = _serialize_context_store(store)
    for value in summary.values():
        if isinstance(value, dict):
            return value
    return {}


def _enrich_turn_report(
    turn: dict[str, Any],
    store: base.MemoryContextStore,
    *,
    pre_context_snapshot: dict[str, Any] | None = None,
) -> None:
    rewrite = _last_stage_summary(turn, "rewrite_intent")
    planner_entries = _stage_entries(turn, "planner")
    planner_summaries = [
        item.get("summary")
        for item in planner_entries
        if isinstance(item.get("summary"), dict)
    ]
    tool_summary = _last_stage_summary(turn, "tools")
    selfcheck_summary = _last_stage_summary(turn, "final_selfcheck")
    send_summary = _last_stage_summary(turn, "send")
    context_snapshot = _first_context_summary(store)
    pre_context_snapshot = pre_context_snapshot or {}
    selected_indices = rewrite.get("selected_indices") or []
    target_rows = tool_summary.get("target_rows") or []

    turn["rewrite"] = {
        "rewritten_query": rewrite.get("effective_query") or "",
        "intent": rewrite.get("intent") or "",
        "query_state": rewrite.get("query_state") or {},
        "constraint_proof": rewrite.get("constraint_proof") or {},
        "tool_requirements": rewrite.get("tool_requirements") or {},
        "needs_clarification": bool(rewrite.get("needs_clarification")),
        "clarification_text": rewrite.get("clarification_text") or "",
        "read_blackbox": bool(rewrite),
    }
    turn["planner"] = {
        "attempt_count": len(planner_summaries),
        "attempts": planner_summaries,
        "actions": planner_summaries[-1].get("actions", []) if planner_summaries else [],
        "need_rewrite_clarification": bool(
            planner_summaries[-1].get("need_rewrite_clarification")
        )
        if planner_summaries
        else False,
    }
    turn["tool"] = tool_summary
    turn["selfcheck"] = {
        "entered": bool(selfcheck_summary),
        "status": selfcheck_summary.get("selfcheck_status") or "",
        "rule_status": selfcheck_summary.get("rule_status") or "",
        "rule_reason": selfcheck_summary.get("rule_reason") or "",
        "llm_status": selfcheck_summary.get("llm_status") or "",
        "needs_planner_retry": bool(selfcheck_summary.get("needs_planner_retry")),
        "planner_retry_reason": selfcheck_summary.get("planner_retry_reason") or "",
    }
    turn["send"] = send_summary
    turn["blackbox"] = {
        "read_by_rewrite": bool(rewrite),
        "raw_dialog_context_count_before_turn": pre_context_snapshot.get("raw_dialog_context_count", 0),
        "turn_record_count_before_turn": pre_context_snapshot.get("turn_record_count", 0),
        "last_candidate_count_before_turn": pre_context_snapshot.get("last_candidate_count", 0),
        "confirmed_room_before_turn": pre_context_snapshot.get("confirmed_room"),
        "pending_video_sends_before_turn": pre_context_snapshot.get("pending_video_sends"),
        "raw_dialog_context_count_after_turn": context_snapshot.get("raw_dialog_context_count", 0),
        "turn_record_count_after_turn": context_snapshot.get("turn_record_count", 0),
        "last_candidate_count_after_turn": context_snapshot.get("last_candidate_count", 0),
        "confirmed_room_after_turn": context_snapshot.get("confirmed_room"),
        "pending_video_sends_after_turn": context_snapshot.get("pending_video_sends"),
    }
    turn["candidate_binding"] = {
        "selected_indices": selected_indices,
        "target_rows": target_rows,
        "target_listing_ids": tool_summary.get("target_listing_ids") or [],
        "video_listing_ids": tool_summary.get("video_listing_ids") or [],
        "image_listing_ids": tool_summary.get("image_listing_ids") or [],
        "bound_last_candidate": bool(selected_indices and target_rows),
    }


def _timing_summary(windows: list[dict[str, Any]]) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    stages: dict[str, list[float]] = {}
    for window in windows:
        for turn in window.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            turns.append(turn)
            for item in turn.get("stage_timings") or []:
                if not isinstance(item, dict):
                    continue
                stage = str(item.get("stage") or "unknown")
                try:
                    elapsed = float(item.get("elapsed_sec") or 0.0)
                except (TypeError, ValueError):
                    elapsed = 0.0
                stages.setdefault(stage, []).append(elapsed)
    turn_elapsed = []
    for turn in turns:
        try:
            turn_elapsed.append(float(turn.get("elapsed_sec") or 0.0))
        except (TypeError, ValueError):
            turn_elapsed.append(0.0)
    stage_summary = {}
    for stage, values in sorted(stages.items()):
        total = sum(values)
        count = len(values)
        stage_summary[stage] = {
            "count": count,
            "total_sec": round(total, 3),
            "avg_sec": round(total / count, 3) if count else 0.0,
            "max_sec": round(max(values), 3) if values else 0.0,
        }
    slowest_turns = sorted(
        (
            {
                "window_id": window.get("window_id"),
                "turn": turn.get("turn"),
                "elapsed_sec": turn.get("elapsed_sec"),
                "user": turn.get("user"),
                "stages": {
                    str(item.get("stage") or "unknown"): item.get("elapsed_sec")
                    for item in turn.get("stage_timings") or []
                    if isinstance(item, dict)
                },
            }
            for window in windows
            for turn in window.get("turns") or []
            if isinstance(turn, dict)
        ),
        key=lambda item: float(item.get("elapsed_sec") or 0.0),
        reverse=True,
    )[:10]
    total_turn_time = sum(turn_elapsed)
    return {
        "turn_count": len(turns),
        "total_turn_elapsed_sec": round(total_turn_time, 3),
        "avg_turn_elapsed_sec": round(total_turn_time / len(turns), 3) if turns else 0.0,
        "max_turn_elapsed_sec": round(max(turn_elapsed), 3) if turn_elapsed else 0.0,
        "stages": stage_summary,
        "slowest_turns": slowest_turns,
    }


def load_fixture_windows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(raw_windows, list) or not raw_windows:
        raise RuntimeError(f"fixture has no replay windows: {path}")
    windows: list[dict[str, Any]] = []
    for index, window in enumerate(raw_windows, start=1):
        if not isinstance(window, dict):
            raise RuntimeError(f"fixture window #{index} must be an object: {path}")
        turns = [str(turn).strip() for turn in window.get("turns") or [] if str(turn).strip()]
        if not turns:
            raise RuntimeError(f"fixture window #{index} has no turns: {path}")
        windows.append(
            {
                "id": str(window.get("id") or f"fixture_window_{index:03d}"),
                "source": str(window.get("source") or payload.get("schema") or "fixture"),
                "turns": turns,
            }
        )
    return windows


async def run_all(
    *,
    turn_timeout: float = 90,
    window_limit: int | None = None,
    window_id: str = "",
    windows: list[dict[str, Any]] | None = None,
    artifact_prefix: str = "rag_10windows_10turns_utf8",
    conversation_prefix: str = CONVERSATION_PREFIX,
    required_tokens: tuple[str, ...] | list[str] | None = None,
    expected_window_count: int | None = 10,
    min_window_count: int = 1,
    min_turn_count: int = 100,
    fail_fast_on_problem: bool = False,
) -> Path:
    source_windows = windows if windows is not None else WINDOWS
    integrity = chinese_integrity_report(
        source_windows,
        required_tokens=required_tokens,
        expected_window_count=expected_window_count,
        min_window_count=min_window_count,
        min_turn_count=min_turn_count,
    )
    if not integrity["passed"]:
        raise RuntimeError("10窗口QA输入编码或覆盖异常：" + json.dumps(integrity, ensure_ascii=False))
    ARTIFACT_DIR.mkdir(exist_ok=True)
    artifact = artifact_path_for(artifact_prefix)
    if window_id:
        selected_windows = [window for window in source_windows if window.get("id") == window_id]
        if not selected_windows:
            raise RuntimeError(f"unknown window_id: {window_id}")
    else:
        selected_windows = source_windows[:window_limit] if window_limit else source_windows
    all_results: list[dict[str, Any]] = []

    def current_completion_status(*, selected_completed: bool) -> dict[str, Any]:
        selected_window_count = len(selected_windows)
        expected_full_window_count = len(source_windows)
        expected_selected_turn_count = sum(len(window["turns"]) for window in selected_windows)
        actual_turn_count = sum(len(window.get("turns") or []) for window in all_results)
        full_suite_requested = not window_id and not window_limit
        return build_completion_status(
            selected_completed=selected_completed,
            selected_window_count=selected_window_count,
            expected_full_window_count=expected_full_window_count,
            expected_selected_turn_count=expected_selected_turn_count,
            actual_window_count=len(all_results),
            actual_turn_count=actual_turn_count,
            expected_full_turn_count=sum(len(window["turns"]) for window in source_windows),
            full_suite_requested=full_suite_requested,
        )

    def write_artifact(completed: bool) -> None:
        completion = current_completion_status(selected_completed=completed)
        timing = _timing_summary(all_results)
        quality = build_quality_status(all_results, completed=completed)
        payload = {
            "created_at": datetime.now().isoformat(),
            "script_path": _display_path(SCRIPT_PATH),
            "input_integrity": integrity,
            **completion,
            "window_count": len(selected_windows),
            "turn_timeout": turn_timeout,
            "fail_fast_on_problem": fail_fast_on_problem,
            "timing_summary": timing,
            "quality_status": quality,
            "offline_guard": offline_guard_status(),
            "windows": all_results,
        }
        payload["summary"] = build_machine_summary(payload)
        payload["canonical_result_hash"] = canonical_result_hash(payload)
        try:
            _write_json_atomic(artifact, payload)
        except OSError as error:
            failure_path = artifact.with_name(f"{artifact.stem}_write_failed.json")
            failure_payload = _artifact_write_failure_payload(payload, error, artifact)
            _write_json_atomic(failure_path, failure_payload)
            raise ArtifactWriteError(failure_path, error) from error

    write_artifact(False)
    originals = {
        "wecom_kf": main.wecom_kf,
        "wecom_kf_context_store": main.wecom_kf_context_store,
        "kf_turn_tasks": dict(main.kf_turn_tasks),
        "kf_turn_generations": dict(main.kf_turn_generations),
        "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
        "offline_service_stubs": base.install_offline_service_stubs(),
    }
    try:
        for window_index, window in enumerate(selected_windows, start=1):
            fake = base.CaptureWeComKf()
            store = base.MemoryContextStore()
            main.wecom_kf = fake
            main.wecom_kf_context_store = store
            main.kf_turn_tasks.clear()
            main.kf_turn_generations.clear()
            main.kf_turn_pending_messages.clear()
            conversation_id = f"{conversation_prefix}_{window_index}_{window['id']}"
            turns: list[dict[str, Any]] = []
            window_result = {
                "window_index": window_index,
                "window_id": window["id"],
                "conversation_id": conversation_id,
                "turns": turns,
                "context_summary": {},
            }
            all_results.append(window_result)
            write_artifact(False)
            for turn_index, user_text in enumerate(window["turns"], start=1):
                pre_context_snapshot = _first_context_summary(store)
                turn = await base.send_turn(
                    fake,
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    user_text=user_text,
                    turn_timeout=turn_timeout,
                )
                _enrich_turn_report(turn, store, pre_context_snapshot=pre_context_snapshot)
                turn["problem"] = _turn_problem(turn)
                turn["problems"] = [turn["problem"]]
                turns.append(turn)
                window_result["context_summary"] = _serialize_context_store(store)
                write_artifact(False)
                if fail_fast_on_problem and _is_blocking_turn_problem(turn):
                    return artifact
    finally:
        main.wecom_kf = originals["wecom_kf"]
        main.wecom_kf_context_store = originals["wecom_kf_context_store"]
        main.kf_turn_tasks.clear()
        main.kf_turn_tasks.update(originals["kf_turn_tasks"])
        main.kf_turn_generations.clear()
        main.kf_turn_generations.update(originals["kf_turn_generations"])
        main.kf_turn_pending_messages.clear()
        main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])
        base.restore_offline_service_stubs(originals["offline_service_stubs"])

    completed = len(all_results) == len(selected_windows)
    if completed:
        for result, source in zip(all_results, selected_windows):
            result_turns = result.get("turns") or []
            source_turns = source.get("turns") or []
            if len(result_turns) != len(source_turns) or any(turn.get("error") for turn in result_turns):
                completed = False
                break
    write_artifact(completed)
    return artifact


def _is_blocking_turn_problem(turn: dict[str, Any]) -> bool:
    if turn.get("error"):
        return True
    problem = turn.get("problem") or {}
    severity = str(problem.get("severity") or "")
    return severity in {"high", "medium"}


def print_summary(artifact: Path) -> None:
    data = json.loads(artifact.read_text(encoding="utf-8"))
    print("SUMMARY_JSON " + json.dumps(data.get("summary") or {}, ensure_ascii=False, sort_keys=True))
    print(f"ARTIFACT {artifact}")
    print(
        "INPUT_INTEGRITY "
        f"passed={data['input_integrity']['passed']} "
        f"windows={data['input_integrity']['window_count']} turns={data['input_integrity']['turn_count']}"
    )
    print(
        "QA_SCOPE "
        f"selected_windows={data.get('selected_window_count')} "
        f"actual_windows={data.get('actual_window_count')} "
        f"completed={data.get('completed')} "
        f"full_suite_completed={data.get('full_suite_completed')} "
        f"passed={(data.get('quality_status') or {}).get('passed')} "
        f"exit_code={(data.get('quality_status') or {}).get('exit_code')}"
    )
    timing = data.get("timing_summary") or {}
    print(
        "TIMING "
        f"turns={timing.get('turn_count')} "
        f"avg={timing.get('avg_turn_elapsed_sec')}s "
        f"max={timing.get('max_turn_elapsed_sec')}s"
    )
    for stage, item in (timing.get("stages") or {}).items():
        print(
            "TIMING_STAGE "
            f"{stage} count={item.get('count')} "
            f"avg={item.get('avg_sec')}s max={item.get('max_sec')}s total={item.get('total_sec')}s"
        )
    for window in data["windows"]:
        high = sum(1 for turn in window["turns"] if (turn.get("problem") or {}).get("severity") == "high")
        medium = sum(1 for turn in window["turns"] if (turn.get("problem") or {}).get("severity") == "medium")
        print(f"\nWINDOW {window['window_index']} {window['window_id']} high={high} medium={medium}")
        for turn in window["turns"]:
            bot = turn.get("bot") or {}
            texts = " | ".join(text.replace("\n", " / ") for text in bot.get("texts", []))
            problem = turn.get("problem") or {}
            print(f"R{turn['turn']} 用户: {turn['user']}")
            print(f"R{turn['turn']} 机器人: {texts[:500]}")
            print(
                f"R{turn['turn']} 动作: image={bot.get('image_count')} video={bot.get('video_count')} "
                f"severity={problem.get('severity')} link={problem.get('likely_link')}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn-timeout", type=float, default=90)
    parser.add_argument("--window-limit", type=int, default=0)
    parser.add_argument("--window-id", default="")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--artifact-prefix", default="")
    parser.add_argument("--min-window-count", type=int, default=0)
    parser.add_argument("--min-turn-count", type=int, default=0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    fixture_windows = load_fixture_windows(args.fixture) if args.fixture else None
    artifact_prefix = args.artifact_prefix or (
        f"rag_fixture_{args.fixture.stem}" if args.fixture else "rag_10windows_10turns_utf8"
    )
    min_window_count = args.min_window_count or 1
    min_turn_count = args.min_turn_count or (1 if args.fixture else 100)
    try:
        artifact_path = asyncio.run(
            run_all(
                turn_timeout=args.turn_timeout,
                window_limit=args.window_limit or None,
                window_id=args.window_id,
                windows=fixture_windows,
                artifact_prefix=artifact_prefix,
                conversation_prefix="conv_fixture_replay" if args.fixture else CONVERSATION_PREFIX,
                required_tokens=() if args.fixture else None,
                expected_window_count=None if args.fixture else 10,
                min_window_count=min_window_count,
                min_turn_count=min_turn_count,
            )
        )
    except ArtifactWriteError as error:
        print(f"ARTIFACT_WRITE_ERROR {error.artifact_path}")
        raise SystemExit(2) from error
    print_summary(artifact_path)
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    raise SystemExit(int((data.get("quality_status") or {}).get("exit_code") or 0))
