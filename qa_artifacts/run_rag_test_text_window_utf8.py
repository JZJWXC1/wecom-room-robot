from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.offline_guard import activate_offline_test_mode, repo_root


activate_offline_test_mode()

import app.main as main


SCRIPT_PATH = Path(__file__)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "qa"
INPUT_SOURCE_PATH = FIXTURE_DIR / "test_text_full_utf8.json"
DEFAULT_WINDOW_INPUT_PATH = FIXTURE_DIR / "single_window_required_utf8.json"
CONVERSATION_PREFIX = "conv_test_text_window"
FULL_REQUIRED_TOKENS = (
    "万达",
    "荣润府",
    "石桥",
    "东新园",
    "新天地",
    "视频",
    "房源表",
    "押一付一",
    "今天能看",
)
BAD_TOKENS = ("???", "�", "锟", "涓", "鑽", "鐭", "鎴")


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


def load_questions(path: Path = INPUT_SOURCE_PATH) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("turns"), list):
        return [str(item).strip() for item in data["turns"] if str(item).strip()]
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise RuntimeError(f"测试输入文件格式异常：{path}")
    return [str(item).strip() for item in questions if str(item).strip()]


def chinese_integrity_report(
    texts: list[str],
    *,
    required_tokens: tuple[str, ...] = (),
    label: str,
    input_source_path: Path = INPUT_SOURCE_PATH,
) -> dict[str, Any]:
    joined = "\n".join(texts)
    chinese_count = sum(1 for char in joined if "\u4e00" <= char <= "\u9fff")
    total_count = max(len(joined), 1)
    missing = [token for token in required_tokens if token not in joined]
    bad_tokens = [token for token in BAD_TOKENS if token in joined]
    return {
        "label": label,
        "script_path": str(SCRIPT_PATH),
        "input_source_path": str(input_source_path),
        "encoding": "utf-8",
        "chinese_char_count": chinese_count,
        "total_char_count": len(joined),
        "chinese_ratio": round(chinese_count / total_count, 4),
        "required_tokens": list(required_tokens),
        "missing_required_tokens": missing,
        "bad_tokens": bad_tokens,
        "passed": not missing and not bad_tokens and chinese_count / total_count > 0.35,
        "first_user_raw": texts[0] if texts else "",
    }


def assert_utf8_inputs(
    selected: list[str],
    all_questions: list[str],
    *,
    selected_source_path: Path = INPUT_SOURCE_PATH,
) -> dict[str, Any]:
    full_report = chinese_integrity_report(
        all_questions,
        required_tokens=FULL_REQUIRED_TOKENS,
        label="full_test_text",
        input_source_path=INPUT_SOURCE_PATH,
    )
    window_report = chinese_integrity_report(
        selected,
        label="selected_window",
        input_source_path=selected_source_path,
    )
    if not full_report["passed"] or not window_report["passed"]:
        raise RuntimeError(
            "测试输入编码异常，停止执行："
            + json.dumps(
                {"full": full_report, "window": window_report},
                ensure_ascii=False,
            )
        )
    return {"full": full_report, "window": window_report}


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


