from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from tests.offline_guard import activate_offline_test_mode


activate_offline_test_mode()

from app.services.rewrite_inventory_index import load_rewrite_inventory_index
from qa_artifacts.run_rag_10windows_10turns_utf8 import (
    ArtifactWriteError,
    build_machine_summary,
    canonical_result_hash,
    chinese_integrity_report,
    print_summary,
    run_all,
    _write_json_atomic,
)


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "area_budget_media",
        "areas": ["万达", "拱墅万达", "新天地", "东站", "石桥", "华丰"],
        "budgets": ["1500左右", "2000以下", "3000以内", "4000到5000", "5000左右"],
        "layouts": ["一室", "一室一厅", "两室", "两室一厅", "整租"],
        "turns": [
            "{area}附近{budget}有{layout}吗？客户想今天先筛两套。",
            "如果有，先按价格低一点的列出来。",
            "前两套视频发我，客户想先看感觉。",
            "视频太糊的话，有没有原视频或者高清链接？",
            "第一套水电怎么收？",
            "这套今天能看吗，密码多少？",
            "如果密码不对或者门打不开怎么办？",
            "客户看中了怎么定房，合同联系谁？",
            "这套能不能免押，服务费怎么算？",
            "房源表也发我一份，客户还想自己看。",
        ],
    },
    {
        "id": "fuzzy_community_confirm",
        "areas": ["杨家府", "棠闰府", "荣润府", "石桥名苑", "皋塘"],
        "budgets": ["1600到1800", "3000上下", "4000左右", "4500以内"],
        "layouts": ["一室", "一室一厅", "两室", "三室"],
        "turns": [
            "{area}还有房子吗？客户名字可能没说准。",
            "预算{budget}，最好是{layout}。",
            "如果能确认到具体小区，先发几套候选。",
            "1和2视频发我。",
            "第一套图片也发一下。",
            "这套押一付一和押二付一分别多少？",
            "水电怎么收？",
            "怎么看房，今天能不能自己看？",
            "如果还没空出来能约看吗？",
            "换成新天地附近4000-5000的两室再查一下。",
        ],
    },
    {
        "id": "candidate_switching",
        "areas": ["东新园", "杭氧", "新天地", "闸弄口", "东站"],
        "budgets": ["3500-4500", "4000-5000", "2600以内", "3800左右"],
        "layouts": ["一室一厅", "两室", "两室一厅"],
        "turns": [
            "{area}这边{budget}的{layout}还有吗？",
            "4000-5000的呢，还是这个区域。",
            "前两套视频先发我。",
            "第一个有没有原视频，客户要保存转发。",
            "第二套图片发一下。",
            "这两套水电和价格帮我对比一下。",
            "上一个看房密码多少？",
            "这套如果密码错了找谁？",
            "客户想定其中一套怎么操作？",
            "万达2000以下一室一厅再推荐几套。",
        ],
    },
    {
        "id": "inventory_sheet_detail",
        "areas": ["万达", "新天地", "石桥", "东站", "皋塘"],
        "budgets": ["1500左右", "3000以内", "4000左右", "5000左右"],
        "layouts": ["一室", "两室", "整租"],
        "turns": [
            "房源表先发我一下。",
            "表里{area}附近{budget}的{layout}是哪几套？",
            "前两套视频发我。",
            "第一套图也发一下。",
            "这套视频能不能给高清原视频？",
            "这套水电怎么收？",
            "今天能不能看，密码多少？",
            "还没空出来的话怎么安排客户看？",
            "免押需要什么条件？",
            "客户如果看中了怎么定？",
        ],
    },
    {
        "id": "price_and_viewing",
        "areas": ["华丰", "永佳", "半山", "新塘", "元宝塘"],
        "budgets": ["2000以下", "2500到3000", "3500左右", "4500左右"],
        "layouts": ["一室", "一室一厅", "两室"],
        "turns": [
            "{area}附近{budget}有什么{layout}？",
            "押一付一的优先，押二付一也一起报。",
            "先发前三套视频。",
            "继续发剩下能发的视频。",
            "第1和第3套图片发我。",
            "第三套水电怎么算？",
            "第三套怎么看房？",
            "密码不对怎么处理？",
            "客户问免押是不是免费，怎么解释？",
            "最后房源表发我，客户自己再筛。",
        ],
    },
]

