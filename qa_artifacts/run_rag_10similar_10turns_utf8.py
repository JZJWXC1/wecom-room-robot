from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from qa_artifacts import run_rag_10windows_10turns_utf8 as runner


SCRIPT_PATH = Path(__file__)

WINDOWS = [
    {
        "id": "xintiandi_two_room_budget_shift",
        "turns": [
            "新天地附近有没有3800到4200的两室？客户想要能尽快入住的。",
            "如果放到4300到5000呢，还是新天地和杭氧这片。",
            "前两套视频先发我，客户想先看感觉。",
            "第一套图片有没有？有的话一起发。",
            "第一套水电怎么收？",
            "这套今天能不能看，密码多少？",
            "如果密码不对或者门打不开，让客户找谁？",
            "客户如果看中了，怎么定房签合同？",
            "免押金要什么条件，服务费怎么算？",
            "最后房源表也发我一份，客户想自己再筛一下。",
        ],
    },
    {
        "id": "wanda_one_room_low_budget",
        "turns": [
            "万达附近有没有1800以内的一室，最好今天能看。",
            "一室一厅也算，便宜的优先。",
            "列出来的前两套视频发我。",
            "第二套水电怎么收？",
            "第一套押一付一和押二付一分别多少钱？",
            "这个能自己去看吗？",
            "视频如果转发太糊，有没有原视频或清楚点的方式？",
            "这两套有没有图片，也发我看看。",
            "客户想定便宜那套，流程怎么走？",
            "万达2000以内的一室一厅再帮我补几套。",
        ],
    },
    {
        "id": "shiqiao_area_or_community",
        "turns": [
            "石桥5000左右有没有整租两室？",
            "我说的是石桥那片区域，不是只问石桥铭苑。",
            "前两套视频发我。",
            "这两套哪套水电更划算？",
            "第一套今天能看吗，看房方式是什么？",
            "如果还没空出来还能约看吗？",
            "1和2的图片也发一下。",
            "客户嫌视频模糊，有没有原视频？",
            "其中一套看中了，联系谁定房？",
            "把石桥华丰这片的房源表也发我。",
        ],
    },
    {
        "id": "yangjiafu_fuzzy_then_specific",
        "turns": [
            "杨家府附近还有房子吗？客户名字可能说得不准。",
            "兴业杨家府的，预算4500左右。",
            "有的话视频和图片都发我。",
            "这套押一付一和押二付一分别多少？",
            "水电费怎么收？",
            "今天可以看吗，密码怎么给？",
            "如果客户想签合同，找哪个号码？",
            "免押能不能做，芝麻分要多少？",
            "杨家新雅苑有没有三室的？",
            "杨家新雅苑那套也发视频，最好清楚一点。",
        ],
    },
    {
        "id": "typo_community_context_reuse",
        "turns": [
            "荣润府1600到1800有没有押一付一的？",
            "如果你说的是棠润府，那就看15-2-801B在不在。",
            "这套视频发我。",
            "有没有原视频或者飞书素材源链接？",
            "这套图片也发一下。",
            "这套水电怎么收？",
            "押一付一和押二付一分别多少？",
            "什么时候空出，今天能自己看吗？",
            "密码不对的话联系谁？",
            "客户看中了怎么签合同？",
        ],
    },
    {
        "id": "dongzhan_gaotang_price_followup",
        "turns": [
            "皋塘还有2600以内的一室吗？东站附近也可以。",
            "带独卫或者独立厨房的优先。",
            "第一套视频发我。",
            "这个图片也发一下。",
            "这套今天能不能看，密码多少？",
            "4000左右的两室东站附近有吗？",
            "前两套都发视频给客户筛一下。",
            "这两套水电和价格对比一下。",
            "房源表也发一份。",
            "客户想定其中一套的话怎么操作？",
        ],
    },
    {
        "id": "multi_area_compare_budget_replace",
        "turns": [
            "万达和东新园两边都可以，3000以内有什么能住的？",
            "东新园这边两室有没有便宜点的？",
            "4000到5000的呢？还是东新园杭氧新天地这片。",
            "第1和第3套视频发我。",
            "这两套水电和价格帮我对比一下。",
            "第一个看房密码多少？",
            "客户到门口打不开门怎么办？",
            "这两个有没有原视频，方便客户保存转发？",
            "免押服务费怎么算？",
            "最后把房源表发我。",
        ],
    },
    {
        "id": "sheet_first_then_detail",
        "turns": [
            "先把最新房源表发我，客户想自己看一遍。",
            "表里面新天地4000左右两室是哪几套？",
            "4000到5000的呢？",
            "前两套视频发我。",
            "第一套图片有吗？",
            "这套水电费怎么收？",
            "这套能今天看吗？",
            "客户看完视频想定，怎么定？",
            "免押金能不能做？",
            "换成万达2000以下一室，还有哪些？",
        ],
    },
    {
        "id": "batch_video_pending_and_new_topic",
        "turns": [
            "华丰和半山附近5000左右整租视频都发我几套。",
            "能发的都发，先不要超过5套。",
            "剩下的继续发。",
            "第2和第5套水电怎么收？",
            "第5套如果没视频，那就发图片。",
            "这几套有没有原视频？",
            "客户今天想看其中两套，密码怎么给？",
            "如果没有密码或者还没空出怎么处理？",
            "客户看中了怎么定房？",
            "换成东新园4000左右两室再推荐几套。",
        ],
    },
    {
        "id": "community_room_then_new_budget",
        "turns": [
            "棠润府15-2-801B还在吗？",
            "这套视频和图片都发我。",
            "这套价格和水电说一下。",
            "这套今天能不能自己看？",
            "客户问免押和定房流程，帮我说清楚。",
            "另外新天地附近4000左右两室一厅还有吗？",
            "4000到5000的呢？",
            "前两套视频发我。",
            "第一套原视频有没有？",
            "最后把最新房源表再发一下。",
        ],
    },
]

