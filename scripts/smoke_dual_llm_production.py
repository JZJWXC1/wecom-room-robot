from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# This smoke defaults to a pure offline contract path.  Do not import app.config
# or ReplyGenerator here; app.config may read .env as soon as it is imported.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("KF_DUAL_LLM_MODE", "production")

from app.services.kf_dual_llm_production import (  # noqa: E402
    compose_production_outbound_package,
    package_log_payload,
    package_passed,
    package_retry_reason,
    production_enabled,
)
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow  # noqa: E402


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


class FakeReplyGenerator:
    def __init__(self) -> None:
        self.llm2_call_count = 0

    async def compose_kf_outbound_production(self, **kwargs: Any) -> dict[str, Any]:
        self.llm2_call_count += 1
        task_packet = kwargs.get("task_packet") if isinstance(kwargs.get("task_packet"), dict) else {}
        tasks = task_packet.get("tasks") if isinstance(task_packet.get("tasks"), list) else []
        first_task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
        task_id = str(first_task.get("task_id") or "task-1-reply")
        return {
            "reply_text": "offline contract smoke reply",
            "answered_task_ids": [task_id],
            "claims": [],
            "action_captions": [],
            "self_review": {
                "status": "pass",
                "source": "fake_llm_offline_contract",
                "llm2_decides_media_targets": False,
            },
            "source": "fake_llm_offline_contract",
        }


def _current_mode() -> str:
    return str(os.environ.get("KF_DUAL_LLM_MODE") or "").strip().lower()


def _offline_task_packet(content: str):
    return build_kf_task_packet_shadow(
        {
            "rewritten_query": content,
            "response_strategy": {"mode": "answer"},
            "task_atoms": [
                {
                    "task_id": "task-1-reply",
                    "task_type": "reply_text",
                    "user_text": content,
                    "constraint_operation": "inherit",
                    "constraints": {},
                    "required_tools": ["reply.compose"],
                }
            ],
            "tool_plan": {
                "actions": ["generate_reply"],
                "required_tools": ["reply.compose"],
                "need_rewrite_clarification": False,
                "source": "offline_contract_smoke",
            },
        },
        content=content,
        raw_dialog_context=[],
        structured_memory={},
        inventory_index={"schema_version": "production_smoke.offline.v1", "row_count": 0, "room_index": []},
        candidate_set={},
        legacy_rewrite={},
        legacy_planner={},
        conversation_id="production-smoke",
        turn_id="production-smoke-turn",
        case_id="production-smoke-case",
        source_label="llm1_production_offline_contract",
        mode="production",
    ).packet


async def _run_offline_smoke() -> int:
    mode = _current_mode()
    if not production_enabled(mode):
        _print({"ok": False, "stage": "precheck", "reason": "KF_DUAL_LLM_MODE is not production", "mode": mode})
        return 2

    generator = FakeReplyGenerator()
    content = "offline hello"
    try:
        task_packet = _offline_task_packet(content)
    except Exception as exc:
        _print({"ok": False, "stage": "llm1", "error_type": type(exc).__name__})
        return 3

    packet_payload = task_packet.to_safe_dict()
    if not packet_payload.get("tasks"):
        _print({"ok": False, "stage": "llm1", "reason": "empty_task_packet"})
        return 4

    draft_reply = "offline contract smoke reply"
    try:
        package = await asyncio.wait_for(
            compose_production_outbound_package(
                reply_generator=generator,
                task_packet=task_packet,
                tool_evidence={
                    "actions": ["generate_reply"],
                    "rule_evidence": {"greeting": ["production_smoke_safe_greeting"]},
                    "suppress_actions": False,
                },
                draft_reply=draft_reply,
                planner_result={"actions": ["generate_reply"], "source": "production_smoke"},
                reply_result={"reply": draft_reply, "reply_source": "production_smoke"},
            ),
            timeout=30,
        )
    except Exception as exc:
        _print({"ok": False, "stage": "llm2", "error_type": type(exc).__name__})
        return 5

    log_payload = package_log_payload(package)
    send_action_count = len(package.send_actions)
    result = {
        "ok": bool(package_passed(package) and str(package.reply_text or "").strip() and send_action_count == 0),
        "stage": "llm2",
        "mode": mode,
        "offline": True,
        "env_file_read": False,
        "fake_llm": True,
        "contract_only": True,
        "send_transport_invoked": False,
        "llm_transport_invoked": False,
        "llm1_task_count": len(packet_payload.get("tasks") or []),
        "llm2_call_count": generator.llm2_call_count,
        "reply_source": package.reply_source,
        "reply_text_present": bool(str(package.reply_text or "").strip()),
        "send_action_count": send_action_count,
        "self_review": log_payload.get("self_review"),
    }
    if not result["ok"]:
        result["retry_reason"] = package_retry_reason(package)
        if send_action_count:
            result["send_guard_reason"] = "production smoke is contract-only and must not produce send actions"
    _print(result)
    return 0 if result["ok"] else 6