REQUIRED_CATEGORIES = {
    "区域预算": ("附近", "预算", "左右", "以内"),
    "候选编号": ("前两套", "1和2", "第1", "第一套"),
    "批量视频": ("视频", "继续发"),
    "图片": ("图片", "图也发"),
    "房源表": ("房源表",),
    "看房密码": ("密码", "怎么看房"),
    "未空出": ("还没空",),
    "水电": ("水电",),
    "免押": ("免押",),
    "定房": ("定", "合同"),
    "原视频": ("原视频", "高清", "保存转发"),
    "上下文接话": ("这套", "上一个", "还是这个区域"),
}

RANDOM_GUARD_WINDOW_COUNT = 20
RANDOM_GUARD_TURNS_PER_WINDOW = 10

TOOL_INVOCATION_CATEGORIES = {
    "房源查询": "房源推荐、在租、候选绑定必须经过房源表/工具证据。",
    "房源表图片": "客户要房源表时必须触发房源表图片或房源表证据。",
    "房间视频": "视频咨询必须触发视频素材检索或发送。",
    "房间图片": "图片咨询必须触发图片素材检索或发送。",
    "原视频或缺素材": "原视频/高清视频/缺素材咨询必须留下素材状态证据。",
    "价格水电": "价格、水电、押一付一/押二付一必须由房源工具证据承接。",
    "看房密码": "看房、密码、门锁必须由看房/房源工具证据承接。",
    "未空出约看": "未空出、约看必须由房态/看房规则证据承接。",
    "合同定房": "合同、定房、订房必须由业务规则或知识库证据承接。",
    "免押政策": "免押、芝麻信用、服务费必须由规则或知识库证据承接。",
}


def _int_price(value: Any) -> int:
    numbers = re.findall(r"\d{2,5}", str(value or ""))
    return int(numbers[0]) if numbers else 0


def _room_prices(room: dict[str, Any]) -> list[int]:
    prices = [_int_price(room.get("price_yayi")), _int_price(room.get("price_yaer"))]
    return [price for price in prices if price > 0]


