from __future__ import annotations

import argparse
import asyncio
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
    chinese_integrity_report,
    print_summary,
    run_all,
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
    count: int = 10,
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


def generate_random_guard_windows(*, seed: int | None = None, count: int = 10) -> list[dict[str, Any]]:
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


def coverage_report(windows: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(turn for window in windows for turn in window["turns"])
    missing = [
        name
        for name, tokens in REQUIRED_CATEGORIES.items()
        if not any(token in text for token in tokens)
    ]
    return {
        "window_count": len(windows),
        "turn_count": sum(len(window["turns"]) for window in windows),
        "missing_categories": missing,
        "passed": len(windows) == 10 and not missing,
    }


async def run_random_guard(*, seed: int | None = None, turn_timeout: float = 90) -> Path:
    windows = generate_random_guard_windows(seed=seed)
    coverage = coverage_report(windows)
    integrity = chinese_integrity_report(windows, required_tokens=())
    if not coverage["passed"]:
        raise RuntimeError(f"随机保底QA覆盖不完整：{coverage}")
    if not integrity["passed"]:
        raise RuntimeError(f"随机保底QA输入UTF-8异常：{integrity}")
    return await run_all(
        turn_timeout=turn_timeout,
        windows=windows,
        artifact_prefix="rag_random_guard_utf8",
        conversation_prefix="conv_random_guard",
        required_tokens=(),
    )


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
            )
        )
    except ArtifactWriteError as error:
        print(f"ARTIFACT_WRITE_ERROR {error.artifact_path}")
        raise SystemExit(2) from error
    print_summary(artifact_path)
    import json

    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    raise SystemExit(int((data.get("quality_status") or {}).get("exit_code") or 0))