def _short_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _room_label(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    community = str(row.get("小区") or row.get("community") or "").strip()
    room = str(row.get("房号") or row.get("room") or "").strip()
    return f"{community}{room}".strip()


def _room_labels(rows: Any, limit: int = 8) -> list[str]:
    if not isinstance(rows, list):
        return []
    labels = [_room_label(row) for row in rows]
    return [label for label in labels if label][:limit]


def _summarize_stage_result(stage: str, result: Any) -> dict[str, Any]:
    if stage == "rewrite_intent" and isinstance(result, dict):
        return {
            "intent": result.get("intent"),
            "effective_query": _short_text(result.get("effective_query") or result.get("rewritten_query")),
            "needs_clarification": bool(result.get("needs_clarification")),
            "clarification_text": _short_text(result.get("clarification_text")),
            "selected_indices": result.get("selected_indices"),
            "entity_resolution": result.get("entity_resolution"),
            "constraint_proof": result.get("constraint_proof"),
            "tool_requirements": (result.get("structured_task") or {}).get("tool_requirements")
            if isinstance(result.get("structured_task"), dict)
            else None,
        }
    if stage == "planner" and isinstance(result, dict):
        return {
            "actions": result.get("actions"),
            "reply_text": _short_text(result.get("reply_text") or result.get("reply")),
            "need_rewrite_clarification": bool(result.get("need_rewrite_clarification")),
            "missing_evidence": _short_text(result.get("missing_evidence")),
            "pre_tool_reply_text": _short_text(result.get("pre_tool_reply_text")),
            "source": result.get("source"),
        }
    if stage == "tools" and isinstance(result, dict):
        return {
            "actions": result.get("actions"),
            "inventory_rows": _room_labels(result.get("inventory_rows")),
            "target_rows": _room_labels(result.get("target_rows")),
            "video_rows": _room_labels(result.get("video_rows")),
            "image_rows": _room_labels(result.get("image_rows")),
            "video_count": len(result.get("video_paths") or []),
            "image_count": len(result.get("image_paths") or []),
            "inventory_image_count": len(result.get("inventory_images") or []),
            "missing_media": result.get("missing_media"),
            "media_status": result.get("media_status"),
            "suppress_actions": bool(result.get("suppress_actions")),
        }
    if stage == "final_selfcheck" and isinstance(result, dict):
        selfcheck = result.get("selfcheck") if isinstance(result.get("selfcheck"), dict) else {}
        rule = selfcheck.get("rule") if isinstance(selfcheck.get("rule"), dict) else {}
        llm = selfcheck.get("llm") if isinstance(selfcheck.get("llm"), dict) else {}
        return {
            "reply": _short_text(result.get("reply")),
            "draft_reply": _short_text(result.get("draft_reply")),
            "planner_reply_result": result.get("planner_reply_result"),
            "needs_planner_retry": bool(result.get("needs_planner_retry")),
            "planner_retry_reason": _short_text(result.get("planner_retry_reason"), 800),
            "selfcheck_status": selfcheck.get("status"),
            "rule_status": rule.get("status") or rule.get("action"),
            "rule_source": rule.get("source"),
            "rule_reason": _short_text(rule.get("reason") or rule.get("fallback_text")),
            "llm_status": llm.get("status"),
            "llm_source": llm.get("source"),
            "llm_reason": _short_text(llm.get("reason") or llm.get("planner_retry_reason")),
        }
    if stage == "send" and isinstance(result, dict):
        return {
            "sent_actions": result.get("sent_actions"),
        }
    return {"type": type(result).__name__}


def judge_turn_chain(turn: dict[str, Any]) -> dict[str, str]:
    if turn.get("error"):
        timings = turn.get("stage_timings") or []
        last_stage = timings[-1].get("stage") if timings else "测试脚本/编码环境"
        return {"status": "error", "likely_link": str(last_stage), "reason": str(turn.get("error"))}
    summaries = {item.get("stage"): item.get("summary") or {} for item in turn.get("stage_timings") or []}
    rewrite = summaries.get("rewrite_intent") or {}
    planner = summaries.get("planner") or {}
    final = summaries.get("final_selfcheck") or {}
    send = summaries.get("send") or {}
    bot = turn.get("bot") or {}
    if rewrite.get("needs_clarification"):
        return {
            "status": "clarification",
            "likely_link": "问题重写/意图分析",
            "reason": "意图层生成追问，需人工判断追问是否基于真实房源/素材证据。",
        }
    if planner.get("need_rewrite_clarification"):
        return {
            "status": "planner_feedback",
            "likely_link": "Planner",
            "reason": planner.get("missing_evidence") or "Planner 要求回意图层补证据。",
        }
    planner_reply_result = final.get("planner_reply_result") or {}
    if isinstance(planner_reply_result, dict) and planner_reply_result.get("reply_text") == "" and final.get("needs_planner_retry"):
        return {
            "status": "retry_needed",
            "likely_link": "Planner工具后回复生成",
            "reason": "工具后 Planner 没有生成客户可见 reply_text。",
        }
    if final.get("needs_planner_retry"):
        return {
            "status": "retry_needed",
            "likely_link": "最终自检/自检回流",
            "reason": final.get("rule_reason") or final.get("llm_reason") or "最终自检要求回 Planner。",
        }
    if not bot.get("texts") and not bot.get("images") and not bot.get("videos"):
        return {"status": "no_output", "likely_link": "发送阶段", "reason": "本轮没有捕获到客户可见输出。"}
    if not send and (bot.get("images") or bot.get("videos")):
        return {"status": "needs_review", "likely_link": "发送阶段", "reason": "捕获到素材动作，但缺少发送阶段摘要。"}
    return {"status": "recorded", "likely_link": "人工复核", "reason": "链路摘要已记录，需按验收标准人工确认回复是否完全正确。"}


def build_quality_status(
    turns: list[dict[str, Any]],
    *,
    completed: bool,
    input_integrity: dict[str, Any],
) -> dict[str, Any]:
    infrastructure_errors: list[dict[str, Any]] = []
    if not input_integrity.get("full", {}).get("passed"):
        infrastructure_errors.append(
            {"stage": "input_integrity", "reason": "完整 UTF-8 输入 fixture 未通过校验。"}
        )
    if not input_integrity.get("window", {}).get("passed"):
        infrastructure_errors.append(
            {"stage": "input_integrity", "reason": "当前测试窗口 UTF-8 输入未通过校验。"}
        )
    for turn in turns:
        if turn.get("error"):
            infrastructure_errors.append(
                {"stage": "turn_execution", "turn": turn.get("turn"), "reason": turn.get("error")}
            )
    if not completed:
        infrastructure_errors.append(
            {"stage": "completion", "reason": "固定 QA 未完整执行所有轮次。"}
        )
    passed = bool(completed and not infrastructure_errors)
    return {
        "completed": bool(completed),
        "passed": passed,
        "infrastructure_error": bool(infrastructure_errors),
        "business_failure": False,
        "exit_code": 0 if passed else 2,
        "infrastructure_errors": infrastructure_errors,
    }


async def send_turn(
    fake: CaptureWeComKf,
    *,
    conversation_id: str,
    turn_index: int,
    user_text: str,
    turn_timeout: float,
) -> dict[str, Any]:
    before = len(fake.events)
    message = {
        "msgid": f"{conversation_id}-{turn_index}-{int(time.time() * 1000)}",
        "open_kfid": "kf_sim",
        "external_userid": conversation_id,
        "origin": 3,
        "msgtype": "text",
        "text": {"content": user_text},
    }
    started = time.time()
    stage_timings: list[dict[str, Any]] = []
    error = ""
    originals = {
        "_understand_message": main._understand_message,
        "_plan_actions": main._plan_actions,
        "_execute_tools": main._execute_tools,
        "_generate_reply_result": main._generate_reply_result,
        "_send_final_actions": main._send_final_actions,
    }

    def timed_stage(name: str, func: Any) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            stage_started = time.time()
            result: Any = None
            stage_error = ""
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                stage_error = repr(exc)
                raise
            finally:
                stage_timings.append(
                    {
                        "stage": name,
                        "elapsed_sec": round(time.time() - stage_started, 3),
                        "summary": _summarize_stage_result(name, result) if stage_error == "" else {},
                        "error": stage_error,
                    }
                )

        return wrapper

    try:
        main._understand_message = timed_stage("rewrite_intent", originals["_understand_message"])
        main._plan_actions = timed_stage("planner", originals["_plan_actions"])
        main._execute_tools = timed_stage("tools", originals["_execute_tools"])
        main._generate_reply_result = timed_stage("final_selfcheck", originals["_generate_reply_result"])
        main._send_final_actions = timed_stage("send", originals["_send_final_actions"])
        await asyncio.wait_for(main._handle_text_message(message), timeout=turn_timeout)
    except Exception as exc:
        error = repr(exc)
    finally:
        main._understand_message = originals["_understand_message"]
        main._plan_actions = originals["_plan_actions"]
        main._execute_tools = originals["_execute_tools"]
        main._generate_reply_result = originals["_generate_reply_result"]
        main._send_final_actions = originals["_send_final_actions"]
    turn = {
        "turn": turn_index,
        "user": user_text,
        "elapsed_sec": round(time.time() - started, 2),
        "stage_timings": stage_timings,
        "error": error,
        "bot": summarize_events(fake.events[before:]),
    }
    turn["chain_judgment"] = judge_turn_chain(turn)
    return turn


async def run_window(
    *,
    offset: int = 0,
    count: int = 10,
    input_path: Path = INPUT_SOURCE_PATH,
    turn_timeout: float = 90,
) -> Path:
    all_questions = load_questions(input_path)
    selected = all_questions[offset : offset + count]
    if len(selected) < count:
        raise RuntimeError(f"测试窗口不足 {count} 条：offset={offset}, actual={len(selected)}")
    input_integrity = assert_utf8_inputs(
        selected,
        load_questions(INPUT_SOURCE_PATH),
        selected_source_path=input_path,
    )
    conversation_id = f"{CONVERSATION_PREFIX}_{offset}_{count}"
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
    artifact_dir = Path("qa_artifacts")
    artifact_dir.mkdir(exist_ok=True)
    artifact = artifact_dir / (
        f"rag_test_text_window_utf8_offset{offset}_count{count}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    def write_artifact(*, completed: bool) -> None:
        quality = build_quality_status(
            turns,
            completed=completed,
            input_integrity=input_integrity,
        )
        artifact.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().isoformat(),
                    "script_path": _display_path(SCRIPT_PATH),
                    "input_source_path": _display_path(input_path),
                    "input_integrity": input_integrity,
                    "first_user_raw": selected[0],
                    "conversation_id": conversation_id,
                    "offset": offset,
                    "count": count,
                    "turn_timeout": turn_timeout,
                    "completed": completed,
                    "quality_status": quality,
                    "turns": turns,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    write_artifact(completed=False)
    try:
        for index, user_text in enumerate(selected, start=1):
            turns.append(
                await send_turn(
                    fake,
                    conversation_id=conversation_id,
                    turn_index=index,
                    user_text=user_text,
                    turn_timeout=turn_timeout,
                )
            )
            write_artifact(completed=False)
    finally:
        main.wecom_kf = originals["wecom_kf"]
        main.wecom_kf_context_store = originals["wecom_kf_context_store"]
        main.kf_turn_tasks.clear()
        main.kf_turn_tasks.update(originals["kf_turn_tasks"])
        main.kf_turn_generations.clear()
        main.kf_turn_generations.update(originals["kf_turn_generations"])
        main.kf_turn_pending_messages.clear()
        main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])

    write_artifact(completed=len(turns) == len(selected) and not any(turn.get("error") for turn in turns))
    return artifact