def _budget_label_for_room(room: dict[str, Any]) -> str:
    prices = _room_prices(room)
    if not prices:
        return "4000左右"
    price = min(prices)
    if price <= 1800:
        return f"{max(price - 300, 0)}到{price + 300}"
    low = max((price // 500) * 500 - 500, 0)
    high = low + 1000
    return f"{low}-{high}"


def _replacement_budget_label(room: dict[str, Any]) -> str:
    prices = _room_prices(room)
    if not prices:
        return "4000-5000"
    price = min(prices)
    low = max(((price + 500) // 500) * 500, 0)
    return f"{low}-{low + 1000}"


def _layout_query(room: dict[str, Any]) -> str:
    layout = str(room.get("layout") or "").strip()
    if "三室" in layout:
        return "三室"
    if "两室" in layout or "二室" in layout:
        return "两室"
    if "一室" in layout or "单间" in layout:
        return "一室"
    return layout or "一室"


def _area_aliases_by_canonical(index: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in index.get("area_aliases") or []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip()
        alias = str(item.get("alias") or "").strip()
        if canonical and alias:
            result.setdefault(canonical, []).append(alias)
    return result


def _area_alias_for_room(index: dict[str, Any], room: dict[str, Any], rng: random.Random) -> str:
    area = str(room.get("area") or "").strip()
    aliases = _area_aliases_by_canonical(index).get(area) or []
    if aliases:
        return rng.choice(aliases)
    parts = [part for part in re.split(r"[\s/、]+", area) if part.strip()]
    return rng.choice(parts) if parts else area or "新天地"


def _usable_index_rooms(index: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = []
    for item in index.get("room_index") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("community") or "").strip() and str(item.get("room_no") or "").strip():
            rooms.append(item)
    return rooms


def _similar_community_hint(index: dict[str, Any], fallback: str) -> str:
    for item in index.get("similar_communities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        options = item.get("options") or []
        if name and options:
            if "杨家" in name:
                return "杨家府"
            if "棠润府" in name:
                return "棠闰府"
            return name[:-1] if len(name) >= 3 else name
    return fallback[:-1] if len(fallback) >= 3 else fallback


def _fact_window_turns(
    *,
    index: dict[str, Any],
    rng: random.Random,
    room: dict[str, Any],
    variant: int,
) -> list[str]:
    community = str(room.get("community") or "").strip()
    room_no = str(room.get("room_no") or "").strip()
    room_key = f"{community}{room_no}"
    area_alias = _area_alias_for_room(index, room, rng)
    budget = _budget_label_for_room(room)
    replacement_budget = _replacement_budget_label(room)
    layout = _layout_query(room)
    fuzzy_name = _similar_community_hint(index, community)

    variants = [
        [
            f"{area_alias}附近{budget}有{layout}吗？客户想今天先筛两套。",
            "如果有，先按价格低一点的列出来。",
            "前两套视频发我，客户想先看感觉。",
            "视频太糊的话，有没有原视频或者高清链接？",
            "第一套水电怎么收？",
            "这套今天能看吗，密码多少？",
            "如果密码不对或者门打不开怎么办？",
            "客户看中了怎么定房，合同联系谁？",
            "这套能不能免押，服务费怎么算？",
            "房源表也发我一份，客户还想自己看。",
        ],
        [
            f"{fuzzy_name}还有房子吗？客户名字可能没说准。",
            f"如果是{community}，预算{budget}，最好是{layout}。",
            "如果能确认到具体小区，先发几套候选。",
            "1和2视频发我。",
            "第一套图片也发一下。",
            "这套押一付一和押二付一分别多少？",
            "水电怎么收？",
            "怎么看房，今天能不能自己看？",
            "如果还没空出来能约看吗？",
            f"换成{area_alias}附近{replacement_budget}的{layout}再查一下。",
        ],
        [
            f"{area_alias}这边{budget}的{layout}还有吗？",
            f"{replacement_budget}的呢，还是这个区域。",
            "前两套视频先发我。",
            "第一个有没有原视频，客户要保存转发。",
            "第二套图片发一下。",
            "这两套水电和价格帮我对比一下。",
            "上一个看房密码多少？",
            "这套如果密码错了找谁？",
            "客户想定其中一套怎么操作？",
            "万达2000以下一室一厅再推荐几套。",
        ],
        [
            "房源表先发我一下。",
            f"表里{area_alias}附近{budget}的{layout}是哪几套？",
            "前两套视频发我。",
            "第一套图也发一下。",
            "这套视频能不能给高清原视频？",
            "这套水电怎么收？",
            "今天能不能看，密码多少？",
            "还没空出来的话怎么安排客户看？",
            "免押需要什么条件？",
            "客户如果看中了怎么定？",
        ],
        [
            f"{community}有视频吗？",
            f"那就查{room_key}这套。",
            "这套视频和图片都发我。",
            "这套价格和水电说一下。",
            "这套今天能不能自己看？",
            f"客户又问{area_alias}附近{replacement_budget}{layout}。",
            f"{replacement_budget}的呢？",
            "前两套视频发我。",
            "第一个原视频有没有？",
            "最后说下免押和定房流程。",
        ],
    ]
    return variants[variant % len(variants)]


def generate_fact_based_guard_windows(
    index: dict[str, Any],
    *,
    seed: int | None = None,
    count: int = RANDOM_GUARD_WINDOW_COUNT,
) -> list[dict[str, Any]]:
    rooms = _usable_index_rooms(index)
    if len(rooms) < 3:
        return []
    rng = random.Random(seed if seed is not None else int(time.time()))
    rng.shuffle(rooms)
    signature = str(index.get("signature") or "")
    generated_at = str(index.get("generated_at") or "")
    windows: list[dict[str, Any]] = []
    for index_no in range(count):
        room = rooms[index_no % len(rooms)]
        windows.append(
            {
                "id": f"random_{index_no + 1}_fact_index",
                "scenario": "fact_index",
                "generation_source": "rewrite_inventory_index",
                "inventory_signature": signature,
                "inventory_generated_at": generated_at,
                "seed": seed,
                "area": room.get("area"),
                "community": room.get("community"),
                "room_no": room.get("room_no"),
                "budget": _budget_label_for_room(room),
                "layout": _layout_query(room),
                "turns": _fact_window_turns(
                    index=index,
                    rng=rng,
                    room=room,
                    variant=index_no,
                ),
            }
        )
    return windows


def generate_random_guard_windows(
    *,
    seed: int | None = None,
    count: int = RANDOM_GUARD_WINDOW_COUNT,
) -> list[dict[str, Any]]:
    fact_windows = generate_fact_based_guard_windows(
        load_rewrite_inventory_index(),
        seed=seed,
        count=count,
    )
    if fact_windows:
        return fact_windows

    rng = random.Random(seed if seed is not None else int(time.time()))
    windows: list[dict[str, Any]] = []
    scenario_order = list(SCENARIOS)
    rng.shuffle(scenario_order)
    for index in range(count):
        scenario = scenario_order[index % len(scenario_order)]
        area = rng.choice(scenario["areas"])
        budget = rng.choice(scenario["budgets"])
        layout = rng.choice(scenario["layouts"])
        turns = [
            template.format(area=area, budget=budget, layout=layout)
            for template in scenario["turns"]
        ]
        windows.append(
            {
                "id": f"random_{index + 1}_{scenario['id']}",
                "scenario": scenario["id"],
                "seed": seed,
                "area": area,
                "budget": budget,
                "layout": layout,
                "turns": turns,
            }
        )
    return windows


def coverage_report(
    windows: list[dict[str, Any]],
    *,
    expected_window_count: int = RANDOM_GUARD_WINDOW_COUNT,
    expected_turns_per_window: int = RANDOM_GUARD_TURNS_PER_WINDOW,
) -> dict[str, Any]:
    text = "\n".join(turn for window in windows for turn in window["turns"])
    turn_counts = [len(window.get("turns") or []) for window in windows]
    unexpected_turn_windows = [
        window.get("id")
        for window, turn_count in zip(windows, turn_counts)
        if turn_count != expected_turns_per_window
    ]
    missing = [
        name
        for name, tokens in REQUIRED_CATEGORIES.items()
        if not any(token in text for token in tokens)
    ]
    return {
        "window_count": len(windows),
        "expected_window_count": expected_window_count,
        "expected_turns_per_window": expected_turns_per_window,
        "turn_count": sum(turn_counts),
        "unexpected_turn_windows": unexpected_turn_windows,
        "missing_categories": missing,
        "passed": (
            len(windows) == expected_window_count
            and not unexpected_turn_windows
            and not missing
        ),
    }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _as_int(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _turn_user_text(turn: dict[str, Any]) -> str:
    return str(turn.get("user") or "")


def _turn_all_text(turn: dict[str, Any]) -> str:
    bot_text = "\n".join(str(item) for item in (turn.get("bot") or {}).get("texts") or [])
    return "\n".join([_turn_user_text(turn), bot_text, _as_text(turn.get("tool")), _as_text(turn.get("send"))])


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _tool_count(tool: dict[str, Any], *keys: str) -> int:
    return sum(_as_int(tool.get(key)) for key in keys)


def _rule_or_knowledge_evidence(turn: dict[str, Any]) -> bool:
    tool = turn.get("tool") or {}
    if tool.get("rule_evidence") or tool.get("business_knowledge"):
        return True
    source = str(tool.get("deterministic_reply_source") or "")
    return "business_knowledge" in source or "rule" in source


def _missing_media_has(tool: dict[str, Any], kind: str) -> bool:
    suffix = f":{kind}"
    return any(str(item).endswith(suffix) for item in tool.get("missing_media") or [])


def _media_status_has(tool: dict[str, Any], kind: str) -> bool:
    status = tool.get("media_status")
    return isinstance(status, dict) and isinstance(status.get(kind), dict)


def _tool_category_hits(turn: dict[str, Any]) -> set[str]:
    user_text = _turn_user_text(turn)
    all_text = _turn_all_text(turn)
    tool = turn.get("tool") if isinstance(turn.get("tool"), dict) else {}
    has_inventory_rows = _tool_count(tool, "inventory_row_count", "target_row_count", "inventory_rows", "target_rows") > 0
    has_rule_or_knowledge = _rule_or_knowledge_evidence(turn)
    hits: set[str] = set()

    if has_inventory_rows:
        hits.add("房源查询")
    if _tool_count(tool, "inventory_image_count", "inventory_sheet_artifact_evidence_count", "inventory_images") > 0:
        hits.add("房源表图片")
    if (
        _tool_count(tool, "video_count", "video_media_manifest_evidence_count", "video_paths", "video_rows") > 0
        or _missing_media_has(tool, "视频")
        or _media_status_has(tool, "video")
    ):
        hits.add("房间视频")
    if (
        _tool_count(tool, "image_count", "image_media_manifest_evidence_count", "image_paths", "image_rows") > 0
        or _missing_media_has(tool, "图片")
        or _media_status_has(tool, "image")
    ):
        hits.add("房间图片")
    if (
        tool.get("original_video_request")
        or _tool_count(tool, "original_video_url_count", "material_page_url_count", "original_video_media_manifest_evidence_count") > 0
        or tool.get("missing_media")
        or tool.get("media_status")
    ):
        hits.add("原视频或缺素材")
    if _has_any(user_text, ("价格", "多少钱", "水电", "押一付一", "押二付一", "租金")) and (
        has_inventory_rows
    ):
        hits.add("价格水电")
    if _has_any(user_text, ("看房", "密码", "门锁", "门禁", "自己看", "怎么看房")) and (
        has_inventory_rows
        or _tool_count(tool, "viewing_instruction_evidence_count") > 0
    ):
        hits.add("看房密码")
    if _has_any(user_text, ("还没空", "空出来", "约看", "安排客户看", "什么时候空")) and (
        has_inventory_rows
        or _tool_count(tool, "viewing_instruction_evidence_count") > 0
    ):
        hits.add("未空出约看")
    if _has_any(user_text, ("合同", "定房", "订房", "看中了", "怎么定")) and (
        has_rule_or_knowledge
        or _has_any(all_text, ("18758141785", "13282125992", "19941091943"))
    ):
        hits.add("合同定房")
    if _has_any(user_text, ("免押", "芝麻", "服务费", "无忧住")) and (
        has_rule_or_knowledge
        or _has_any(all_text, ("无忧住", "5.5%", "8%", "芝麻信用"))
    ):
        hits.add("免押政策")
    return hits


def tool_invocation_coverage_report(windows: list[dict[str, Any]]) -> dict[str, Any]:
    hits = {name: [] for name in TOOL_INVOCATION_CATEGORIES}
    for window in windows:
        window_id = str(window.get("window_id") or window.get("id") or "")
        for turn in window.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            for category in _tool_category_hits(turn):
                if category in hits:
                    hits[category].append(
                        {
                            "window_id": window_id,
                            "turn": turn.get("turn"),
                            "user": turn.get("user"),
                        }
                    )
    missing = [name for name, items in hits.items() if not items]
    return {
        "schema": "rag_random_guard_tool_coverage.v1",
        "required_categories": TOOL_INVOCATION_CATEGORIES,
        "covered_categories": [name for name, items in hits.items() if items],
        "missing_categories": missing,
        "hits": hits,
        "passed": not missing,
    }


def _apply_tool_coverage_gate(artifact_path: Path) -> Path:
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    coverage = tool_invocation_coverage_report(payload.get("windows") or [])
    payload["random_guard_tool_coverage"] = coverage
    quality = dict(payload.get("quality_status") or {})
    if bool(quality.get("passed")) and not coverage["passed"]:
        failures = list(quality.get("business_failures") or [])
        failures.append(
            {
                "stage": "random_guard_tool_coverage",
                "severity": "high",
                "reason": "随机保底 QA 未覆盖全部中介工具调用类目。",
                "missing_categories": coverage["missing_categories"],
            }
        )
        quality.update(
            {
                "passed": False,
                "business_failure": True,
                "exit_code": 4,
                "business_failures": failures,
            }
        )
        payload["quality_status"] = quality
    payload["summary"] = build_machine_summary(payload)
    payload["canonical_result_hash"] = canonical_result_hash(payload)
    _write_json_atomic(artifact_path, payload)
    return artifact_path


async def run_random_guard(
    *,
    seed: int | None = None,
    turn_timeout: float = 90,
    fail_fast_on_problem: bool = True,
) -> Path:
    windows = generate_random_guard_windows(seed=seed)
    coverage = coverage_report(windows)
    integrity = chinese_integrity_report(
        windows,
        required_tokens=(),
        expected_window_count=RANDOM_GUARD_WINDOW_COUNT,
        min_turn_count=RANDOM_GUARD_WINDOW_COUNT * RANDOM_GUARD_TURNS_PER_WINDOW,
    )
    if not coverage["passed"]:
        raise RuntimeError(f"随机保底QA覆盖不完整：{coverage}")
    if not integrity["passed"]:
        raise RuntimeError(f"随机保底QA输入UTF-8异常：{integrity}")
    artifact_path = await run_all(
        turn_timeout=turn_timeout,
        windows=windows,
        artifact_prefix="rag_random_guard_utf8",
        conversation_prefix="conv_random_guard",
        required_tokens=(),
        expected_window_count=RANDOM_GUARD_WINDOW_COUNT,
        min_turn_count=RANDOM_GUARD_WINDOW_COUNT * RANDOM_GUARD_TURNS_PER_WINDOW,
        fail_fast_on_problem=fail_fast_on_problem,
    )
    return _apply_tool_coverage_gate(artifact_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--turn-timeout", type=float, default=90)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        artifact_path = asyncio.run(
            run_random_guard(
                seed=args.seed or None,
                turn_timeout=args.turn_timeout,
                fail_fast_on_problem=True,
            )
        )
    except ArtifactWriteError as error:
        print(f"ARTIFACT_WRITE_ERROR {error.artifact_path}")
        raise SystemExit(2) from error
    print_summary(artifact_path)
    import json

    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    raise SystemExit(int((data.get("quality_status") or {}).get("exit_code") or 0))
