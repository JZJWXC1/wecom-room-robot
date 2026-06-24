from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.offline_guard import activate_offline_test_mode


activate_offline_test_mode()

import app.main as main


SCRIPT_PATH = Path(__file__)
INPUT_SOURCE_PATH = SCRIPT_PATH
CONVERSATION_ID = "conv_3q_10turns"

TURNS: list[str] = [
    "万达有什么2000以下的一室",
    "荣润府有没有押一付一的？预算1600到1800。",
    "石桥附近5000左右有两室吗？最好整租。",
    "先把万达2000以下一室里最合适的两套视频发我。",
    "荣润府如果有的话，视频和图片都发客户看看。",
    "石桥5000左右两室整租的前两套视频也发我。",
    "房源表也发我一下，客户想自己筛。",
    "这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
    "客户看中了怎么定房？定金和合同怎么弄？",
    "免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。",
]
REQUIRED_INPUT_TOKENS = ("万达", "荣润府", "石桥", "视频", "房源表", "免押")


class FakeStateStore:
    def __init__(self) -> None:
        self.processed: set[str] = set()

    def is_processed(self, msgid: str) -> bool:
        return msgid in self.processed

    def mark_processed(self, msgid: str) -> None:
        self.processed.add(msgid)

    def last_welcome_sent_at(self, key: str) -> float:
        return 0.0

    def mark_welcome_sent(self, key: str, sent_at: float | None = None) -> None:
        return None


class CaptureWeComKf:
    def __init__(self) -> None:
        self.state_store = FakeStateStore()
        self.events: list[dict[str, Any]] = []

    def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict[str, Any]:
        self.events.append(
            {"conv": external_userid, "type": "text", "text": text, "time": time.time()}
        )
        return {"errcode": 0}

    def send_image(self, open_kfid: str, external_userid: str, image_path: Path) -> dict[str, Any]:
        self.events.append(
            {"conv": external_userid, "type": "image", "path": str(image_path), "time": time.time()}
        )
        return {"errcode": 0}

    def send_video(
        self,
        open_kfid: str,
        external_userid: str,
        video_path: Path,
        title: str = "",
    ) -> dict[str, Any]:
        self.events.append(
            {
                "conv": external_userid,
                "type": "video",
                "path": str(video_path),
                "title": title,
                "time": time.time(),
            }
        )
        return {"errcode": 0}

    def send_welcome_text_on_event(self, welcome_code: str, content: str) -> dict[str, Any]:
        self.events.append({"conv": "welcome", "type": "welcome", "text": content, "time": time.time()})
        return {"errcode": 0}


class MemoryContextStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.data.get(key)
        return copy.deepcopy(value) if value is not None else None

    def save(self, key: str, context: dict[str, Any]) -> None:
        self.data[key] = copy.deepcopy(context)


def chinese_integrity_report(texts: list[str]) -> dict[str, Any]:
    joined = "\n".join(texts)
    chinese_count = sum(1 for char in joined if "\u4e00" <= char <= "\u9fff")
    total_count = max(len(joined), 1)
    missing = [token for token in REQUIRED_INPUT_TOKENS if token not in joined]
    bad_tokens = [token for token in ("???", "�", "锟", "涓", "鑽", "鐭", "鎴") if token in joined]
    return {
        "script_path": str(SCRIPT_PATH),
        "input_source_path": str(INPUT_SOURCE_PATH),
        "encoding": "utf-8",
        "chinese_char_count": chinese_count,
        "total_char_count": len(joined),
        "chinese_ratio": round(chinese_count / total_count, 4),
        "required_tokens": list(REQUIRED_INPUT_TOKENS),
        "missing_required_tokens": missing,
        "bad_tokens": bad_tokens,
        "passed": not missing and not bad_tokens and chinese_count / total_count > 0.35,
        "first_user_raw": texts[0] if texts else "",
    }


