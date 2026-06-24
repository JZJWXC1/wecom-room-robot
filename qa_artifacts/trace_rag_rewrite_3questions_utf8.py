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
from qa_artifacts.run_rag_3questions_10turns_utf8 import (
    CONVERSATION_ID,
    TURNS,
    CaptureWeComKf,
    MemoryContextStore,
    assert_utf8_inputs,
    send_turn,
)


def _clip(value: Any, limit: int = 1600) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_clip(item, limit=limit) for item in value[:8]]
    if isinstance(value, dict):
        return {str(key): _clip(item, limit=limit) for key, item in value.items()}
    return value


class TraceReplyGenerator:
    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.rewrite_calls: list[dict[str, Any]] = []
        self.stage2_calls: list[dict[str, Any]] = []
        self.selfcheck_calls: list[dict[str, Any]] = []

    async def rewrite_kf_message(self, **kwargs: Any) -> dict[str, Any]:
        result = await self.wrapped.rewrite_kf_message(**kwargs)
        memory = kwargs.get("structured_memory") or {}
        self.rewrite_calls.append(
            {
                "content": kwargs.get("content", ""),
                "has_structured_memory": bool(memory),
                "memory_keys": sorted(memory.keys()) if isinstance(memory, dict) else [],
                "raw_dialog_context": _clip(memory.get("raw_dialog_context", []), 1000)
                if isinstance(memory, dict)
                else [],
                "last_turn_record": _clip(memory.get("last_turn_record", {}), 1000)
                if isinstance(memory, dict)
                else {},
                "recent_turn_records": _clip(memory.get("recent_turn_records", []), 1000)
                if isinstance(memory, dict)
                else [],
                "last_candidate_set": _clip(memory.get("last_candidate_set", {}), 1000)
                if isinstance(memory, dict)
                else {},
                "confirmed_room": _clip(memory.get("confirmed_room", {}), 1000)
                if isinstance(memory, dict)
                else {},
                "inventory_index_summary": {
                    "keys": sorted((kwargs.get("inventory_index") or {}).keys())
                    if isinstance(kwargs.get("inventory_index"), dict)
                    else [],
                    "row_count": (kwargs.get("inventory_index") or {}).get("row_count")
                    if isinstance(kwargs.get("inventory_index"), dict)
                    else None,
                    "areas": _clip((kwargs.get("inventory_index") or {}).get("areas", []), 500)
                    if isinstance(kwargs.get("inventory_index"), dict)
                    else [],
                },
                "output": _clip(
                    {
                        "intent": result.get("intent"),
                        "intent_confidence": result.get("intent_confidence"),
                        "rewritten_query": result.get("rewritten_query"),
                        "effective_query": result.get("effective_query"),
                        "query_state": result.get("query_state"),
                        "context_reference": result.get("context_reference"),
                        "candidate_action": result.get("candidate_action"),
                        "selected_indices": result.get("selected_indices"),
                        "needs_clarification": result.get("needs_clarification"),
                        "clarification_text": result.get("clarification_text"),
                        "tool_plan": result.get("tool_plan"),
                    },
                    1200,
                ),
            }
        )
        return result

    async def plan_kf_reply_text(self, **kwargs: Any) -> dict[str, Any]:
        result = await self.wrapped.plan_kf_reply_text(**kwargs)
        self.stage2_calls.append(
            {
                "content": kwargs.get("content", ""),
                "structured_task": _clip(kwargs.get("structured_task", {}), 1200),
                "entity_resolution": _clip(kwargs.get("entity_resolution", {}), 1200),
                "constraint_proof": _clip(kwargs.get("constraint_proof", {}), 1200),
                "planner_result": _clip(kwargs.get("planner_result", {}), 1200),
                "tool_evidence": _clip(kwargs.get("tool_evidence", {}), 1200),
                "planner_retry_reason": _clip(kwargs.get("planner_retry_reason", ""), 1000),
                "output": _clip(result, 1200),
            }
        )
        return result

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        return await self.wrapped.generate(*args, **kwargs)

    async def assess_kf_final_reply(self, **kwargs: Any) -> dict[str, Any]:
        result = await self.wrapped.assess_kf_final_reply(**kwargs)
        self.selfcheck_calls.append(
            {
                "content": kwargs.get("content", ""),
                "raw_dialog_context": _clip(kwargs.get("raw_dialog_context", []), 1000),
                "constraint_proof": _clip(kwargs.get("constraint_proof", {}), 1000),
                "draft_reply": _clip(kwargs.get("draft_reply", ""), 1000),
                "output": _clip(result, 1000),
            }
        )
        return result