def print_summary(artifact: Path) -> None:
    data = json.loads(artifact.read_text(encoding="utf-8"))
    integrity = data["input_integrity"]
    print(f"ARTIFACT {artifact}")
    print(
        "INPUT_INTEGRITY "
        f"full={integrity['full']['passed']} window={integrity['window']['passed']} "
        f"first={data['first_user_raw']}"
    )
    quality = data.get("quality_status") or {}
    print(
        "QUALITY "
        f"passed={quality.get('passed')} "
        f"infrastructure_error={quality.get('infrastructure_error')} "
        f"exit_code={quality.get('exit_code')}"
    )
    for turn in data["turns"]:
        bot = turn["bot"]
        text = " | ".join(item.replace("\n", " / ") for item in bot.get("texts", []))
        print(f"\nR{turn['turn']} 用户: {turn['user']}")
        print(f"R{turn['turn']} 机器人: {text[:1000]}")
        timings = ", ".join(
            f"{item.get('stage')}={item.get('elapsed_sec')}"
            for item in turn.get("stage_timings", [])
        )
        print(
            f"R{turn['turn']} 动作: image={bot.get('image_count')} "
            f"video={bot.get('video_count')} elapsed={turn['elapsed_sec']} error={turn['error']}"
        )
        if timings:
            print(f"R{turn['turn']} 链路耗时: {timings}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--input", type=Path, default=INPUT_SOURCE_PATH)
    parser.add_argument("--turn-timeout", type=float, default=90)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    output = asyncio.run(
        run_window(
            offset=args.offset,
            count=args.count,
            input_path=args.input,
            turn_timeout=args.turn_timeout,
        )
    )
    print_summary(output)
    output_data = json.loads(output.read_text(encoding="utf-8"))
    raise SystemExit(int((output_data.get("quality_status") or {}).get("exit_code") or 0))
