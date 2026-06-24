from __future__ import annotations

import asyncio
import json
from pathlib import Path

from qa_artifacts import run_rag_10windows_10turns_utf8 as runner


SCRIPT_PATH = Path(__file__)

WINDOWS = [
    {
        "id": "holdout_wanda_beiruan_one_room",
        "turns": [
            "北部软件园附近1800以内的一室还有吗？最好今天能看。",
            "万达那边也算，便宜的优先，带独卫的有没有？",
            "先把匹配到的前两套视频发我。",
            "第一套图片也发一下，客户想看装修。",
            "这套押一付一和押二付一分别多少钱？",
            "水电怎么收？",
            "今天自己去看密码怎么给？",
            "如果密码不对或者门打不开，客户联系谁？",
            "客户看中了怎么定房签合同？",
            "最后把万达附近2000以内的一室再补几套。",
        ],
    },
    {
        "id": "holdout_xintiandi_budget_upgrade",
        "turns": [
            "新天地附近两室一厅，客户预算4000到4500，有没有整租？",
            "如果4500到5200呢，还是新天地和杭氧这片。",
            "前三套视频都发我，客户先粗筛。",
            "第2套有没有图片？",
            "第2套水电费怎么算？",
            "第2套今天能看吗？",
            "视频有点糊，有没有原视频或者能保存转发的方式？",
            "这几套免押金要什么条件？",
            "客户如果今晚想定，流程怎么走？",
            "房源表也发我一份。",
        ],
    },
    {
        "id": "holdout_shiqiao_area_vs_community",
        "turns": [
            "石桥4800左右有两室吗？客户想整租。",
            "我是说石桥这一片，不一定是石桥铭苑。",
            "筛出来的1和3视频发我。",
            "这两套哪个价格低一点？",
            "1号那套水电怎么收？",
            "3号那套看房方式是什么？",
            "如果还没空出来还能约看吗？",
            "1和3的图片也发我。",
            "客户看中了其中一套，定房找哪个号码？",
            "石桥华丰这片房源表发我。",
        ],
    },
    {
        "id": "holdout_gaotang_dongzhan_context",
        "turns": [
            "皋塘东站附近2600以内的一室有没有？",
            "独立厨房或者独卫的优先。",
            "第一套视频发我看看。",
            "这套图片也要。",
            "这套多少钱，押一押二都说下。",
            "这套水电呢？",
            "客户今天下午能不能自己看？",
            "密码如果没有或者打不开怎么办？",
            "东站附近3000以内还有其他一室吗？",
            "房源表发一下给客户自己挑。",
        ],
    },
    {
        "id": "holdout_xingqiao_typo_and_media",
        "turns": [
            "星桥锦秀嘉苑2000左右的一室还有吗？名字可能打错了。",
            "如果是星桥锦绣嘉苑，先查还在不在。",
            "有视频就先发视频。",
            "图片也发我。",
            "这套押一付一多少，押二付一多少？",
            "水电怎么算？",
            "看房密码多少，今天可以看吗？",
            "客户觉得视频糊，有没有原视频？",
            "客户看中了怎么定？",
            "免押可以做吗，服务费怎么算？",
        ],
    },
    {
        "id": "holdout_yanglefu_yangjiaxinyayuan",
        "turns": [
            "杨家府附近5000左右三室有没有？客户说的小区不太准。",
            "不是杨家府，是杨乐府或者杨家新雅苑，你帮我看下三室。",
            "有的话先发视频，最多发两套。",
            "第1套图片也发。",
            "第1套户型特点怎么样？",
            "水电是民用还是商用？",
            "今天能不能看，密码怎么给？",
            "如果客户要免押，条件是什么？",
            "客户想定房，流程和联系方式发我。",
            "换成杨家新雅苑一室一厅还有吗？",
        ],
    },
    {
        "id": "holdout_huafeng_banshan_whole_rent",
        "turns": [
            "华丰半山附近4500左右整租两室还有吗？",
            "最好装修好一点，能马上入住的。",
            "前两套视频先发我。",
            "第2套图片有没有？",
            "第2套价格和水电说一下。",
            "第1套今天能不能自己看？",
            "客户到门口密码不对怎么办？",
            "这两套有没有原视频或者飞书素材链接？",
            "客户问免押和服务费，怎么回？",
            "最后发一下华丰半山这片房源表。",
        ],
    },
    {
        "id": "holdout_dongxinyuan_short_followups",
        "turns": [
            "东新园这边3500到4200的两室还有哪些？",
            "4000到5000的呢？",
            "带视频的优先，先发前两套。",
            "第1套和第2套水电分别怎么收？",
            "哪套押一付一更低？",
            "第2套今天能看吗？",
            "第2套图片也发我。",
            "客户如果想直接定第2套，怎么操作？",
            "免押金能做吗？",
            "换成一室一厅，有没有3000以内的？",
        ],
    },
    {
        "id": "holdout_sheet_then_candidate",
        "turns": [
            "先把最新房源表发我，我给客户整体看一下。",
            "表里万达2000以下的一室有哪些？",
            "第4套视频发我。",
            "如果没有第4套，就告诉我现在一共列了几套。",
            "第1套图片发我。",
            "第1套水电和价格说一下。",
            "第1套今天能看吗？",
            "客户想定这套怎么弄？",
            "免押条件也说下。",
            "再换新天地4000左右两室看看。",
        ],
    },
    {
        "id": "holdout_tangrunfu_typo_reuse",
        "turns": [
            "棠闰府15-2-801B还在吗？客户可能把小区名写错了。",
            "如果你确认是棠润府，那这套视频发我。",
            "图片也发。",
            "这套价格和水电都说清楚。",
            "这套什么时候空出，能不能自己看？",
            "密码没有或者不对的话联系谁？",
            "客户要原视频保存转发，有没有办法？",
            "免押和服务费怎么说？",
            "客户想定这套，合同怎么签？",
            "除了这套，棠润府1600到1800还有别的吗？",
        ],
    },
]

REQUIRED_TOKENS = (
    "万达",
    "北部软件园",
    "新天地",
    "杭氧",
    "石桥",
    "皋塘",
    "东站",
    "杨家府",
    "星桥锦绣嘉苑",
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
BAD_TOKENS = ("???", "锟", "�", "闁", "濞")


def configure_runner() -> None:
    runner.SCRIPT_PATH = SCRIPT_PATH
    runner.WINDOWS = WINDOWS
    runner.REQUIRED_TOKENS = REQUIRED_TOKENS
    runner.BAD_TOKENS = BAD_TOKENS
    runner.CONVERSATION_PREFIX = "conv_holdout_10turns"


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