def assert_utf8_inputs() -> dict[str, Any]:
    joined = "\n".join(TURNS)
    report = chinese_integrity_report(TURNS)
    if not report["passed"]:
        raise RuntimeError(f"测试输入编码异常，停止执行：{json.dumps(report, ensure_ascii=False)}, joined={joined!r}")
    return report


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [
        str(event.get("text") or "")
        for event in events
        if event.get("type") in ("text", "welcome") and event.get("text")
    ]
    images = [event.get("path") for event in events if event.get("type") == "image"]
    videos = [event.get("path") for event in events if event.get("type") == "video"]
    return {
        "texts": texts,
        "images": images,
        "videos": videos,
        "image_count": len(images),
        "video_count": len(videos),
        "types": [event.get("type") for event in events],
    }


async def send_turn(fake: CaptureWeComKf, turn_index: int, user_text: str) -> dict[str, Any]:
    before = len(fake.events)
    message = {
        "msgid": f"{CONVERSATION_ID}-{turn_index}-{int(time.time() * 1000)}",
        "open_kfid": "kf_sim",
        "external_userid": CONVERSATION_ID,
        "origin": 3,
        "msgtype": "text",
        "text": {"content": user_text},
    }
    started = time.time()
    error = ""
    try:
        await asyncio.wait_for(main._handle_text_message(message), timeout=240)
    except Exception as exc:
        error = repr(exc)
    return {
        "turn": turn_index,
        "user": user_text,
        "elapsed_sec": round(time.time() - started, 2),
        "error": error,
        "bot": summarize_events(fake.events[before:]),
    }


async def run() -> Path:
    input_integrity = assert_utf8_inputs()
    fake = CaptureWeComKf()
    store = MemoryContextStore()
    originals = {
        "wecom_kf": main.wecom_kf,
        "wecom_kf_context_store": main.wecom_kf_context_store,
        "kf_turn_tasks": dict(main.kf_turn_tasks),
        "kf_turn_generations": dict(main.kf_turn_generations),
        "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
    }
    main.wecom_kf = fake
    main.wecom_kf_context_store = store
    main.kf_turn_tasks.clear()
    main.kf_turn_generations.clear()
    main.kf_turn_pending_messages.clear()
    turns: list[dict[str, Any]] = []
    try:
        for index, user_text in enumerate(TURNS, start=1):
            turns.append(await send_turn(fake, index, user_text))
    finally:
        main.wecom_kf = originals["wecom_kf"]
        main.wecom_kf_context_store = originals["wecom_kf_context_store"]
        main.kf_turn_tasks.clear()
        main.kf_turn_tasks.update(originals["kf_turn_tasks"])
        main.kf_turn_generations.clear()
        main.kf_turn_generations.update(originals["kf_turn_generations"])
        main.kf_turn_pending_messages.clear()
        main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])

    artifact_dir = Path("qa_artifacts")
    artifact_dir.mkdir(exist_ok=True)
    artifact = artifact_dir / f"rag_3questions_10turns_utf8_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    artifact.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(),
                "script_path": str(SCRIPT_PATH),
                "input_source_path": str(INPUT_SOURCE_PATH),
                "input_integrity": input_integrity,
                "first_user_raw": TURNS[0],
                "conversation_id": CONVERSATION_ID,
                "turns": turns,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return artifact


def print_summary(artifact: Path) -> None:
    data = json.loads(artifact.read_text(encoding="utf-8"))
    print(f"ARTIFACT {artifact}")
    print(f"INPUT_INTEGRITY passed={data['input_integrity']['passed']} first={data['first_user_raw']}")
    for turn in data["turns"]:
        bot = turn["bot"]
        text = " | ".join(item.replace("\n", " / ") for item in bot.get("texts", []))
        print(f"\nR{turn['turn']} 用户: {turn['user']}")
        print(f"R{turn['turn']} 机器人: {text[:900]}")
        print(
            f"R{turn['turn']} 动作: image={bot.get('image_count')} "
            f"video={bot.get('video_count')} elapsed={turn['elapsed_sec']} error={turn['error']}"
        )


if __name__ == "__main__":
    output = asyncio.run(run())
    print_summary(output)