async def run() -> Path:
    assert_utf8_inputs()
    fake = CaptureWeComKf()
    store = MemoryContextStore()
    trace_generator = TraceReplyGenerator(main.reply_generator)
    originals = {
        "reply_generator": main.reply_generator,
        "wecom_kf": main.wecom_kf,
        "wecom_kf_context_store": main.wecom_kf_context_store,
        "kf_turn_tasks": dict(main.kf_turn_tasks),
        "kf_turn_generations": dict(main.kf_turn_generations),
        "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
    }
    main.reply_generator = trace_generator
    main.wecom_kf = fake
    main.wecom_kf_context_store = store
    main.kf_turn_tasks.clear()
    main.kf_turn_generations.clear()
    main.kf_turn_pending_messages.clear()
    turns: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    try:
        for index, user_text in enumerate(TURNS, start=1):
            before_rewrites = len(trace_generator.rewrite_calls)
            before_stage2 = len(trace_generator.stage2_calls)
            before_selfchecks = len(trace_generator.selfcheck_calls)
            turn = await send_turn(fake, index, user_text)
            turns.append(turn)
            context = copy.deepcopy(store.data.get(f"kf_sim:{CONVERSATION_ID}") or {})
            snapshots.append(
                {
                    "turn": index,
                    "user": user_text,
                    "rewrite_calls": trace_generator.rewrite_calls[before_rewrites:],
                    "stage2_calls": trace_generator.stage2_calls[before_stage2:],
                    "selfcheck_calls": trace_generator.selfcheck_calls[before_selfchecks:],
                    "structured_memory": _clip(context.get("structured_memory", {}), 1500),
                    "last_candidate_set": _clip(context.get("last_candidate_set", {}), 1500),
                    "confirmed_room": _clip(context.get("confirmed_room", {}), 1000),
                }
            )
    finally:
        main.reply_generator = originals["reply_generator"]
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
    artifact = artifact_dir / f"rag_3questions_rewrite_trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    artifact.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(),
                "conversation_id": CONVERSATION_ID,
                "turns": turns,
                "trace": snapshots,
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
    for item in data["trace"]:
        rewrite = item["rewrite_calls"][0] if item["rewrite_calls"] else {}
        output = rewrite.get("output") or {}
        memory = rewrite.get("last_candidate_set") or {}
        print(f"\nR{item['turn']} 用户: {item['user']}")
        print(f"  调用黑匣子: {rewrite.get('has_structured_memory')} keys={rewrite.get('memory_keys')}")
        print(f"  黑匣子候选: shown={memory.get('shown_count')} total={memory.get('total_count')} query={memory.get('query')}")
        print(f"  重写输出: intent={output.get('intent')} effective={output.get('effective_query') or output.get('rewritten_query')}")
        print(f"  query_state={output.get('query_state')}")
        print(f"  clarification={output.get('needs_clarification')} {output.get('clarification_text')}")
        if item["plan_calls"]:
            plan = item["plan_calls"][0]
            print(f"  Planner输入content: {plan.get('content')}")
            print(f"  Planner输出: {plan.get('output')}")


if __name__ == "__main__":
    output = asyncio.run(run())
    print_summary(output)
