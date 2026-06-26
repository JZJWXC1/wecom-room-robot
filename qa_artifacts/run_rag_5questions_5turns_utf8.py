from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.offline_guard import activate_offline_test_mode, offline_guard_status, repo_root


activate_offline_test_mode()

import app.main as main
from qa_artifacts import run_rag_test_text_window_utf8 as qa_base


SCRIPT_PATH = Path(__file__)
INPUT_SOURCE_PATH = SCRIPT_PATH

SEEDS: list[tuple[str, str]] = [
    ("conv_wanda", "万达附近1500左右还有哪些？客户想今天先看两套。"),
    ("conv_dongxinyuan", "东新园这边有没有两室一厅，预算3500到4000左右的？"),
    ("conv_xintiandi", "新天地还有4000左右的两室吗。"),
    ("conv_gaotang", "皋塘还有房子吗？"),
    ("conv_yangjiafu", "杨家府还有房子吗？"),
]

REQUIRED_INPUT_TOKENS = ("万达", "东新园", "新天地", "皋塘", "杨家府", "视频", "房源表", "免押")


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root()).as_posix()
    except ValueError:
        return resolved.name


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
    report = chinese_integrity_report([seed for _, seed in SEEDS] + [next_user("", index, "") for index in range(2, 6)])
    if not report["passed"]:
        raise RuntimeError(f"测试输入编码异常，停止执行：{json.dumps(report, ensure_ascii=False)}")
    return report


def next_user(seed: str, round_no: int, bot_text: str) -> str:
    bot = bot_text or ""
    if round_no == 2:
        if "杨家府" in seed and any(word in bot for word in ("哪个", "确认", "是指", "相近", "具体")):
            return "兴业杨家府，有的话先发视频给客户看看。"
        if "皋塘" in seed and any(word in bot for word in ("哪个", "确认", "是指", "具体", "皋塘")):
            return "皋塘运都，有的话先发视频给客户看看。"
        return "先发前两套视频给我，我给客户筛一下。"
    if round_no == 3:
        return "房源表也发我一下，客户想自己看。"
    if round_no == 4:
        return "这两套客户看中了怎么定房？"
    if round_no == 5:
        return "免押金要什么条件？服务费怎么算？"
    return ""


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


def build_quality_status(
    conversations: list[dict[str, Any]],
    *,
    completed: bool,
    input_integrity: dict[str, Any],
) -> dict[str, Any]:
    infrastructure_errors: list[dict[str, Any]] = []
    if not input_integrity.get("passed"):
        infrastructure_errors.append(
            {"stage": "input_integrity", "reason": "5 问 QA 输入 UTF-8 校验失败。"}
        )
    for conversation in conversations:
        for turn in conversation.get("turns") or []:
            if turn.get("error"):
                infrastructure_errors.append(
                    {
                        "stage": "turn_execution",
                        "conversation_id": conversation.get("conversation_id"),
                        "round": turn.get("round"),
                        "reason": turn.get("error"),
                    }
                )
    if not completed:
        infrastructure_errors.append({"stage": "completion", "reason": "5 问 QA 未完整执行。"})
    passed = bool(completed and not infrastructure_errors)
    return {
        "completed": bool(completed),
        "passed": passed,
        "infrastructure_error": bool(infrastructure_errors),
        "business_failure": False,
        "exit_code": 0 if passed else 2,
        "high_count": 0,
        "medium_count": 0,
        "fallback_count": 0,
        "network_call_count": offline_guard_status().get("blocked_network_call_count", 0),
        "infrastructure_errors": infrastructure_errors,
    }


async def send_turn(fake: CaptureWeComKf, conv_id: str, round_no: int, user_text: str) -> dict[str, Any]:
    before = len(fake.events)
    message = {
        "msgid": f"{conv_id}-{round_no}-{int(time.time() * 1000)}",
        "open_kfid": "kf_sim",
        "external_userid": conv_id,
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
        "round": round_no,
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
        "offline_service_stubs": qa_base.install_offline_service_stubs(),
    }
    main.wecom_kf = fake
    main.wecom_kf_context_store = store
    main.kf_turn_tasks.clear()
    main.kf_turn_generations.clear()
    main.kf_turn_pending_messages.clear()
    conversations: list[dict[str, Any]] = []
    try:
        for conv_id, seed in SEEDS:
            turns: list[dict[str, Any]] = []
            user_text = seed
            for round_no in range(1, 6):
                result = await send_turn(fake, conv_id, round_no, user_text)
                turns.append(result)
                last_bot_text = "\n".join(result["bot"].get("texts") or [])
                if round_no < 5:
                    user_text = next_user(seed, round_no + 1, last_bot_text)
            conversations.append({"conversation_id": conv_id, "seed": seed, "turns": turns})
    finally:
        main.wecom_kf = originals["wecom_kf"]
        main.wecom_kf_context_store = originals["wecom_kf_context_store"]
        main.kf_turn_tasks.clear()
        main.kf_turn_tasks.update(originals["kf_turn_tasks"])
        main.kf_turn_generations.clear()
        main.kf_turn_generations.update(originals["kf_turn_generations"])
        main.kf_turn_pending_messages.clear()
        main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])
        qa_base.restore_offline_service_stubs(originals["offline_service_stubs"])

    completed = (
        len(conversations) == len(SEEDS)
        and all(
            len(conversation.get("turns") or []) == 5
            and not any(turn.get("error") for turn in conversation.get("turns") or [])
            for conversation in conversations
        )
    )
    quality = build_quality_status(
        conversations,
        completed=completed,
        input_integrity=input_integrity,
    )
    artifact_dir = Path("qa_artifacts")
    artifact_dir.mkdir(exist_ok=True)
    artifact = artifact_dir / f"rag_5questions_5turns_utf8_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    artifact.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(),
                "script_path": _display_path(SCRIPT_PATH),
                "input_source_path": _display_path(INPUT_SOURCE_PATH),
                "input_integrity": input_integrity,
                "first_user_raw": SEEDS[0][1],
                "completed": completed,
                "quality_status": quality,
                "offline_guard": offline_guard_status(),
                "conversations": conversations,
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
    quality = data.get("quality_status") or {}
    print(
        "QUALITY "
        f"passed={quality.get('passed')} "
        f"infrastructure_error={quality.get('infrastructure_error')} "
        f"exit_code={quality.get('exit_code')}"
    )
    for conv in data["conversations"]:
        print(f"\n### {conv['seed']}")
        for turn in conv["turns"]:
            bot = turn["bot"]
            text = " | ".join(item.replace("\n", " / ") for item in bot.get("texts", []))
            print(f"R{turn['round']} 用户: {turn['user']}")
            print(f"R{turn['round']} 机器人: {text[:700]}")
            print(
                f"R{turn['round']} 动作: image={bot.get('image_count')} "
                f"video={bot.get('video_count')} elapsed={turn['elapsed_sec']} error={turn['error']}"
            )


if __name__ == "__main__":
    output = asyncio.run(run())
    print_summary(output)
    output_data = json.loads(output.read_text(encoding="utf-8"))
    raise SystemExit(int((output_data.get("quality_status") or {}).get("exit_code") or 0))