async def _run_live_smoke() -> int:
    from app.config import settings  # noqa: PLC0415
    from app.services.llm import ReplyGenerator  # noqa: PLC0415

    mode = str(getattr(settings, "kf_dual_llm_mode", "") or "").strip().lower()
    if not production_enabled(mode):
        _print({"ok": False, "stage": "precheck", "reason": "KF_DUAL_LLM_MODE is not production", "mode": mode})
        return 2

    generator = ReplyGenerator()
    content = "live production smoke hello"
    try:
        task_packet = await asyncio.wait_for(
            generator.build_kf_task_packet(
                content=content,
                raw_dialog_context=[],
                structured_memory={},
                inventory_index={"schema_version": "production_smoke.live.v1", "row_count": 0, "room_index": []},
                candidate_set={},
                legacy_rewrite={},
                legacy_planner={},
                conversation_id="production-smoke",
                turn_id="production-smoke-turn",
                case_id="production-smoke-case",
                mode="production",
            ),
            timeout=30,
        )
    except Exception as exc:
        _print({"ok": False, "stage": "llm1", "live": True, "error_type": type(exc).__name__})
        return 3

    try:
        package = await asyncio.wait_for(
            compose_production_outbound_package(
                reply_generator=generator,
                task_packet=task_packet,
                tool_evidence={"actions": ["generate_reply"], "suppress_actions": False},
                draft_reply="live production smoke reply",
                planner_result={"actions": ["generate_reply"], "source": "production_smoke_live"},
                reply_result={"reply": "live production smoke reply", "reply_source": "production_smoke_live"},
            ),
            timeout=30,
        )
    except Exception as exc:
        _print({"ok": False, "stage": "llm2", "live": True, "error_type": type(exc).__name__})
        return 5

    send_action_count = len(package.send_actions)
    result = {
        "ok": bool(package_passed(package) and str(package.reply_text or "").strip() and send_action_count == 0),
        "stage": "llm2",
        "mode": mode,
        "offline": False,
        "contract_only": True,
        "send_transport_invoked": False,
        "llm_transport_invoked": True,
        "reply_source": package.reply_source,
        "reply_text_present": bool(str(package.reply_text or "").strip()),
        "send_action_count": send_action_count,
        "self_review": package_log_payload(package).get("self_review"),
    }
    if not result["ok"]:
        result["retry_reason"] = package_retry_reason(package)
    _print(result)
    return 0 if result["ok"] else 6


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a safe dual-LLM production smoke without sending messages.")
    parser.add_argument(
        "--allow-live-llm",
        action="store_true",
        help="Opt in to the real LLM smoke. Default is a deterministic offline contract smoke.",
    )
    args = parser.parse_args()
    if args.allow_live_llm:
        return asyncio.run(_run_live_smoke())
    return asyncio.run(_run_offline_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