REQUIRED_TOKENS = (
    "万达",
    "新天地",
    "石桥",
    "杨家府",
    "棠润府",
    "皋塘",
    "视频",
    "图片",
    "房源表",
    "免押",
    "原视频",
    "水电",
    "密码",
    "定房",
)
BAD_TOKENS = ("???", "锟", "閿", "娑", "閼", "閻", "閹")


def configure_runner() -> None:
    runner.SCRIPT_PATH = SCRIPT_PATH
    runner.WINDOWS = WINDOWS
    runner.REQUIRED_TOKENS = REQUIRED_TOKENS
    runner.BAD_TOKENS = BAD_TOKENS
    runner.CONVERSATION_PREFIX = "conv_10similar_10turns"


def print_compact_summary(artifact: Path) -> None:
    data = json.loads(artifact.read_text(encoding="utf-8"))
    print(f"ARTIFACT {artifact}")
    print(
        "INPUT_INTEGRITY "
        f"passed={data['input_integrity']['passed']} "
        f"windows={data['input_integrity']['window_count']} "
        f"turns={data['input_integrity']['turn_count']} "
        f"chinese_ratio={data['input_integrity']['chinese_ratio']}"
    )
    print(f"COMPLETED {data.get('completed')}")
    for window in data.get("windows") or []:
        severity = {"high": 0, "medium": 0, "low": 0, "info": 0}
        for turn in window.get("turns") or []:
            problem = turn.get("problem") or {}
            key = str(problem.get("severity") or "info")
            severity[key if key in severity else "info"] += 1
        print(
            f"WINDOW {window.get('window_index')} {window.get('window_id')} "
            f"high={severity['high']} medium={severity['medium']} low={severity['low']}"
        )


async def main() -> None:
    configure_runner()
    artifact = await runner.run_all(turn_timeout=90)
    print_compact_summary(artifact)


if __name__ == "__main__":
    asyncio.run(main())
