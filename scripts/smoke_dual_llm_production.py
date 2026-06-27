from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.services.kf_dual_llm_production import (  # noqa: E402
    compose_production_outbound_package,
    package_log_payload,
    package_passed,
    package_retry_reason,
    production_enabled,
)
from app.services.llm import ReplyGenerator  # noqa: E402


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


async def _run_smoke() -> int:
    mode = str(getattr(settings, "kf_dual_llm_mode", "") or "").strip().lower()
    if not production_enabled(mode):
        _print({"ok": False, "stage": "precheck", "reason": "KF_DUAL_LLM_MODE is not production", "mode": mode})
        return 2

    generator = ReplyGenerator()
    content = "你好，想先了解一下还有哪些房源可以看。"
    try:
        task_packet = await asyncio.wait_for(
            generator.build_kf_task_packet(
                content=content,
                raw_dialog_context=[],
                structured_memory={},
                inventory_index={"schema_version": "production_smoke.v1", "row_count": 0, "room_index": []},
                candidate_set={},
                legacy_rewrite={
                    "intent": "general",
                    "rewritten_query": content,
                    "effective_query": content,
                    "tool_plan": {"actions": ["generate_reply"], "source": "production_smoke"},
                },
                legacy_planner={"actions": ["generate_reply"], "source": "production_smoke"},
                conversation_id="production-smoke",
                turn_id="production-smoke-turn",
                case_id="production-smoke-case",
                mode="production",
            ),
            timeout=30,
        )
    except Exception as exc:
        _print({"ok": False, "stage": "llm1", "error_type": type(exc).__name__})
        return 3

    packet_payload = task_packet.to_safe_dict()
    if not packet_payload.get("tasks"):
        _print({"ok": False, "stage": "llm1", "reason": "empty_task_packet"})
        return 4

    draft_reply = "我在的，你可以直接告诉我小区、预算、户型、房号，或者说要视频/房源表。"
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
    result = {
        "ok": bool(package_passed(package) and str(package.reply_text or "").strip()),
        "stage": "llm2",
        "mode": mode,
        "llm1_task_count": len(packet_payload.get("tasks") or []),
        "reply_source": package.reply_source,
        "reply_text_present": bool(str(package.reply_text or "").strip()),
        "send_action_count": len(package.send_actions),
        "self_review": log_payload.get("self_review"),
    }
    if not result["ok"]:
        result["retry_reason"] = package_retry_reason(package)
    _print(result)
    return 0 if result["ok"] else 6


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a safe dual-LLM production smoke without sending messages.")
    parser.parse_args()
    return asyncio.run(_run_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
