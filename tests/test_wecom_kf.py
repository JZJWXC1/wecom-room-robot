import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

import app.main as main
from app.config import Settings
from app.models import ReplyPlan
from app.services import inventory_read_turn, kf_context_memory
from app.services.inventory_snapshot_models import generate_listing_id
from app.services.kf_llm1_task_packet import build_kf_task_packet_shadow
from app.services.media_manifest import (
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    MediaItem,
    MediaManifest,
    MediaManifestProductionAdapter,
)
from app.services.rewrite_inventory_index import FIELD_SEMANTICS, build_rewrite_inventory_index
from app.services.wecom_kf import (
    WeComKfClient,
    WeComKfContextStore,
    WeComKfStateStore,
    extract_kf_external_userid,
    extract_kf_open_kfid,
    extract_kf_text,
    extract_kf_welcome_code,
    is_kf_enter_session_event,
    is_kf_message_event,
    kf_callback_payload_event_message,
    should_auto_reply_kf_message,
    _raise_for_status_sanitized,
)


def _write_manifest_evidence_for_send(root: Path, listing_id: str, path: Path, media_kind: str) -> tuple[MediaManifest, dict]:
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = MediaManifest.build(
        listing_ids=[listing_id],
        items=[
            MediaItem(
                listing_id=listing_id,
                kind=media_kind,
                file_name=path.name,
                relative_path=path.relative_to(root).as_posix(),
                sha256=sha256,
                binding_method="listing_id",
                confidence=1.0,
                ambiguity=False,
                candidate_only=False,
                access_verified=True,
            )
        ],
        generated_at="2026-06-27T08:00:00Z",
    )
    manifest.write_json(root / "media_manifest.json")
    evidence = MediaManifestProductionAdapter.from_path(root / "media_manifest.json", local_root=root).evidence_for_listing(listing_id)
    assert len(evidence) == 1
    return manifest, evidence[0].to_dict()


def test_constraint_selfcheck_does_not_force_search_terms_for_contract_followup() -> None:
    result = main._constraint_consistency_selfcheck(
        content="客户看中了怎么定房，合同怎么弄？",
        draft_reply=(
            "客户看中了就让他联系 18758141785 / 13282125992 / 19941091943 定房，"
            "定金、签电子合同和具体入住时间都让这几个号码确认。"
        ),
        understanding={
            "intent": "contract",
            "constraint_proof": {
                "area": "东新园\n杭氧\n新天地",
                "budget_range": [4000, 5000],
                "layout": "两室一厅",
            },
            "structured_task": {"tool_requirements": {"needs_contract_contact": True}},
        },
        tool_evidence={
            "actions": ["search_inventory", "compact_listing", "send_contract_contact", "generate_reply"],
            "rule_evidence": {"contract_contact": ["18758141785", "13282125992", "19941091943"]},
            "inventory_rows": [{"小区": "长浜龙吟轩", "房号": "11-1603", "户型": "两室一厅"}],
        },
    )

    assert result["status"] == "pass"


def test_tool_evidence_rows_can_enrich_legacy_rows_with_stable_listing_id() -> None:
    community = "\u68e0\u6da6\u5e9c"
    room_no = "15-2-801B"
    row = {"\u5c0f\u533a": community, "\u623f\u53f7": room_no}

    enriched = main._rows_with_listing_ids([row])

    assert enriched[0]["listing_id"] == generate_listing_id(community, room_no)
    assert "listing_id" not in row


def test_dual_llm_production_timeout_config_defaults() -> None:
    fields = Settings.model_fields

    assert fields["kf_llm1_production_timeout_seconds"].default == 12.0
    assert fields["kf_llm2_production_timeout_seconds"].default == 15.0


def test_llm1_production_timeout_metadata_uses_config_and_stays_sanitized(monkeypatch) -> None:
    async def run_case() -> None:
        sensitive_text = "客户原文 sk-test-secret token=abc123 https://example.test/base?api_key=abc123"

        class SlowReplyGenerator:
            async def build_kf_task_packet(self, **kwargs):
                await asyncio.sleep(0.05)
                raise AssertionError("slow LLM1 should have timed out first")

        monkeypatch.setattr(main, "reply_generator", SlowReplyGenerator())
        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        monkeypatch.setattr(main.settings, "kf_llm1_production_timeout_seconds", 0.01)
        monkeypatch.setattr(main.settings, "llm_rewrite_provider", "dashscope")
        monkeypatch.setattr(main.settings, "dashscope_rewrite_model", "rewrite-timeout-model")

        result = await main._apply_llm1_production_task_packet(
            content=sensitive_text,
            context={"conversation_id": "conv-1"},
            result={
                "structured_task": {},
                "planner_feedback": {"reason": "retry"},
            },
            rewrite_view={"raw_dialog_context": [{"content": sensitive_text}]},
            inventory_index={},
            inventory_read_context=SimpleNamespace(
                request_id="req-1",
                turn_id="turn-1",
                decision_id="case-1",
                snapshot_id="snapshot-1",
            ),
        )

        llm1_meta = result["dual_llm_production"]["llm1"]
        dumped = json.dumps(llm1_meta, ensure_ascii=False, sort_keys=True)

        assert result["tool_plan"]["need_rewrite_clarification"] is True
        assert llm1_meta["stage"] == "llm1"
        assert llm1_meta["mode"] == "production"
        assert llm1_meta["prompt_version"] == "dual_llm_production.llm1_task_packet.v1"
        assert llm1_meta["status"] == "retry"
        assert llm1_meta["timeout_seconds"] == 0.01
        assert llm1_meta["error_type"] == "TimeoutError"
        assert isinstance(llm1_meta["elapsed_ms"], int)
        assert llm1_meta["provider"] == "dashscope"
        assert llm1_meta["model"] == "rewrite-timeout-model"
        assert "sk-test-secret" not in dumped
        assert "token=abc123" not in dumped
        assert "api_key=abc123" not in dumped
        assert sensitive_text not in dumped

    asyncio.run(run_case())


def test_llm2_production_timeout_metadata_uses_config_and_stays_sanitized(monkeypatch) -> None:
    async def run_case() -> None:
        sensitive_text = "客户原文 sk-test-secret token=abc123 https://example.test/base?api_key=abc123"
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "plain answer",
                "task_atoms": [{"task_id": "task-1", "task_type": "reply_text"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="hello",
            source_label="llm1_production",
            mode="production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                raise AssertionError("LLM2 timeout must gate before final selfcheck")

        class SlowReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                await asyncio.sleep(0.05)
                raise AssertionError("slow LLM2 should have timed out first")

        tool_evidence = {
            "actions": ["generate_reply"],
            "dual_llm_production": {"llm1": {"status": "pass", "source": "test"}},
        }
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", SlowReplyGenerator())
        monkeypatch.setattr(main, "_needs_llm_final_selfcheck", lambda **kwargs: False)
        monkeypatch.setattr(main.settings, "kf_dual_llm_mode", "production")
        monkeypatch.setattr(main.settings, "kf_llm2_production_timeout_seconds", 0.01)
        monkeypatch.setattr(main.settings, "llm_reply_provider", "dashscope")
        monkeypatch.setattr(main.settings, "dashscope_reply_model", "reply-timeout-model")

        result = await main._generate_reply_result(
            content=sensitive_text,
            context=kf_context_memory.empty_context(),
            understanding={
                "intent": "general",
                "effective_query": "hello",
                "structured_task": {"tool_requirements": {}},
                "constraint_proof": {},
                "llm1_task_packet": task_packet,
            },
            tool_evidence=tool_evidence,
            planner_result={"actions": ["generate_reply"], "reply_text": "旧话术"},
        )

        llm2_meta = tool_evidence["dual_llm_production"]["llm2"]
        dumped = json.dumps(llm2_meta, ensure_ascii=False, sort_keys=True)

        assert result["reply"] == ""
        assert result["needs_planner_retry"] is True
        assert "LLM2 production outbound failed" in result["planner_retry_reason"]
        retry_payload = json.loads(result["planner_retry_reason"])
        retry_dump = json.dumps(retry_payload, ensure_ascii=False, sort_keys=True)
        assert retry_payload["source"] == "llm2_production_safe_retry_payload"
        assert "original_content" not in retry_payload
        assert "effective_query" not in retry_payload
        assert "draft_reply" not in retry_payload
        assert "llm2_production_outbound_package" not in tool_evidence
        assert llm2_meta["stage"] == "llm2"
        assert llm2_meta["mode"] == "production"
        assert llm2_meta["prompt_version"] == "kf_llm2_outbound.production.v1"
        assert llm2_meta["timeout_seconds"] == 0.01
        assert llm2_meta["error_type"] == "TimeoutError"
        assert isinstance(llm2_meta["elapsed_ms"], int)
        assert llm2_meta["provider"] == "dashscope"
        assert llm2_meta["model"] == "reply-timeout-model"
        assert llm2_meta["reply_text_present"] is False
        assert "sk-test-secret" not in dumped
        assert "token=abc123" not in dumped
        assert "api_key=abc123" not in dumped
        assert sensitive_text not in dumped
        assert "sk-test-secret" not in retry_dump
        assert "token=abc123" not in retry_dump
        assert "api_key=abc123" not in retry_dump
        assert sensitive_text not in retry_dump
        assert "旧话术" not in retry_dump

    asyncio.run(run_case())


def test_llm2_production_retry_payload_redacts_unknown_reason_and_intent() -> None:
    sensitive_text = "客户原文 sk-test-secret token=abc123 https://example.test/base?api_key=abc123"

    retry_payload = json.loads(
        main._llm2_production_retry_reason_payload(
            understanding={
                "intent": sensitive_text,
                "effective_query": sensitive_text,
                "llm1_task_packet": {
                    "task_atoms": [{"task_id": "task-1", "task_type": "reply_text", "user_text": sensitive_text}],
                    "tool_plan": {"actions": ["generate_reply"]},
                },
            },
            tool_evidence={
                "actions": ["generate_reply"],
                "dual_llm_production": {
                    "llm2": {
                        "stage": "llm2",
                        "mode": "production",
                        "self_review": {"status": "retry", "reason": sensitive_text},
                    }
                },
            },
            rule_selfcheck={"status": "retry", "source": "test", "reason": sensitive_text},
            llm_selfcheck={"status": "retry", "source": "test", "reason": sensitive_text},
            reason=sensitive_text,
        )
    )
    retry_dump = json.dumps(retry_payload, ensure_ascii=False, sort_keys=True)

    assert retry_payload["reason"] == "LLM2 production output gate requested planner retry."
    assert retry_payload["intent"] == "unknown"
    assert "original_content" not in retry_payload
    assert "effective_query" not in retry_payload
    assert "draft_reply" not in retry_payload
    assert "planner_result" not in retry_payload
    assert sensitive_text not in retry_dump
    assert "sk-test-secret" not in retry_dump
    assert "token=abc123" not in retry_dump
    assert "api_key=abc123" not in retry_dump


def test_generate_reply_result_uses_llm2_production_package(monkeypatch) -> None:
    async def run_case() -> None:
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "plain answer",
                "task_atoms": [{"task_id": "task-1", "task_type": "reply_text"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="hello",
            source_label="llm1_production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_text="")

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "我这边按证据给你回复。",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        original_mode = main.settings.kf_dual_llm_mode
        tool_evidence = {"actions": ["generate_reply"]}
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", FakeReplyGenerator())
        monkeypatch.setattr(main, "_needs_llm_final_selfcheck", lambda **kwargs: False)
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._generate_reply_result(
                content="hello",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "general",
                    "effective_query": "hello",
                    "structured_task": {"tool_requirements": {}},
                    "constraint_proof": {},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": ["generate_reply"], "reply_text": "旧话术"},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["reply"] == "我这边按证据给你回复。"
        assert result["selfcheck"]["status"] == "pass"
        assert tool_evidence["deterministic_reply_source"] == "kf_llm2_outbound_production"
        assert tool_evidence["dual_llm_production"]["llm2"]["reply_source"] == "kf_llm2_outbound_production"

    asyncio.run(run_case())


def test_generate_reply_result_production_llm2_bypasses_legacy_planner_reply_gate(monkeypatch) -> None:
    async def run_case() -> None:
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "plain answer",
                "task_atoms": [{"task_id": "task-1", "task_type": "reply_text"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content="hello",
            source_label="llm1_production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_text="")

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.llm2_called = False

            async def compose_kf_outbound_production(self, **kwargs):
                self.llm2_called = True
                return {
                    "reply_text": "我这边按证据给你回复。",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        fake_reply = FakeReplyGenerator()
        original_mode = main.settings.kf_dual_llm_mode
        tool_evidence = {"actions": ["generate_reply"]}
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", fake_reply)
        monkeypatch.setattr(main, "_needs_llm_final_selfcheck", lambda **kwargs: False)
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._generate_reply_result(
                content="hello",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "general",
                    "effective_query": "hello",
                    "structured_task": {"tool_requirements": {}},
                    "constraint_proof": {},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": ["generate_reply"]},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert fake_reply.llm2_called is True
        assert result["reply"] == "我这边按证据给你回复。"
        assert result["selfcheck"]["status"] == "pass"
        assert tool_evidence["deterministic_reply_source"] == "kf_llm2_outbound_production"

    asyncio.run(run_case())


def test_generate_reply_result_production_blocks_l2_validation_failure(monkeypatch) -> None:
    async def run_case() -> None:
        sensitive_text = "第1套视频 sk-test-secret token=abc123 https://example.test/base?api_key=abc123"
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "第1套视频",
                "task_atoms": [{"task_id": "task-video", "task_type": "send_video", "user_text": "第1套视频"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content=sensitive_text,
            source_label="llm1_production",
            mode="production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                raise AssertionError("L0-L2 validation failure must gate before final selfcheck")

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "我这边按证据给你回复。",
                    "answered_task_ids": ["task-video"],
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

            async def assess_kf_final_reply(self, **kwargs):
                raise AssertionError("validation failure must block before final LLM selfcheck")

        original_mode = main.settings.kf_dual_llm_mode
        tool_evidence = {"actions": ["generate_reply"]}
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", FakeReplyGenerator())
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._generate_reply_result(
                content=sensitive_text,
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": sensitive_text,
                    "structured_task": {"tool_requirements": {"needs_video": True}},
                    "constraint_proof": {"wants_video": True},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": ["generate_reply"], "reply_text": "旧话术 sk-test-secret token=abc123"},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["reply"] == ""
        assert result["needs_planner_retry"] is True
        assert result["selfcheck"]["rule"]["source"] == "llm2_production_output_gate"
        assert "kf_outbound_validation L0-L2 blocked" in result["planner_retry_reason"]
        retry_payload = json.loads(result["planner_retry_reason"])
        retry_dump = json.dumps(retry_payload, ensure_ascii=False, sort_keys=True)
        assert retry_payload["source"] == "llm2_production_safe_retry_payload"
        assert "original_content" not in retry_payload
        assert "effective_query" not in retry_payload
        assert "draft_reply" not in retry_payload
        assert "planner_result" not in retry_payload
        assert sensitive_text not in retry_dump
        assert "sk-test-secret" not in retry_dump
        assert "token=abc123" not in retry_dump
        assert "api_key=abc123" not in retry_dump
        assert "旧话术" not in retry_dump
        validation = tool_evidence["dual_llm_production"]["llm2"]["outbound_validation"]
        assert validation["status"] == "blocked"
        assert any(
            issue["code"] == "l2.task_not_answered"
            for issue in validation["issues"]
        )
        assert "llm2_production_outbound_package" not in tool_evidence

    asyncio.run(run_case())


def test_generate_reply_result_production_blocks_l3_validation_rewrite(monkeypatch) -> None:
    async def run_case() -> None:
        sensitive_text = "普通回复 sk-test-secret token=abc123 https://example.test/base?api_key=abc123"
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "普通回复",
                "task_atoms": [{"task_id": "task-reply", "task_type": "reply_text", "user_text": "普通回复"}],
                "tool_plan": {"actions": ["generate_reply"]},
            },
            content=sensitive_text,
            source_label="llm1_production",
            mode="production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                raise AssertionError("L3 rewrite gate must run before final selfcheck")

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "listing_id=lst-1，我这边按证据给你回复。",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

        original_mode = main.settings.kf_dual_llm_mode
        tool_evidence = {"actions": ["generate_reply"]}
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", FakeReplyGenerator())
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._generate_reply_result(
                content=sensitive_text,
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "general",
                    "effective_query": sensitive_text,
                    "structured_task": {"tool_requirements": {}},
                    "constraint_proof": {},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": ["generate_reply"], "reply_text": "旧话术 sk-test-secret token=abc123"},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["reply"] == ""
        assert result["needs_planner_retry"] is True
        assert "kf_outbound_validation L3 rewrite required" in result["planner_retry_reason"]
        retry_payload = json.loads(result["planner_retry_reason"])
        retry_dump = json.dumps(retry_payload, ensure_ascii=False, sort_keys=True)
        assert retry_payload["source"] == "llm2_production_safe_retry_payload"
        assert "original_content" not in retry_payload
        assert "effective_query" not in retry_payload
        assert "draft_reply" not in retry_payload
        assert "planner_result" not in retry_payload
        assert sensitive_text not in retry_dump
        assert "sk-test-secret" not in retry_dump
        assert "token=abc123" not in retry_dump
        assert "api_key=abc123" not in retry_dump
        assert "旧话术" not in retry_dump
        validation = tool_evidence["dual_llm_production"]["llm2"]["outbound_validation"]
        assert validation["status"] == "rewrite_required"
        assert validation["facts_passed"] is True
        assert validation["send_allowed"] is False
        assert any(issue["code"] == "l3.internal_name_leak" for issue in validation["issues"])
        assert "llm2_production_outbound_package" not in tool_evidence

    asyncio.run(run_case())


def test_controlled_password_reply_stays_out_of_internal_artifacts_and_final_llm(monkeypatch) -> None:
    async def run_case() -> None:
        password_canary = "432987#"
        task_packet = build_kf_task_packet_shadow(
            {
                "rewritten_query": "石桥铭苑6-1102密码多少",
                "task_atoms": [
                    {
                        "task_id": "task-password",
                        "task_type": "viewing_guidance",
                        "user_text": "石桥铭苑6-1102密码多少",
                    }
                ],
                "tool_plan": {"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
            },
            content="石桥铭苑6-1102密码多少",
            source_label="llm1_production",
            mode="production",
        ).packet.to_safe_dict()

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_text="")

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                return {
                    "reply_text": "",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

            async def assess_kf_final_reply(self, **kwargs):
                raise AssertionError("controlled password replies must not enter LLM final selfcheck")

        original_mode = main.settings.kf_dual_llm_mode
        tool_evidence = {
            "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
            "target_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": f"{password_canary} 看房提前联系"}],
            "inventory_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": f"{password_canary} 看房提前联系"}],
            "rule_evidence": {
                "viewing": {
                    "rooms": [
                        {
                            "room": "石桥铭苑6-1102",
                            "viewing": f"{password_canary} 看房提前联系",
                            "has_password": True,
                            "needs_contact": True,
                            "listing_id": "listing-viewing-canary",
                            "evidence_id": "evd-viewing-source-canary",
                        }
                    ],
                    "contact_numbers": list(main.CONTACT_NUMBERS),
                }
            },
        }
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", FakeReplyGenerator())
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._generate_reply_result(
                content="石桥铭苑6-1102密码多少",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "viewing",
                    "effective_query": "石桥铭苑6-1102密码多少",
                    "structured_task": {"intent": "viewing", "tool_requirements": {}},
                    "constraint_proof": {},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
            )
            artifact = main._build_orchestrator_shadow_artifact(
                content="石桥铭苑6-1102密码多少",
                open_kfid="kf",
                external_userid="wm",
                msgids=["msg-1"],
                generation=1,
                inventory_read_context=main._local_inventory_read_context("controlled-password"),
                understanding={"intent": "viewing", "llm1_task_packet": task_packet},
                planner_result={"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
                tool_evidence=tool_evidence,
                reply_result=result,
                final_reply=result["reply"],
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["reply"].strip()
        assert password_canary in result["reply"]
        assert result["selfcheck"]["llm"]["source"] == "llm_selfcheck_skipped_by_tiered_final_selfcheck"
        assert tool_evidence["dual_llm_production"]["llm2"]["outbound_validation"]["status"] == "pass"
        for payload in (
            tool_evidence["outbound_package"],
            main._tool_evidence_summary(tool_evidence),
            artifact,
        ):
            dumped = json.dumps(payload, ensure_ascii=False, default=str)
            assert password_canary not in dumped

    asyncio.run(run_case())


def test_production_controlled_channels_cover_contract_password_deposit_and_viewing(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="")

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_text="")

        class FakeReplyGenerator:
            async def compose_kf_outbound_production(self, **kwargs):
                task_types = {str(task.get("task_type") or "") for task in (kwargs.get("task_packet") or {}).get("tasks") or []}
                if "deposit_policy" in task_types:
                    return {
                        "reply_text": (
                            "免押走支付宝无忧住芝麻信用评估，不是免费免押；"
                            "服务费按租期一般是5.5%-8%。水电要按具体房源备注查，你把小区+房号发我，我再按那套确认。"
                        ),
                        "claims": [],
                        "action_captions": [],
                        "self_review": {"status": "pass"},
                    }
                return {
                    "reply_text": "",
                    "claims": [],
                    "action_captions": [],
                    "self_review": {"status": "pass"},
                }

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        async def run_production_case(*, content: str, task_type: str, actions: list[str], tool_evidence: dict, intent: str) -> dict:
            task_packet = build_kf_task_packet_shadow(
                {
                    "rewritten_query": content,
                    "task_atoms": [{"task_id": f"task-{task_type}", "task_type": task_type, "user_text": content}],
                    "tool_plan": {"actions": actions},
                },
                content=content,
                source_label="llm1_production",
            ).packet.to_safe_dict()
            return await main._generate_reply_result(
                content=content,
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": intent,
                    "effective_query": content,
                    "structured_task": {"intent": intent, "tool_requirements": {}},
                    "constraint_proof": {},
                    "llm1_task_packet": task_packet,
                },
                tool_evidence=tool_evidence,
                planner_result={"actions": actions},
            )

        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "agentic_rag", FakeRag())
        monkeypatch.setattr(main, "reply_generator", FakeReplyGenerator())
        monkeypatch.setattr(main, "_needs_llm_final_selfcheck", lambda **kwargs: False)
        main.settings.kf_dual_llm_mode = "production"
        try:
            contract_result = await run_production_case(
                content="客户看中了怎么定房？定金和合同怎么弄？",
                task_type="contract_contact",
                actions=["send_contract_contact", "generate_reply"],
                tool_evidence={"actions": ["send_contract_contact", "generate_reply"]},
                intent="contract",
            )
            password_result = await run_production_case(
                content="石桥铭苑6-1102密码发我",
                task_type="viewing_guidance",
                actions=["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                tool_evidence={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "target_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": "101004# 看房提前联系"}],
                    "inventory_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": "101004# 看房提前联系"}],
                    "rule_evidence": {
                        "viewing": {
                            "rooms": [
                                {
                                    "room": "石桥铭苑6-1102",
                                    "viewing": "101004# 看房提前联系",
                                    "has_password": True,
                                    "needs_contact": True,
                                    "listing_id": "listing-viewing-1",
                                    "evidence_id": "evd-viewing-source-1",
                                }
                            ],
                            "contact_numbers": list(main.CONTACT_NUMBERS),
                        }
                    },
                },
                intent="viewing",
            )
            deposit_result = await run_production_case(
                content="免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。",
                task_type="deposit_policy",
                actions=["send_deposit_policy", "generate_reply"],
                tool_evidence={
                    "actions": ["send_deposit_policy", "generate_reply"],
                    "rule_evidence": {"deposit_policy": main._deposit_policy_evidence()},
                },
                intent="deposit",
            )
            viewing_result = await run_production_case(
                content="这套还没空出的话能预约看房吗？",
                task_type="viewing_guidance",
                actions=["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                tool_evidence={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "target_rows": [{"小区": "长浜龙吟轩", "房号": "9-901", "看房方式密码": "6.24空出 看房提前联系"}],
                    "inventory_rows": [{"小区": "长浜龙吟轩", "房号": "9-901", "看房方式密码": "6.24空出 看房提前联系"}],
                    "rule_evidence": {
                        "viewing": {
                            "rooms": [
                                {
                                    "room": "长浜龙吟轩9-901",
                                    "viewing": "6.24空出 看房提前联系",
                                    "has_password": False,
                                    "needs_contact": True,
                                    "future_or_unavailable": True,
                                    "listing_id": "listing-viewing-2",
                                    "evidence_id": "evd-viewing-source-2",
                                }
                            ],
                            "contact_numbers": list(main.CONTACT_NUMBERS),
                        }
                    },
                },
                intent="viewing",
            )
            unasked_password_result = await run_production_case(
                content="石桥铭苑6-1102今天能看吗",
                task_type="viewing_guidance",
                actions=["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                tool_evidence={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "target_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": "101004# 看房提前联系"}],
                    "inventory_rows": [{"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": "101004# 看房提前联系"}],
                    "rule_evidence": {
                        "viewing": {
                            "rooms": [
                                {
                                    "room": "石桥铭苑6-1102",
                                    "viewing": "101004# 看房提前联系",
                                    "has_password": True,
                                    "needs_contact": True,
                                    "listing_id": "listing-viewing-3",
                                    "evidence_id": "evd-viewing-source-3",
                                }
                            ],
                            "contact_numbers": list(main.CONTACT_NUMBERS),
                        }
                    },
                },
                intent="viewing",
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        results = [contract_result, password_result, deposit_result, viewing_result, unasked_password_result]
        for result in results:
            assert result["reply"].strip(), result
            assert not result["needs_planner_retry"], result
            assert "我先帮您确认一下" not in result["reply"]
            assert "稍后给您准确回复" not in result["reply"]

        assert "18758141785" in contract_result["reply"]
        assert "电子合同" in contract_result["reply"]
        assert "定金" in contract_result["reply"]
        assert "101004#" in password_result["reply"]
        assert "18758141785" in password_result["reply"]
        assert "支付宝无忧住" in deposit_result["reply"]
        assert "5.5%-8%" in deposit_result["reply"]
        assert "水电" in deposit_result["reply"]
        assert "18758141785" in viewing_result["reply"]
        assert "101004#" not in unasked_password_result["reply"]

    asyncio.run(run_case())


def test_outbound_validation_allows_password_only_when_user_asked_and_evidence_bound() -> None:
    from app.services.kf_contracts import EvidenceItem, PreparedOutboundPackage, SendAction, StructuredTaskPacket, TaskAtom, ToolEvidenceBundle
    from app.services.kf_outbound_validation import OutboundValidationContext, ValidationStatus, validate_prepared_outbound_package

    task_packet = StructuredTaskPacket(
        tasks=[TaskAtom(task_id="task-password", task_type="viewing_guidance", user_text="石桥铭苑6-1102密码多少")]
    )
    evidence_bundle = ToolEvidenceBundle(
        evidence=[
            EvidenceItem(
                evidence_id="evd-controlled-viewing-password-1",
                evidence_type="viewing_password",
                summary="石桥铭苑6-1102 的看房密码已由受控通道绑定。",
                metadata={"controlled_channel": "viewing_password", "evidence_bound": True},
            )
        ]
    )
    package = PreparedOutboundPackage(
        reply_text="看房密码由受控通道发送。",
        evidence_bundle=evidence_bundle,
        send_actions=[
            SendAction(
                action_id="send-controlled-viewing-password-1",
                action_type="viewing_password",
                evidence_id="evd-controlled-viewing-password-1",
                metadata={"controlled_channel": "viewing_password", "evidence_bound": True},
                sensitive_payload={"viewing_password": "101004#"},
            )
        ],
    )

    allowed = validate_prepared_outbound_package(
        package,
        context=OutboundValidationContext(task_packet=task_packet, user_asked_password=True),
    )
    blocked = validate_prepared_outbound_package(
        package,
        context=OutboundValidationContext(task_packet=task_packet, user_asked_password=False),
    )
    unbound_package = package.model_copy(deep=True)
    unbound_package.send_actions[0].evidence_id = "evd-missing"
    unbound = validate_prepared_outbound_package(
        unbound_package,
        context=OutboundValidationContext(task_packet=task_packet, user_asked_password=True),
    )

    assert allowed.status == ValidationStatus.PASS
    assert any(issue.code == "l2.password_not_requested" for issue in blocked.issues)
    assert any(issue.code in {"l0.unknown_evidence_ref", "l2.password_not_evidence_bound"} for issue in unbound.issues)


def test_understand_message_production_skips_legacy_rewrite_and_fallback(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.task_packet_kwargs: dict = {}

            async def rewrite_kf_message(self, **kwargs):
                raise AssertionError("production LLM1 must not call legacy rewrite")

            async def build_kf_task_packet(self, **kwargs):
                self.task_packet_kwargs = kwargs
                return build_kf_task_packet_shadow(
                    {"rewritten_query": kwargs["content"], "tool_plan": {"actions": ["generate_reply"]}},
                    content=kwargs["content"],
                    source_label="llm1_production",
                    mode="production",
                ).packet

        async def fake_rows(*args, **kwargs):
            return []

        async def fake_index(*args, **kwargs):
            return {}

        async def fake_meta(*args, **kwargs):
            return {}

        def fail_fallback(*args, **kwargs):
            raise AssertionError("production LLM1 must not use deterministic fallback understanding")

        fake_reply = FakeReplyGenerator()
        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "reply_generator", fake_reply)
        monkeypatch.setattr(main, "_inventory_rows_for_resolution", fake_rows)
        monkeypatch.setattr(main, "_inventory_rewrite_index_for_read_context", fake_index)
        monkeypatch.setattr(main, "_inventory_metadata_for_read_context", fake_meta)
        monkeypatch.setattr(main, "_fallback_understanding", fail_fallback)
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._understand_message(
                content="你好",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("你好"),
                inventory_read_context=main._local_inventory_read_context("prod-llm1"),
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert fake_reply.task_packet_kwargs["content"] == "你好"
        assert result["tool_plan"]["retry_required"] is True
        assert result["dual_llm_production"]["llm1"]["status"] == "retry"
        assert result["structured_task"]["source"] == "llm1_production_minimal_bootstrap"

    asyncio.run(run_case())


def test_understand_message_production_routes_unverified_not_found_to_tools(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                raise AssertionError("production LLM1 must not call legacy rewrite")

            async def build_kf_task_packet(self, **kwargs):
                legacy_rewrite = kwargs["legacy_rewrite"]
                legacy_rewrite["intent"] = "inventory"
                legacy_rewrite["needs_clarification"] = True
                legacy_rewrite["clarification_text"] = "最新房源表里暂时没查到合峙悦府这个小区。你确认一下小区名。"
                legacy_rewrite["query_state"] = {"intent": "inventory"}
                return build_kf_task_packet_shadow(
                    {
                        "rewritten_query": kwargs["content"],
                        "tool_plan": {"actions": ["search_inventory", "generate_reply"]},
                    },
                    content=kwargs["content"],
                    source_label="llm1_production",
                    mode="production",
                ).packet

        async def fake_rows(*args, **kwargs):
            return []

        async def fake_index(*args, **kwargs):
            return {}

        async def fake_meta(*args, **kwargs):
            return {}

        fake_reply = FakeReplyGenerator()
        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "reply_generator", fake_reply)
        monkeypatch.setattr(main, "_inventory_rows_for_resolution", fake_rows)
        monkeypatch.setattr(main, "_inventory_rewrite_index_for_read_context", fake_index)
        monkeypatch.setattr(main, "_inventory_metadata_for_read_context", fake_meta)
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._understand_message(
                content="合峙悦府有没有在租房源？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("合峙悦府有没有在租房源？"),
                inventory_read_context=main._local_inventory_read_context("prod-llm1"),
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["needs_clarification"] is False
        assert result["clarification_text"] == ""
        assert result["rewrite_layer_not_found_claim_routed_to_tools"] is True
        assert result["query_state"]["needs_tool_verification"] is True
        assert result["structured_task"]["clarification"]["reason"] == "rewrite_layer_not_found_claim_routed_to_tools"

    asyncio.run(run_case())


def test_understand_message_production_early_return_routes_apply_result(monkeypatch) -> None:
    async def run_case() -> None:
        async def fake_apply_llm1_production_task_packet(**kwargs):
            result = kwargs["result"]
            result["intent"] = "inventory"
            result["needs_clarification"] = True
            result["clarification_text"] = "最新房源表里暂时没查到合峙悦府这个小区。你确认一下小区名。"
            result["query_state"] = {"intent": "inventory"}
            result.setdefault("structured_task", {})["clarification"] = {
                "needed": True,
                "text": result["clarification_text"],
            }
            return result

        async def fake_rows(*args, **kwargs):
            return []

        async def fake_index(*args, **kwargs):
            return {}

        async def fake_meta(*args, **kwargs):
            return {}

        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "_apply_llm1_production_task_packet", fake_apply_llm1_production_task_packet)
        monkeypatch.setattr(main, "_inventory_rows_for_resolution", fake_rows)
        monkeypatch.setattr(main, "_inventory_rewrite_index_for_read_context", fake_index)
        monkeypatch.setattr(main, "_inventory_metadata_for_read_context", fake_meta)
        main.settings.kf_dual_llm_mode = "production"
        try:
            result = await main._understand_message(
                content="合峙悦府有没有在租房源？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("合峙悦府有没有在租房源？"),
                inventory_read_context=main._local_inventory_read_context("prod-llm1"),
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert result["needs_clarification"] is False
        assert result["clarification_text"] == ""
        assert result["rewrite_layer_not_found_claim_routed_to_tools"] is True
        assert result["query_state"]["needs_tool_verification"] is True
        assert result["structured_task"]["clarification"]["reason"] == "rewrite_layer_not_found_claim_routed_to_tools"

    asyncio.run(run_case())


def test_orchestrator_artifact_uses_production_audit_in_production_mode(monkeypatch) -> None:
    def fail_shadow_builder(**kwargs):
        raise AssertionError("production mode must not build mode=shadow artifact")

    original_mode = main.settings.kf_dual_llm_mode
    monkeypatch.setattr(main.kf_orchestrator_shadow, "build_shadow_artifact", fail_shadow_builder)
    main.settings.kf_dual_llm_mode = "production"
    try:
        artifact = main._build_orchestrator_shadow_artifact(
            content="你好",
            open_kfid="kf",
            external_userid="wm",
            msgids=["msg-1"],
            generation=1,
            inventory_read_context=main._local_inventory_read_context("prod-audit"),
            understanding={
                "intent": "production_llm1",
                "llm1_task_packet": {"tasks": []},
                "legacy_unknown_fields": {"video_paths": ["C:/room_database/video/secret-room.mp4"]},
            },
            planner_result={"actions": [], "debug_paths": ["C:/room_database/video/secret-room.mp4"]},
            tool_evidence={
                "actions": ["send_video"],
                "video_paths": ["C:/room_database/video/secret-room.mp4"],
                "outbound_package": {"video_paths": ["C:/room_database/video/secret-room.mp4"]},
                "inventory_source_metadata": {
                    "row_count": 1,
                    "hash": "b" * 64,
                    "cache_path": "C:/room_database/cache/private-inventory.json",
                },
            },
            reply_result={
                "selfcheck": {"status": "retry"},
                "context": {"pending_video_sends": {"paths": ["C:/room_database/video/secret-room.mp4"]}},
            },
            final_reply="",
        )
    finally:
        main.settings.kf_dual_llm_mode = original_mode

    assert artifact["schema_version"] == "rag_v2_orchestrator_production_audit.v1"
    assert artifact["mode"] == "production"
    assert artifact["mode"] != "shadow"
    dumped = json.dumps(artifact, ensure_ascii=False)
    assert "secret-room.mp4" not in dumped
    assert "private-inventory.json" not in dumped
    assert "debug_paths" not in dumped
    assert artifact["tool_evidence_summary"]["video_count"] == 1
    assert artifact["tool_evidence_summary"]["outbound_package_present"] is True
    assert artifact["tool_evidence_summary"]["inventory_source_metadata"]["row_count"] == 1
    assert "cache_path" not in artifact["tool_evidence_summary"]["inventory_source_metadata"]


def test_plan_actions_production_llm1_retry_blocks_inventory_sheet_fallback(monkeypatch) -> None:
    async def run_case() -> None:
        original_mode = main.settings.kf_dual_llm_mode
        main.settings.kf_dual_llm_mode = "production"
        try:
            planner = await main._plan_actions(
                content="房源表发一下",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory_sheet",
                    "tool_plan": {
                        "actions": [],
                        "retry_required": True,
                        "missing_evidence": "LLM1 needs another pass",
                        "source": "llm1_production",
                    },
                    "dual_llm_production": {"llm1": {"status": "retry", "source": "llm1_production"}},
                },
                signals={"wants_inventory_sheet": True},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert planner["need_rewrite_clarification"] is True
        assert planner["actions"] == []
        assert "send_inventory_sheet" not in planner["actions"]

    asyncio.run(run_case())


def test_plan_actions_production_llm1_retry_blocks_deposit_fallback(monkeypatch) -> None:
    async def run_case() -> None:
        original_mode = main.settings.kf_dual_llm_mode
        main.settings.kf_dual_llm_mode = "production"
        try:
            planner = await main._plan_actions(
                content="免押是什么",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "deposit",
                    "tool_plan": {
                        "actions": [],
                        "need_rewrite_clarification": True,
                        "missing_evidence": "LLM1 retry gate",
                        "source": "llm1_production",
                    },
                    "dual_llm_production": {"llm1": {"status": "retry", "source": "llm1_production"}},
                },
                signals={"wants_deposit": True},
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert planner["need_rewrite_clarification"] is True
        assert planner["actions"] == []
        assert "send_deposit_policy" not in planner["actions"]

    asyncio.run(run_case())


def test_collect_room_media_production_uses_manifest_exact_listing(monkeypatch) -> None:
    async def run_case() -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "exact.mp4"
            video_path.write_bytes(b"video")
            video_sha = hashlib.sha256(b"video").hexdigest()
            manifest_source_hash = hashlib.sha256(b"manifest").hexdigest()
            listing_id = generate_listing_id("星河苑", "1-101")

            class FakeMediaStore:
                def media_manifest_evidence_for_listing(self, value: str):
                    assert value == listing_id
                    return [
                        {
                            "listing_id": listing_id,
                            "media_type": "video",
                            "kind": "video",
                            "media_id": "med_exact_video",
                            "evidence_id": "media_manifest:test:med_exact_video",
                            "source_hash": manifest_source_hash,
                            "sha256": video_sha,
                            "send_ready": True,
                            "candidate_only": False,
                            "ambiguity": False,
                            "binding_method": "listing_id",
                            "adapter_mode": "production_read",
                            "evidence_profile": "media_manifest.production_read.v1",
                            "local_path": str(video_path),
                        }
                    ]

                def list_room_database_videos(self, *args, **kwargs):
                    raise AssertionError("production media send must not use fuzzy video lookup")

                def list_room_database_images(self, *args, **kwargs):
                    raise AssertionError("production media send must not use fuzzy image lookup")

            original_mode = main.settings.kf_dual_llm_mode
            monkeypatch.setattr(main, "media_store", FakeMediaStore())
            main.settings.kf_dual_llm_mode = "production"
            try:
                paths, rows, missing, _sync = await main._collect_room_media(
                    [{"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}],
                    media_kind="video",
                )
            finally:
                main.settings.kf_dual_llm_mode = original_mode

            assert paths == [video_path]
            assert rows[0]["listing_id"] == listing_id
            assert missing == []
            assert _sync["source"] == "media_manifest"
            assert _sync["_media_manifest_evidence"][0]["source_hash"] == manifest_source_hash

    asyncio.run(run_case())


def test_collect_room_media_production_does_not_fallback_to_fuzzy(monkeypatch) -> None:
    async def run_case() -> None:
        listing_id = generate_listing_id("星河苑", "1-101")

        class FakeMediaStore:
            def media_manifest_evidence_for_listing(self, value: str):
                assert value == listing_id
                return []

            def list_room_database_videos(self, *args, **kwargs):
                raise AssertionError("production media send must not use fuzzy video lookup")

            def list_room_database_images(self, *args, **kwargs):
                raise AssertionError("production media send must not use fuzzy image lookup")

        class FailFeishuClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("production media send must not run on-demand Feishu sync")

        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "media_store", FakeMediaStore())
        monkeypatch.setattr(main, "FeishuClient", FailFeishuClient)
        main.settings.kf_dual_llm_mode = "production"
        try:
            paths, rows, missing, _sync = await main._collect_room_media(
                [{"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}],
                media_kind="video",
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert paths == []
        assert rows == []
        assert missing == ["星河苑1-101"]
        assert _sync["on_demand_sync"] == "skipped_in_production"

    asyncio.run(run_case())


def test_execute_tools_production_pending_video_does_not_send_stored_paths(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeMediaStore:
            def media_manifest_evidence_for_listing(self, value: str):
                return []

            def list_room_database_videos(self, *args, **kwargs):
                raise AssertionError("production pending video must not use fuzzy video lookup")

            def list_room_database_images(self, *args, **kwargs):
                raise AssertionError("production pending video must not use fuzzy image lookup")

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=["C:/room_database/video/stale-fuzzy.mp4"],
            labels=["星河苑1-101"],
            requested_count=1,
            sent_count=0,
        )
        original_mode = main.settings.kf_dual_llm_mode
        monkeypatch.setattr(main, "media_store", FakeMediaStore())
        main.settings.kf_dual_llm_mode = "production"
        try:
            evidence = await main._execute_tools(
                actions=["send_video"],
                content="继续发",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "继续发",
                    "constraint_proof": {"wants_video": True, "pending_video_action": "continue"},
                    "query_state": {"pending_video_action": "continue"},
                    "structured_task": {"intent": "media", "tool_requirements": {"needs_video": True}},
                },
            )
        finally:
            main.settings.kf_dual_llm_mode = original_mode

        assert evidence["video_paths"] == []
        assert evidence["media_status"]["video"]["sent_count"] == 0
        assert "stale-fuzzy.mp4" not in json.dumps(evidence, ensure_ascii=False)

    asyncio.run(run_case())


def test_send_videos_production_blocks_paths_without_manifest_evidence(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.videos: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

            def send_video(self, open_kfid: str, external_userid: str, path: Path) -> dict:
                self.videos.append(str(path))
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "wrong.mp4"
            video_path.write_bytes(b"video")
            listing_id = generate_listing_id("星河苑", "1-101")
            fake = FakeWeComKf()
            original_wecom = main.wecom_kf
            original_mode = main.settings.kf_dual_llm_mode
            main.wecom_kf = fake
            main.settings.kf_dual_llm_mode = "production"
            try:
                sent, context = await main._send_videos_with_receipts(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    paths=[str(video_path)],
                    rows=[{"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}],
                    tool_evidence={"video_paths": [str(video_path)], "video_rows": []},
                )
            finally:
                main.wecom_kf = original_wecom
                main.settings.kf_dual_llm_mode = original_mode

            assert fake.texts == []
            assert fake.videos == []
            assert sent == [{"type": "video_blocked", "path": str(video_path), "reason": "missing_media_manifest_evidence"}]
            receipt = context["send_receipts"]["receipts"][-1]
            assert receipt["status"] == "failed"
            assert receipt["metadata"]["blocked"] is True
            assert receipt["metadata"]["media_manifest_required"] is True

    asyncio.run(run_case())


def test_send_videos_production_uses_manifest_evidence_in_receipt(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.videos: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

            def send_video(self, open_kfid: str, external_userid: str, path: Path) -> dict:
                self.videos.append(str(path))
                return {"errcode": 0, "msgid": "video-1"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "room_database"
            listing_id = generate_listing_id("星河苑", "1-101")
            video_path = root / "video" / listing_id / "exact.mp4"
            video_path.parent.mkdir(parents=True)
            video_path.write_bytes(b"video")
            manifest, media_evidence = _write_manifest_evidence_for_send(root, listing_id, video_path, MEDIA_KIND_VIDEO)
            video_sha = media_evidence["sha256"]
            manifest_source_hash = manifest.source_hash
            row = {"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}
            fake = FakeWeComKf()
            original_wecom = main.wecom_kf
            original_mode = main.settings.kf_dual_llm_mode
            previous_room_database_path = main.settings.room_database_path
            main.wecom_kf = fake
            main.settings.kf_dual_llm_mode = "production"
            main.settings.room_database_path = root
            try:
                sent, context = await main._send_videos_with_receipts(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    paths=[str(video_path)],
                    rows=[row],
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [row],
                        "video_media_manifest_evidence": [media_evidence],
                    },
                )
            finally:
                main.wecom_kf = original_wecom
                main.settings.kf_dual_llm_mode = original_mode
                main.settings.room_database_path = previous_room_database_path

            assert fake.texts == ["这是星河苑1-101的视频。"]
            assert fake.videos == [str(video_path)]
            assert sent == [{"type": "video", "path": str(video_path), "room": "星河苑1-101", "count": 1}]
            receipt = context["send_receipts"]["receipts"][-1]
            assert receipt["status"] == "sent"
            assert receipt["listing_id"] == listing_id
            assert receipt["source_hash"] == manifest_source_hash
            assert receipt["sha256"] == video_sha
            assert receipt["metadata"]["media_evidence_profile"] == "media_manifest.production_read.v1"

    asyncio.run(run_case())


def test_send_images_production_blocks_paths_without_manifest_evidence(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.images: list[str] = []

            def send_image(self, open_kfid: str, external_userid: str, path: Path) -> dict:
                self.images.append(str(path))
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "wrong.jpg"
            image_path.write_bytes(b"image")
            listing_id = generate_listing_id("星河苑", "1-101")
            row = {"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}
            fake = FakeWeComKf()
            original_wecom = main.wecom_kf
            original_mode = main.settings.kf_dual_llm_mode
            main.wecom_kf = fake
            main.settings.kf_dual_llm_mode = "production"
            try:
                sent, context = await main._send_images_with_receipts(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    paths=[str(image_path)],
                    rows=[row],
                    tool_evidence={"image_paths": [str(image_path)], "image_rows": [row]},
                    require_media_manifest=True,
                )
            finally:
                main.wecom_kf = original_wecom
                main.settings.kf_dual_llm_mode = original_mode

            assert fake.images == []
            assert sent == [{"type": "image_blocked", "path": str(image_path), "reason": "missing_media_manifest_evidence"}]
            receipt = context["send_receipts"]["receipts"][-1]
            assert receipt["status"] == "failed"
            assert receipt["metadata"]["blocked"] is True
            assert receipt["metadata"]["media_manifest_required"] is True

    asyncio.run(run_case())


def test_send_images_production_uses_exact_manifest_evidence_in_receipt(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.images: list[str] = []

            def send_image(self, open_kfid: str, external_userid: str, path: Path) -> dict:
                self.images.append(str(path))
                return {"errcode": 0, "msgid": "image-1"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "room_database"
            listing_id = generate_listing_id("星河苑", "1-101")
            image_path = root / "images" / listing_id / "exact.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            manifest, media_evidence = _write_manifest_evidence_for_send(root, listing_id, image_path, MEDIA_KIND_IMAGE)
            row = {"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}
            fake = FakeWeComKf()
            original_wecom = main.wecom_kf
            original_mode = main.settings.kf_dual_llm_mode
            previous_room_database_path = main.settings.room_database_path
            main.wecom_kf = fake
            main.settings.kf_dual_llm_mode = "production"
            main.settings.room_database_path = root
            try:
                sent, context = await main._send_images_with_receipts(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    paths=[str(image_path)],
                    rows=[row],
                    tool_evidence={
                        "image_paths": [str(image_path)],
                        "image_rows": [row],
                        "image_media_manifest_evidence": [media_evidence],
                    },
                    require_media_manifest=True,
                )
            finally:
                main.wecom_kf = original_wecom
                main.settings.kf_dual_llm_mode = original_mode
                main.settings.room_database_path = previous_room_database_path

            assert fake.images == [str(image_path)]
            assert sent == [{"type": "image", "path": str(image_path), "count": 1}]
            receipt = context["send_receipts"]["receipts"][-1]
            assert receipt["status"] == "sent"
            assert receipt["listing_id"] == listing_id
            assert receipt["source_hash"] == manifest.source_hash
            assert receipt["sha256"] == media_evidence["sha256"]
            assert receipt["media_id"] == media_evidence["media_id"]
            assert receipt["metadata"]["media_evidence_profile"] == "media_manifest.production_read.v1"

    asyncio.run(run_case())


def test_send_images_production_blocks_manifest_source_hash_drift(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.images: list[str] = []

            def send_image(self, open_kfid: str, external_userid: str, path: Path) -> dict:
                self.images.append(str(path))
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "room_database"
            listing_id = generate_listing_id("星河苑", "1-101")
            image_path = root / "images" / listing_id / "exact.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            _manifest, media_evidence = _write_manifest_evidence_for_send(root, listing_id, image_path, MEDIA_KIND_IMAGE)
            manifest_path = root / "media_manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["source_hash"] = "0" * 64
            manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            row = {"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}
            fake = FakeWeComKf()
            original_wecom = main.wecom_kf
            original_mode = main.settings.kf_dual_llm_mode
            previous_room_database_path = main.settings.room_database_path
            main.wecom_kf = fake
            main.settings.kf_dual_llm_mode = "production"
            main.settings.room_database_path = root
            try:
                sent, context = await main._send_images_with_receipts(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    paths=[str(image_path)],
                    rows=[row],
                    tool_evidence={
                        "image_paths": [str(image_path)],
                        "image_rows": [row],
                        "image_media_manifest_evidence": [media_evidence],
                    },
                    require_media_manifest=True,
                )
            finally:
                main.wecom_kf = original_wecom
                main.settings.kf_dual_llm_mode = original_mode
                main.settings.room_database_path = previous_room_database_path

            assert fake.images == []
            assert sent == [{"type": "image_blocked", "path": str(image_path), "reason": "missing_media_manifest_evidence"}]
            receipt = context["send_receipts"]["receipts"][-1]
            assert receipt["status"] == "failed"
            assert receipt["metadata"]["blocked"] is True
            assert receipt["metadata"]["media_manifest_required"] is True

    asyncio.run(run_case())


def test_constraint_selfcheck_passes_when_tool_has_single_matching_inventory_row() -> None:
    result = main._constraint_consistency_selfcheck(
        content="万达1500左右一室有吗？",
        draft_reply="有的，合嵣悦府6-1-1204B还在，一室一厅，押一付一1500。",
        understanding={
            "intent": "inventory",
            "constraint_proof": {
                "area": "拱墅万达\n北部软件园\n城北万象城",
                "budget_range": [1300, 1700],
                "layout": "一室",
            },
            "structured_task": {"tool_requirements": {"needs_inventory_search": True}},
        },
        tool_evidence={
            "actions": ["search_inventory", "compact_listing", "generate_reply"],
            "inventory_rows": [
                {
                    "小区": "合嵣悦府",
                    "房号": "6-1-1204B",
                    "户型描述": "一室一厅",
                    "押一付一": "1500",
                    "押二付一": "1400",
                }
            ],
        },
    )

    assert result["status"] == "pass"


class InventoryReadRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_inventory_read_context_flows_through_rewrite_and_tools(self) -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            async def rewrite_kf_message(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "intent": "inventory",
                    "rewritten_query": "合幢悦府1204",
                    "effective_query": "合幢悦府1204",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            def cache_meta(self) -> dict:
                return {"status": "success", "hash": "m1d2a_fake_hash", "row_count": 1}

            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型描述": "一室一厅",
                        "押一付一": "1500",
                        "看房方式密码": "1234#",
                    }
                ]

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return await self.all_rows(limit=limit)

        fake_reply = FakeReplyGenerator()
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = fake_reply
        main.inventory = FakeInventory()
        try:
            read_context = main._local_inventory_read_context("m1d2a")
            context = kf_context_memory.empty_context()
            understanding = await main._understand_message(
                content="合幢悦府有哪些",
                context=context,
                signals=main._deterministic_signals("合幢悦府有哪些"),
                inventory_read_context=read_context,
            )
            evidence = await main._execute_tools(
                actions=["search_inventory", "compact_listing"],
                content="合幢悦府有哪些",
                context=context,
                understanding=understanding,
                inventory_read_context=read_context,
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(fake_reply.kwargs["inventory_index"]["row_count"], 1)
        self.assertEqual(evidence["inventory_read_context"]["decision_id"], read_context.decision_id)
        self.assertEqual(evidence["inventory_source_metadata"]["hash"], "m1d2a_fake_hash")
        self.assertEqual(evidence["inventory_rows"][0]["小区"], "合幢悦府")
        self.assertEqual(evidence["inventory_listing_evidence"][0]["source_kind"], "legacy")
        self.assertNotIn("看房方式密码", json.dumps(evidence["inventory_listing_evidence"], ensure_ascii=False))
        self.assertEqual(context["inventory_read_context"]["decision_id"], read_context.decision_id)

    def test_customer_shadow_context_stays_legacy_and_does_not_probe_snapshot(self) -> None:
        original_mode = main.settings.inventory_snapshot_mode
        main.settings.inventory_snapshot_mode = "shadow"
        try:
            read_context = main._create_inventory_read_context(
                prefix="kf",
                open_kfid="open-kf",
                external_userid="external-user",
                content="万达房源",
                msgids=["msg-1"],
                generation=1,
            )
        finally:
            main.settings.inventory_snapshot_mode = original_mode

        self.assertEqual(read_context.source_kind, "legacy")
        self.assertEqual(read_context.selection_mode, "shadow")
        legacy_health = read_context.health_at_selection["details"]["legacy"]
        self.assertTrue(read_context.source_hash)
        self.assertEqual(read_context.source_hash, legacy_health["details"]["source_hash"])
        self.assertEqual(read_context.health_at_selection["details"]["shadow_snapshot"]["status"], "not_queried")

    async def test_provider_failure_does_not_fallback_to_direct_rewrite_reads(self) -> None:
        class FakeInventory:
            def cache_meta(self) -> dict:
                return {"status": "success", "hash": "no_fallback_hash", "row_count": 0}

        async def fail_metadata(*args, **kwargs):
            raise RuntimeError("provider metadata failed")

        async def fail_rewrite_index(*args, **kwargs):
            raise RuntimeError("provider rewrite index failed")

        def fail_direct_metadata():
            raise AssertionError("direct metadata fallback must not run")

        def fail_direct_rewrite_index():
            raise AssertionError("direct rewrite index fallback must not run")

        def fail_write_rewrite_index(*args, **kwargs):
            raise AssertionError("direct rewrite index write fallback must not run")

        originals = {
            "inventory": main.inventory,
            "metadata_for_context": inventory_read_turn.metadata_for_context,
            "rewrite_index_for_context": inventory_read_turn.rewrite_index_for_context,
            "_inventory_cache_meta_for_prompt": main._inventory_cache_meta_for_prompt,
            "load_rewrite_inventory_index": main.load_rewrite_inventory_index,
            "write_rewrite_inventory_index": main.write_rewrite_inventory_index,
        }
        main.inventory = FakeInventory()
        read_context = main._local_inventory_read_context("no_fallback")
        inventory_read_turn.metadata_for_context = fail_metadata
        inventory_read_turn.rewrite_index_for_context = fail_rewrite_index
        main._inventory_cache_meta_for_prompt = fail_direct_metadata
        main.load_rewrite_inventory_index = fail_direct_rewrite_index
        main.write_rewrite_inventory_index = fail_write_rewrite_index
        try:
            cache_meta = await main._inventory_metadata_for_read_context(read_context)
            persisted_index = await main._inventory_rewrite_index_for_read_context(read_context)
            inventory_index = main._build_inventory_rewrite_index(
                content="合幢悦府有哪些",
                rows=[],
                signals={},
                persisted_index=persisted_index,
                cache_meta=cache_meta,
            )
        finally:
            main.inventory = originals["inventory"]
            inventory_read_turn.metadata_for_context = originals["metadata_for_context"]
            inventory_read_turn.rewrite_index_for_context = originals["rewrite_index_for_context"]
            main._inventory_cache_meta_for_prompt = originals["_inventory_cache_meta_for_prompt"]
            main.load_rewrite_inventory_index = originals["load_rewrite_inventory_index"]
            main.write_rewrite_inventory_index = originals["write_rewrite_inventory_index"]

        self.assertEqual(cache_meta, {})
        self.assertEqual(persisted_index, {})
        self.assertEqual(inventory_index["cache_meta"], {})
        self.assertEqual(inventory_index["row_count"], 0)

    async def test_process_text_turn_selects_router_once_and_reuses_decision_id(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.inventory_indexes: list[dict] = []

            async def rewrite_kf_message(self, **kwargs):
                self.inventory_indexes.append(kwargs["inventory_index"])
                return {
                    "intent": "inventory",
                    "rewritten_query": "合幢悦府有哪些",
                    "effective_query": "合幢悦府有哪些",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                    "tool_plan": {
                        "actions": ["search_inventory", "compact_listing", "generate_reply"],
                        "source": "test_tool_plan",
                    },
                }

        class FakeInventory:
            def __init__(self) -> None:
                self.search_calls: list[tuple[str, int]] = []
                self.all_rows_calls: list[dict] = []

            def cache_meta(self) -> dict:
                return {"status": "success", "hash": "turn_hash", "row_count": 1}

            async def all_rows(self, **kwargs):
                self.all_rows_calls.append(dict(kwargs))
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型描述": "一室一厅",
                        "押一付一": "1500",
                    }
                ]

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                self.search_calls.append((query, limit))
                return await self.all_rows(limit=limit)

        original_router = inventory_read_turn.InventoryReadRouter
        select_calls: list[tuple[str, str]] = []

        class CountingRouter(original_router):
            def select_context(self, *, request_id: str, turn_id: str):
                select_calls.append((request_id, turn_id))
                return super().select_context(request_id=request_id, turn_id=turn_id)

        rewrite_index_calls: list[int] = []

        def fake_rewrite_index_loader() -> dict:
            rewrite_index_calls.append(1)
            return {"signature": "rewrite-fixture", "row_count": 1, "room_index": []}

        reply_results: list[dict] = []
        send_results: list[dict] = []

        async def fake_generate_reply_result(**kwargs):
            reply_results.append(kwargs)
            return {
                "reply": "有的，合幢悦府6-1-1204B在表里。",
                "draft_reply": "有的，合幢悦府6-1-1204B在表里。",
                "context": kwargs["context"],
                "selfcheck": {"status": "pass"},
                "needs_planner_retry": False,
                "planner_retry_reason": "",
            }

        async def fake_send_final_actions(**kwargs):
            send_results.append(kwargs)
            return {"sent_actions": [{"type": "text", "count": 1}], "context": kwargs["context"]}

        fake_inventory = FakeInventory()
        fake_reply = FakeReplyGenerator()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "load_rewrite_inventory_index": main.load_rewrite_inventory_index,
            "_generate_reply_result": main._generate_reply_result,
            "_send_final_actions": main._send_final_actions,
            "kf_turn_generations": dict(main.kf_turn_generations),
            "router": inventory_read_turn.InventoryReadRouter,
        }
        main.wecom_kf = FakeWeComKf()
        main.wecom_kf_context_store = FakeContextStore()
        main.reply_generator = fake_reply
        main.inventory = fake_inventory
        main.load_rewrite_inventory_index = fake_rewrite_index_loader
        main._generate_reply_result = fake_generate_reply_result
        main._send_final_actions = fake_send_final_actions
        inventory_read_turn.InventoryReadRouter = CountingRouter
        main.kf_turn_generations[main._conversation_key("kf", "wm")] = 0
        try:
            await main._process_text_turn(
                open_kfid="kf",
                external_userid="wm",
                pending_items=[{"msgid": "msg-turn", "content": "合幢悦府有哪些"}],
                generation=0,
            )
        finally:
            main.wecom_kf = originals["wecom_kf"]
            main.wecom_kf_context_store = originals["wecom_kf_context_store"]
            main.reply_generator = originals["reply_generator"]
            main.inventory = originals["inventory"]
            main.load_rewrite_inventory_index = originals["load_rewrite_inventory_index"]
            main._generate_reply_result = originals["_generate_reply_result"]
            main._send_final_actions = originals["_send_final_actions"]
            inventory_read_turn.InventoryReadRouter = originals["router"]
            main.kf_turn_generations.clear()
            main.kf_turn_generations.update(originals["kf_turn_generations"])

        self.assertEqual(len(select_calls), 1)
        self.assertEqual(len(rewrite_index_calls), 1)
        self.assertEqual(len(fake_reply.inventory_indexes), 1)
        self.assertEqual(len(fake_inventory.search_calls), 1)
        self.assertEqual(fake_inventory.search_calls[0], ("合幢悦府有哪些", 10))
        self.assertEqual(len(reply_results), 1)
        self.assertEqual(len(send_results), 1)
        rewrite_decision = fake_reply.inventory_indexes[0]["cache_meta"]["hash"]
        tool_context = send_results[0]["tool_evidence"]["inventory_read_context"]
        self.assertEqual(rewrite_decision, "turn_hash")
        self.assertEqual(tool_context["decision_id"], reply_results[0]["inventory_read_context"].decision_id)
        self.assertEqual(tool_context["source_kind"], "legacy")
        self.assertEqual(
            send_results[0]["tool_evidence"]["inventory_listing_evidence"][0]["source_hash"],
            "turn_hash",
        )

    async def test_process_text_turn_shadow_success_does_not_change_send_result(self) -> None:
        send_results, shadow_calls, processed, sent_texts = await self._run_shadow_process_text_turn(
            shadow_builder=lambda **kwargs: {
                "schema_version": "rag_v2_orchestrator_shadow.v1",
                "shadow_a": {"verdict": "pass"},
            }
        )

        self.assertEqual(len(shadow_calls), 1)
        self.assertEqual(len(send_results), 1)
        self.assertEqual(send_results[0]["final_reply"], "影子观测不改变发送。")
        self.assertEqual(sent_texts, [])
        self.assertEqual(processed, {"msg-shadow"})

    async def test_process_text_turn_shadow_failure_only_warns_and_still_sends(self) -> None:
        shadow_calls: list[dict] = []

        def failing_shadow_builder(**kwargs):
            shadow_calls.append(kwargs)
            raise RuntimeError("shadow failed")

        send_results, _, processed, sent_texts = await self._run_shadow_process_text_turn(
            shadow_builder=failing_shadow_builder
        )

        self.assertEqual(len(shadow_calls), 1)
        self.assertEqual(len(send_results), 1)
        self.assertEqual(send_results[0]["final_reply"], "影子观测不改变发送。")
        self.assertEqual(sent_texts, [])
        self.assertEqual(processed, {"msg-shadow"})

    async def test_process_text_turn_shadow_uses_clarification_result_after_selfcheck_retry(self) -> None:
        async def fake_understand_message(**kwargs):
            if kwargs.get("planner_feedback"):
                return {
                    "intent": "inventory",
                    "rewritten_query": "合幢悦府有哪些",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "你确认下是合幢悦府哪套，我再按最新房源表查。",
                }
            return {
                "intent": "inventory",
                "rewritten_query": "合幢悦府有哪些",
                "effective_query": "合幢悦府有哪些",
                "query_state": {"intent": "inventory"},
                "needs_clarification": False,
                "tool_plan": {
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "source": "test_tool_plan",
                },
            }

        failed_reply_result = {
            "reply": "旧失败草稿，不应该进入 shadow。",
            "draft_reply": "旧失败草稿，不应该进入 shadow。",
            "context": {},
            "selfcheck": {"status": "retry", "source": "test_failed_selfcheck"},
            "needs_planner_retry": True,
            "planner_retry_reason": "test_retry",
        }
        clarification_result = {
            "reply": "你确认下是合幢悦府哪套，我再按最新房源表查。",
            "draft_reply": "你确认下是合幢悦府哪套，我再按最新房源表查。",
            "context": {},
            "selfcheck": {"status": "pass", "source": "test_clarification_selfcheck"},
            "needs_planner_retry": False,
            "planner_retry_reason": "",
        }
        generate_calls: list[dict] = []

        async def fake_generate_reply_result(**kwargs):
            generate_calls.append(kwargs)
            if len(generate_calls) == 1:
                return {**failed_reply_result, "context": kwargs["context"]}
            return {**clarification_result, "context": kwargs["context"]}

        send_results, shadow_calls, processed, sent_texts = await self._run_shadow_process_text_turn(
            shadow_builder=lambda **kwargs: {
                "schema_version": "rag_v2_orchestrator_shadow.v1",
                "shadow_a": {"verdict": "pass"},
            },
            understand_message=fake_understand_message,
            generate_reply_result=fake_generate_reply_result,
        )

        self.assertEqual(len(generate_calls), 2)
        self.assertEqual(len(shadow_calls), 1)
        self.assertEqual(
            shadow_calls[0]["reply_result"]["selfcheck"],
            {"status": "pass", "source": "test_clarification_selfcheck"},
        )
        self.assertFalse(shadow_calls[0]["reply_result"]["needs_planner_retry"])
        self.assertEqual(
            shadow_calls[0]["reply_result"]["reply"],
            "你确认下是合幢悦府哪套，我再按最新房源表查。",
        )
        self.assertEqual(
            shadow_calls[0]["planner_result"],
            {"actions": ["clarification"], "reply_source": "rewrite_clarification"},
        )
        self.assertEqual(
            shadow_calls[0]["tool_evidence"],
            {"actions": ["clarification"], "deterministic_reply_source": "rewrite_clarification"},
        )
        self.assertEqual(len(send_results), 1)
        self.assertEqual(send_results[0]["final_reply"], "你确认下是合幢悦府哪套，我再按最新房源表查。")
        self.assertEqual(sent_texts, [])
        self.assertEqual(processed, {"msg-shadow"})

    async def _run_shadow_process_text_turn(
        self,
        *,
        shadow_builder,
        reply_generator=None,
        understand_message=None,
        generate_reply_result=None,
    ):
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "合幢悦府有哪些",
                    "effective_query": "合幢悦府有哪些",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                    "tool_plan": {
                        "actions": ["search_inventory", "compact_listing", "generate_reply"],
                        "source": "test_tool_plan",
                    },
                }

        class FakeInventory:
            def cache_meta(self) -> dict:
                return {"status": "success", "hash": "shadow_hash", "row_count": 1}

            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型描述": "一室一厅",
                        "押一付一": "1500",
                    }
                ]

            async def search(self, query: str, limit: int = 10):
                return await self.all_rows(limit=limit)

        async def fake_generate_reply_result(**kwargs):
            return {
                "reply": "影子观测不改变发送。",
                "draft_reply": "影子观测不改变发送。",
                "context": kwargs["context"],
                "selfcheck": {"status": "pass"},
                "needs_planner_retry": False,
                "planner_retry_reason": "",
            }

        send_results: list[dict] = []

        async def fake_send_final_actions(**kwargs):
            send_results.append(kwargs)
            return {"sent_actions": [{"type": "text", "count": 1}], "context": kwargs["context"]}

        shadow_calls: list[dict] = []

        def recording_shadow_builder(**kwargs):
            result = shadow_builder(**kwargs)
            if shadow_calls is not result:
                shadow_calls.append(kwargs)
            return result

        fake_wecom = FakeWeComKf()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "_understand_message": main._understand_message,
            "_generate_reply_result": main._generate_reply_result,
            "_send_final_actions": main._send_final_actions,
            "shadow_builder": main.kf_orchestrator_shadow.build_shadow_artifact,
            "kf_turn_generations": dict(main.kf_turn_generations),
        }
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = FakeContextStore()
        main.reply_generator = reply_generator or FakeReplyGenerator()
        main.inventory = FakeInventory()
        if understand_message is not None:
            main._understand_message = understand_message
        main._generate_reply_result = generate_reply_result or fake_generate_reply_result
        main._send_final_actions = fake_send_final_actions
        main.kf_orchestrator_shadow.build_shadow_artifact = recording_shadow_builder
        main.kf_turn_generations[main._conversation_key("kf", "wm")] = 0
        try:
            await main._process_text_turn(
                open_kfid="kf",
                external_userid="wm",
                pending_items=[{"msgid": "msg-shadow", "content": "合幢悦府有哪些"}],
                generation=0,
            )
        finally:
            main.wecom_kf = originals["wecom_kf"]
            main.wecom_kf_context_store = originals["wecom_kf_context_store"]
            main.reply_generator = originals["reply_generator"]
            main.inventory = originals["inventory"]
            main._understand_message = originals["_understand_message"]
            main._generate_reply_result = originals["_generate_reply_result"]
            main._send_final_actions = originals["_send_final_actions"]
            main.kf_orchestrator_shadow.build_shadow_artifact = originals["shadow_builder"]
            main.kf_turn_generations.clear()
            main.kf_turn_generations.update(originals["kf_turn_generations"])

        return send_results, shadow_calls, fake_wecom.state_store.processed, fake_wecom.texts

    async def test_inventory_evidence_mismatch_clears_customer_visible_facts(self) -> None:
        class FakeInventory:
            def cache_meta(self) -> dict:
                return {"status": "success", "hash": "expected_hash", "row_count": 1}

        original_inventory = main.inventory
        original_search = main._inventory_search_rows_for_context
        main.inventory = FakeInventory()
        try:
            read_context = main._local_inventory_read_context("mismatch")

            async def mismatched_search(context, query_state, *, limit=8):
                return [
                    {"小区": "合幢悦府", "房号": "6-1-1204B"}
                ], [
                    main.InventoryListingEvidence(
                        evidence_id="evd_bad",
                        decision_id=context.decision_id,
                        listing_id="lst_bad",
                        source_kind=context.source_kind,
                        source_hash="different_hash",
                        schema_version=context.schema_version,
                        area="拱墅万达",
                        community="合幢悦府",
                        room_no="6-1-1204B",
                    )
                ]

            main._inventory_search_rows_for_context = mismatched_search
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video"],
                content="合幢悦府1204视频",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "合幢悦府1204视频",
                    "query_state": {"intent": "inventory"},
                    "constraint_proof": {},
                },
                inventory_read_context=read_context,
            )
        finally:
            main.inventory = original_inventory
            main._inventory_search_rows_for_context = original_search

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["video_paths"], [])
        self.assertEqual(evidence["inventory_read_error"]["code"], "mixed_source_hash")


class MainUnderstandingGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_deposit_question_does_not_need_room_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "contract",
                    "rewritten_query": "用户咨询免押和服务费",
                    "query_state": {"intent": "contract"},
                    "needs_clarification": True,
                    "clarification_text": "请问哪套房源？",
                }

        original = main.reply_generator
        main.reply_generator = FakeReplyGenerator()
        try:
            understanding = await main._understand_message(
                content="能免押吗？客户问服务费怎么算。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("能免押吗？客户问服务费怎么算。"),
            )
        finally:
            main.reply_generator = original

        self.assertEqual(understanding["intent"], "deposit")
        self.assertFalse(understanding["needs_clarification"])
        self.assertEqual(understanding["clarification_text"], "")
        self.assertTrue(understanding["query_state"]["wants_deposit"])
        self.assertIn("免押", understanding["effective_query"])
        self.assertNotIn("请问哪套房源", understanding["effective_query"])

    async def test_inventory_sheet_signal_replaces_bad_rewrite_query(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory_sheet",
                    "rewritten_query": "用户缺少区域、预算和户型，无法生成有效房源表。",
                    "effective_query": "用户缺少区域、预算和户型，无法生成有效房源表。",
                    "query_state": {"intent": "inventory_sheet"},
                    "needs_clarification": True,
                    "clarification_text": "请问想看哪个小区或什么价位的房源表？",
                }

        original = main.reply_generator
        main.reply_generator = FakeReplyGenerator()
        try:
            understanding = await main._understand_message(
                content="那房源表也发我一份。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("那房源表也发我一份。"),
            )
        finally:
            main.reply_generator = original

        self.assertEqual(understanding["intent"], "inventory_sheet")
        self.assertFalse(understanding["needs_clarification"])
        self.assertEqual(understanding["clarification_text"], "")
        self.assertTrue(understanding["query_state"]["wants_inventory_sheet"])
        self.assertIn("最新房源表 PNG", understanding["effective_query"])
        self.assertNotIn("无法生成有效房源表", understanding["effective_query"])

    def test_unasked_deposit_context_is_removed_from_rewrite(self) -> None:
        result = {
            "intent": "inventory",
            "rewritten_query": "用户在拱墅万达寻找2000以内一室一厅，并隐含希望了解免押及服务费政策。",
            "effective_query": "用户在拱墅万达寻找2000以内一室一厅，并隐含希望了解免押及服务费政策。",
            "query_state": {"intent": "inventory", "wants_deposit": True},
        }

        cleaned = main._strip_unasked_deposit_from_understanding(
            "拱墅万达这边有没有2000以内的一室一厅？",
            result,
            {"wants_deposit": False},
        )

        self.assertNotIn("免押", cleaned["effective_query"])
        self.assertNotIn("服务费", cleaned["effective_query"])
        self.assertNotIn("wants_deposit", cleaned["query_state"])
        self.assertTrue(cleaned["query_state"]["unasked_deposit_context_removed"])

    def test_unasked_media_context_is_removed_from_rewrite(self) -> None:
        result = {
            "intent": "media",
            "rewritten_query": "在拱墅万达区域，预算2000以内筛选一室一厅，并优先发送前几套房源的视频供用户筛选。",
            "effective_query": "在拱墅万达区域，预算2000以内筛选一室一厅，并优先发送前几套房源的视频供用户筛选。",
            "query_state": {"intent": "media", "wants_video": True, "pending_video_action": "hold"},
            "candidate_action": "select",
            "selected_indices": [1],
        }

        cleaned = main._strip_unasked_media_from_understanding(
            "拱墅万达这边有没有2000以内的一室一厅？",
            result,
            main._deterministic_signals("拱墅万达这边有没有2000以内的一室一厅？"),
        )

        self.assertEqual(cleaned["intent"], "inventory")
        self.assertNotIn("视频", cleaned["effective_query"])
        self.assertNotIn("wants_video", cleaned["query_state"])
        self.assertEqual(cleaned["selected_indices"], [])
        self.assertTrue(cleaned["query_state"]["unasked_media_context_removed"])

    def test_target_rows_select_exact_room_from_multiple_search_results(self) -> None:
        rows = [
            {"小区": "华丰欣苑", "房号": "14-2-901"},
            {"小区": "华丰欣苑", "房号": "19-3-1104"},
        ]

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "用户请求发送华丰欣苑14-2-901的房源视频",
                "context_reference": False,
            },
            {},
            rows,
        )

        self.assertEqual(target_rows, [{"小区": "华丰欣苑", "房号": "14-2-901"}])

    def test_effective_query_preserves_room_ref_from_constraint_proof(self) -> None:
        effective_query = main._enforce_effective_query(
            content="华丰欣苑14-2-901视频发我，客户想看看装修。",
            understanding={
                "effective_query": "查询石桥街道华丰附近一室一厅视频",
                "rewritten_query": "查询石桥街道华丰附近一室一厅视频",
            },
            constraint_proof={
                "communities": ["华丰欣苑"],
                "room_refs": ["14-2-901"],
                "wants_video": True,
            },
        )

        self.assertIn("华丰欣苑", effective_query)
        self.assertIn("14-2-901", effective_query)

    def test_explicit_room_ref_beats_stale_confirmed_context_for_media(self) -> None:
        rows = [
            {"小区": "华丰欣苑", "房号": "14-2-901"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]
        context = {
            "confirmed_room": {
                "row": {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                "label": "星桥锦绣嘉苑20-1606A",
            }
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "查询石桥街道华丰附近一室一厅视频",
                "rewritten_query": "查询石桥街道华丰附近一室一厅视频",
                "context_reference": True,
                "constraint_proof": {"room_refs": ["14-2-901"], "wants_video": True},
                "structured_task": {
                    "original_text": "华丰欣苑14-2-901视频发我，客户想看看装修。",
                    "effective_query": "查询石桥街道华丰附近一室一厅视频",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            rows,
        )

        self.assertEqual(target_rows, [{"小区": "华丰欣苑", "房号": "14-2-901"}])

    def test_room_ref_mismatch_clarification_keeps_raw_community_hint(self) -> None:
        entity_resolution = main._build_entity_resolution("皋塘运都9-2-402B视频发我", [])

        clarification = main._room_ref_mismatch_clarification(
            "皋塘运都9-2-402B视频发我",
            entity_resolution,
            [],
        )

        self.assertIn("皋塘运都", clarification)
        self.assertIn("9-2-402B", clarification)
        self.assertNotIn("确认下小区+房号", clarification)

    def test_explicit_room_ref_beats_stale_candidate_index_for_media(self) -> None:
        rows = [
            {"小区": "华丰欣苑", "房号": "14-2-901"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "query": "旧的万达候选列表",
            "candidates": [{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}],
            "shown_count": 1,
            "total_count": 1,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "在石桥街道华丰区域，查询华丰欣苑14-2-901的视频",
                "rewritten_query": "查询华丰欣苑14-2-901视频",
                "context_reference": True,
                "candidate_action": "select",
                "selected_indices": [1],
                "constraint_proof": {
                    "communities": ["华丰欣苑"],
                    "room_refs": ["14-2-901"],
                    "wants_video": True,
                },
                "structured_task": {
                    "original_text": "华丰欣苑14-2-901视频发我，客户想看看装修。",
                    "effective_query": "查询华丰欣苑14-2-901视频",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            rows,
        )

        self.assertEqual(target_rows, [{"小区": "华丰欣苑", "房号": "14-2-901"}])

    def test_new_scoped_inventory_query_does_not_use_stale_confirmed_room(self) -> None:
        context = {
            "confirmed_room": {
                "row": {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                "label": "星桥锦绣嘉苑20-1606A",
            }
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "在石桥街道华丰石桥永佳半山区域，查询5000左右两室整租",
                "rewritten_query": "石桥附近5000左右两室整租在租房源",
                "context_reference": True,
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "budget_range": [4500, 5500],
                    "layout": "两室",
                    "wants_video": False,
                    "wants_image": False,
                },
                "structured_task": {
                    "original_text": "石桥附近5000左右有两室吗？最好整租。",
                    "effective_query": "石桥附近5000左右两室整租在租房源",
                    "tool_requirements": {"needs_inventory_search": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [])

    def test_context_followup_video_request_can_use_confirmed_room(self) -> None:
        confirmed = {"小区": "棠润府", "房号": "15-2-801B"}
        context = {
            "confirmed_room": {
                "row": confirmed,
                "label": "棠润府15-2-801B",
            }
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "这套视频发我",
                "rewritten_query": "发送上一套已确认房源的视频",
                "context_reference": True,
                "intent": "media",
                "constraint_proof": {"wants_video": True},
                "structured_task": {
                    "original_text": "视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [confirmed])

    def test_plain_previous_reference_can_use_confirmed_room(self) -> None:
        confirmed = {"小区": "棠润府", "房号": "15-2-801B"}
        context = {
            "confirmed_room": {
                "row": confirmed,
                "label": "棠润府15-2-801B",
            }
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "上一个视频发我",
                "rewritten_query": "发送上一套已确认房源的视频",
                "context_reference": True,
                "intent": "media",
                "constraint_proof": {"wants_video": True},
                "structured_task": {
                    "original_text": "上一个视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [confirmed])

    def test_candidate_context_reference_can_bind_by_community_hint(self) -> None:
        star_row = {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以内一室",
            "candidates": [
                {"小区": "棠润府", "房号": "15-2-801B"},
                star_row,
            ],
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "星桥那个视频发我",
                "rewritten_query": "发送上一轮候选里星桥锦绣嘉苑那套视频",
                "context_reference": True,
                "intent": "media",
                "constraint_proof": {"wants_video": True},
                "structured_task": {
                    "original_text": "星桥那个视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [star_row])

    def test_candidate_context_reference_can_bind_fuzzy_community_hint(self) -> None:
        tangrun_row = {"小区": "棠润府", "房号": "15-2-801B"}
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以内一室",
            "candidates": [
                tangrun_row,
                {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
            ],
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "棠闰府那套视频发我",
                "rewritten_query": "发送上一轮候选里棠润府15-2-801B的视频",
                "context_reference": True,
                "intent": "media",
                "constraint_proof": {"communities": ["棠润府"], "wants_video": True},
                "structured_task": {
                    "original_text": "棠闰府那套视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [tangrun_row])

    def test_candidate_indices_bind_selected_media_rows(self) -> None:
        rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
            {"小区": "合峙悦府", "房号": "6-1-1204B"},
            {"小区": "荣润府", "房号": "10-1004C"},
            {"小区": "大华海派风景", "房号": "2-1-402A"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以内一室",
            "candidates": rows,
            "shown_count": 5,
            "total_count": 5,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "1和5视频",
                "rewritten_query": "发送候选第1和第5套视频",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 5],
                "constraint_proof": {"selected_indices": [1, 5], "wants_video": True},
                "structured_task": {
                    "original_text": "1和5视频",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [rows[0], rows[4]])

    def test_selected_indices_parse_sparse_video_number_connectors(self) -> None:
        understanding = {"intent": "media", "constraint_proof": {"wants_video": True}}

        self.assertEqual(
            main._selected_indices_from_understanding(understanding, "1和3的视频发我"),
            [1, 3],
        )
        self.assertEqual(
            main._selected_indices_from_understanding(understanding, "1、3的视频发我"),
            [1, 3],
        )

    def test_structured_selected_indices_win_over_incomplete_text_selection(self) -> None:
        original_selection = main._selection_indices_from_text
        main._selection_indices_from_text = lambda text: [1]
        try:
            selected = main._selected_indices_from_understanding(
                {
                    "intent": "media",
                    "selected_indices": [1, 3],
                    "constraint_proof": {"selected_indices": [1, 3], "wants_video": True},
                },
                "1和3的视频发我",
            )
        finally:
            main._selection_indices_from_text = original_selection

        self.assertEqual(selected, [1, 3])

    def test_selected_indices_bind_first_and_third_candidate_rows(self) -> None:
        rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
            {"小区": "合峙悦府", "房号": "6-1-1204B"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以内一室",
            "candidates": rows,
            "shown_count": 3,
            "total_count": 3,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "1和3的视频发我",
                "rewritten_query": "发送候选第1和第3套视频",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 3],
                "constraint_proof": {"selected_indices": [1, 3], "wants_video": True},
                "structured_task": {
                    "original_text": "1和3的视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [{"小区": "当前搜索小区", "房号": "1-101"}],
        )

        self.assertEqual(target_rows, [rows[0], rows[2]])

    def test_selected_indices_without_candidate_set_do_not_bind_current_search_rows(self) -> None:
        search_rows = [
            {"小区": "当前搜索小区", "房号": "1-101"},
            {"小区": "当前搜索小区", "房号": "1-102"},
            {"小区": "当前搜索小区", "房号": "1-103"},
        ]

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "1和3的视频发我",
                "rewritten_query": "发送候选第1和第3套视频",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 3],
                "constraint_proof": {"selected_indices": [1, 3], "wants_video": True},
                "structured_task": {
                    "original_text": "1和3的视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            kf_context_memory.empty_context(),
            search_rows,
        )

        self.assertEqual(target_rows, [])

    def test_send_all_limit_phrase_is_not_selected_index_five(self) -> None:
        phrase = "能发的都发，先不要超过5套。"

        self.assertEqual(main._selection_indices_from_text(phrase), [])
        self.assertEqual(main._selected_indices_from_understanding({}, phrase), [])

    def test_llm1_bound_candidate_numbers_work_without_text_heuristic(self) -> None:
        packet = {
            "legacy_unknown_fields": {
                "llm1_production": {
                    "candidate_binding": {
                        "status": "bound",
                        "selected_candidate_numbers": [1, 3],
                        "dropped_candidate_numbers": [],
                        "candidate_count": 5,
                    }
                }
            },
            "tasks": [{"constraints": {"candidate_numbers": [1, 3]}}],
        }

        self.assertEqual(
            main._selected_indices_from_understanding(
                {
                    "intent": "media",
                    "llm1_task_packet": packet,
                    "constraint_proof": {"wants_video": True},
                },
                "上面那两个视频发我",
            ),
            [1, 3],
        )

    def test_partial_llm1_binding_does_not_revive_task_candidate_numbers(self) -> None:
        packet = {
            "legacy_unknown_fields": {
                "llm1_production": {
                    "candidate_binding": {
                        "status": "partial",
                        "selected_candidate_numbers": [1],
                        "dropped_candidate_numbers": [3],
                        "candidate_count": 2,
                    }
                }
            },
            "tasks": [{"constraints": {"candidate_numbers": [1]}}],
        }

        self.assertEqual(
            main._selected_indices_from_understanding(
                {
                    "intent": "media",
                    "llm1_task_packet": packet,
                    "constraint_proof": {"wants_video": True},
                },
                "上面那两个视频发我",
            ),
            [],
        )

    def test_llm1_task_constraints_without_candidate_binding_are_not_trusted_selection(self) -> None:
        packet = {"tasks": [{"constraints": {"candidate_numbers": [1, 2, 3, 4, 5]}}]}

        understanding = {
            "intent": "media",
            "llm1_task_packet": packet,
            "constraint_proof": {"wants_video": True},
        }

        self.assertEqual(
            main._selected_indices_from_understanding(understanding, "上面那几个视频发我"),
            [],
        )
        self.assertEqual(
            main._pending_media_selection_indices("能发的都发，先不要超过5套。", understanding),
            [],
        )

    def test_rewritten_query_candidate_number_does_not_bind_without_user_selection_or_llm1_binding(self) -> None:
        rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以内一室",
            "candidates": rows,
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "第1套视频",
                "rewritten_query": "第1套视频",
                "context_reference": False,
                "intent": "media",
                "constraint_proof": {"wants_video": True},
                "structured_task": {
                    "original_text": "视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [])

    def test_current_community_media_request_without_selection_does_not_bind_first_search_row(self) -> None:
        search_rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "棠润府", "房号": "15-2-802B"},
        ]

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "棠润府第1套视频",
                "rewritten_query": "棠润府第1套视频",
                "context_reference": False,
                "intent": "media",
                "selected_indices": [1],
                "constraint_proof": {
                    "communities": ["棠润府"],
                    "selected_indices": [1],
                    "wants_video": True,
                },
                "structured_task": {
                    "original_text": "棠润府视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            kf_context_memory.empty_context(),
            search_rows,
        )

        self.assertEqual(target_rows, [])

    def test_pending_media_send_all_limit_phrase_ignores_structured_selected_indices(self) -> None:
        self.assertEqual(
            main._pending_media_selection_indices(
                "能发的都发，先不要超过5套。",
                {
                    "intent": "media",
                    "selected_indices": [1, 2, 3, 4, 5],
                    "constraint_proof": {
                        "selected_indices": [1, 2, 3, 4, 5],
                        "wants_video": True,
                    },
                },
            ),
            [],
        )

    def test_selected_indices_ignore_inherited_room_refs_for_candidate_binding(self) -> None:
        rows = [
            {"\u5c0f\u533a": "\u68e0\u6da6\u5e9c", "\u623f\u53f7": "15-2-801B"},
            {"\u5c0f\u533a": "\u5408\u5d62\u60a6\u5e9c", "\u623f\u53f7": "6-1-1204B"},
            {"\u5c0f\u533a": "\u8363\u6da6\u5e9c", "\u623f\u53f7": "10-1004C"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "\u4e07\u8fbe2000\u4ee5\u5185\u4e00\u5ba4",
            "candidates": rows,
            "shown_count": 3,
            "total_count": 3,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "\u53d1\u9001\u68e0\u6da6\u5e9c15-2-801B\u7684\u524d\u4e24\u5957\u89c6\u9891",
                "rewritten_query": "\u53d1\u9001\u68e0\u6da6\u5e9c15-2-801B\u7684\u524d\u4e24\u5957\u89c6\u9891",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "room_refs": ["15-2-801b"],
                    "selected_indices": [1, 2],
                    "wants_video": True,
                },
                "structured_task": {
                    "original_text": "\u524d\u4e24\u5957\u89c6\u9891\u53d1\u6211\u3002",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [rows[0]],
        )

        self.assertEqual(target_rows, rows[:2])

    def test_selected_indices_prefer_candidate_context_over_inherited_search_scope(self) -> None:
        candidate_rows = [
            {"\u5c0f\u533a": "\u68e0\u6da6\u5e9c", "\u623f\u53f7": "15-2-801B"},
            {"\u5c0f\u533a": "\u5408\u5d62\u60a6\u5e9c", "\u623f\u53f7": "6-1-1204B"},
        ]
        search_rows = [
            {"\u5c0f\u533a": "\u534e\u4e30\u6b23\u82d1", "\u623f\u53f7": "14-2-901"},
            {"\u5c0f\u533a": "\u77f3\u6865\u94ed\u82d1", "\u623f\u53f7": "6-1102"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "\u4e07\u8fbe2000\u4ee5\u5185\u4e00\u5ba4",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "\u534e\u4e30\u6b23\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                "rewritten_query": "\u534e\u4e30\u6b23\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "communities": ["\u534e\u4e30\u6b23\u82d1"],
                    "selected_indices": [1, 2],
                    "wants_image": True,
                },
                "structured_task": {
                    "original_text": "\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                    "tool_requirements": {"needs_image": True},
                },
            },
            context,
            search_rows,
        )

        self.assertEqual(target_rows, candidate_rows)

    def test_explicit_new_community_selected_indices_do_not_fallback_to_stale_candidates_when_search_misses(self) -> None:
        candidate_rows = [
            {"\u5c0f\u533a": "\u68e0\u6da6\u5e9c", "\u623f\u53f7": "15-2-801B"},
            {"\u5c0f\u533a": "\u5408\u5d62\u60a6\u5e9c", "\u623f\u53f7": "6-1-1204B"},
        ]
        search_rows = [
            {"\u5c0f\u533a": "\u534e\u4e30\u6b23\u82d1", "\u623f\u53f7": "14-2-901"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "\u4e07\u8fbe2000\u4ee5\u5185\u4e00\u5ba4",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                "rewritten_query": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                "context_reference": True,
                "intent": "media",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "communities": ["\u6768\u5bb6\u65b0\u96c5\u82d1"],
                    "selected_indices": [1, 2],
                    "wants_image": True,
                },
                "structured_task": {
                    "original_text": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                    "tool_requirements": {"needs_image": True},
                },
            },
            context,
            search_rows,
        )

        self.assertEqual(target_rows, [])

    def test_selected_indices_do_not_partially_bind_current_search_rows(self) -> None:
        rows = [{"小区": "东新园", "房号": "8-1201"}]

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "前两套视频",
                "rewritten_query": "发送前两套视频",
                "intent": "media",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "communities": ["东新园"],
                    "selected_indices": [1, 2],
                    "wants_video": True,
                },
                "structured_task": {
                    "original_text": "前两套视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            kf_context_memory.empty_context(),
            rows,
        )

        self.assertEqual(target_rows, [])

    def test_selected_indices_do_not_partially_bind_candidate_context_rows(self) -> None:
        row = {"小区": "东新园", "房号": "8-1201"}
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "东新园",
            "candidates": [row],
            "shown_count": 1,
            "total_count": 1,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "前两套视频",
                "rewritten_query": "发送前两套视频",
                "intent": "media",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "selected_indices": [1, 2],
                    "wants_video": True,
                },
                "structured_task": {
                    "original_text": "前两套视频发我",
                    "tool_requirements": {"needs_video": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(target_rows, [])

    def test_single_room_pronoun_prefers_confirmed_room_over_llm_selected_index(self) -> None:
        confirmed = {"小区": "兴业杨家府", "房号": "4-1502"}
        stale_candidates = [
            {"小区": "石桥铭苑", "房号": "6-1102"},
            {"小区": "华丰欣苑", "房号": "14-2-901"},
        ]
        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "row": confirmed,
            "label": "兴业杨家府4-1502",
        }
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "被媒体/看房临时搜索污染的候选",
            "candidates": stale_candidates,
            "shown_count": 2,
            "total_count": 2,
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "这套今天能看吗？",
                "rewritten_query": "查询上一套已确认房源的看房方式",
                "context_reference": True,
                "intent": "viewing",
                "selected_indices": [1],
                "constraint_proof": {"selected_indices": [1]},
                "structured_task": {
                    "original_text": "这套今天能看吗？",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            context,
            stale_candidates,
        )

        self.assertEqual(target_rows, [confirmed])

    def test_short_password_followup_binds_confirmed_room_without_context_flag(self) -> None:
        confirmed = {"小区": "兴业杨家府", "房号": "4-1502"}
        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "row": confirmed,
            "label": "兴业杨家府4-1502",
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "密码是多少？",
                "rewritten_query": "查询上一套已确认房源的看房密码",
                "context_reference": False,
                "intent": "viewing",
                "constraint_proof": {},
                "structured_task": {
                    "original_text": "密码是多少？",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            context,
            [{"小区": "永佳新苑", "房号": "3-702"}],
        )

        self.assertEqual(target_rows, [confirmed])

    def test_short_price_followup_binds_confirmed_room_without_context_flag(self) -> None:
        confirmed = {"小区": "兴业杨家府", "房号": "4-1502", "押一付一": "4500", "押二付一": "4200"}
        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "row": confirmed,
            "label": "兴业杨家府4-1502",
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "押一付一和押二付一分别多少钱？ 石桥街道 华丰 石桥 永佳 半山 一室一厅",
                "rewritten_query": "查询上一套已确认房源的两种付款方式月租价格",
                "context_reference": False,
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道\n华丰 石桥\n永佳 半山",
                    "layout": "一室一厅",
                },
                "structured_task": {
                    "original_text": "押一付一和押二付一分别多少钱？",
                    "tool_requirements": {"needs_inventory_search": True},
                },
            },
            context,
            [{"小区": "永佳新苑", "房号": "3-702"}],
        )

        self.assertEqual(target_rows, [confirmed])

    def test_short_utilities_followup_binds_confirmed_room_without_context_flag(self) -> None:
        confirmed = {"小区": "兴业杨家府", "房号": "4-1502", "备注": "民用水电"}
        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "row": confirmed,
            "label": "兴业杨家府4-1502",
        }

        target_rows = main._target_rows_from_understanding(
            {
                "effective_query": "水电费怎么算？ 石桥街道 华丰 石桥 永佳 半山 一室一厅",
                "rewritten_query": "查询上一套已确认房源的水电费",
                "context_reference": False,
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道\n华丰 石桥\n永佳 半山",
                    "layout": "一室一厅",
                    "wants_utilities": True,
                },
                "structured_task": {
                    "original_text": "水电费怎么算？",
                    "tool_requirements": {
                        "needs_inventory_search": True,
                        "needs_utilities": True,
                    },
                },
            },
            context,
            [{"小区": "永佳新苑", "房号": "3-702"}],
        )

        self.assertEqual(target_rows, [confirmed])

    def test_unverified_inventory_not_found_clarification_is_routed_to_tools(self) -> None:
        result = {
            "intent": "media",
            "rewritten_query": "查询华丰欣苑14-2-901房源并发送视频",
            "query_state": {"intent": "media", "wants_video": True},
            "needs_clarification": True,
            "clarification_text": "未在房源表中找到“华丰欣苑14-2-901”的确切匹配，请确认小区名称或房号是否正确？",
            "structured_task": {"query_state": {}, "clarification": {"needed": True, "text": "旧澄清"}},
        }

        routed = main._route_unverified_not_found_to_tools(result, planner_feedback=None)

        self.assertFalse(routed["needs_clarification"])
        self.assertEqual(routed["clarification_text"], "")
        self.assertTrue(routed["rewrite_layer_not_found_claim_routed_to_tools"])
        self.assertTrue(routed["query_state"]["needs_tool_verification"])
        self.assertTrue(routed["structured_task"]["query_state"]["needs_tool_verification"])
        self.assertFalse(routed["structured_task"]["clarification"]["needed"])
        self.assertEqual(
            routed["structured_task"]["clarification"]["reason"],
            "rewrite_layer_not_found_claim_routed_to_tools",
        )

    def test_planner_feedback_clarification_is_preserved(self) -> None:
        result = {
            "intent": "media",
            "needs_clarification": True,
            "clarification_text": "未在房源表中找到，需要确认。",
        }

        routed = main._route_unverified_not_found_to_tools(
            result,
            planner_feedback={"need_rewrite_clarification": True},
        )

        self.assertTrue(routed["needs_clarification"])
        self.assertEqual(routed["clarification_text"], "未在房源表中找到，需要确认。")

    def test_selfcheck_retry_not_found_clarification_is_routed_to_tools(self) -> None:
        result = {
            "intent": "inventory",
            "query_state": {"intent": "inventory"},
            "needs_clarification": True,
            "clarification_text": "最新房源表里暂时没查到合峙悦府这个小区。你确认一下小区名。",
            "structured_task": {"query_state": {}, "clarification": {"needed": True, "text": "旧澄清"}},
        }

        routed = main._route_unverified_not_found_to_tools(
            result,
            planner_feedback={"planner_retry_reason": "final_selfcheck_retry"},
        )

        self.assertFalse(routed["needs_clarification"])
        self.assertEqual(routed["clarification_text"], "")
        self.assertTrue(routed["rewrite_layer_not_found_claim_routed_to_tools"])
        self.assertTrue(routed["query_state"]["needs_tool_verification"])

    def test_inventory_bound_similar_room_clarification_can_be_preserved(self) -> None:
        result = {
            "intent": "viewing",
            "query_state": {"intent": "viewing"},
            "needs_clarification": True,
            "clarification_text": "最新房源表没查到兴业杨家府10-1-304这套，只匹配到相近房号：兴业杨家府3-601。你确认是不是这套？",
            "structured_task": {"query_state": {}, "clarification": {"needed": True, "text": "旧澄清"}},
        }

        preserved = main._route_unverified_not_found_to_tools(
            result,
            planner_feedback=None,
            allow_inventory_bound_clarification=True,
        )
        routed = main._route_unverified_not_found_to_tools(
            dict(result),
            planner_feedback=None,
            allow_inventory_bound_clarification=False,
        )

        self.assertTrue(preserved["needs_clarification"])
        self.assertFalse(preserved.get("rewrite_layer_not_found_claim_routed_to_tools", False))
        self.assertFalse(routed["needs_clarification"])
        self.assertTrue(routed["query_state"]["needs_tool_verification"])


class WeComKfStateStoreTests(unittest.TestCase):
    def test_sync_messages_saves_next_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WeComKfStateStore(path=Path(directory) / "state.json")
            client = WeComKfClient(state_store=store)

            async def fake_sync_pages(open_kfid: str, token: str, cursor: str):
                self.assertEqual(open_kfid, "kf_xxx")
                self.assertEqual(token, "token_xxx")
                self.assertEqual(cursor, "")
                return ([{"msgid": "msg-1", "msgtype": "event"}], "cursor-next")

            client._sync_message_pages = fake_sync_pages  # type: ignore[method-assign]
            messages = asyncio.run(client.sync_messages("kf_xxx", "token_xxx"))

            self.assertEqual(messages, [{"msgid": "msg-1", "msgtype": "event"}])
            self.assertEqual(store.load()["cursor"], "cursor-next")

    def test_persists_cursor_processed_msgids_and_welcome_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = WeComKfStateStore(path=path, max_msgids=2)

            store.save_cursor("cursor-1")
            store.mark_processed("msg-1")
            store.mark_processed("msg-2")
            store.mark_processed("msg-3")
            store.mark_welcome_sent("kf:wm", sent_at=123)

            reloaded = WeComKfStateStore(path=path, max_msgids=2)
            self.assertEqual(reloaded.load()["cursor"], "cursor-1")
            self.assertFalse(reloaded.is_processed("msg-1"))
            self.assertTrue(reloaded.is_processed("msg-2"))
            self.assertTrue(reloaded.is_processed("msg-3"))
            self.assertEqual(reloaded.last_welcome_sent_at("kf:wm"), 123)
            self.assertNotIn("kf:wm", path.read_text(encoding="utf-8"))


class WeComKfPayloadTests(unittest.TestCase):
    def test_builds_text_payloads_and_extracts_kf_fields(self) -> None:
        client = WeComKfClient()
        payload = client.build_text_payload("kf_xxx", "wm_xxx", "你好")

        self.assertEqual(payload["touser"], "wm_xxx")
        self.assertEqual(payload["open_kfid"], "kf_xxx")
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["content"], "你好")

        message = {
            "msgtype": "text",
            "origin": 3,
            "open_kfid": "kf_xxx",
            "external_userid": "wm_xxx",
            "text": {"content": "房源表发一下"},
        }
        self.assertTrue(should_auto_reply_kf_message(message))
        self.assertEqual(extract_kf_text(message), "房源表发一下")
        self.assertEqual(extract_kf_open_kfid(message), "kf_xxx")
        self.assertEqual(extract_kf_external_userid(message), "wm_xxx")

    def test_detects_enter_session_welcome_event(self) -> None:
        message = {
            "msgtype": "event",
            "event": {
                "event_type": "enter_session",
                "welcome_code": "welcome-code",
                "open_kfid": "kf_xxx",
                "external_userid": "wm_xxx",
            },
        }

        self.assertTrue(is_kf_enter_session_event(message))
        self.assertEqual(extract_kf_welcome_code(message), "welcome-code")
        self.assertEqual(extract_kf_open_kfid(message), "kf_xxx")
        self.assertEqual(extract_kf_external_userid(message), "wm_xxx")

    def test_normalizes_direct_callback_enter_session_payload(self) -> None:
        message = kf_callback_payload_event_message(
            {
                "MsgType": "event",
                "Event": "enter_session",
                "WelcomeCode": "welcome-code",
                "OpenKfId": "kf_xxx",
                "ExternalUserID": "wm_xxx",
            }
        )

        self.assertTrue(is_kf_enter_session_event(message))
        self.assertEqual(extract_kf_welcome_code(message), "welcome-code")
        self.assertEqual(extract_kf_open_kfid(message), "kf_xxx")
        self.assertEqual(extract_kf_external_userid(message), "wm_xxx")

    def test_detects_kf_message_event(self) -> None:
        self.assertTrue(is_kf_message_event({"Event": "kf_msg_or_event", "Token": "next-token"}))
        self.assertFalse(is_kf_message_event({"msgtype": "text"}))

    def test_http_status_error_is_sanitized_before_logging(self) -> None:
        request = httpx.Request(
            "GET",
            "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token=token_CANARY_abcdefghijklmnopqrstuvwxyz&corpsecret=secret_CANARY_abcdefghijklmnopqrstuvwxyz",
        )
        response = httpx.Response(500, request=request, text="server error")

        with self.assertRaises(RuntimeError) as raised:
            _raise_for_status_sanitized(response, "微信客服发送")

        message = str(raised.exception)
        self.assertNotIn("token_CANARY", message)
        self.assertNotIn("secret_CANARY", message)
        self.assertNotIn("access_token=token", message)
        self.assertIn("[REDACTED]", message)


class WeComKfContextStoreTests(unittest.TestCase):
    def test_persists_structured_memory_active_query_and_candidate_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WeComKfContextStore(path=Path(directory) / "context.json")
            context = kf_context_memory.empty_context(now=lambda: 100.0)
            context["active_query_state"] = {
                "intent": "media",
                "area": "拱墅万达",
                "budget": "1500左右",
            }
            context["last_candidate_set"] = {
                "intent": "inventory",
                "query": "拱墅万达1500左右",
                "candidates": [{"小区": "荣润府", "房号": "15-2-801B"}],
                "shown_count": 1,
                "total_count": 6,
                "created_at": 100.0,
            }
            context = kf_context_memory.start_structured_turn(
                context,
                state={
                    "intent": "media",
                    "effective_query": "1和5视频",
                    "selected_indices": [1, 5],
                },
                user_input={"content": "1和5视频"},
                rewrite_result={
                    "intent": "media",
                    "rewritten_query": "发送候选第1和第5套视频",
                },
                now=lambda: 101.0,
            )

            store.save("kf:wm", context)
            loaded = store.get("kf:wm") or {}

            self.assertEqual(loaded["active_query_state"]["area"], "拱墅万达")
            self.assertEqual(loaded["last_candidate_set"]["shown_count"], 1)
            self.assertEqual(loaded["last_candidate_set"]["total_count"], 6)
            memory = loaded["structured_memory"]
            self.assertNotIn("state", memory)
            self.assertEqual(
                memory["turn_records"][-1]["rewritten_query"],
                "发送候选第1和第5套视频",
            )

    def test_context_store_writes_redacted_safe_context_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            store = WeComKfContextStore(path=path)
            raw_customer_id = "wm_CUSTOMER_CANARY_12345678901234567890"
            context = kf_context_memory.empty_context(now=lambda: 100.0)
            context["recent_messages"] = [
                {
                    "role": "user",
                    "content": "手机19900009999，看房密码246810#，token=token_CANARY_abcdefghijklmnopqrstuvwxyz",
                    "created_at": 100.0,
                }
            ]
            context["last_candidate_set"] = {
                "intent": "inventory",
                "query": "荣润府 手机19900009999",
                "candidates": [
                    {
                        "listing_id": "lst_safe_001",
                        "小区": "荣润府",
                        "房号": "15-2-801B",
                        "押一付一": "1800",
                        "看房方式密码": "246810#",
                        "手机号": "19900009999",
                        "token": "token_CANARY_abcdefghijklmnopqrstuvwxyz",
                    }
                ],
                "shown_count": 1,
                "total_count": 1,
                "created_at": 100.0,
            }
            context["confirmed_room"] = {
                "row": {
                    "listing_id": "lst_safe_001",
                    "小区": "荣润府",
                    "房号": "15-2-801B",
                    "看房方式密码": "246810#",
                    "备注": "电话19900009999",
                },
                "label": "荣润府15-2-801B",
                "created_at": 100.0,
                "inventory_cache_meta": {"msg_signature": "sig_CANARY_abcdefghijklmnopqrstuvwxyz"},
            }

            store.save(f"kf:{raw_customer_id}", context)
            dumped = path.read_text(encoding="utf-8")
            loaded = json.loads(dumped)
            saved_context = next(iter(loaded.values()))

            self.assertNotIn(raw_customer_id, dumped)
            self.assertNotIn("19900009999", dumped)
            self.assertNotIn("246810#", dumped)
            self.assertNotIn("token_CANARY", dumped)
            self.assertNotIn("sig_CANARY", dumped)
            self.assertNotIn("看房方式密码", dumped)
            self.assertEqual(saved_context["last_candidate_set"]["candidates"][0]["listing_id"], "lst_safe_001")
            self.assertEqual(saved_context["last_candidate_set"]["candidates"][0]["小区"], "荣润府")
            self.assertTrue(saved_context["last_candidate_set"]["candidates"][0]["has_password"])


class MainAgenticRagFlowTests(unittest.IsolatedAsyncioTestCase):
    def _prepared_inventory_image(self) -> str:
        image = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            image.write(b"\x89PNG\r\n\x1a\n")
            return image.name
        finally:
            image.close()
            self.addCleanup(lambda: Path(image.name).unlink(missing_ok=True))

    def test_prepared_inventory_image_fixture_is_real_png_temp_file(self) -> None:
        image_path = Path(self._prepared_inventory_image())

        assert image_path.exists()
        assert image_path.stat().st_size > 0
        assert image_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert Path.cwd().resolve() not in image_path.resolve().parents
        image_path.unlink(missing_ok=True)
        assert not image_path.exists()

    def test_first_stage_planner_reply_is_audit_only_until_tools_finish(self) -> None:
        planner = {
            "actions": ["search_inventory", "generate_reply"],
            "reply_text": "有的，我先给你列几套。",
            "confidence": 0.9,
            "source": "test_planner",
        }

        result = main._ensure_planner_action_contract(
            planner,
            understanding={
                "intent": "inventory",
                "structured_task": {"intent": "inventory"},
            },
            signals={},
        )

        self.assertEqual(result["actions"], ["search_inventory", "generate_reply"])
        self.assertEqual(result["reply_text"], "")
        self.assertEqual(result["pre_tool_reply_text"], "有的，我先给你列几套。")
        self.assertIn("action_contract", result["source"])

    def test_inventory_sheet_request_does_not_swallow_video_action(self) -> None:
        result = main._ensure_required_actions(
            {"actions": ["send_inventory_sheet"], "source": "test"},
            understanding={
                "intent": "inventory_sheet",
                "constraint_proof": {"wants_inventory_sheet": True, "wants_video": True},
                "structured_task": {
                    "intent": "inventory_sheet",
                    "tool_requirements": {
                        "needs_inventory_sheet": True,
                        "needs_video": True,
                    },
                },
            },
            signals={"wants_inventory_sheet": True, "wants_video": True},
        )

        self.assertIn("send_inventory_sheet", result["actions"])
        self.assertIn("search_inventory", result["actions"])
        self.assertIn("send_video", result["actions"])
        self.assertIn("generate_reply", result["actions"])

    def test_planner_unrequested_viewing_and_deposit_actions_are_removed(self) -> None:
        result = main._ensure_required_actions(
            {
                "actions": [
                    "search_inventory",
                    "send_inventory_sheet",
                    "explain_unavailable_viewing",
                    "send_deposit_policy",
                ],
                "source": "test",
            },
            understanding={
                "intent": "inventory",
                "constraint_proof": {"room_refs": ["3-1002A", "3-1002B"]},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_inventory_search": True}},
            },
            signals={},
        )

        self.assertEqual(result["actions"], ["search_inventory", "compact_listing", "generate_reply"])
        self.assertNotIn("send_inventory_sheet", result["actions"])
        self.assertNotIn("send_deposit_policy", result["actions"])
        self.assertNotIn("explain_unavailable_viewing", result["actions"])

    def test_viewing_signal_recognizes_today_wants_to_view(self) -> None:
        self.assertTrue(main._content_wants_viewing("客户今天想看，能自己看吗？"))
        self.assertTrue(main._content_wants_viewing("客户比较急，有没有马上空出来的？"))

    def test_price_comparison_selfcheck_requires_direct_conclusion(self) -> None:
        rows = [
            {"小区": "新柠长木府", "房号": "3-1002A", "押一付一": "4600", "押二付一": "4300"},
            {"小区": "新柠长木府", "房号": "3-1002B", "押一付一": "3500", "押二付一": "3200"},
        ]

        result = main._constraint_consistency_selfcheck(
            content="新柠长木府3-1002A和3-1002B价格一样吗？",
            draft_reply=(
                "新柠长木府3-1002A押一付一4600，押二付一4300。\n"
                "新柠长木府3-1002B押一付一3500，押二付一3200。"
            ),
            understanding={"intent": "inventory", "constraint_proof": {"room_refs": ["3-1002A", "3-1002B"]}},
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": rows},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("直接对比结论", result["reason"])

    def test_media_existence_selfcheck_allows_fulfilled_send_action(self) -> None:
        row = {"小区": "杨家新雅苑", "房号": "49-1102", "户型分类": "一室一厅"}

        result = main._constraint_consistency_selfcheck(
            content="杨家新雅苑49-1102视频有吗？先发我。",
            draft_reply="杨家新雅苑49-1102的视频发你了，这是杨家新雅苑49-1102的视频，你可以先看一下。",
            understanding={
                "intent": "media",
                "constraint_proof": {
                    "communities": ["杨家新雅苑"],
                    "room_refs": ["49-1102"],
                    "wants_video": True,
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "target_rows": [row],
                "video_paths": ["room_database/video/杨家新雅苑49-1102/demo.mp4"],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_clarification_selfcheck_does_not_require_inventory_answer(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="杨家府附近还有房子吗？客户名字可能说得不准。",
            draft_reply=(
                "你说的“杨家府”我这边有几个相近小区：兴业杨家府、杨乐府、"
                "杨家新雅苑、杨乐府北区。你确认下是哪一个，我再按最新房源表查。"
            ),
            understanding={
                "intent": "inventory",
                "entity_resolution": {
                    "community_options": [
                        {
                            "raw_text": "杨家府",
                            "options": ["兴业杨家府", "杨乐府", "杨家新雅苑", "杨乐府北区"],
                        }
                    ]
                },
                "constraint_proof": {
                    "areas": ["石桥街道/华丰/石桥/永佳/半山"],
                    "communities": ["杨家府"],
                },
            },
            tool_evidence={
                "actions": ["clarification"],
                "deterministic_reply_source": "rewrite_clarification",
            },
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result.get("scope"), "clarification")

    def test_clarification_selfcheck_requires_real_options_and_clear_request(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="杨家府附近还有房子吗？客户名字可能说得不准。",
            draft_reply="我先查一下。",
            understanding={
                "intent": "inventory",
                "entity_resolution": {
                    "community_options": [
                        {
                            "raw_text": "杨家府",
                            "options": ["兴业杨家府", "杨乐府", "杨家新雅苑", "杨乐府北区"],
                        }
                    ]
                },
                "constraint_proof": {
                    "areas": ["石桥街道/华丰/石桥/永佳/半山"],
                    "communities": ["杨家府"],
                },
            },
            tool_evidence={
                "actions": ["clarification"],
                "deterministic_reply_source": "rewrite_clarification",
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("真实候选小区", result["reason"])
        self.assertIn("补充或确认", result["reason"])

    def test_community_correction_selfcheck_requires_transparent_reply(self) -> None:
        row = {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}

        result = main._constraint_consistency_selfcheck(
            content="荣润府15-2-801B还在吗？1600那套视频发我。",
            draft_reply="有的，棠润府15-2-801B还在，这是这套的视频，发你了。",
            understanding={
                "intent": "media",
                "entity_resolution": {
                    "community_corrections": [
                        {"raw_text": "荣润府", "canonical": "棠润府", "reason": "unique_room_ref"}
                    ]
                },
                "constraint_proof": {
                    "communities": ["棠润府"],
                    "room_refs": ["15-2-801b"],
                    "wants_video": True,
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "target_rows": [row],
                "video_paths": ["room_database/video/棠润府15-2-801B/demo.mp4"],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("透明说明", result["reason"])

    def test_payment_selfcheck_accepts_price_before_payment_field(self) -> None:
        row = {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"}

        result = main._constraint_consistency_selfcheck(
            content="棠润府15-2-801B还在吗？1600那套视频发我。",
            draft_reply="还在的，这套是1600押一付一，1400押二付一，视频发你了。",
            understanding={
                "intent": "media",
                "constraint_proof": {"communities": ["棠润府"], "room_refs": ["15-2-801b"], "wants_video": True},
            },
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "target_rows": [row],
                "video_paths": ["room_database/video/棠润府15-2-801B/demo.mp4"],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_payment_selfcheck_rejects_wrong_price_before_payment_field(self) -> None:
        failures = main._payment_field_consistency_failures(
            "棠润府15-2-801B这套是1400押一付一，1600押二付一。",
            [{"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"}],
        )

        self.assertTrue(failures)
        self.assertIn("押一付一应为1600", failures[0])

    def test_payment_selfcheck_does_not_treat_room_no_as_price_before_field(self) -> None:
        failures = main._payment_field_consistency_failures(
            "石桥铭苑6-1102押一付一月租4800元。",
            [{"小区": "石桥铭苑", "房号": "6-1102", "押一付一": "4800", "押二付一": "4300"}],
        )

        self.assertEqual(failures, [])

    def test_budget_selfcheck_rejects_over_budget_wording_for_in_budget_price(self) -> None:
        failures = main._budget_payment_scope_failures(
            reply_text="大华海派风景2-1-402A押一付一1600元也刚过预算。",
            evidence_rows=[
                {"小区": "大华海派风景", "房号": "2-1-402A", "押一付一": "1600", "押二付一": "1500"}
            ],
            budget_range=[0, 1800],
        )

        self.assertTrue(failures)
        self.assertIn("不能说超预算", failures[0])

    def test_budget_selfcheck_rejects_exceed_wording_for_in_budget_price(self) -> None:
        failures = main._budget_payment_scope_failures(
            reply_text="大华海派风景2-1-402A押一付一1600元超出预算，押二付一1500元在预算内。",
            evidence_rows=[
                {"小区": "大华海派风景", "房号": "2-1-402A", "押一付一": "1600", "押二付一": "1500"}
            ],
            budget_range=[0, 1800],
        )

        self.assertTrue(failures)
        self.assertIn("不能说超预算", failures[0])

    async def test_exact_room_ref_falls_back_to_user_raw_when_rewrite_corrupts_room_no(self) -> None:
        class FakeInventory:
            async def search(self, query: str, limit: int = 8):
                self.last_query = query
                return []

            async def all_rows(self, *, limit: int = 500, refresh_if_needed: bool = True):
                return [
                    {
                        "小区": "杨家新雅苑",
                        "房号": "15-1-603",
                        "户型分类": "三室一厅",
                        "押一付一": "5300",
                        "押二付一": "5000",
                    }
                ]

        class FakeMediaStore:
            def list_room_database_videos(self, query: str, limit: int = 6):
                if "杨家新雅苑15-1-603" in query:
                    return [Path("room_database/video/杨家新雅苑15-1-603/demo.mp4")]
                return []

            def list_room_database_images(self, query: str, limit: int = 6):
                return []

        originals = {"inventory": main.inventory, "media_store": main.media_store}
        main.inventory = FakeInventory()
        main.media_store = FakeMediaStore()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                content="杨家新雅苑15-1-603有没有视频？客户预算5300左右。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "杨家新雅苑15-603 视频",
                    "rewritten_query": "杨家新雅苑15-603 视频",
                    "constraint_proof": {"wants_video": True, "room_refs": ["15-603"]},
                    "structured_task": {
                        "intent": "media",
                        "original_text": "杨家新雅苑15-1-603有没有视频？客户预算5300左右。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = originals["inventory"]
            main.media_store = originals["media_store"]

        self.assertEqual(evidence["target_rows"][0]["房号"], "15-1-603")
        self.assertEqual(
            [path.replace("\\", "/") for path in evidence["video_paths"]],
            ["room_database/video/杨家新雅苑15-1-603/demo.mp4"],
        )

    async def test_room_ref_mismatch_in_exact_community_asks_similar_room_confirmation(self) -> None:
        class FakeInventory:
            async def all_rows(self, *, limit: int = 500, refresh_if_needed: bool = True):
                return [
                    {"区域": "石桥街道\n华丰 石桥\n永佳 半山", "小区": "杨家新雅苑", "房号": "15-603"},
                    {"区域": "石桥街道\n华丰 石桥\n永佳 半山", "小区": "杨家新雅苑", "房号": "49-1102"},
                ]

        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "杨家新雅苑15-1-603视频",
                    "effective_query": "杨家新雅苑15-1-603视频",
                    "query_state": {"intent": "media", "layout": "一室一厅", "wants_video": True},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        originals = {"inventory": main.inventory, "reply_generator": main.reply_generator}
        main.inventory = FakeInventory()
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._understand_message(
                content="杨家新雅苑15-1-603有没有视频？客户预算5300左右。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("杨家新雅苑15-1-603有没有视频？客户预算5300左右。"),
            )
        finally:
            main.inventory = originals["inventory"]
            main.reply_generator = originals["reply_generator"]

        self.assertTrue(result["needs_clarification"])
        self.assertIn("杨家新雅苑15-603", result["clarification_text"])
        self.assertIn("确认是不是这套", result["clarification_text"])

    async def test_risky_similar_community_asks_confirmation_instead_of_not_found(self) -> None:
        class FakeInventory:
            async def all_rows(self, *, limit: int = 500, refresh_if_needed: bool = True):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "押一付一": "1600",
                        "押二付一": "1400",
                    }
                ]

        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "荣润府有没有押一付一的？预算1600到1800。",
                    "effective_query": "荣润府有没有押一付一的？预算1600到1800。",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        originals = {"inventory": main.inventory, "reply_generator": main.reply_generator}
        main.inventory = FakeInventory()
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._understand_message(
                content="荣润府有没有押一付一的？预算1600到1800。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("荣润府有没有押一付一的？预算1600到1800。"),
            )
        finally:
            main.inventory = originals["inventory"]
            main.reply_generator = originals["reply_generator"]

        self.assertTrue(result["needs_clarification"])
        self.assertIn("棠润府", result["clarification_text"])
        self.assertIn("你说的是", result["clarification_text"])
        self.assertNotIn("暂时没查到荣润府", result["clarification_text"])

    async def test_collect_room_media_syncs_current_room_from_feishu_when_local_missing(self) -> None:
        class FakeMediaStore:
            def __init__(self) -> None:
                self.synced = False
                self.path = Path("room_database/video/棠润府15-2-801B/demo.mp4")

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                return [self.path] if self.synced and "棠润府15-2-801B" in query else []

            def list_room_database_images(self, query: str, limit: int = 6) -> list[Path]:
                return []

        class FakeFeishuClient:
            sync_calls: list[dict] = []

            async def sync_media_for_rooms(self, rows, *, media_kind=None, target_root=None):
                self.sync_calls.append({"source": "all", "rows": rows, "media_kind": media_kind})
                return {"downloaded": [], "skipped": [], "missing": []}

            async def sync_drive_media_for_rooms(self, rows, *, media_kind=None, folder_token=None, target_root=None):
                fake_media_store.synced = True
                self.sync_calls.append(
                    {
                        "source": "region_drive",
                        "rows": rows,
                        "media_kind": media_kind,
                        "folder_token": folder_token,
                    }
                )
                return {"downloaded": [str(fake_media_store.path)], "skipped": [], "missing": []}

        fake_media_store = FakeMediaStore()
        original_media_store = main.media_store
        original_client = main.FeishuClient
        original_region_token = main.settings.feishu_region_sync_target_drive_folder_token
        main.media_store = fake_media_store
        main.FeishuClient = FakeFeishuClient
        main.settings.feishu_region_sync_target_drive_folder_token = "region-token"
        try:
            paths, rows, missing, sync_result = await main._collect_room_media(
                [{"小区": "棠润府", "房号": "15-2-801B"}],
                media_kind="video",
            )
        finally:
            main.media_store = original_media_store
            main.FeishuClient = original_client
            main.settings.feishu_region_sync_target_drive_folder_token = original_region_token

        self.assertEqual(paths, [Path("room_database/video/棠润府15-2-801B/demo.mp4")])
        self.assertEqual(rows, [{"小区": "棠润府", "房号": "15-2-801B"}])
        self.assertEqual(missing, [])
        self.assertIn("region_drive", sync_result)
        self.assertEqual(FakeFeishuClient.sync_calls[-1]["folder_token"], "region-token")

    async def test_collect_room_media_returns_local_hits_without_blocking_missing_sync(self) -> None:
        class FakeMediaStore:
            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                if "棠润府15-2-801B" in query:
                    return [Path("room_database/video/棠润府15-2-801B/demo.mp4")]
                return []

            def list_room_database_images(self, query: str, limit: int = 6) -> list[Path]:
                return []

        class FakeFeishuClient:
            sync_calls: list[dict] = []

            async def sync_media_for_rooms(self, rows, *, media_kind=None, target_root=None):
                self.sync_calls.append({"source": "all", "rows": rows, "media_kind": media_kind})
                return {"downloaded": [], "skipped": [], "missing": []}

        rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "17-503B"},
        ]
        originals = {
            "media_store": main.media_store,
            "FeishuClient": main.FeishuClient,
            "region_token": main.settings.feishu_region_sync_target_drive_folder_token,
        }
        main.media_store = FakeMediaStore()
        main.FeishuClient = FakeFeishuClient
        main.settings.feishu_region_sync_target_drive_folder_token = ""
        try:
            paths, matched_rows, missing, sync_result = await main._collect_room_media(
                rows,
                media_kind="video",
            )
        finally:
            main.media_store = originals["media_store"]
            main.FeishuClient = originals["FeishuClient"]
            main.settings.feishu_region_sync_target_drive_folder_token = originals["region_token"]

        self.assertEqual(paths, [Path("room_database/video/棠润府15-2-801B/demo.mp4")])
        self.assertEqual(matched_rows, [rows[0]])
        self.assertEqual(missing, ["星桥锦绣嘉苑17-503B"])
        self.assertEqual(sync_result, {})
        self.assertEqual(FakeFeishuClient.sync_calls, [])

    async def test_inventory_sheet_signal_overrides_llm_clarification(self) -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            async def rewrite_kf_message(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "intent": "unclear",
                    "rewritten_query": "房源表发一下",
                    "query_state": {"intent": "unclear"},
                    "needs_clarification": True,
                    "clarification_text": "你想看哪个小区？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                    }
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 1}

        fake = FakeReplyGenerator()
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = fake
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="房源表发一下",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("房源表发一下"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(result["intent"], "inventory_sheet")
        self.assertFalse(result["needs_clarification"])
        self.assertTrue(result["query_state"]["wants_inventory_sheet"])
        inventory_index = fake.kwargs["inventory_index"]
        self.assertTrue(inventory_index["sheet_request"])
        self.assertEqual(inventory_index["row_count"], 1)
        self.assertEqual(inventory_index["communities"][0]["name"], "合幢悦府")

    async def test_rewrite_layer_receives_latest_inventory_fact_index(self) -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            async def rewrite_kf_message(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "intent": "inventory",
                    "rewritten_query": "万达1500左右有哪些",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型": "一室一厅",
                        "押一付": "1500",
                    },
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "荣润府",
                        "房号": "15-2-801B",
                        "户型": "一室一厅",
                        "押一付": "1600",
                    },
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 2}

        fake = FakeReplyGenerator()
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = fake
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="万达1500左右有哪些",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("万达1500左右有哪些"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        inventory_index = fake.kwargs["inventory_index"]
        self.assertEqual(inventory_index["row_count"], 2)
        self.assertIn("区域", inventory_index["field_catalog"])
        self.assertEqual(inventory_index["exact_area_hits"][0]["canonical"], "拱墅万达\n北部软件园\n城北万象城")
        community_names = {item["name"] for item in inventory_index["communities"]}
        self.assertEqual(community_names, {"合幢悦府", "荣润府"})
        self.assertEqual(result["constraint_proof"]["area"], "拱墅万达\n北部软件园\n城北万象城")

    async def test_contextual_followup_replaces_budget_and_keeps_area_layout(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "unclear",
                    "rewritten_query": "4000-5000 的呢",
                    "query_state": {"intent": "unclear"},
                    "needs_clarification": True,
                    "clarification_text": "你确认下小区+房号，我再查。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "东新园\n杭氧\n新天地",
                        "小区": "新柠长木府",
                        "房号": "3-1002A",
                        "户型分类": "两室一厅",
                        "押一付一": "4600",
                        "押二付一": "4300",
                    },
                    {
                        "区域": "东新园\n杭氧\n新天地",
                        "小区": "长浜龙吟轩",
                        "房号": "11-1603",
                        "户型分类": "两室一厅",
                        "押一付一": "4200",
                        "押二付一": "3900",
                    },
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 2}

        previous_reply = (
            "有的，东新园、杭氧、新天地、3500-4500左右、两室我查到这些还在租：\n"
            "1. 诸葛龙吟院10-601A，两室一厅，押一付一3700，押二付一3400\n"
            "2. 长浜龙吟轩11-1603，两室一厅，押一付一4200，押二付一3900\n"
            "你要视频、图片或者看房方式的话，直接回序号或小区+房号就行。"
        )
        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="新天地有什么4000左右的两室",
        )
        context = kf_context_memory.start_structured_turn(
            context,
            state={},
            user_input={"content": "新天地有什么4000左右的两室"},
            rewrite_result={
                "intent": "inventory",
                "rewritten_query": "东新园/杭氧/新天地 3500-4500 两室 在租房源",
                "query_state": {
                    "intent": "inventory",
                    "area": "东新园\n杭氧\n新天地",
                    "budget_range": [3500, 4500],
                    "layout": "两室",
                },
            },
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply=previous_reply,
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content=previous_reply,
        )

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="4000-5000 的呢",
                context=context,
                signals=main._deterministic_signals("4000-5000 的呢"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["intent"], "inventory")
        self.assertIn("东新园", result["effective_query"])
        self.assertIn("4000-5000", result["effective_query"])
        self.assertIn("两室", result["effective_query"])
        self.assertEqual(result["query_state"]["budget_range"], [4000, 5000])
        self.assertEqual(result["query_state"]["layout"], "两室")
        self.assertEqual(result["constraint_proof"]["budget_range"], [4000, 5000])
        self.assertEqual(result["constraint_proof"]["layout"], "两室")

    async def test_contextual_followup_inventory_index_uses_blackbox_constraints_before_llm(self) -> None:
        captured: dict[str, object] = {}

        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                captured.update(kwargs)
                return {
                    "intent": "unclear",
                    "rewritten_query": "4000-5000 \u7684\u5462",
                    "query_state": {"intent": "unclear"},
                    "needs_clarification": True,
                    "clarification_text": "\u4f60\u786e\u8ba4\u4e0b\u5c0f\u533a+\u623f\u53f7\uff0c\u6211\u518d\u67e5\u3002",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "\u533a\u57df": "\u4e1c\u65b0\u56ed\n\u676d\u6c27\n\u65b0\u5929\u5730",
                        "\u5c0f\u533a": "\u65b0\u67e0\u957f\u6728\u5e9c",
                        "\u623f\u53f7": "3-1002A",
                        "\u6237\u578b\u5206\u7c7b": "\u4e24\u5ba4\u4e00\u5385",
                        "\u62bc\u4e00\u4ed8\u4e00": "4600",
                        "\u62bc\u4e8c\u4ed8\u4e00": "4300",
                    }
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 1}

        previous_reply = (
            "\u6709\u7684\uff0c\u4e1c\u65b0\u56ed\u3001\u676d\u6c27\u3001\u65b0\u5929\u5730\u3001"
            "3500-4500\u5de6\u53f3\u3001\u4e24\u5ba4\u6211\u67e5\u5230\u8fd9\u4e9b\u8fd8\u5728\u79df\uff1a\n"
            "1. \u65b0\u67e0\u957f\u6728\u5e9c3-1002A\uff0c\u4e24\u5ba4\u4e00\u5385\uff0c\u62bc\u4e00\u4ed8\u4e004600\uff0c\u62bc\u4e8c\u4ed8\u4e004300\n"
            "\u4f60\u8981\u89c6\u9891\u3001\u56fe\u7247\u6216\u8005\u770b\u623f\u65b9\u5f0f\u7684\u8bdd\uff0c\u76f4\u63a5\u56de\u5e8f\u53f7\u6216\u5c0f\u533a+\u623f\u53f7\u5c31\u884c\u3002"
        )
        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="\u65b0\u5929\u5730\u6709\u4ec0\u4e483500\u5de6\u53f3\u7684\u4e24\u5ba4\uff1f",
        )
        context = kf_context_memory.start_structured_turn(
            context,
            state={},
            user_input={"content": "\u65b0\u5929\u5730\u6709\u4ec0\u4e483500\u5de6\u53f3\u7684\u4e24\u5ba4\uff1f"},
            rewrite_result={
                "intent": "inventory",
                "rewritten_query": "\u4e1c\u65b0\u56ed/\u676d\u6c27/\u65b0\u5929\u5730 3500-4500 \u4e24\u5ba4 \u5728\u79df\u623f\u6e90",
                "query_state": {
                    "intent": "inventory",
                    "area": "\u4e1c\u65b0\u56ed\n\u676d\u6c27\n\u65b0\u5929\u5730",
                    "budget_range": [3500, 4500],
                    "layout": "\u4e24\u5ba4",
                },
            },
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply=previous_reply,
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content=previous_reply,
        )

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            await main._understand_message(
                content="4000-5000 \u7684\u5462",
                context=context,
                signals=main._deterministic_signals("4000-5000 \u7684\u5462"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        inventory_index = captured["inventory_index"]
        self.assertIn("4000-5000", inventory_index["rewrite_index_query"])
        self.assertIn("\u65b0\u5929\u5730", inventory_index["rewrite_index_query"])
        self.assertIn("\u4e24\u5ba4", inventory_index["rewrite_index_query"])
        self.assertTrue(
            any(
                "\u65b0\u5929\u5730" in str(item.get("canonical") or "")
                for item in inventory_index["exact_area_hits"]
            )
        )

    async def test_contextual_followup_prefers_blackbox_context_over_wrong_llm_guess(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "拱墅万达 4000-5000 一室 在租房源",
                    "query_state": {
                        "intent": "inventory",
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "budget_range": [4000, 5000],
                        "layout": "一室",
                    },
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "东新园\n杭氧\n新天地",
                        "小区": "新柠长木府",
                        "房号": "3-1002A",
                        "户型分类": "两室一厅",
                        "押一付一": "4600",
                        "押二付一": "4300",
                    },
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "荣润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                        "押二付一": "1400",
                    },
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 2}

        previous_reply = (
            "有的，东新园、杭氧、新天地、3500-4500左右、两室我查到这些还在租：\n"
            "1. 长浜龙吟轩11-1603，两室一厅，押一付一4200，押二付一3900\n"
            "2. 新柠长木府3-1002A，两室一厅，押一付一4600，押二付一4300\n"
            "你要视频、图片或者看房方式的话，直接回序号或小区+房号就行。"
        )
        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="新天地有什么4000左右的两室",
        )
        context = kf_context_memory.start_structured_turn(
            context,
            state={},
            user_input={"content": "新天地有什么4000左右的两室"},
            rewrite_result={
                "intent": "inventory",
                "rewritten_query": "东新园/杭氧/新天地 3500-4500 两室 在租房源",
                "query_state": {
                    "intent": "inventory",
                    "area": "东新园\n杭氧\n新天地",
                    "budget_range": [3500, 4500],
                    "layout": "两室",
                },
            },
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply=previous_reply,
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content=previous_reply,
        )

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="4000-5000 的呢",
                context=context,
                signals=main._deterministic_signals("4000-5000 的呢"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["intent"], "inventory")
        self.assertIn("东新园", result["effective_query"])
        self.assertIn("4000-5000", result["effective_query"])
        self.assertIn("两室", result["effective_query"])
        self.assertNotIn("拱墅万达", result["effective_query"])
        self.assertEqual(result["query_state"]["area"], "东新园\n杭氧\n新天地")
        self.assertEqual(result["query_state"]["layout"], "两室")
        self.assertEqual(result["constraint_proof"]["area"], "东新园\n杭氧\n新天地")
        self.assertEqual(result["constraint_proof"]["layout"], "两室")

    async def test_new_explicit_area_query_drops_unasked_inherited_budget(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "石桥街道/华丰/石桥/永佳/半山 4800-5800 带燃气一室一厅 在租房源",
                    "query_state": {
                        "intent": "inventory",
                        "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "budget_range": [4800, 5800],
                        "budget": "4800-5800",
                        "layout": "一室一厅",
                        "features": ["燃气"],
                    },
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "华丰欣苑",
                        "房号": "14-2-901",
                        "户型描述": "一室一厅带燃气",
                        "户型分类": "一室一厅",
                        "押一付一": "4200",
                        "押二付一": "3900",
                    }
                ]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 1}

        previous_reply = "还在，石桥铭苑6-1102一室一厅，押一付一4800元，押二付一4300元，民用水电。"
        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="石桥铭苑6-1102还在吗？押一押二价格发下。",
        )
        context = kf_context_memory.start_structured_turn(
            context,
            state={},
            user_input={"content": "石桥铭苑6-1102还在吗？押一押二价格发下。"},
            rewrite_result={
                "intent": "inventory",
                "rewritten_query": "石桥铭苑6-1102 价格",
                "query_state": {"intent": "inventory"},
                "needs_clarification": True,
            },
        )
        context = kf_context_memory.record_structured_assistant_output(
            context,
            final_reply="最新房源表没查到杨家新雅苑15-1-603这套，只匹配到相近房号：杨家新雅苑15-603。你确认是不是这套？",
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content="最新房源表没查到杨家新雅苑15-1-603这套，只匹配到相近房号：杨家新雅苑15-603。你确认是不是这套？",
        )

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="华丰附近有没有带燃气的一室一厅？",
                context=context,
                signals=main._deterministic_signals("华丰附近有没有带燃气的一室一厅？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["intent"], "inventory")
        self.assertNotIn("4800", result["effective_query"])
        self.assertNotIn("5800", result["effective_query"])
        self.assertNotIn("budget_range", result["query_state"])
        self.assertNotIn("budget_range", result["constraint_proof"])
        self.assertEqual(result["constraint_proof"]["area"], "石桥街道\n华丰\n石桥\n永佳\n半山")
        self.assertEqual(result["constraint_proof"]["layout"], "一室一厅")
        self.assertIn("燃气", result["constraint_proof"]["features"])

    def test_candidate_hint_returns_multiple_rows_for_explicit_same_community(self) -> None:
        candidates = [
            {"小区": "兴业杨家府", "房号": "3-601"},
            {"小区": "兴业杨家府", "房号": "10-1-304"},
            {"小区": "杨家新雅苑", "房号": "15-1-603"},
        ]

        rows = main._candidate_rows_from_context_hint(
            candidates=candidates,
            query_text="兴业杨家府的呢",
            proof={"communities": ["兴业杨家府"]},
            context_reference=True,
        )

        self.assertEqual([row["房号"] for row in rows], ["3-601", "10-1-304"])

    def test_candidate_hint_all_returns_previous_candidates(self) -> None:
        candidates = [
            {"小区": "兴业杨家府", "房号": "3-601"},
            {"小区": "杨家新雅苑", "房号": "15-1-603"},
        ]

        rows = main._candidate_rows_from_context_hint(
            candidates=candidates,
            query_text="这两个都发我",
            proof={},
            context_reference=True,
        )

        self.assertEqual(rows, candidates)

    async def test_candidate_selection_media_followup_does_not_become_community_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "拱墅万达/北部软件园/城北万象城 0-2000 一室 视频",
                    "query_state": {
                        "intent": "media",
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "budget": "0-2000",
                        "layout": "一室",
                        "wants_video": True,
                    },
                    "needs_clarification": True,
                    "clarification_text": "最新房源表里暂时没查到前两套这个小区。",
                }

        candidates = [
            {"区域": "拱墅万达\n北部软件园\n城北万象城", "小区": "瑷颐湾", "房号": "13-1-402A", "户型分类": "一室", "押一付一": "600"},
            {"区域": "拱墅万达\n北部软件园\n城北万象城", "小区": "大华海派风景", "房号": "2-1-402A", "户型分类": "一室", "押一付一": "1600"},
            {"区域": "拱墅万达\n北部软件园\n城北万象城", "小区": "星桥锦绣嘉苑", "房号": "20-1606A", "户型分类": "一室一厅", "押一付一": "1900"},
        ]

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return candidates

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": len(candidates)}

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "拱墅万达 0-2000 一室",
            "candidates": candidates,
            "shown_count": 3,
            "total_count": 3,
        }

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="前两套视频先发我，我给客户筛一下。",
                context=context,
                signals=main._deterministic_signals("前两套视频先发我，我给客户筛一下。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["selected_indices"], [1, 2])
        self.assertEqual(result["constraint_proof"]["selected_indices"], [1, 2])
        self.assertNotIn("room_refs", result["constraint_proof"])
        rows = main._target_rows_from_understanding(result, context, candidates)
        self.assertEqual([row["小区"] for row in rows], ["瑷颐湾", "大华海派风景"])

    async def test_bound_room_followup_does_not_become_community_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "皋塘运都9-402B 水电",
                    "query_state": {"intent": "inventory", "wants_utilities": True},
                    "needs_clarification": True,
                    "clarification_text": "最新房源表里暂时没查到水电怎么收这个小区。",
                }

        row = {
            "区域": "闸弄口\n新塘\n元宝塘\n东站",
            "小区": "皋塘运都",
            "房号": "9-402B",
            "户型分类": "一室一厅",
            "押一付一": "2600",
            "备注": "水30/月，电1元/度",
        }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [row]

            def cache_meta(self) -> dict:
                return {"status": "success", "row_count": 1}

        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {"row": row, "label": "皋塘运都9-402B", "intent": "details"}

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="这套水电怎么收？",
                context=context,
                signals=main._deterministic_signals("这套水电怎么收？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        rows = main._target_rows_from_understanding(result, context, [row])
        self.assertEqual(rows, [row])

    def test_constraint_proof_keeps_multiple_resolved_areas(self) -> None:
        proof = main._build_constraint_proof(
            content="万达、东新园两边都可以，3000以内有什么能住的？",
            effective_query="万达和东新园两边，预算3000以内，查询在租房源。",
            understanding={"intent": "inventory", "query_state": {"intent": "inventory"}},
            entity_resolution={
                "status": "resolved",
                "areas": [
                    {"raw_text": "万达", "canonical": "拱墅万达\n北部软件园\n城北万象城"},
                    {"raw_text": "东新园", "canonical": "东新园\n杭氧\n新天地"},
                ],
            },
            signals=main._deterministic_signals("万达、东新园两边都可以，3000以内有什么能住的？"),
        )

        self.assertIn("拱墅万达", proof["area"])
        self.assertIn("东新园", proof["area"])
        self.assertEqual(proof["budget_range"], [0, 3000])

    def test_constraint_proof_normalizes_llm_area_list_artifact(self) -> None:
        proof = main._build_constraint_proof(
            content="4000-5000的呢？",
            effective_query="['东新园\\n杭氧\\n新天地'] 4000-5000 两室一厅 在租房源",
            understanding={
                "intent": "inventory",
                "query_state": {
                    "intent": "inventory",
                    "area": ["东新园\n杭氧\n新天地"],
                    "layout": "两室一厅",
                },
            },
            entity_resolution={"status": "resolved", "areas": []},
            signals=main._deterministic_signals("4000-5000的呢？"),
        )
        effective = main._enforce_effective_query(
            content="4000-5000的呢？",
            understanding={
                "effective_query": "['东新园\\n杭氧\\n新天地'] 4000-5000 两室一厅 在租房源",
                "rewritten_query": "['东新园\\n杭氧\\n新天地'] 4000-5000 两室一厅 在租房源",
            },
            constraint_proof=proof,
        )

        self.assertEqual(proof["area"], "东新园\n杭氧\n新天地")
        self.assertNotIn("[", proof["area"])
        self.assertNotIn("[", effective)
        self.assertIn("东新园", effective)
        self.assertIn("4000-5000", effective)

    def test_unasked_llm_inferred_layout_is_dropped_for_new_budget_query(self) -> None:
        result = {
            "intent": "inventory",
            "effective_query": "查询闸弄口东站附近预算1500到1800元的在租房源，包含一室和一室一厅户型。",
            "rewritten_query": "查询闸弄口东站附近预算1500到1800元的在租房源，包含一室和一室一厅户型。",
            "query_state": {"intent": "inventory", "layout": "一室一厅"},
        }
        content = "闸弄口东站附近1500到1800的还有吗？"

        self.assertTrue(main._should_drop_unasked_llm_inferred_layout_features(content, result["effective_query"]))
        cleaned = main._drop_unasked_llm_inferred_layout_features(result, content=content)

        self.assertEqual(cleaned["effective_query"], content)
        self.assertNotIn("layout", cleaned["query_state"])

    def test_unasked_layout_in_query_state_is_dropped_even_if_effective_query_is_clean(self) -> None:
        result = {
            "intent": "inventory",
            "effective_query": "查询闸弄口东站附近预算1500到1800元的在租房源。",
            "rewritten_query": "查询闸弄口东站附近预算1500到1800元的在租房源。",
            "query_state": {"intent": "inventory", "layout": "一室"},
        }
        content = "闸弄口东站附近1500到1800的还有吗？"

        self.assertTrue(
            main._should_drop_unasked_llm_inferred_layout_features(
                content,
                result["effective_query"],
                result["query_state"],
            )
        )
        cleaned = main._drop_unasked_llm_inferred_layout_features(result, content=content)

        self.assertEqual(cleaned["effective_query"], content)
        self.assertNotIn("layout", cleaned["query_state"])

    async def test_planner_missing_evidence_returns_to_rewrite_layer(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: list[str] = []

            def is_processed(self, msgid: str) -> bool:
                return msgid in self.processed

            def mark_processed(self, msgid: str) -> None:
                self.processed.append(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.rewrite_calls: list[dict] = []

            async def rewrite_kf_message(self, **kwargs):
                self.rewrite_calls.append(kwargs)
                if kwargs.get("planner_feedback"):
                    return {
                        "intent": "media",
                        "rewritten_query": "根据上一轮候选发送视频",
                        "effective_query": "根据上一轮候选发送视频",
                        "query_state": {"intent": "media", "wants_video": True},
                        "context_reference": True,
                        "selected_indices": [],
                        "needs_clarification": False,
                        "tool_plan": {"actions": ["generate_reply"], "confidence": 0.9},
                    }
                return {
                    "intent": "media",
                    "rewritten_query": "视频发我",
                    "effective_query": "视频发我",
                    "query_state": {"intent": "media", "wants_video": True},
                    "context_reference": True,
                    "selected_indices": [],
                    "needs_clarification": False,
                    "tool_plan": {
                        "actions": [],
                        "need_rewrite_clarification": True,
                        "missing_evidence": "缺少上下文绑定证据",
                    },
                }

            async def generate(
                self,
                message,
                inventory_snapshot: str,
                media_images: list[str],
                media_videos: list[str],
                conversation_context: str = "",
                knowledge_context: str = "",
            ) -> ReplyPlan:
                return ReplyPlan(text="我按刚才那套重新查了。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeInventory:
            async def all_rows(self, **kwargs) -> list[dict]:
                return []

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return []

            async def snapshot(self, limit: int = 20) -> str:
                return "暂无房源"

            def format_rows(self, rows: list[dict], limit: int = 10) -> str:
                return ""

            def cache_meta(self) -> dict:
                return {}

        class FakeAgenticRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", evidence=[], dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", reason="", fallback_reply="")

        fake_wecom = FakeWeComKf()
        fake_context_store = FakeContextStore()
        fake_reply_generator = FakeReplyGenerator()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "agentic_rag": main.agentic_rag,
        }
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = fake_context_store
        main.reply_generator = fake_reply_generator
        main.inventory = FakeInventory()
        main.agentic_rag = FakeAgenticRag()
        try:
            await main._handle_text_message(
                {
                    "msgid": "msg-1",
                    "msgtype": "text",
                    "origin": 3,
                    "open_kfid": "kf",
                    "external_userid": "wm",
                    "text": {"content": "视频发我"},
                }
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(hasattr(fake_reply_generator, "plan_kf_tool_actions"))
        self.assertEqual(len(fake_reply_generator.rewrite_calls), 2)
        self.assertEqual(
            fake_reply_generator.rewrite_calls[1]["planner_feedback"]["missing_evidence"],
            "缺少上下文绑定证据",
        )
        self.assertIn("不能乱发视频", fake_wecom.texts[-1])
        self.assertIn("小区名+房号", fake_wecom.texts[-1])
        saved_context = fake_context_store.data[main._conversation_key("kf", "wm")]
        record = saved_context["structured_memory"]["turn_records"][-1]
        self.assertEqual(record["assistant_sent_summary"]["final_reply"], fake_wecom.texts[-1])
        self.assertEqual(record["intent"], "media")
        self.assertNotIn("planner_feedback", record)

    async def test_understanding_adds_entity_resolution_and_constraint_proof(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "鑫天地4000左右两室在租房源",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "东新园\n杭氧\n新天地",
                        "小区": "新柠长木府",
                        "房号": "3-1002B",
                        "户型分类": "两室一厅",
                        "押一付": "3500",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="鑫天地有没有4000左右的两室",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("鑫天地有没有4000左右的两室"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["areas"][0]["canonical"], "东新园\n杭氧\n新天地")
        self.assertEqual(result["constraint_proof"]["budget_range"], [3500, 4500])
        self.assertEqual(result["constraint_proof"]["layout"], "两室")
        self.assertIn("东新园", result["effective_query"])
        self.assertEqual(result["structured_task"]["constraint_proof"]["layout"], "两室")

    async def test_exact_community_resolution_overrides_llm_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "兴业杨家府在租房源",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "我这边为了避免发错，先不乱发。你把小区+房号或更具体条件发我一下。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道\n华丰 石桥\n永佳 半山",
                        "小区": "兴业杨家府",
                        "房号": "4-1502",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                        "押二付一": "4200",
                    },
                    {
                        "区域": "石桥街道\n华丰 石桥\n永佳 半山",
                        "小区": "兴业杨家府",
                        "房号": "10-1-1205",
                        "户型分类": "两室一厅",
                        "押一付一": "3900",
                        "押二付一": "3700",
                    },
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="兴业杨家府有什么房",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("兴业杨家府有什么房"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["constraint_proof"]["communities"], ["兴业杨家府"])
        self.assertEqual(result["query_state"]["community"], "兴业杨家府")
        self.assertEqual(
            result["structured_task"]["clarification"]["reason"],
            "exact_community_resolved",
        )

    async def test_constraint_proof_keeps_broad_one_room_from_user_text(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "在拱墅万达/北部软件园/城北万象城区域，2000以下的一室或一室一厅房源",
                    "query_state": {
                        "intent": "inventory",
                        "area": "拱墅万达/北部软件园/城北万象城",
                        "budget": "2000以下",
                        "layout": "一室（宽匹配）",
                    },
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="万达有什么2000以下的一室",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("万达有什么2000以下的一室"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["constraint_proof"]["layout"], "一室")
        self.assertEqual(result["structured_task"]["constraint_proof"]["layout"], "一室")

    async def test_constraint_proof_keeps_feature_constraints_from_user_text(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "华丰附近带燃气的一室一厅在租房源",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="华丰附近有没有带燃气的一室一厅？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("华丰附近有没有带燃气的一室一厅？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["constraint_proof"]["layout"], "一室一厅")
        self.assertEqual(result["constraint_proof"]["features"], ["燃气"])
        self.assertEqual(result["structured_task"]["constraint_proof"]["features"], ["燃气"])

    def test_entity_resolution_matches_letter_prefix_room_ref_from_community(self) -> None:
        result = main._build_entity_resolution(
            "东方茂T3-1540是不是一室一厅？价格多少？",
            [{"小区": "东方茂商业中心T", "房号": "3-1540", "区域": "东新园\n杭氧\n新天地"}],
        )

        self.assertEqual(result["communities"][0]["canonical"], "东方茂商业中心T")
        self.assertEqual(result["room_ref_hits"][0]["room_no"], "3-1540")

    async def test_exact_community_with_particle_resolves_without_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "兴业杨家府在租房源",
                    "effective_query": "兴业杨家府在租房源",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道 华丰 石桥 永佳 半山",
                        "小区": "兴业杨家府",
                        "房号": "4-1502",
                    },
                    {
                        "区域": "石桥街道 华丰 石桥 永佳 半山",
                        "小区": "兴业杨家府",
                        "房号": "10-1-1205",
                    },
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="兴业杨家府呢",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("兴业杨家府呢"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["communities"][0]["canonical"], "兴业杨家府")
        self.assertIn("兴业杨家府", result["constraint_proof"]["communities"])

    async def test_ambiguous_community_generates_real_candidate_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "杨家府有吗",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"小区": "杨乐府", "房号": "1-101"},
                    {"小区": "杨家新雅苑", "房号": "2-202"},
                    {"小区": "兴业杨家府", "房号": "3-303"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="杨家府有吗",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("杨家府有吗"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("杨乐府", result["clarification_text"])
        self.assertIn("兴业杨家府", result["clarification_text"])
        self.assertEqual(result["entity_resolution"]["status"], "ambiguous")

    async def test_unique_room_ref_does_not_override_conflicting_unknown_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "荣润府15-2-801B还在吗，1600那套视频",
                    "query_state": {"intent": "media", "wants_video": True},
                    "needs_clarification": True,
                    "clarification_text": "你说的是棠润府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="荣润府15-2-801B还在吗？1600那套视频发我。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("荣润府15-2-801B还在吗？1600那套视频发我。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "needs_confirmation")
        self.assertEqual(result["entity_resolution"]["communities"], [])
        self.assertEqual(result["entity_resolution"]["community_options"][0]["reason"], "room_ref_community_mismatch")
        self.assertIn("棠润府", result["clarification_text"])
        self.assertIn("确认", result["clarification_text"])

    async def test_unique_room_ref_allows_configured_community_typo_alias(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "棠闰府15-2-801B视频",
                    "query_state": {"intent": "media", "wants_video": True},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="棠闰府15-2-801B视频发我。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("棠闰府15-2-801B视频发我。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["constraint_proof"]["communities"], ["棠润府"])
        self.assertIn("15-2-801b", result["constraint_proof"]["room_refs"])

    async def test_confirmed_context_allows_low_similarity_community_reuse(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "荣润府15-2-801B视频",
                    "query_state": {"intent": "media", "wants_video": True},
                    "needs_clarification": True,
                    "clarification_text": "你说的是棠润府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="荣润府15-2-801B还在吗？",
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content="你说的应该是棠润府15-2-801B，还在的，押一付一1600。",
        )
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="荣润府15-2-801B视频发我。",
                context=context,
                signals=main._deterministic_signals("荣润府15-2-801B视频发我。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["communities"][0]["source"], "conversation_memory")
        self.assertEqual(result["constraint_proof"]["communities"], ["棠润府"])
        self.assertIn("15-2-801b", result["constraint_proof"]["room_refs"])

    async def test_suggested_context_allows_low_similarity_community_reuse(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "荣润府有没有押一付一的，预算1600到1800",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是棠润府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="荣润府15-2-801B还在吗？1600那套视频发我。",
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content="你说的是棠润府吗？我先确认一下小区名。",
        )
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="荣润府有没有押一付一的？预算1600到1800。",
                context=context,
                signals=main._deterministic_signals("荣润府有没有押一付一的？预算1600到1800。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["communities"][0]["source"], "conversation_memory")
        self.assertEqual(result["constraint_proof"]["communities"], ["棠润府"])

    async def test_three_character_unknown_community_asks_confirmation_without_auto_resolve(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "荣润府有没有押一付一的，预算1600到1800",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是棠润府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="荣润府有没有押一付一的？预算1600到1800。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("荣润府有没有押一付一的？预算1600到1800。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["communities"], [])
        self.assertNotIn("棠润府", result["constraint_proof"].get("communities") or [])
        self.assertIn("你说的是棠润府吗", result["clarification_text"])

    async def test_discourse_prefix_is_not_part_of_fuzzy_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "客户又问杨家新雅苑有没有三室的",
                    "effective_query": "客户又问杨家新雅苑有没有三室的",
                    "query_state": {"intent": "inventory", "community": "又问杨家新雅苑", "layout": "三室"},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "杨家新雅苑",
                        "房号": "15-603",
                        "户型分类": "三室一厅",
                        "押一付一": "5600",
                    },
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "兴业杨家府",
                        "房号": "3-601",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                    },
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="客户又问杨家新雅苑有没有三室的。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("客户又问杨家新雅苑有没有三室的。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["constraint_proof"]["communities"], ["杨家新雅苑"])
        self.assertNotIn("又问杨家新雅苑", result["constraint_proof"]["communities"])
        self.assertIn("杨家新雅苑", result["effective_query"])
        self.assertNotIn("又问杨家新雅苑", result["effective_query"])

    async def test_room_ref_digits_are_not_promoted_to_budget_constraint(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "你说的是棠润府的话，15-2-801B还在吗？801预算",
                    "effective_query": "你说的是棠润府的话，15-2-801B还在吗？801预算",
                    "query_state": {"intent": "inventory", "community": "棠润府", "budget": "15-2-801B预算"},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="你说的是棠润府的话，15-2-801B还在吗？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("你说的是棠润府的话，15-2-801B还在吗？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(result["constraint_proof"]["communities"], ["棠润府"])
        self.assertEqual(result["constraint_proof"]["room_refs"], ["15-2-801b"])
        self.assertNotIn("budget_range", result["constraint_proof"])
        self.assertNotIn("budget_label", result["constraint_proof"])
        self.assertNotIn("801预算", result["effective_query"])

    async def test_contextual_community_correction_reuses_previous_typo_resolution(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "杨家府有没有押一付一的，预算4500左右",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是杨乐府、杨家新雅苑还是兴业杨家府？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "杨乐府",
                        "房号": "9-1002",
                        "户型分类": "一室一厅",
                        "押一付一": "4800",
                    },
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "杨家新雅苑",
                        "房号": "49-1102",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                    },
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "兴业杨家府",
                        "房号": "3-601",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                        "押二付一": "4200",
                    }
                ]

        context = kf_context_memory.empty_context()
        context = kf_context_memory.append_dialog_message(
            context,
            role="user",
            content="杨家府3-601还在吗？视频发我。",
        )
        context = kf_context_memory.append_dialog_message(
            context,
            role="assistant",
            content="你说的应该是兴业杨家府3-601，还在的，有的，这是兴业杨家府3-601的视频。",
        )
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="杨家府有没有押一付一的？预算4500左右。",
                context=context,
                signals=main._deterministic_signals("杨家府有没有押一付一的？预算4500左右。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["communities"][0]["source"], "conversation_memory")
        self.assertEqual(result["constraint_proof"]["communities"], ["兴业杨家府"])
        self.assertEqual(
            result["entity_resolution"]["community_corrections"][-1]["reason"],
            "conversation_memory",
        )

    async def test_contextual_community_correction_reads_turn_records_beyond_raw_dialog(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "杨家府有没有押一付一的，预算4500左右",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是杨乐府、杨家新雅苑还是兴业杨家府？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "杨乐府",
                        "房号": "9-1002",
                        "户型分类": "一室一厅",
                        "押一付一": "4800",
                    },
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "杨家新雅苑",
                        "房号": "49-1102",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                    },
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "兴业杨家府",
                        "房号": "3-601",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                        "押二付一": "4200",
                    }
                ]

        context = kf_context_memory.empty_context()
        context["structured_memory"] = {
            "raw_dialog_context": [
                {"role": "user", "content": "万达附近的房源表发我一下"},
                {"role": "assistant", "content": "拱墅万达附近的房源表发你了。"},
            ],
            "turn_records": [
                {
                    "turn_id": "turn-2",
                    "turn_index": 2,
                    "user_raw": "杨家府3-601还在吗？视频发我。",
                    "rewrite_result": {
                        "rewritten_query": "兴业杨家府3-601还在吗，视频发我",
                        "intent": "media",
                    },
                    "assistant_sent_summary": {
                        "final_reply": "你说的应该是兴业杨家府3-601，还在，有的，这是兴业杨家府3-601的视频。"
                    },
                },
                {
                    "turn_id": "turn-7",
                    "turn_index": 7,
                    "user_raw": "万达附近的房源表发我一下",
                    "rewrite_result": {
                        "rewritten_query": "拱墅万达附近的房源表发我一下",
                        "intent": "inventory_sheet",
                    },
                    "assistant_sent_summary": {
                        "final_reply": "拱墅万达附近的房源表发你了。"
                    },
                },
            ],
        }
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="杨家府有没有押一付一的？预算4500左右。",
                context=context,
                signals=main._deterministic_signals("杨家府有没有押一付一的？预算4500左右。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["communities"][0]["source"], "conversation_memory")
        self.assertEqual(result["constraint_proof"]["communities"], ["兴业杨家府"])

    async def test_rewrite_clarification_cannot_reference_stale_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "合峙悦府6-1-1204B是不是1500，今天能看吗",
                    "query_state": {"intent": "viewing"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是合嵣悦府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"小区": "棠润府", "房号": "15-2-801B"},
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="合峙悦府6-1-1204B是不是1500？今天能看吗？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("合峙悦府6-1-1204B是不是1500？今天能看吗？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertNotIn("合嵣悦府", result["clarification_text"])
        self.assertEqual(result["clarification_text"], "")
        self.assertTrue(result["query_state"]["needs_tool_verification"])
        self.assertEqual(
            result["structured_task"]["clarification"]["reason"],
            "rewrite_layer_not_found_claim_routed_to_tools",
        )

    async def test_rewrite_drops_stale_room_ref_when_current_query_has_no_room_ref(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "皋塘运都9-2-402B是否有两室户型？预算4500以内。",
                    "effective_query": "皋塘运都9-2-402B是否有两室户型？预算4500以内。 闸弄口 新塘 元宝塘 东站",
                    "query_state": {"intent": "inventory", "layout": "两室"},
                    "needs_clarification": True,
                    "clarification_text": "最新房源表中未查到皋塘运都9-2-402B这套房，您确认下是否为标准房号？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "闸弄口\n新塘\n元宝塘\n东站", "小区": "骏塘名庭", "房号": "8-1101A", "户型分类": "一室"},
                    {"区域": "闸弄口\n新塘\n元宝塘\n东站", "小区": "京漾东韵府", "房号": "4-2-601D", "户型分类": "一室"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="皋塘运都有没有两室？预算4500以内。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("皋塘运都有没有两室？预算4500以内。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertNotIn("9-2-402", result["effective_query"])
        self.assertNotIn("room_refs", result["constraint_proof"])
        self.assertTrue(result.get("dropped_inherited_room_refs"))

    async def test_similar_community_with_unmatched_room_ref_routes_not_found_to_tools(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "合峙悦府6-1-1204B是不是1500，今天能看吗",
                    "query_state": {"intent": "viewing"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是合嵣悦府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"小区": "合嵣悦府", "房号": "20-1-2604A", "押一付": "2700"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="合峙悦府6-1-1204B是不是1500？今天能看吗？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("合峙悦府6-1-1204B是不是1500？今天能看吗？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_text"], "")
        self.assertTrue(result["query_state"]["needs_tool_verification"])
        self.assertEqual(
            result["structured_task"]["clarification"]["reason"],
            "rewrite_layer_not_found_claim_routed_to_tools",
        )

    async def test_alias_community_with_matching_room_ref_resolves_without_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "合峙悦府6-1-1204B是不是1500，今天能看吗",
                    "query_state": {"intent": "viewing"},
                    "needs_clarification": True,
                    "clarification_text": "你说的是合嵣悦府吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达 北部软件园 城北万象城",
                        "小区": "合嵣悦府",
                        "房号": "6-1-1204B",
                        "押一付一": "1500",
                        "押二付一": "1300",
                        "看房方式密码": "6.19空出 看房提前联系",
                    },
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="合峙悦府6-1-1204B是不是1500？今天能看吗？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("合峙悦府6-1-1204B是不是1500？今天能看吗？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["constraint_proof"]["communities"], ["合嵣悦府"])
        self.assertIn("6-1-1204b", [str(item).lower() for item in result["constraint_proof"]["room_refs"]])

    async def test_orchestrator_tool_plan_drives_actions_without_old_planner_context(self) -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.kwargs: dict = {}

        fake = FakeReplyGenerator()
        original = main.reply_generator
        main.reply_generator = fake
        try:
            result = await main._plan_actions(
                content="新天地4000左右两室",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "新天地4000左右两室",
                    "query_state": {"intent": "inventory"},
                    "structured_task": {
                        "intent": "inventory",
                        "effective_query": "新天地4000左右两室",
                    },
                    "entity_resolution": {"status": "resolved"},
                    "constraint_proof": {"area": "东新园\n杭氧\n新天地", "budget_range": [3500, 4500], "layout": "两室"},
                    "tool_plan": {
                        "actions": ["search_inventory", "generate_reply"],
                        "confidence": 0.9,
                        "reason": "按区域预算户型查房源",
                    },
                },
                signals=main._deterministic_signals("新天地4000左右两室"),
            )
        finally:
            main.reply_generator = original

        self.assertEqual(fake.kwargs, {})
        self.assertFalse(result.get("need_rewrite_clarification"))
        self.assertEqual(result.get("reply_text"), "")
        self.assertIn("search_inventory", result.get("actions", []))
        self.assertIn("generate_reply", result.get("actions", []))
        self.assertFalse(result.get("planner_missing_reply"))

    async def test_planner_clarification_keeps_reply_text_empty(self) -> None:
        class FakeReplyGenerator:
            pass

        original = main.reply_generator
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._plan_actions(
                content="杨家府还有房子吗",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "杨家府还有房子吗",
                    "query_state": {"intent": "inventory", "community": "杨家府"},
                    "structured_task": {"intent": "inventory", "effective_query": "杨家府还有房子吗"},
                    "entity_resolution": {"status": "ambiguous"},
                    "constraint_proof": {"communities": ["杨家府"]},
                    "tool_plan": {
                        "actions": [],
                        "need_rewrite_clarification": True,
                        "missing_evidence": "小区名可能指杨家新雅苑或兴业杨家府，需要意图层追问。",
                        "reply_text": "请问你说的是哪个杨家府？",
                    },
                },
                signals=main._deterministic_signals("杨家府还有房子吗"),
            )
        finally:
            main.reply_generator = original

        self.assertTrue(result.get("need_rewrite_clarification"))
        self.assertEqual(result.get("reply_text"), "")
        self.assertIn("missing_evidence", result)

    async def test_planner_reply_enters_selfcheck_when_it_satisfies_constraints(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="查到了，这几套都还不错。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeInventory:
            async def snapshot(self, limit: int = 20) -> str:
                return "新柠长木府3-1002B 两室一厅 3500"

            def format_rows(self, rows: list[dict], limit: int = 10) -> str:
                return "新柠长木府3-1002B 两室一厅 3500"

        class FakeAgenticRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", evidence=[], dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_reply="")

        originals = {
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "agentic_rag": main.agentic_rag,
        }
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        main.agentic_rag = FakeAgenticRag()
        try:
            result = await main._generate_reply_result(
                content="新天地有没有4000左右的两室",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "东新园 杭氧 新天地 3500到4500预算 两室",
                    "query_state": {"intent": "inventory"},
                    "structured_task": {"intent": "inventory"},
                    "constraint_proof": {
                        "area": "东新园\n杭氧\n新天地",
                        "budget_range": [3500, 4500],
                        "layout": "两室",
                    },
                },
                planner_result={
                    "actions": ["search_inventory", "generate_reply"],
                    "reply_text": "有的，新天地4000左右两室我查到这套还在租：新柠长木府3-1002B，押一付一3500。",
                },
                tool_evidence={
                    "actions": ["search_inventory", "generate_reply"],
                    "inventory_rows": [
                        {
                            "区域": "东新园\n杭氧\n新天地",
                            "小区": "新柠长木府",
                            "房号": "3-1002B",
                            "户型分类": "两室一厅",
                            "押一付": "3500",
                        }
                    ],
                    "target_rows": [],
                    "image_paths": [],
                    "video_paths": [],
                    "missing_media": [],
                },
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("有的", result["reply"])
        self.assertIn("新天地", result["reply"])
        self.assertIn("3500", result["reply"])
        self.assertIn("两室", result["reply"])
        self.assertIn("新柠长木府3-1002B", result["reply"])

    async def test_tool_grounded_selfcheck_retry_returns_to_planner_in_full_flow(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: list[str] = []

            def is_processed(self, msgid: str) -> bool:
                return msgid in self.processed

            def mark_processed(self, msgid: str) -> None:
                self.processed.append(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.rewrite_calls: list[dict] = []
                self.generate_calls = 0
                self.selfcheck_calls = 0

            async def rewrite_kf_message(self, **kwargs):
                self.rewrite_calls.append(kwargs)
                if kwargs.get("planner_feedback"):
                    return {
                        "intent": "inventory",
                        "rewritten_query": "拱墅万达1500左右在租房源",
                        "effective_query": "拱墅万达1500左右在租房源",
                        "query_state": {"intent": "inventory", "area": "拱墅万达", "budget": "1500左右"},
                        "needs_clarification": False,
                        "tool_plan": {
                            "actions": ["search_inventory", "compact_listing", "generate_reply"],
                            "confidence": 0.95,
                            "reason": "根据自检证据重做房源列表取证",
                        },
                    }
                return {
                    "intent": "inventory",
                    "rewritten_query": "拱墅万达1500左右在租房源",
                    "effective_query": "拱墅万达1500左右在租房源",
                    "query_state": {"intent": "inventory", "area": "拱墅万达", "budget": "1500左右"},
                    "needs_clarification": False,
                    "tool_plan": {
                        "actions": ["generate_reply"],
                        "confidence": 0.9,
                        "reason": "模拟 Orchestrator 首次工具计划不完整",
                    },
                }

            async def assess_kf_final_reply(self, **kwargs):
                self.selfcheck_calls += 1
                if self.selfcheck_calls == 1:
                    return {
                        "status": "retry",
                        "reason": "客户要查房源，草稿却回答免押，答非所问",
                        "planner_retry_reason": "重新按拱墅万达1500左右查房源并生成列表回复",
                    }
                return {"status": "pass"}

        class FakeInventory:
            async def all_rows(self, **kwargs) -> list[dict]:
                return [
                    {
                        "区域": "拱墅万达",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型": "一室一厅",
                        "押一付": "1500",
                    }
                ]

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {
                        "区域": "拱墅万达",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型": "一室一厅",
                        "押一付": "1500",
                    }
                ]

            async def snapshot(self, limit: int = 20) -> str:
                return "合幢悦府6-1-1204B 一室一厅 押一1500"

            def format_rows(self, rows: list[dict], limit: int = 10) -> str:
                return "合幢悦府6-1-1204B 一室一厅 押一1500"

            def cache_meta(self) -> dict:
                return {}

        class FakeAgenticRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", evidence=[], dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_reply="")

        fake_wecom = FakeWeComKf()
        fake_context_store = FakeContextStore()
        fake_reply_generator = FakeReplyGenerator()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "agentic_rag": main.agentic_rag,
        }
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = fake_context_store
        main.reply_generator = fake_reply_generator
        main.inventory = FakeInventory()
        main.agentic_rag = FakeAgenticRag()
        try:
            await main._handle_text_message(
                {
                    "msgid": "msg-selfcheck-retry",
                    "msgtype": "text",
                    "origin": 3,
                    "open_kfid": "kf",
                    "external_userid": "wm",
                    "text": {"content": "万达1500左右有哪些"},
                }
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(hasattr(fake_reply_generator, "plan_kf_tool_actions"))
        self.assertEqual(len(fake_reply_generator.rewrite_calls), 2)
        self.assertIn("planner_retry_reason", fake_reply_generator.rewrite_calls[1]["planner_feedback"])
        self.assertFalse(hasattr(fake_reply_generator, "plan_kf_reply_text"))
        self.assertEqual(fake_reply_generator.selfcheck_calls, 0)
        self.assertIn("合幢悦府6-1-1204B", fake_wecom.texts[-1])
        self.assertNotIn("免押", fake_wecom.texts[-1])
        saved_context = fake_context_store.data[main._conversation_key("kf", "wm")]
        record = saved_context["structured_memory"]["turn_records"][-1]
        self.assertNotIn("selfcheck_result", record)
        self.assertEqual(record["assistant_sent_summary"]["final_reply"], fake_wecom.texts[-1])

    async def test_non_deterministic_final_selfcheck_failure_returns_full_evidence_to_planner(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="免押是支付宝无忧住，服务费5.5%-8%。")

            async def assess_kf_final_reply(self, **kwargs):
                return {
                    "status": "retry",
                    "reason": "客户要查房源，草稿却回答免押，答非所问",
                    "planner_retry_reason": "重新按拱墅万达1500左右查房源并生成列表回复",
                }

        class FakeInventory:
            async def snapshot(self, limit: int = 20) -> str:
                return "合幢悦府6-1-1204B 一室一厅 押一1500"

            def format_rows(self, rows: list[dict], limit: int = 10) -> str:
                return ""

        class FakeAgenticRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", evidence=[], dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_reply="")

        originals = {
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "agentic_rag": main.agentic_rag,
        }
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        main.agentic_rag = FakeAgenticRag()
        try:
            result = await main._generate_reply_result(
                content="万达1500左右有哪些",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "general",
                    "effective_query": "万达1500左右有哪些",
                    "query_state": {"intent": "general"},
                    "structured_task": {"intent": "general"},
                    "constraint_proof": {},
                },
                planner_result={
                    "actions": ["generate_reply"],
                    "reply_text": "免押是支付宝无忧住，服务费5.5%-8%。",
                },
                tool_evidence={
                    "actions": ["generate_reply"],
                    "inventory_rows": [],
                    "target_rows": [],
                    "image_paths": [],
                    "video_paths": [],
                    "missing_media": [],
                },
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_planner_retry"])
        retry_reason = result["planner_retry_reason"]
        self.assertIn("original_content", retry_reason)
        self.assertIn("effective_query", retry_reason)
        self.assertIn("tool_evidence", retry_reason)
        self.assertIn("draft_reply", retry_reason)
        self.assertIn("llm_selfcheck", retry_reason)

    async def test_removed_planner_reply_text_after_retry_uses_tool_grounded_inventory_reply(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                raise AssertionError("常规工具证据回复不应该再阻塞式调用 LLM 终检")

        class FakeInventory:
            async def snapshot(self, limit: int = 20) -> str:
                return "皋塘运都9-402B 一室一厅"

            def format_rows(self, rows: list[dict], limit: int = 10) -> str:
                return "皋塘运都9-402B 一室一厅"

        class FakeAgenticRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", evidence=[], dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(status="pass", action="pass", reason="", fallback_reply="")

        row = {
            "区域": "闸弄口 新塘 元宝塘 东站",
            "小区": "皋塘运都",
            "房号": "9-402B",
            "户型描述": "一室一厅朝南带阳台，独立厨卫",
            "户型分类": "一室一厅",
            "押一付一": "2600",
            "押二付一": "2400",
            "备注": "民用水电",
        }
        originals = {
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "agentic_rag": main.agentic_rag,
        }
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        main.agentic_rag = FakeAgenticRag()
        try:
            result = await main._generate_reply_result(
                content="有带独厨卫的吗？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "有带独厨卫的吗？ 闸弄口 新塘 元宝塘 东站 一室一厅",
                    "query_state": {"intent": "inventory"},
                    "structured_task": {"intent": "inventory"},
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "闸弄口\n新塘\n元宝塘\n东站",
                        "layout": "一室一厅",
                        "features": ["独立厨卫"],
                    },
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "",
                    "need_rewrite_clarification": False,
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": [row],
                    "target_rows": [row],
                    "image_paths": [],
                    "video_paths": [],
                    "missing_media": [],
                },
                retry_reason="planner_retry_after_removed_legacy_reply_text",
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("皋塘运都9-402B", result["reply"])
        self.assertIn("独立厨卫", result["reply"])
        self.assertIn("押一付一2600", result["reply"])

    async def test_execute_tools_accepts_inventory_cache_meta_property(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {
                        "区域": "拱墅万达",
                        "小区": "合嵣悦府",
                        "房号": "6-1-1204B",
                        "户型": "一室一厅",
                        "押一付": "1500",
                    }
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            evidence = await main._execute_tools(
                actions=["search_inventory", "compact_listing"],
                content="万达1500左右有哪些",
                context=context,
                understanding={
                    "intent": "inventory",
                    "effective_query": "拱墅万达 1500预算",
                    "constraint_proof": {"area": "拱墅万达"},
                    "query_state": {"intent": "inventory"},
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(len(evidence["inventory_rows"]), 1)
        self.assertEqual(context["last_candidate_set"]["inventory_cache_meta"]["source"], "test_property")

    def test_tool_evidence_summary_redacts_viewing_secret(self) -> None:
        canary = "M1D2B1_SUMMARY_SECRET#"
        row = {
            "community": "HeDe",
            "room_no": "6-1-1204B",
            "\u5c0f\u533a": "HeDe",
            "\u623f\u53f7": "6-1-1204B",
            "\u770b\u623f\u65b9\u5f0f\u5bc6\u7801": canary,
        }

        summary = main._tool_evidence_summary(
            {
                "actions": ["search_inventory", "explain_unavailable_viewing"],
                "inventory_rows": [row],
                "target_rows": [row],
                "rule_evidence": {
                    "viewing": {
                        "rooms": [
                            {
                                "room": "HeDe6-1-1204B",
                                "viewing": canary,
                                "has_password": True,
                            }
                        ]
                    }
                },
            }
        )

        dumped = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(canary, dumped)
        self.assertEqual(summary["inventory_rows"][0]["has_viewing"], "True")
        self.assertNotIn(canary, summary["inventory_rows"][0]["viewing_summary"])

    async def test_video_action_does_not_create_viewing_instruction_evidence(self) -> None:
        canary = "M1D2B1_VIDEO_SECRET#"
        row = {
            "community": "HeDe",
            "room_no": "6-1-1204B",
            "\u5c0f\u533a": "HeDe",
            "\u623f\u53f7": "6-1-1204B",
            "\u770b\u623f\u65b9\u5f0f\u5bc6\u7801": canary,
        }

        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "video_no_viewing"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [row]

        async def fake_collect_room_media(rows: list[dict], *, media_kind: str, limit: int):
            return [], [], [main._row_label(item) for item in rows], None

        originals = {
            "inventory": main.inventory,
            "_collect_room_media": main._collect_room_media,
        }
        main.inventory = FakeInventory()
        main._collect_room_media = fake_collect_room_media
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video"],
                content="HeDe 6-1-1204B video",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "HeDe 6-1-1204B video",
                    "query_state": {"intent": "media"},
                    "constraint_proof": {
                        "communities": ["HeDe"],
                        "room_refs": ["6-1-1204B"],
                        "wants_video": True,
                    },
                    "structured_task": {"tool_requirements": {"needs_video": True}},
                },
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(len(evidence["target_rows"]), 1)
        self.assertNotIn("viewing_instruction_evidence", evidence)
        self.assertNotIn("viewing", evidence["rule_evidence"])
        summary_dumped = json.dumps(main._tool_evidence_summary(evidence), ensure_ascii=False)
        self.assertNotIn(canary, summary_dumped)

    async def test_send_inventory_sheet_uses_artifact_evidence(self) -> None:
        calls = {"refresh": 0, "list": 0}
        with tempfile.TemporaryDirectory() as tmpdir:
            png = Path(tmpdir) / "inventory_01.png"
            png.write_bytes(b"png")

            async def fake_refresh():
                calls["refresh"] += 1
                return {"ok": True}

            def fake_current_images():
                calls["list"] += 1
                return [png]

            originals = {
                "_refresh_current_inventory_images_for_sheet": main._refresh_current_inventory_images_for_sheet,
                "_current_inventory_images": main._current_inventory_images,
            }
            main._refresh_current_inventory_images_for_sheet = fake_refresh
            main._current_inventory_images = fake_current_images
            try:
                evidence = await main._execute_tools(
                    actions=["send_inventory_sheet"],
                    content="inventory sheet",
                    context=kf_context_memory.empty_context(),
                    understanding={
                        "intent": "inventory_sheet",
                        "effective_query": "inventory sheet",
                        "query_state": {"intent": "inventory_sheet"},
                        "constraint_proof": {"wants_inventory_sheet": True},
                        "structured_task": {
                            "tool_requirements": {"needs_inventory_sheet": True}
                        },
                    },
                )
            finally:
                for name, value in originals.items():
                    setattr(main, name, value)

        self.assertEqual(calls, {"refresh": 1, "list": 1})
        self.assertEqual(evidence["inventory_images"], [str(png)])
        artifact = evidence["inventory_sheet_artifact_evidence"][0]
        self.assertEqual(artifact["decision_id"], evidence["inventory_read_context"]["decision_id"])
        self.assertEqual(artifact["source_kind"], evidence["inventory_read_context"]["source_kind"])
        self.assertEqual(artifact["safe_filename"], "inventory_01.png")

    async def test_execute_tools_does_not_fallback_search_when_candidate_index_out_of_range(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {"小区": "永佳新苑", "房号": "16-1001A"},
                    {"小区": "华丰欣苑", "房号": "14-2-901"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "新天地4000左右两室",
                "candidates": [{"小区": "新柠长木府", "房号": "3-1002A"}],
                "shown_count": 1,
                "total_count": 1,
            }
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video"],
                content="第二套视频发我",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "发送上一轮第二套房源的视频",
                    "context_reference": True,
                    "selected_indices": [2],
                    "constraint_proof": {
                        "selected_indices": [2],
                        "wants_video": True,
                    },
                    "structured_task": {
                        "original_text": "第二套视频发我",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["video_paths"], [])
        self.assertEqual(evidence["selection_error"]["requested_indices"], [2])
        self.assertEqual(evidence["selection_error"]["candidate_count"], 1)

    async def test_empty_new_inventory_query_clears_stale_candidates_before_batch_video(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return []

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "皋塘一室",
                "candidates": [{"小区": "皋塘运都", "房号": "9-2-402B"}],
                "shown_count": 1,
                "total_count": 1,
            }
            context["confirmed_room"] = {
                "row": {"小区": "皋塘运都", "房号": "9-2-402B"},
                "label": "皋塘运都9-2-402B",
            }

            first_evidence = await main._execute_tools(
                actions=["search_inventory", "generate_reply"],
                content="东站附近4000左右两室有没有？",
                context=context,
                understanding={
                    "intent": "inventory",
                    "effective_query": "闸弄口 新塘 元宝塘 东站 4000左右 两室",
                    "rewritten_query": "东站附近4000左右两室在租房源",
                    "constraint_proof": {
                        "area": "闸弄口 新塘 元宝塘 东站",
                        "budget_range": [3500, 4500],
                        "layout": "两室",
                    },
                    "structured_task": {
                        "original_text": "东站附近4000左右两室有没有？",
                        "tool_requirements": {"needs_inventory_search": True},
                    },
                },
            )

            second_evidence = await main._execute_tools(
                actions=["search_inventory", "send_video", "generate_reply"],
                content="前两套视频发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "发送上一轮前两套候选房源的视频",
                    "rewritten_query": "上一轮候选前两套视频",
                    "context_reference": True,
                    "selected_indices": [1, 2],
                    "constraint_proof": {
                        "selected_indices": [1, 2],
                        "wants_video": True,
                    },
                    "structured_task": {
                        "original_text": "前两套视频发我。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(first_evidence["inventory_rows"], [])
        self.assertEqual(first_evidence["candidate_context_cleared"]["reason"], "empty_new_scoped_inventory_search")
        self.assertNotIn("last_candidate_set", context)
        self.assertNotIn("confirmed_room", context)
        self.assertEqual(second_evidence["target_rows"], [])
        self.assertEqual(second_evidence["video_paths"], [])
        self.assertEqual(second_evidence["selection_error"]["reason"], "missing_current_candidate_set")
        self.assertEqual(second_evidence["selection_error"]["requested_indices"], [1, 2])

    async def test_single_contextual_inventory_result_replaces_previous_candidate_set(self) -> None:
        single_row = {
            "区域": "闸弄口 新塘 元宝塘 东站",
            "小区": "皋塘运都",
            "房号": "9-402B",
            "户型描述": "一室朝南独厨独卫",
            "户型分类": "一室一厅",
        }

        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [single_row]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "2600以下一室",
                "candidates": [{"小区": "棠润府", "房号": "10-1004C"}],
                "shown_count": 1,
                "total_count": 1,
            }

            first_evidence = await main._execute_tools(
                actions=["search_inventory", "generate_reply"],
                content="有带独厨卫的吗？",
                context=context,
                understanding={
                    "intent": "inventory",
                    "effective_query": "2600以下一室 带独厨卫",
                    "rewritten_query": "上一轮2600以下一室里筛选带独厨卫的房源",
                    "context_reference": True,
                    "constraint_proof": {},
                    "structured_task": {
                        "original_text": "有带独厨卫的吗？",
                        "tool_requirements": {"needs_inventory_search": True},
                    },
                },
            )

            second_evidence = await main._execute_tools(
                actions=["search_inventory", "send_video", "generate_reply"],
                content="第一套视频发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "发送上一轮第一套候选房源的视频",
                    "rewritten_query": "上一轮候选第一套视频",
                    "context_reference": True,
                    "selected_indices": [1],
                    "constraint_proof": {
                        "selected_indices": [1],
                        "wants_video": True,
                    },
                    "structured_task": {
                        "original_text": "第一套视频发我。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in first_evidence["inventory_rows"]], ["9-402B"])
        self.assertEqual(context["last_candidate_set"]["candidates"][0]["房号"], "9-402B")
        target_rows_without_listing_id = [dict(row) for row in second_evidence["target_rows"]]
        for row in target_rows_without_listing_id:
            row.pop("listing_id", None)
        self.assertEqual(target_rows_without_listing_id, [single_row])
        self.assertEqual(second_evidence["target_rows"][0]["listing_id"], main._row_listing_id(single_row))

    async def test_vague_send_media_followup_binds_previous_candidate_set(self) -> None:
        previous_candidates = [
            {"小区": "兴业杨家府", "房号": "4-1502", "户型分类": "一室一厅"},
            {"小区": "兴业杨家府", "房号": "8-1203", "户型分类": "一室一厅"},
        ]
        unrelated_rows = [
            {"小区": "杨家新雅苑", "房号": "49-1102", "户型分类": "一室一厅"},
            {"小区": "永佳新苑", "房号": "2-703", "户型分类": "一室一厅"},
        ]

        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return unrelated_rows

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "兴业杨家府 4000-5000 一室一厅",
                "candidates": previous_candidates,
                "shown_count": 2,
                "total_count": 2,
            }
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_image", "send_video", "generate_reply"],
                content="如果有的话先发视频和图片给客户看看。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "如果有的话先发视频和图片给客户看看 石桥街道 华丰 石桥 永佳 半山 4000到5000预算 一室一厅",
                    "rewritten_query": "如果有的话先发视频和图片给客户看看 石桥街道 华丰 石桥 永佳 半山 4000到5000预算 一室一厅",
                    "context_reference": True,
                    "constraint_proof": {
                        "area": "石桥街道 华丰 石桥 永佳 半山",
                        "budget_range": [4000, 5000],
                        "layout": "一室一厅",
                        "wants_video": True,
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "如果有的话先发视频和图片给客户看看。",
                        "tool_requirements": {"needs_video": True, "needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in evidence["target_rows"]], ["4-1502", "8-1203"])

    async def test_selected_candidate_field_followup_narrows_inventory_rows(self) -> None:
        previous_candidates = [
            {"小区": "兴业杨家府", "房号": "4-1502", "户型分类": "一室一厅", "押一付一": "4500", "押二付一": "4200"},
            {"小区": "兴业杨家府", "房号": "8-1203", "户型分类": "一室一厅", "押一付一": "4500", "押二付一": "4200"},
        ]
        broad_rows = [
            {"小区": "杨家新雅苑", "房号": "49-1102", "户型分类": "一室一厅", "押一付一": "4500"},
            *previous_candidates,
        ]

        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return broad_rows

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "兴业杨家府 4000-5000 一室一厅",
                "candidates": previous_candidates,
                "shown_count": 2,
                "total_count": 2,
            }
            evidence = await main._execute_tools(
                actions=["search_inventory", "generate_reply"],
                content="第一套多少钱，押一付一和押二付一分别多少？",
                context=context,
                understanding={
                    "intent": "inventory",
                    "effective_query": "第一套价格 押一付一 押二付一",
                    "rewritten_query": "第一套价格 押一付一 押二付一",
                    "context_reference": True,
                    "selected_indices": [1],
                    "constraint_proof": {
                        "selected_indices": [1],
                        "wants_price": True,
                    },
                    "structured_task": {
                        "original_text": "第一套多少钱，押一付一和押二付一分别多少？",
                        "tool_requirements": {"needs_inventory_search": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in evidence["target_rows"]], ["4-1502"])
        self.assertEqual([row["房号"] for row in evidence["inventory_rows"]], ["4-1502"])

    def test_selection_indices_treat_third_room_as_single_index(self) -> None:
        self.assertEqual(main._selection_indices_from_text("第三套今天能看吗？"), [3])
        self.assertEqual(main._selection_indices_from_text("把前三套视频都发我"), [1, 2, 3])
        self.assertEqual(main._selection_indices_from_text("第1和第3套视频发我。"), [1, 3])
        self.assertEqual(main._selection_indices_from_text("1和5视频"), [1, 5])

    def test_structured_selected_indices_require_explicit_user_selection_text(self) -> None:
        self.assertEqual(
            main._selected_indices_from_understanding(
                {
                    "selected_indices": [1, 2, 3, 4],
                    "constraint_proof": {"selected_indices": [1, 2, 3, 4]},
                },
                "有带独厨卫的吗？ 闸弄口 新塘 元宝塘 东站 一室一厅",
            ),
            [],
        )
        self.assertEqual(
            main._selected_indices_from_understanding(
                {"constraint_proof": {"selected_indices": [1]}},
                "第一套视频发我。",
            ),
            [1],
        )

    async def test_rewrite_timeout_falls_back_to_contextual_image_task(self) -> None:
        async def timeout_rewrite(**kwargs):
            raise asyncio.TimeoutError()

        original_rewrite = main.reply_generator.rewrite_kf_message
        original_rows = main._inventory_rows_for_resolution
        main.reply_generator.rewrite_kf_message = timeout_rewrite
        main._inventory_rows_for_resolution = lambda: asyncio.sleep(0, result=[])
        try:
            context = kf_context_memory.empty_context()
            context["confirmed_room"] = {
                "row": {"小区": "皋塘运都", "房号": "9-402B"},
                "label": "皋塘运都9-402B",
            }
            context["last_candidate_set"] = {
                "query": "皋塘运都9-402B",
                "candidates": [{"小区": "皋塘运都", "房号": "9-402B"}],
                "shown_count": 1,
                "total_count": 1,
            }
            context = kf_context_memory.append_dialog_message(
                context,
                role="assistant",
                content="这是皋塘运都9-402B的视频。",
            ) or context
            result = await main._understand_message(
                content="这个图片也发一下。",
                context=context,
                signals=main._deterministic_signals("这个图片也发一下。"),
            )
        finally:
            main.reply_generator.rewrite_kf_message = original_rewrite
            main._inventory_rows_for_resolution = original_rows

        self.assertEqual(result["intent"], "media")
        self.assertTrue(result["context_reference"])
        self.assertTrue(result["constraint_proof"]["wants_image"])
        self.assertTrue(result["structured_task"]["tool_requirements"]["needs_image"])

    async def test_execute_tools_does_not_recover_viewing_when_candidate_index_out_of_range(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {"小区": "新柠长木府", "房号": "3-1002A", "看房方式密码": "336699#"},
                    {"小区": "杨乐府", "房号": "9-604B", "看房方式密码": "提前联系"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "query": "新天地4000左右两室",
                "candidates": [
                    {"小区": "新柠长木府", "房号": "3-1002A", "看房方式密码": "336699#"},
                    {"小区": "杨乐府", "房号": "9-604B", "看房方式密码": "提前联系"},
                ],
                "shown_count": 2,
                "total_count": 2,
            }
            evidence = await main._execute_tools(
                actions=["search_inventory", "explain_unavailable_viewing"],
                content="第三套今天能看吗？",
                context=context,
                understanding={
                    "intent": "viewing",
                    "effective_query": "查询上一轮第三套房源看房方式",
                    "context_reference": True,
                    "selected_indices": [3],
                    "constraint_proof": {
                        "selected_indices": [3],
                    },
                    "structured_task": {
                        "original_text": "第三套今天能看吗？",
                        "tool_requirements": {"needs_viewing_policy": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["rule_evidence"].get("viewing", {}).get("rooms"), [])
        self.assertEqual(evidence["selection_error"]["requested_indices"], [3])
        self.assertEqual(evidence["selection_error"]["candidate_count"], 2)

    async def test_execute_tools_binds_confirmed_room_for_water_and_password_followup(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {"小区": "东方茂商业中心T", "房号": "3-1540", "备注": "民用水电", "看房方式密码": "提前联系"},
                    {"小区": "白田畈龙吟府", "房号": "4-902B", "备注": "水30/月，电1元/度", "看房方式密码": "902902#"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            confirmed = {
                "小区": "白田畈龙吟府",
                "房号": "4-902B",
                "备注": "水30/月，电1元/度",
                "看房方式密码": "902902#",
            }
            context["confirmed_room"] = {"row": confirmed, "label": "白田畈龙吟府4-902B"}
            evidence = await main._execute_tools(
                actions=["search_inventory", "explain_unavailable_viewing", "context_tools"],
                content="水电和密码一起发我。",
                context=context,
                understanding={
                    "intent": "viewing",
                    "effective_query": "水电和密码一起发我。 东新园 杭氧 新天地 2000到4000预算 一室一厅",
                    "constraint_proof": {
                        "area": "东新园\n杭氧\n新天地",
                        "budget_range": [2000, 4000],
                        "layout": "一室一厅",
                        "wants_utilities": True,
                    },
                    "structured_task": {
                        "original_text": "水电和密码一起发我。",
                        "tool_requirements": {"needs_viewing_policy": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([main._row_label(row) for row in evidence["target_rows"]], ["白田畈龙吟府4-902B"])
        self.assertIn("viewing", evidence["rule_evidence"])
        result = main._constraint_consistency_selfcheck(
            content="水电和密码一起发我。",
            draft_reply="白田畈龙吟府4-902B：水电是水30/月，电1元/度；看房方式/密码是902902#。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {"wants_utilities": True},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence=evidence,
        )
        self.assertEqual(result["status"], "pass")

    def test_candidate_selection_error_reply_explains_candidate_count(self) -> None:
        reply = main._reply_for_candidate_selection_error(
            {
                "selection_error": {
                    "requested_indices": [2],
                    "candidate_count": 1,
                    "candidate_labels": ["华丰欣苑14-2-901"],
                }
            }
        )

        self.assertIn("上一轮我只列了1套", reply)
        self.assertIn("没有第2套", reply)
        self.assertIn("华丰欣苑14-2-901", reply)

    def test_filter_count_request_is_not_candidate_selection(self) -> None:
        self.assertEqual(main._selection_indices_from_text("客户想今天先筛两套。"), [])
        self.assertEqual(main._selection_indices_from_text("先把万达2000以下一室里最合适的两套视频发我。"), [])
        self.assertEqual(main._requested_room_count_from_text("先把万达2000以下一室里最合适的两套视频发我。"), 2)
        self.assertEqual(main._selection_indices_from_text("这两套视频发我。"), [1, 2])

    def test_field_target_error_reply_asks_specific_room(self) -> None:
        reply = main._reply_for_field_target_error(
            {
                "field_target_error": {
                    "field": "水电",
                    "candidate_count": 2,
                    "candidate_labels": ["杨乐府9-604B", "新柠长木府3-1002A"],
                }
            }
        )

        self.assertIn("水电要按具体房源查", reply)
        self.assertIn("杨乐府9-604B", reply)
        self.assertIn("回序号或小区+房号", reply)

    async def test_community_only_video_request_requires_room_selection(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {"小区": "兴业杨家府", "房号": "10-1-1205", "户型分类": "两室一厅"},
                    {"小区": "兴业杨家府", "房号": "3-601", "户型分类": "一室一厅"},
                    {"小区": "兴业杨家府", "房号": "49-1102", "户型分类": "一室一厅"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video", "generate_reply"],
                content="兴业杨家府有视频吗",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "查询兴业杨家府房源视频",
                    "rewritten_query": "查询兴业杨家府房源视频",
                    "constraint_proof": {
                        "communities": ["兴业杨家府"],
                        "wants_video": True,
                    },
                    "structured_task": {
                        "original_text": "兴业杨家府有视频吗",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["video_paths"], [])
        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(
            evidence["field_target_error"]["reason"],
            "community_media_request_missing_room_ref",
        )
        self.assertEqual(evidence["field_target_error"]["field"], "视频")
        self.assertIn("兴业杨家府10-1-1205", evidence["field_target_error"]["candidate_labels"])
        self.assertIn("last_candidate_set", context)

    def test_community_media_target_error_reply_asks_room_selection(self) -> None:
        reply = main._reply_for_field_target_error(
            {
                "field_target_error": {
                    "field": "视频",
                    "reason": "community_media_request_missing_room_ref",
                    "candidate_count": 3,
                    "candidate_labels": [
                        "兴业杨家府10-1-1205",
                        "兴业杨家府3-601",
                        "兴业杨家府49-1102",
                    ],
                }
            }
        )

        self.assertIn("视频要按具体房源查", reply)
        self.assertIn("这个小区有多套在租", reply)
        self.assertIn("兴业杨家府10-1-1205", reply)
        self.assertIn("回序号或小区+房号", reply)
        self.assertNotIn("这是", reply)
        self.assertNotIn("的视频", reply)

    def test_legacy_deposit_utilities_direct_reply_is_removed(self) -> None:
        self.assertFalse(hasattr(main, "_reply_for_deposit_and_utilities"))

    def test_safe_deposit_fallback_does_not_inject_deposit_for_water_only(self) -> None:
        reply = main._safe_fallback_for_intent(
            {
                "intent": "deposit",
                "effective_query": "水电怎么收？",
                "rewritten_query": "水电怎么收？",
                "constraint_proof": {"wants_utilities": True},
                "structured_task": {"original_text": "水电怎么收？"},
            },
            "水电要按具体房源备注查，你把小区+房号发我，我马上按那套核对。",
        )

        self.assertIn("水电要按具体房源备注查", reply)
        self.assertNotIn("免押", reply)

    async def test_field_followup_without_target_does_not_use_area_rows(self) -> None:
        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [
                    {
                        "区域": "东新园\n杭氧\n新天地",
                        "小区": "杨乐府",
                        "房号": "9-604B",
                        "户型分类": "两室一厅",
                        "备注": "水30/月，电1元/度",
                    }
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "generate_reply"],
                content="水电怎么收？",
                context={},
                understanding={
                    "intent": "inventory",
                    "effective_query": "水电怎么收？",
                    "constraint_proof": {
                        "intent": "inventory",
                        "wants_utilities": True,
                        "hard_constraints": {
                            "area": False,
                            "community": False,
                            "room_refs": False,
                            "budget_range": False,
                            "layout": False,
                            "selected_indices": False,
                        },
                    },
                    "structured_task": {"original_text": "水电怎么收？"},
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["field_target_error"]["field"], "水电")

    def test_selection_error_selfcheck_does_not_require_budget_or_layout_terms(self) -> None:
        reply = "上一轮我只列了1套，没有第2套。上一轮候选是：华丰欣苑14-2-901。你可以直接说第1套，或者换区域/预算我重新筛。"

        result = main._constraint_consistency_selfcheck(
            content="第二套户型怎么样？",
            draft_reply=reply,
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道 华丰 石桥 永佳 半山",
                    "budget_range": [4000, 5000],
                    "layout": "一室一厅",
                    "selected_indices": [2],
                },
                "structured_task": {
                    "original_text": "第二套户型怎么样？",
                    "tool_requirements": {"needs_inventory_search": True},
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "compact_listing", "generate_reply"],
                "selection_error": {
                    "requested_indices": [2],
                    "candidate_count": 1,
                    "candidate_labels": ["华丰欣苑14-2-901"],
                },
                "inventory_rows": [],
                "target_rows": [],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_exact_community_inventory_reply_does_not_need_area_repeated(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="兴业杨家府",
            draft_reply=(
                "有的，兴业杨家府我查到这些还在租：\n"
                "1. 兴业杨家府4-1502，一室一厅，押一付一4500，押二付一4200，民用水电"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "communities": ["兴业杨家府"],
                    "hard_constraints": {"area": True, "community": True},
                },
                "structured_task": {"original_text": "兴业杨家府"},
            },
            tool_evidence={
                "actions": ["search_inventory", "compact_listing", "generate_reply"],
                "inventory_rows": [
                    {
                        "区域": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "小区": "兴业杨家府",
                        "房号": "4-1502",
                        "户型分类": "一室一厅",
                        "押一付一": "4500",
                        "押二付一": "4200",
                        "备注": "民用水电",
                    }
                ],
                "target_rows": [],
            },
        )

        self.assertEqual(result["status"], "pass")

    async def test_execute_tools_preserves_raw_low_price_intent_for_inventory_search(self) -> None:
        class FakeInventory:
            def __init__(self) -> None:
                self.queries: list[str] = []

            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                self.queries.append(query)
                if "便宜点" not in query:
                    return [
                        {
                            "区域": "拱墅万达\n北部软件园\n城北万象城",
                            "小区": "大华海派风景",
                            "房号": "2-1-402A",
                            "户型分类": "一室",
                            "押一付": "1600",
                            "押二付": "1500",
                        }
                    ]
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "瑷颐湾",
                        "房号": "13-1-402A",
                        "户型分类": "一室",
                        "押一付": "600",
                        "押二付": "600",
                    },
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "大华海派风景",
                        "房号": "2-1-402A",
                        "户型分类": "一室",
                        "押一付": "1600",
                        "押二付": "1500",
                    },
                ]

        fake_inventory = FakeInventory()
        original_inventory = main.inventory
        main.inventory = fake_inventory
        try:
            context = kf_context_memory.empty_context()
            evidence = await main._execute_tools(
                actions=["search_inventory", "compact_listing"],
                content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
                context=context,
                understanding={
                    "intent": "inventory",
                    "effective_query": "北部软件园附近，预算1800元以内，一室或一室一厅的在租房源有哪些？ 拱墅万达 北部软件园 城北万象城 单间",
                    "constraint_proof": {
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "budget_range": [0, 1800],
                        "layout": "单间",
                    },
                    "query_state": {"intent": "inventory"},
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertTrue(fake_inventory.queries)
        self.assertIn("便宜点", fake_inventory.queries[0])
        self.assertEqual([row["房号"] for row in evidence["inventory_rows"]], ["13-1-402A", "2-1-402A"])

    async def test_media_request_with_count_targets_current_search_rows(self) -> None:
        rows = [
            {"区域": "拱墅万达", "小区": "星桥锦绣嘉苑", "房号": "20-1606A", "户型分类": "一室一厅", "押一付": "1900"},
            {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅", "押一付": "1600"},
            {"区域": "拱墅万达", "小区": "小洋坝家园二区", "房号": "7-1001E", "户型分类": "一室一厅", "押一付": "1800"},
        ]

        class FakeInventory:
            @property
            def cache_meta(self) -> dict:
                return {"source": "test_property"}

            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return rows

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                content="先把万达2000以下一室里最合适的两套视频发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "拱墅万达 2000以下 一室 前两套视频",
                    "rewritten_query": "拱墅万达2000以下一室前两套视频",
                    "constraint_proof": {
                        "area": "拱墅万达",
                        "budget_range": [0, 2000],
                        "layout": "一室",
                        "wants_video": True,
                    },
                    "query_state": {"intent": "media", "area": "拱墅万达", "wants_video": True},
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in evidence["target_rows"]], ["20-1606A", "15-2-801B"])
        self.assertEqual(evidence["media_request"]["requested_count"], 2)
        self.assertEqual(context["last_candidate_set"]["shown_count"], 3)

    def test_candidate_set_reconciles_to_visible_reply_order(self) -> None:
        raw_rows = [
            {"区域": "拱墅万达", "小区": "瑷颐湾", "房号": "13-1-402A"},
            {"区域": "拱墅万达", "小区": "大华海派风景", "房号": "2-1-402A"},
            {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B"},
            {"区域": "拱墅万达", "小区": "小洋坝家园二区", "房号": "7-1001E"},
            {"区域": "拱墅万达", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以下一室",
            "candidates": raw_rows,
            "shown_count": len(raw_rows),
            "total_count": len(raw_rows),
        }
        reply = "\n".join(
            [
                "有的，万达2000以下一室优先看这几套：",
                "1. 棠润府15-2-801B，一室一厅，押一付1600",
                "2. 小洋坝家园二区7-1001E，一室一厅，押一付1800",
                "3. 星桥锦绣嘉苑20-1606A，一室一厅，押一付1900",
            ]
        )

        context = main._reconcile_last_candidate_set_with_visible_reply(
            context,
            reply,
            {"inventory_rows": raw_rows},
        )

        self.assertEqual(
            [row["房号"] for row in context["last_candidate_set"]["candidates"]],
            ["15-2-801B", "7-1001E", "20-1606A"],
        )
        self.assertEqual(context["last_candidate_set"]["shown_count"], 3)

        selected_rows = main._target_rows_from_understanding(
            {
                "intent": "media",
                "effective_query": "前两套视频",
                "selected_indices": [1, 2],
                "constraint_proof": {"selected_indices": [1, 2], "wants_video": True},
            },
            context,
            raw_rows,
        )
        self.assertEqual([row["房号"] for row in selected_rows], ["15-2-801B", "7-1001E"])

    def test_selected_media_request_without_candidates_does_not_bind_single_search_row(self) -> None:
        rows = [{"小区": "石桥铭苑", "房号": "6-1102"}]

        selected_rows = main._target_rows_from_understanding(
            {
                "intent": "media",
                "effective_query": "这两套有没有原视频或者高清点的？",
                "selected_indices": [1, 2],
                "constraint_proof": {
                    "selected_indices": [1, 2],
                    "wants_video": True,
                    "wants_original_video": True,
                },
            },
            kf_context_memory.empty_context(),
            rows,
        )

        self.assertEqual(selected_rows, [])

    async def test_tool_resolver_binds_second_candidate_with_metadata(self) -> None:
        candidate_rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return []

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达一室",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools"],
                content="第二套视频发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "第二套视频",
                    "rewritten_query": "第二套视频",
                    "selected_indices": [2],
                    "constraint_proof": {"selected_indices": [2], "wants_video": True},
                    "structured_task": {
                        "original_text": "第二套视频发我。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([main._row_label(row) for row in evidence["target_rows"]], ["星桥锦绣嘉苑20-1606A"])
        self.assertEqual(evidence["candidate_binding"]["status"], "bound")
        self.assertEqual(evidence["candidate_binding"]["source"], "candidate_set_selection")
        self.assertEqual(evidence["candidate_binding"]["selected_indices"], [2])
        self.assertNotIn("selection_error", evidence)

    async def test_tool_resolver_inherits_these_video_targets_from_candidate_context(self) -> None:
        candidate_rows = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return []

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达一室",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools"],
                content="这几套视频也发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "这几套视频",
                    "rewritten_query": "这几套视频",
                    "context_reference": True,
                    "constraint_proof": {"wants_video": True},
                    "structured_task": {
                        "original_text": "这几套视频也发我。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(
            [main._row_label(row) for row in evidence["target_rows"]],
            ["棠润府15-2-801B", "星桥锦绣嘉苑20-1606A"],
        )
        self.assertEqual(evidence["candidate_binding"]["status"], "bound")
        self.assertEqual(evidence["candidate_binding"]["source"], "candidate_context")
        self.assertNotIn("selection_error", evidence)
        self.assertNotIn("field_target_error", evidence)

    def test_unresolved_community_clarification_requires_real_entity_shape(self) -> None:
        weak_resolution = {
            "communities": [],
            "community_options": [],
            "areas": [],
            "raw_mentions": ["是多少", "是民用", "怎么回"],
        }
        for content in (
            "这套密码是多少，今天能自己看吗？",
            "这套水电是民用吗？",
            "如果客户问押一付一是多少钱，该怎么回？",
        ):
            self.assertEqual(
                main._unresolved_community_mention_clarification(
                    content=content,
                    entity_resolution=weak_resolution,
                ),
                "",
            )

        strong = main._unresolved_community_mention_clarification(
            content="荣润府1600还有吗？",
            entity_resolution={
                "communities": [],
                "community_options": [],
                "areas": [],
                "raw_mentions": ["荣润府"],
            },
        )
        self.assertIn("荣润府", strong)
        self.assertIn("暂时没查到", strong)

    def test_inventory_search_reply_prefers_community_constraint_over_area(self) -> None:
        reply = main._reply_for_inventory_search(
            {
                "intent": "inventory",
                "constraint_proof": {
                    "area": "闸弄口\n新塘\n元宝塘\n东站",
                    "communities": ["皋塘运都"],
                    "budget_range": [0, 4500],
                    "layout": "两室",
                },
                "structured_task": {"tool_requirements": {}},
            },
            {"actions": ["search_inventory"], "inventory_rows": []},
        )

        self.assertIn("皋塘运都", reply)
        self.assertIn("4500以下", reply)
        self.assertIn("两室", reply)
        self.assertNotIn("闸弄口、新塘、元宝塘、东站、4500以下、两室", reply)

    def test_deterministic_selection_overrides_llm_expanded_indices(self) -> None:
        proof = main._build_constraint_proof(
            content="这两套图片和视频都发我。",
            effective_query="万达4000以下两室图片视频",
            understanding={
                "intent": "media",
                "selected_indices": [1, 2, 3, 4, 5],
                "query_state": {"wants_video": True, "wants_image": True},
            },
            entity_resolution={},
            signals={"wants_video": True, "wants_image": True},
        )

        self.assertEqual(proof["selected_indices"], [1, 2])

    def test_media_candidate_selection_does_not_overwrite_last_candidate_set(self) -> None:
        self.assertFalse(
            main._should_remember_candidate_set(
                content="前两套视频先发我",
                understanding={
                    "intent": "media",
                    "selected_indices": [1, 2],
                    "constraint_proof": {"selected_indices": [1, 2], "wants_video": True},
                },
                rows=[
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                    {"小区": "棠润府", "房号": "10-1004C"},
                ],
            )
        )

    def test_context_media_search_rows_do_not_overwrite_last_candidate_set(self) -> None:
        self.assertFalse(
            main._should_remember_candidate_set(
                content="这套图片也发一下",
                understanding={
                    "intent": "media",
                    "context_reference": True,
                    "constraint_proof": {"wants_image": True},
                },
                rows=[
                    {"小区": "兴业杨家府", "房号": "4-1502"},
                    {"小区": "石桥铭苑", "房号": "6-1102"},
                ],
            )
        )

    def test_utilities_validation_uses_remark_field_for_bound_room(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="这套水电怎么收？",
            draft_reply="棠润府15-2-801B：水30/月，电1元/度。",
            understanding={
                "constraint_proof": {"wants_utilities": True},
                "query_state": {"wants_utilities": True},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "target_rows": [
                    {
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "备注": "水30/月，电1元/度",
                    }
                ],
                "rule_evidence": {},
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_bound_room_utilities_selfcheck_ignores_inherited_budget_area_layout(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="这套水电怎么收？",
            draft_reply="棠润府15-2-801B：水30/月，电1元/度。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "拱墅万达 北部软件园 城北万象城",
                    "budget_range": [0, 2000],
                    "layout": "一室一厅",
                    "wants_utilities": True,
                },
                "structured_task": {"tool_requirements": {"needs_utilities": True}},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "target_rows": [
                    {
                        "小区": "棠润府",
                        "房号": "15-2-801B",
                        "户型分类": "一室一厅",
                        "押一付一": "1600",
                        "押二付一": "1400",
                        "备注": "水30/月，电1元/度",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_utilities_field_lookup_normalizes_misclassified_viewing_intent(self) -> None:
        result = main._normalize_field_lookup_understanding(
            "第一套水电怎么收？",
            {
                "intent": "viewing",
                "query_state": {"intent": "viewing"},
                "constraint_proof": {"intent": "viewing"},
                "structured_task": {
                    "intent": "viewing",
                    "query_state": {"intent": "viewing"},
                    "constraint_proof": {"intent": "viewing"},
                    "tool_requirements": {
                        "needs_inventory_search": True,
                        "needs_viewing_policy": True,
                    },
                },
            },
        )

        self.assertEqual(result["intent"], "inventory")
        self.assertTrue(result["query_state"]["wants_utilities"])
        self.assertTrue(result["constraint_proof"]["wants_utilities"])
        self.assertFalse(result["structured_task"]["tool_requirements"]["needs_viewing_policy"])
        self.assertTrue(result["structured_task"]["tool_requirements"]["needs_utilities"])

    def test_utilities_selfcheck_ignores_misclassified_viewing_intent(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="第一套水电怎么收？",
            draft_reply="长岳王马府4-2002：民用水电。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {"wants_utilities": True},
                "structured_task": {
                    "tool_requirements": {
                        "needs_utilities": True,
                        "needs_viewing_policy": True,
                    }
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "小区": "长岳王马府",
                        "房号": "4-2002",
                        "备注": "民用水电",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_outbound_package_suppresses_media_when_actions_are_suppressed(self) -> None:
        package = main._build_outbound_package(
            "我先按安全回复处理。",
            {
                "suppress_actions": True,
                "inventory_images": ["inventory.png"],
                "image_paths": ["room.jpg"],
                "video_paths": ["room.mp4"],
                "image_rows": [{"小区": "星河苑", "房号": "1-101"}],
                "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
            },
        )

        self.assertEqual(package["inventory_images"], [])
        self.assertEqual(package["image_paths"], [])
        self.assertEqual(package["video_paths"], [])
        self.assertNotIn("image_rows", package)
        self.assertNotIn("video_rows", package)
        self.assertEqual(package["image_explanations"], [])
        self.assertEqual(package["video_explanations"], [])

    def test_viewing_selfcheck_rejects_reply_without_viewing_field_evidence(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="这套今天能看吗？",
            draft_reply="还在，这是兴业杨家府4-1502的视频，可以看房了。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {},
                "structured_task": {
                    "original_text": "这套今天能看吗？",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "小区": "兴业杨家府",
                        "房号": "4-1502",
                        "看房方式密码": "看房提前联系",
                    }
                ],
                "rule_evidence": {
                    "viewing": {
                        "rooms": [
                            {
                                "room": "兴业杨家府4-1502",
                                "viewing": "看房提前联系",
                                "needs_contact": True,
                                "contact_numbers": list(main.CONTACT_NUMBERS),
                            }
                        ]
                    }
                },
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("看房方式密码字段", result["reason"])

    def test_utility_field_selfcheck_catches_electricity_as_water_swap(self) -> None:
        failures = main._utility_field_consistency_failures(
            "棠润府15-2-801B：水电费30元/月，水1元/度。",
            [
                {
                    "小区": "棠润府",
                    "房号": "15-2-801B",
                    "备注": "水30/月，电1元/度",
                }
            ],
        )

        self.assertTrue(failures)
        self.assertIn("电费写成了水费", failures[0])

    def test_utilities_price_validation_keeps_price_when_user_asks_both(self) -> None:
        rows = [
            {
                "小区": "长岳王马府",
                "房号": "4-2002",
                "押一付一": "4300",
                "押二付一": "4000",
                "备注": "民用水电",
            },
            {
                "小区": "长浜龙吟轩",
                "房号": "11-1603",
                "押一付一": "4200",
                "押二付一": "3900",
                "备注": "水30/月，电1元/度",
            },
        ]
        result = main._constraint_consistency_selfcheck(
            content="这两套水电和价格帮我对比一下。",
            draft_reply=(
                "长岳王马府4-2002：押一付一4300，押二付一4000，民用水电。\n"
                "长浜龙吟轩11-1603：押一付一4200，押二付一3900，水30/月，电1元/度。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {"wants_utilities": True, "wants_price": True},
                "query_state": {"wants_utilities": True, "wants_price": True},
                "structured_task": {
                    "tool_requirements": {
                        "needs_utilities": True,
                        "needs_price": True,
                    }
                },
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_multi_room_payment_selfcheck_rejects_aggregate_wrong_price(self) -> None:
        failures = main._payment_field_consistency_failures(
            (
                "这是棠润府15-2-801B和小洋坝家园二区7-1001E的视频。"
                "这两套押一付一600元，押二付一1400元。"
            ),
            [
                {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                {"小区": "小洋坝家园二区", "房号": "7-1001E", "押一付一": "1800", "押二付一": "1700"},
            ],
        )

        self.assertTrue(failures)
        self.assertIn("600", failures[0])

    def test_multi_room_utility_selfcheck_rejects_false_aggregate_same_utility(self) -> None:
        failures = main._utility_field_consistency_failures(
            (
                "这是昌运里三区3-1403和棠润府15-2-1901B的视频。"
                "两套水电均为民用水电。"
            ),
            [
                {"小区": "昌运里三区", "房号": "3-1403", "备注": "民用水电"},
                {"小区": "棠润府", "房号": "15-2-1901B", "备注": "水30/月，电1元/度"},
            ],
        )

        self.assertTrue(failures)
        self.assertIn("逐套说明", failures[0])

    def test_outbound_selfcheck_requires_per_room_image_and_video_explanations(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply=(
                "有的，这是昌运里三区3-1403和棠润府15-2-1901B的视频，"
                "两套图片也发你了。"
            ),
            tool_evidence={
                "actions": ["send_image", "send_video"],
                "video_rows": [
                    {"小区": "昌运里三区", "房号": "3-1403"},
                    {"小区": "棠润府", "房号": "15-2-1901B"},
                ],
                "image_rows": [
                    {"小区": "昌运里三区", "房号": "3-1403"},
                    {"小区": "棠润府", "房号": "15-2-1901B"},
                ],
            },
            outbound_package={
                "video_paths": [__file__, __file__],
                "image_paths": [__file__, __file__],
                "video_explanations": [
                    "这是昌运里三区3-1403的视频。",
                    "这是棠润府15-2-1901B的视频。",
                ],
                "image_explanations": [
                    "这是昌运里三区3-1403的图片。",
                    "这是棠润府15-2-1901B的图片。",
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("多套图片动作", result["reason"])

    def test_budget_selfcheck_rejects_listed_room_outside_budget_range(self) -> None:
        failures = main._budget_payment_scope_failures(
            reply_text=(
                "拱墅万达、0-2000、一室一厅我查到这些还在租：\n"
                "1. 棠润府15-2-801B，一室一厅，押一付一1600，押二付一1400\n"
                "2. 棠润府10-1004C，一室一厅，押一付一2600，押二付一2300"
            ),
            evidence_rows=[
                {
                    "小区": "棠润府",
                    "房号": "15-2-801B",
                    "押一付一": "1600",
                    "押二付一": "1400",
                },
                {
                    "小区": "棠润府",
                    "房号": "10-1004C",
                    "押一付一": "2600",
                    "押二付一": "2300",
                },
            ],
            budget_range=[0, 2000],
        )

        self.assertTrue(failures)
        self.assertIn("棠润府10-1004C", failures[0])

    def test_constraint_proof_uses_query_state_budget_range(self) -> None:
        proof = main._build_constraint_proof(
            content="有没有带厅的，一室一厅也算。",
            effective_query="拱墅万达 0-2000 一室一厅 在租房源",
            understanding={
                "intent": "inventory",
                "query_state": {
                    "area": "拱墅万达\n北部软件园\n城北万象城",
                    "budget_range": [0, 2000],
                    "budget": "0-2000",
                    "layout": "一室一厅",
                },
            },
            entity_resolution={},
            signals={},
        )

        self.assertEqual(proof["budget_range"], [0, 2000])
        self.assertTrue(proof["hard_constraints"]["budget_range"])

    def test_constraint_proof_parses_query_state_budget_label(self) -> None:
        proof = main._build_constraint_proof(
            content="4000-5000 的呢？",
            effective_query="新天地 两室 4000-5000 在租房源",
            understanding={
                "intent": "inventory",
                "query_state": {
                    "area": "东新园\n杭氧\n新天地",
                    "budget": "4000-5000",
                    "layout": "两室",
                },
            },
            entity_resolution={},
            signals={},
        )

        self.assertEqual(proof["budget_range"], [4000, 5000])
        self.assertTrue(proof["hard_constraints"]["budget_range"])

    async def test_area_alias_does_not_trigger_community_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "查询石桥附近5000左右两室整租",
                    "query_state": {"intent": "inventory", "area": "石桥"},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "华丰欣苑", "房号": "14-2-901"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "永佳新苑", "房号": "2-703"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="石桥附近5000左右有两室吗？最好整租。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("石桥附近5000左右有两室吗？最好整租。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["community_options"], [])
        self.assertIn("石桥街道", result["constraint_proof"]["area"])

    async def test_one_room_broad_query_does_not_trigger_living_room_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "拱墅万达2000以下一室房源",
                    "query_state": {"intent": "inventory", "area": "拱墅万达", "layout": "一室"},
                    "needs_clarification": True,
                    "clarification_text": "请问您说的一室是指一室户，还是一室一厅也包含在内？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "拱墅万达\n北部软件园\n城北万象城", "小区": "星桥锦绣嘉苑", "房号": "20-1606A", "户型分类": "一室一厅"},
                    {"区域": "拱墅万达\n北部软件园\n城北万象城", "小区": "大华海派风景", "房号": "2-1-402A", "户型分类": "一室"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="万达有什么2000以下的一室",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("万达有什么2000以下的一室"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_text"], "")
        self.assertEqual(result["constraint_proof"]["layout"], "一室")

    async def test_resolved_area_alias_overrides_bad_llm_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "万达1500左右有哪些",
                    "query_state": {"intent": "inventory", "area": "万达"},
                    "needs_clarification": True,
                    "clarification_text": "请问是哪个城市的万达广场？",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {
                        "区域": "拱墅万达\n北部软件园\n城北万象城",
                        "小区": "合幢悦府",
                        "房号": "6-1-1204B",
                        "户型分类": "一室一厅",
                        "押一付一": "1500",
                    }
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="万达1500左右有哪些？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("万达1500左右有哪些？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_text"], "")
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertIn("拱墅万达", result["constraint_proof"]["area"])
        self.assertNotIn("哪个城市", result["effective_query"])

    async def test_area_context_overrides_community_guess_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "石桥5000左右两室整租前两套视频",
                    "query_state": {"intent": "media", "area": "石桥", "wants_video": True},
                    "needs_clarification": True,
                    "clarification_text": "你说的是石桥铭苑吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "华丰欣苑", "房号": "14-2-901"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="石桥5000左右两室整租的前两套视频也发我。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("石桥5000左右两室整租的前两套视频也发我。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_text"], "")
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["community_options"], [])
        self.assertEqual(result["constraint_proof"]["proof_status"], "complete")
        self.assertIn("石桥街道", result["constraint_proof"]["area"])
        self.assertTrue(result["constraint_proof"]["wants_video"])

    async def test_area_context_suppresses_generic_entity_community_guess(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "石桥5000左右两室整租前两套视频",
                    "query_state": {"intent": "media", "area": "石桥", "wants_video": True},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "华丰欣苑", "房号": "14-2-901"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="石桥5000左右两室整租的前两套视频也发我。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("石桥5000左右两室整租的前两套视频也发我。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_text"], "")
        self.assertEqual(result["entity_resolution"]["status"], "resolved")
        self.assertEqual(result["entity_resolution"]["community_options"], [])
        self.assertEqual(result["constraint_proof"]["proof_status"], "complete")
        self.assertIn("石桥街道", result["constraint_proof"]["area"])

    async def test_unclear_area_alias_asks_area_or_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "media",
                    "rewritten_query": "石桥视频",
                    "query_state": {"intent": "media", "wants_video": True},
                    "needs_clarification": True,
                    "clarification_text": "你说的是石桥铭苑吗？我先确认一下小区名。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "华丰欣苑", "房号": "14-2-901"},
                ]

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="石桥视频发我",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("石桥视频发我"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("石桥这个区域", result["clarification_text"])
        self.assertIn("石桥铭苑这个小区", result["clarification_text"])

    async def test_deposit_and_utilities_keep_policy_and_candidate_rows(self) -> None:
        candidate_rows = [
            {"区域": "拱墅万达", "小区": "大华海派风景", "房号": "2-1-402A", "备注": "水30/月，电1元/度"},
            {"区域": "拱墅万达", "小区": "星桥锦绣嘉苑", "房号": "20-1606A", "备注": "水30/月，电1元/度"},
        ]

        class FakeInventory:
            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [{"小区": "不应使用搜索结果", "房号": "0-000", "备注": "错误"}]

        understanding = {
            "intent": "deposit",
            "effective_query": "用户咨询免押和这几套水电",
            "query_state": {"intent": "deposit", "wants_deposit": True, "wants_utilities": True},
            "constraint_proof": {"intent": "deposit", "wants_utilities": True},
            "structured_task": {
                "tool_requirements": {
                    "needs_deposit_policy": True,
                    "needs_utilities": True,
                }
            },
        }
        planner = main._ensure_required_actions(
            {"actions": ["generate_reply"], "source": "test"},
            understanding,
            main._deterministic_signals("免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。"),
        )
        self.assertIn("send_deposit_policy", planner["actions"])
        self.assertIn("search_inventory", planner["actions"])

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            context = kf_context_memory.empty_context()
            context["last_candidate_set"] = {
                "intent": "media",
                "query": "万达2000以下一室",
                "candidates": candidate_rows,
                "shown_count": 2,
                "total_count": 2,
            }
            evidence = await main._execute_tools(
                actions=planner["actions"],
                content="免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。",
                context=context,
                understanding=understanding,
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in evidence["inventory_rows"]], ["2-1-402A", "20-1606A"])
        self.assertIn("deposit_policy", evidence["rule_evidence"])

    def test_deposit_and_utilities_evidence_survives_without_direct_reply_helper(self) -> None:
        rows = [
            {"小区": "大华海派风景", "房号": "2-1-402A", "备注": "水30/月，电1元/度"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "备注": "水30/月，电1元/度"},
        ]
        result = main._constraint_consistency_selfcheck(
            content="免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。",
            draft_reply=(
                "免押走支付宝无忧住芝麻信用评估，服务费按租期5.5%-8%。\n"
                "大华海派风景2-1-402A：水30/月，电1元/度。\n"
                "星桥锦绣嘉苑20-1606A：水30/月，电1元/度。"
            ),
            understanding={"constraint_proof": {"wants_utilities": True}},
            tool_evidence={
                "actions": ["search_inventory", "send_deposit_policy", "generate_reply"],
                "rule_evidence": {"deposit_policy": main._deposit_policy_evidence()},
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_deposit_utilities_signal_forces_inventory_search(self) -> None:
        planner = main._ensure_required_actions(
            {"actions": ["send_deposit_policy", "generate_reply"], "source": "test"},
            {
                "intent": "deposit",
                "constraint_proof": {},
                "structured_task": {"tool_requirements": {"needs_deposit_policy": True}},
            },
            main._deterministic_signals("免押金要什么条件？顺便说下这几套水电怎么收。"),
        )

        self.assertIn("send_deposit_policy", planner["actions"])
        self.assertIn("search_inventory", planner["actions"])
        self.assertIn("context_tools", planner["actions"])
        self.assertIn("generate_reply", planner["actions"])

    def test_utilities_selfcheck_rejects_missing_remark_answer(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="免押金要什么条件？顺便说下这几套水电怎么收。",
            draft_reply="免押走支付宝无忧住，服务费按租期收取。",
            understanding={
                "constraint_proof": {"wants_utilities": True},
                "structured_task": {"tool_requirements": {"needs_utilities": True}},
            },
            tool_evidence={
                "inventory_rows": [
                    {"小区": "大华海派风景", "房号": "2-1-402A", "备注": "水30/月，电1元/度"},
                ]
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("水电", result["reason"])

    def test_viewing_selfcheck_rejects_missing_viewing_evidence(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
            draft_reply="我先帮您确认一下最新房态，稍后给您准确回复。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "target_rows": [
                    {"小区": "棠润府", "房号": "15-2-801B", "看房方式密码": "6.19空出 看房提前联系"},
                ],
                "rule_evidence": {},
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("看房", result["reason"])

    def test_outbound_selfcheck_rejects_placeholder_room_labels(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="先发有视频的：XX小区XX房号视频，其他几套稍后补发你。",
            tool_evidence={"actions": ["send_video"]},
            outbound_package={
                "text": "先发有视频的：XX小区XX房号视频，其他几套稍后补发你。",
                "video_paths": [],
                "image_paths": [],
                "inventory_images": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("占位符", result["reason"])

    def test_inventory_selfcheck_requires_concrete_room_when_rows_found(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply="有的，拱墅万达、北部软件园、城北万象城这边预算1800以内的单间，我查到了几套，发你看看。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "拱墅万达/北部软件园/城北万象城",
                    "budget_range": [0, 1800],
                    "layout": "一室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅", "押一付一": "1600"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("真实小区+房号", result["reason"])

    def test_inventory_selfcheck_rejects_generic_password_in_multi_room_listing(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply=(
                "有的，北部软件园这边1800以内单间有星桥锦绣嘉苑20-1606A和棠润府15-2-801B。"
                "看房密码一般是960615#，需要看哪个随时联系我。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "拱墅万达/北部软件园/城北万象城",
                    "budget_range": [0, 1800],
                    "layout": "一室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("多房源推荐不能泛化看房密码", result["reason"])

    def test_inventory_selfcheck_rejects_found_claim_without_rows(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥附近5000左右的两室整租有的，查到了几套。比如某小区两室一厅，押一付一5000。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "budget_range": [4500, 5500],
                    "layout": "两室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("没有返回匹配房源", result["reason"])

    def test_inventory_selfcheck_rejects_soft_found_claim_without_rows(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥街道附近4500-5500预算的两室一厅整租房源有查到，押一付一价格在5000元左右，看房方式可进一步查看。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "budget_range": [4500, 5500],
                    "layout": "两室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("没有返回匹配房源", result["reason"])

    def test_inventory_selfcheck_rejects_missing_feature_constraint_in_no_match_reply(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="华丰附近有没有带燃气的一室一厅？",
            draft_reply="我这边暂时没查到石桥街道、华丰、石桥、永佳、半山的一室一厅完全匹配房源。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "layout": "一室一厅",
                    "features": ["燃气"],
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("回复遗漏特征约束：燃气", result["reason"])

    def test_inventory_no_match_reply_preserves_feature_constraint(self) -> None:
        reply = main._reply_for_inventory_search(
            {
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "layout": "一室一厅",
                    "features": ["燃气"],
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            {
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertIn("燃气", reply)
        self.assertIn("一室一厅", reply)
        self.assertIn("石桥街道", reply)

    def test_inventory_final_fallback_preserves_feature_constraint(self) -> None:
        reply = main._constraint_preserving_inventory_fallback(
            {
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "layout": "一室一厅",
                    "features": ["燃气"],
                },
            },
            "我这边为了避免发错，先不乱发。你把小区+房号或更具体条件发我一下，我重新按最新房源表查准。",
        )

        self.assertIn("燃气", reply)
        self.assertIn("一室一厅", reply)
        self.assertNotIn("小区+房号", reply)

    def test_price_comparison_selfcheck_rejects_conclusion_when_one_room_missing(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="兴业杨家府3-601和10-1-304哪个价格低点？",
            draft_reply="兴业杨家府3-601押一付一4500元，押二付一4200元；10-1-304没查到具体价格信息，所以3-601押二付一价格更低。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "communities": ["兴业杨家府"],
                    "room_refs": ["3-601", "10-1-304"],
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "兴业杨家府", "房号": "3-601", "押一付一": "4500", "押二付一": "4200"}
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("只查到部分房源", result["reason"])

    def test_price_comparison_selfcheck_allows_missing_room_explanation(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="兴业杨家府3-601和10-1-304哪个价格低点？",
            draft_reply="有的，兴业杨家府3-601押一付一4500元，押二付一4200元；10-1-304暂时没查到。目前只查到3-601这套，没法对比哪套更便宜，建议先确认房号。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "communities": ["兴业杨家府"],
                    "room_refs": ["3-601", "10-1-304"],
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "兴业杨家府", "房号": "3-601", "押一付一": "4500", "押二付一": "4200"}
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_price_comparison_selfcheck_does_not_require_inferred_area_for_exact_rooms(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="新柠长木府3-1002A和3-1002B价格一样吗？",
            draft_reply=(
                "新柠长木府3-1002A和3-1002B价格不一样。"
                "3-1002A押一付一4600元，押二付一4300元；"
                "3-1002B押一付一3300元，押二付一3000元。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "东新园\n杭氧\n新天地",
                    "communities": ["新柠长木府"],
                    "room_refs": ["3-1002A", "3-1002B"],
                    "layout": "两室一厅",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "新柠长木府", "房号": "3-1002A", "押一付一": "4600", "押二付一": "4300"},
                    {"小区": "新柠长木府", "房号": "3-1002B", "押一付一": "3300", "押二付一": "3000"},
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_viewing_selfcheck_requires_viewing_answer_when_today_viewing_requested(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="诸葛龙吟院10-601A还在吗？客户今天想看。",
            draft_reply="还在，诸葛龙吟院10-601A是两室一厅，押一付一3700元，押二付一3400元，水30/月，电1元/度。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["诸葛龙吟院"], "room_refs": ["10-601A"]},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "小区": "诸葛龙吟院",
                        "房号": "10-601A",
                        "押一付一": "3700",
                        "押二付一": "3400",
                        "看房方式密码": "336699#",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("今天看/看房方式", result["reason"])

    def test_viewing_selfcheck_requires_availability_answer_when_asked_still_available(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="诸葛龙吟院10-601A还在吗？客户今天想看。",
            draft_reply="诸葛龙吟院10-601A：看房方式是 336699#。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["诸葛龙吟院"], "room_refs": ["10-601A"]},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "小区": "诸葛龙吟院",
                        "房号": "10-601A",
                        "押一付一": "3700",
                        "押二付一": "3400",
                        "看房方式密码": "336699#",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("有没有/有哪些/还在吗", result["reason"])

    def test_inventory_selfcheck_rejects_contradictory_found_then_no_match(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥街道附近4500-5500预算内有两室一厅整租房源，暂时没查到符合条件的房源。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "石桥街道/华丰/石桥/永佳/半山",
                    "budget_range": [4500, 5500],
                    "layout": "两室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能先说有房源再说没查到", result["reason"])

    def test_inventory_selfcheck_rejects_two_rooms_claim_with_one_label(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply="北部软件园附近预算1800元以内的单间查到了：棠润府15-2-801B，押一付一1600。两套都符合您的预算。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "area": "拱墅万达/北部软件园/城北万象城",
                    "budget_range": [0, 1800],
                    "layout": "一室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("两套房源", result["reason"])

    def test_human_selfcheck_rejects_waiting_reply_when_tool_has_facts(self) -> None:
        result = main._local_human_context_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            draft_reply="我查一下星桥锦绣嘉苑20-1606A还在不在，价格多少，稍等。",
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                ],
            },
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能只回复稍后确认", result["reason"])

    def test_human_selfcheck_allows_password_when_customer_wants_today_viewing(self) -> None:
        result = main._local_human_context_selfcheck(
            content="诸葛龙吟院10-601A还在吗？客户今天想看。",
            draft_reply=(
                "还在的，诸葛龙吟院10-601A这套可以看。看房密码是336699#，"
                "如果现场密码不对或门打不开，就联系18758141785 / 13282125992 / 19941091943确认。"
            ),
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": [
                    {
                        "小区": "诸葛龙吟院",
                        "房号": "10-601A",
                        "押一付一": "3700",
                        "押二付一": "3400",
                        "看房方式密码": "336699#",
                    },
                ],
                "rule_evidence": {"viewing_contact": True},
            },
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "pass")

    def test_human_selfcheck_rejects_unasked_missing_video_comment(self) -> None:
        result = main._local_human_context_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply=(
                "有的，东新园这边有几套空出时间已定。"
                "按当前区域和空出时间暂时没匹配到可直接发视频的具体房源。"
            ),
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能主动提素材缺失", result["reason"])

    def test_price_selfcheck_rejects_reply_without_payment_prices(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            draft_reply="查到了，星桥锦绣嘉苑20-1606A还在的，价格我马上发给您。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["星桥锦绣嘉苑"], "room_refs": ["20-1606A"]},
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("月租价格", result["reason"])

    def test_price_selfcheck_rejects_wrong_payment_field_value(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="荣润府15-2-801B还在吗？1600那套视频发我。",
            draft_reply="棠润府15-2-801B这套房还在，押一付一1600，押二付一1600。这是该房源的视频。",
            understanding={
                "intent": "media",
                "constraint_proof": {
                    "communities": ["棠润府"],
                    "room_refs": ["15-2-801B"],
                    "wants_video": True,
                },
                "structured_task": {"tool_requirements": {"needs_video": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "inventory_rows": [
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
                "target_rows": [
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("押二付一应为1400", result["reason"])

    def test_price_selfcheck_requires_both_payment_prices_for_generic_price_question(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            draft_reply="还在，星桥锦绣嘉苑20-1606A押一付一月租1900元，一室一厅。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["星桥锦绣嘉苑"], "room_refs": ["20-1606A"]},
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("遗漏押二付一月租", result["reason"])

    def test_price_selfcheck_allows_single_payment_method_when_user_asks_yayi_only(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="石桥铭苑6-1102押一付一多少？",
            draft_reply="石桥铭苑6-1102押一付一月租4800元。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["石桥铭苑"], "room_refs": ["6-1102"]},
                "structured_task": {"tool_requirements": {"needs_inventory": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "石桥铭苑", "房号": "6-1102", "押一付一": "4800", "押二付一": "4300"},
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_outbound_selfcheck_rejects_phone_me_wording(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="看房密码960615#，或者电话我18758141785，这几套看中哪套随时联系。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"]},
            outbound_package={
                "text": "看房密码960615#，或者电话我18758141785，这几套看中哪套随时联系。",
                "video_paths": [],
                "image_paths": [],
                "inventory_images": [],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能声称自己会打电话", result["reason"])

    def test_outbound_selfcheck_rejects_inventory_image_text_contradiction(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            result = main._outbound_package_selfcheck(
                draft_reply="抱歉，目前暂未查到拱墅万达附近的最新房源表。我已为您记录需求，一旦有新房源会第一时间发您。",
                tool_evidence={"actions": ["send_inventory_sheet"]},
                outbound_package={
                    "text": "抱歉，目前暂未查到拱墅万达附近的最新房源表。我已为您记录需求，一旦有新房源会第一时间发您。",
                    "inventory_images": [image.name],
                    "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。",
                    "video_paths": [],
                    "image_paths": [],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("文本说不能发送房源表", result["reason"])

    def test_outbound_selfcheck_rejects_inventory_image_generation_failure_text(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            result = main._outbound_package_selfcheck(
                draft_reply="房源表图片这边暂时没生成成功，我先不乱发。",
                tool_evidence={"actions": ["send_inventory_sheet"]},
                outbound_package={
                    "text": "房源表图片这边暂时没生成成功，我先不乱发。",
                    "inventory_images": [image.name],
                    "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。",
                    "video_paths": [],
                    "image_paths": [],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("文本说不能发送房源表", result["reason"])

    def test_human_selfcheck_rejects_waiting_reply_without_tool_facts(self) -> None:
        result = main._local_human_context_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="好的，我查一下石桥及周边区域，预算4500到5500的两室整租房源，稍等告诉你。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能只回复稍后确认", result["reason"])

    def test_human_selfcheck_rejects_proactive_notification_promise(self) -> None:
        result = main._local_human_context_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥这边暂时没查到，我帮你留意着，有合适的马上通知你。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("主动通知", result["reason"])

    def test_human_selfcheck_rejects_first_notice_promise(self) -> None:
        result = main._local_human_context_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥这边暂时没查到，我们正在持续更新房源信息，有新资源会第一时间通知您。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("主动通知", result["reason"])

    def test_human_selfcheck_rejects_push_latest_info_promise(self) -> None:
        result = main._local_human_context_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="在石桥街道附近暂时没查到两室整租。我们正在持续更新房源，稍后会为您推送最新信息。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("主动通知", result["reason"])

    def test_human_selfcheck_rejects_later_send_promise(self) -> None:
        result = main._local_human_context_selfcheck(
            content="华丰欣苑14-2-901视频发我，客户想看看装修。",
            draft_reply="视频先发你，目前暂无其他视频素材，正在补同步，稍后会发您。",
            tool_evidence={"actions": ["send_video"], "video_paths": ["room.mp4"]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("主动通知", result["reason"])

    def test_human_selfcheck_rejects_media_replenish_promise(self) -> None:
        result = main._local_human_context_selfcheck(
            content="万达附近1500左右还有哪些？先发几套视频我筛一下。",
            draft_reply="这几套视频发你了，小洋坝家园三区12-1003B暂时没找到视频，后面素材补齐再处理。",
            tool_evidence={"actions": ["send_video", "explain_missing_media"], "video_paths": ["room.mp4"]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("主动通知", result["reason"])

    def test_missing_media_fallback_does_not_promise_future_replenish(self) -> None:
        reply = main._reply_for_missing_media(
            {"constraint_proof": {"wants_video": True}},
            {
                "missing_media": ["小洋坝家园三区12-1003B:视频"],
                "video_paths": ["room.mp4"],
                "video_rows": [{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}],
            },
        )

        self.assertIn("小洋坝家园三区12-1003B", reply)
        self.assertIn("星桥锦绣嘉苑20-1606A", reply)
        self.assertIn("已找到这些视频", reply)
        self.assertTrue(reply.startswith("有的，"))
        self.assertNotIn("后面素材补齐", reply)
        self.assertNotIn("稍后发", reply)

    def test_missing_media_fallback_without_any_media_does_not_claim_sendable_material(self) -> None:
        reply = main._reply_for_missing_media(
            {"constraint_proof": {"wants_video": True}},
            {"missing_media": ["小洋坝家园三区12-1003B:视频"], "video_paths": []},
        )

        self.assertIn("小洋坝家园三区12-1003B", reply)
        self.assertIn("这次没有可发送的视频", reply)
        self.assertNotIn("已找到的素材", reply)

    def test_budget_selfcheck_requires_partial_payment_scope_note(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply=(
                "有的，查到以下几套符合预算1800元以内的在租房源：\n"
                "1. 星桥锦绣嘉苑20-1606A，一室一厅，押一付一1900元，押二付一1800元。\n"
                "2. 棠润府15-2-801B，一室一厅，押一付一1600元，押二付一1400元。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "budget_range": [0, 1800],
                },
                "structured_task": {"tool_requirements": {"needs_inventory_search": True}},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("只有部分付款方式在预算内", result["reason"])

    def test_budget_selfcheck_allows_partial_payment_scope_note(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply=(
                "有的，有些房源是押一付一或押二付一其中一种月租在预算内，我把两种付款方式都列出来：\n"
                "1. 星桥锦绣嘉苑20-1606A，一室一厅，押一付一1900元，押二付一1800元。\n"
                "2. 棠润府15-2-801B，一室一厅，押一付一1600元，押二付一1400元。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "budget_range": [0, 1800],
                },
                "structured_task": {"tool_requirements": {"needs_inventory_search": True}},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"},
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_human_selfcheck_rejects_unsolicited_viewing_password(self) -> None:
        result = main._local_human_context_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            draft_reply="还在，星桥锦绣嘉苑20-1606A押一付一1900，押二付一1800，看房密码是960615#。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": [{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能主动给看房密码", result["reason"])

    def test_human_selfcheck_rejects_unasked_missing_media_hint(self) -> None:
        result = main._local_human_context_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            draft_reply="还在，星桥锦绣嘉苑20-1606A押一付一1900，押二付一1800。暂时没找到视频素材，可以先看房源表。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": [{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能主动提素材缺失", result["reason"])
        self.assertIn("不能主动让客户看房源表", result["reason"])

    def test_human_selfcheck_rejects_unasked_video_image_not_found_variant(self) -> None:
        result = main._local_human_context_selfcheck(
            content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
            draft_reply="有的，北部软件园附近预算1800元以内查到几套。视频和图片暂未找到，可以先看房源信息。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": [{"小区": "棠润府", "房号": "15-2-801B"}]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能主动提素材缺失", result["reason"])

    def test_human_selfcheck_rejects_listing_query_with_still_available_opening(self) -> None:
        result = main._local_human_context_selfcheck(
            content="万达附近1500左右还有哪些？先发几套视频我筛一下。",
            draft_reply="在拱墅万达区域，预算1500左右的一室一厅房源还在的，我先发几套视频你筛一下。",
            tool_evidence={"actions": ["search_inventory", "send_video"], "inventory_rows": [{"小区": "棠润府", "房号": "15-2-801B"}]},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能用还在/在的开头", result["reason"])

    def test_routine_planner_reply_uses_local_final_selfcheck_without_llm(self) -> None:
        result = main._needs_llm_final_selfcheck(
            content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"communities": ["星桥锦绣嘉苑"], "room_refs": ["20-1606A"]},
            },
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "押一付一": "1900", "押二付一": "1800"},
                ],
            },
            draft_reply="星桥锦绣嘉苑20-1606A还在的，一室一厅，押一付一1900，押二付一1800。",
            rule_selfcheck={"status": "pass"},
            deterministic_reply_source="planner_reply_text",
            retry_reason="",
        )

        self.assertFalse(result)

    def test_high_risk_reply_still_uses_llm_final_selfcheck(self) -> None:
        result = main._needs_llm_final_selfcheck(
            content="密码不对，门打不开怎么处理？",
            understanding={"intent": "viewing", "constraint_proof": {}},
            tool_evidence={"actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"]},
            draft_reply="密码不对就联系18758141785确认。",
            rule_selfcheck={"status": "pass"},
            deterministic_reply_source="planner_reply_text",
            retry_reason="",
        )

        self.assertTrue(result)

    def test_outbound_selfcheck_rejects_sent_video_with_unsynced_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "room.mp4"
            video_path.write_bytes(b"video")
            result = main._outbound_package_selfcheck(
                draft_reply="好的，华丰欣苑14-2-901的视频已经发给你了。如果视频暂时没同步，等补全后我再发你。",
                tool_evidence={"actions": ["send_video"]},
                outbound_package={
                    "text": "好的，华丰欣苑14-2-901的视频已经发给你了。如果视频暂时没同步，等补全后我再发你。",
                    "video_paths": [str(video_path)],
                    "video_explanations": ["这是华丰欣苑14-2-901的视频。"],
                    "image_paths": [],
                    "inventory_images": [],
                    "missing_media": [],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("未同步", result["reason"])

    def test_outbound_selfcheck_rejects_sent_video_same_room_missing_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "room.mp4"
            video_path.write_bytes(b"video")
            result = main._outbound_package_selfcheck(
                draft_reply="棠润府15-2-801B本地暂时没找到视频，我先发你现有素材。",
                tool_evidence={
                    "actions": ["send_video"],
                    "video_rows": [{"小区": "棠润府", "房号": "15-2-801B"}],
                },
                outbound_package={
                    "text": "棠润府15-2-801B本地暂时没找到视频，我先发你现有素材。",
                    "video_paths": [str(video_path)],
                    "video_explanations": ["这是棠润府15-2-801B的视频。"],
                    "image_paths": [],
                    "inventory_images": [],
                    "missing_media": [],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("同一房源没有视频", result["reason"])

    def test_outbound_selfcheck_rejects_sent_and_missing_same_video_room(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "room.mp4"
            video_path.write_bytes(b"video")
            result = main._outbound_package_selfcheck(
                draft_reply="这是棠润府15-2-801B的视频。棠润府15-2-801B暂时没找到视频。",
                tool_evidence={
                    "actions": ["send_video", "explain_missing_media"],
                    "video_rows": [{"小区": "棠润府", "房号": "15-2-801B"}],
                },
                outbound_package={
                    "text": "这是棠润府15-2-801B的视频。棠润府15-2-801B暂时没找到视频。",
                    "video_paths": [str(video_path)],
                    "video_explanations": ["这是棠润府15-2-801B的视频。"],
                    "image_paths": [],
                    "inventory_images": [],
                    "missing_media": ["棠润府15-2-801B: 视频"],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("既准备发送视频", result["reason"])

    def test_outbound_selfcheck_rejects_sent_video_with_extra_pending_promise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "room.mp4"
            video_path.write_bytes(b"video")
            result = main._outbound_package_selfcheck(
                draft_reply="这是华丰欣苑14-2-901的装修视频，您看看是否满意。目前暂无其他视频素材，正在补同步，稍后会发您。",
                tool_evidence={"actions": ["send_video"]},
                outbound_package={
                    "text": "这是华丰欣苑14-2-901的装修视频，您看看是否满意。目前暂无其他视频素材，正在补同步，稍后会发您。",
                    "video_paths": [str(video_path)],
                    "video_explanations": ["这是华丰欣苑14-2-901的视频。"],
                    "image_paths": [],
                    "inventory_images": [],
                    "missing_media": [],
                },
            )

        self.assertEqual(result["status"], "retry")
        self.assertIn("视频已准备发送", result["reason"])

    def test_preserved_sendable_evidence_survives_planner_retry(self) -> None:
        preserved = {
            "actions": ["search_inventory", "send_video", "generate_reply"],
            "video_paths": ["video-a.mp4"],
            "video_rows": [{"小区": "棠润府", "房号": "15-2-801B"}],
            "missing_media": ["小洋坝家园三区12-1003B: video missing"],
            "media_request": {"wants_video": True, "requested_count": 2},
        }
        current = {
            "actions": ["search_inventory", "explain_missing_media", "generate_reply"],
            "missing_media": ["小洋坝家园三区12-1003B: video missing"],
        }

        merged = main._merge_preserved_sendable_evidence(current, preserved)

        self.assertEqual(merged["video_paths"], ["video-a.mp4"])
        self.assertIn("send_video", merged["actions"])
        self.assertEqual(merged["video_rows"][0]["房号"], "15-2-801B")

    def test_outbound_selfcheck_rejects_front_media_hint_without_actions(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="这几套我查到了房源，但本地暂时没找到视频：小洋坝家园三区12-1003B。你可以先看前面已经有的素材。",
            tool_evidence={"actions": ["explain_missing_media"]},
            outbound_package={
                "text": "这几套我查到了房源，但本地暂时没找到视频：小洋坝家园三区12-1003B。你可以先看前面已经有的素材。",
                "video_paths": [],
                "image_paths": [],
                "inventory_images": [],
                "missing_media": ["小洋坝家园三区12-1003B: video missing"],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("前面素材", result["reason"])

    def test_outbound_selfcheck_requires_missing_media_room_label(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="有的，先发你4套视频。有些房源暂时没有视频，正在补同步。",
            tool_evidence={"actions": ["send_video", "explain_missing_media"]},
            outbound_package={
                "text": "有的，先发你4套视频。有些房源暂时没有视频，正在补同步。",
                "video_paths": [],
                "image_paths": [],
                "inventory_images": [],
                "missing_media": ["小洋坝家园三区12-1003B: 视频"],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("点名缺素材", result["reason"])

    def test_human_selfcheck_rejects_final_answer_that_only_promises_to_list(self) -> None:
        result = main._local_human_context_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="石桥街道、华丰、永佳、半山这边4500到5500的两室整租，我帮你查一下，马上列出来给你。",
            tool_evidence={"actions": ["search_inventory", "generate_reply"], "inventory_rows": []},
            deterministic_reply_source="planner_reply_text",
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能只回复稍后确认", result["reason"])

    def test_rewrite_inventory_index_contains_user_field_semantics(self) -> None:
        index = build_rewrite_inventory_index(
            [
                {
                    "区域": "东新园\n杭氧\n新天地",
                    "小区": "新柠长木府",
                    "房号": "3-1002B",
                    "户型描述": "两室一厅朝南带阳台",
                    "户型分类": "两室一厅",
                    "押一付一": "3500",
                    "押二付一": "3200",
                    "看房方式密码": "VIEWING_SECRET_CANARY_246810# 6.22空出 看房提前联系",
                    "备注": "水30/月，电1元/度",
                    "视频数量": "1",
                }
            ]
        )

        self.assertEqual(index["field_semantics"], FIELD_SEMANTICS)
        self.assertEqual(index["areas"][0]["price_range"], [3200, 3500])
        self.assertIn("押一付一", index["field_aliases"])
        self.assertIn("押二付一", index["field_aliases"])
        self.assertEqual(index["room_index"][0]["layout_description"], "两室一厅朝南带阳台")
        self.assertEqual(index["room_index"][0]["utilities"], "水30/月，电1元/度")
        self.assertEqual(index["media_summary"]["rooms_with_videos"], ["新柠长木府3-1002B"])
        self.assertEqual(index["media_summary"]["unknown_image_status_count"], 1)
        self.assertEqual(index["viewing_summary"]["needs_contact"], 1)
        self.assertEqual(index["availability_summary"]["has_empty_out_hint"], 1)
        self.assertNotIn("viewing", index["room_index"][0])
        self.assertTrue(index["room_index"][0]["has_password"])
        self.assertEqual(index["room_index"][0]["viewing_mode"], "password_available")
        self.assertTrue(index["room_index"][0]["viewing_summary"]["has_empty_out_hint"])
        self.assertNotIn(
            "VIEWING_SECRET_CANARY",
            json.dumps(index, ensure_ascii=False),
        )

    def test_rewrite_inventory_index_contains_similar_community_groups(self) -> None:
        index = build_rewrite_inventory_index(
            [
                {"区域": "石桥街道", "小区": "兴业杨家府", "房号": "4-1502"},
                {"区域": "石桥街道", "小区": "杨家新雅苑", "房号": "15-603"},
                {"区域": "石桥街道", "小区": "杨乐府", "房号": "9-604B"},
            ]
        )

        names = {item["name"] for item in index["similar_communities"]}
        self.assertTrue({"兴业杨家府", "杨家新雅苑"} & names)
        sliced = main.slice_rewrite_inventory_index(index, query="杨家府还有吗")
        self.assertIn("similar_communities", sliced)
        self.assertIn("viewing_summary", sliced)
        self.assertIn("availability_summary", sliced)

    def test_area_alias_slice_does_not_claim_exact_community_hit(self) -> None:
        index = build_rewrite_inventory_index(
            [
                {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "兴业杨家府", "房号": "3-601"},
            ],
            area_aliases=main.AREA_ALIASES,
        )

        sliced = main.slice_rewrite_inventory_index(index, query="石桥附近5000左右有两室吗")

        self.assertTrue(sliced["exact_area_hits"])
        self.assertEqual(sliced["exact_community_hits"], [])
        related_names = {item["name"] for item in sliced["area_related_communities"]}
        self.assertIn("石桥铭苑", related_names)

    def test_inventory_rewrite_index_filters_area_alias_from_similar_community_candidates(self) -> None:
        index = main._build_inventory_rewrite_index(
            content="石桥街道附近3000-4000有两室吗？客户想今天先筛两套。",
            rows=[
                {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "兴业杨家府", "房号": "3-601"},
            ],
            signals={},
        )

        self.assertTrue(index["exact_area_hits"])
        self.assertEqual(index["exact_community_hits"], [])
        self.assertFalse(
            any(item["raw_text"] in {"石桥", "石桥街道"} for item in index["similar_community_candidates"])
        )

    async def test_understand_message_strips_area_alias_inferred_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "石桥铭苑 3000-4000 两室 在租房源",
                    "effective_query": "石桥铭苑 3000-4000 两室 在租房源",
                    "query_state": {
                        "intent": "inventory",
                        "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "community": "石桥铭苑",
                        "budget": "3000-4000",
                        "layout": "两室",
                    },
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "兴业杨家府", "房号": "3-601"},
                ]

            def cache_meta(self):
                return {"source_detail": "test"}

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="石桥街道附近3000-4000有两室吗？客户想今天先筛两套。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("石桥街道附近3000-4000有两室吗？客户想今天先筛两套。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertNotIn("石桥铭苑", str(result.get("effective_query") or ""))
        self.assertNotIn("community", result.get("query_state") or {})
        self.assertEqual(result["constraint_proof"].get("communities"), None)
        self.assertEqual(result["constraint_proof"].get("area"), "石桥街道\n华丰\n石桥\n永佳\n半山")
        self.assertEqual(
            result.get("area_alias_community_stripped", {}).get("removed_communities"),
            ["石桥铭苑"],
        )

    async def test_understand_message_strips_area_alias_constraint_proof_community(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "华丰半山附近4500左右整租两室还有吗？ 石桥街道 华丰 石桥 永佳 半山 华丰欣苑 4000到5000预算",
                    "effective_query": "华丰半山附近4500左右整租两室还有吗？ 石桥街道 华丰 石桥 永佳 半山 华丰欣苑 4000到5000预算",
                    "query_state": {},
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "communities": ["华丰欣苑"],
                        "budget_range": [4000, 5000],
                        "layout": "两室",
                        "hard_constraints": {
                            "area": True,
                            "community": True,
                            "budget_range": True,
                            "layout": True,
                        },
                    },
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "石桥铭苑", "房号": "6-1102"},
                    {"区域": "石桥街道\n华丰\n石桥\n永佳\n半山", "小区": "华丰欣苑", "房号": "14-2-901"},
                ]

            def cache_meta(self):
                return {"source_detail": "test"}

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="华丰半山附近4500左右整租两室还有吗？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("华丰半山附近4500左右整租两室还有吗？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        proof = result["constraint_proof"]
        self.assertEqual(proof.get("communities"), None)
        self.assertFalse(proof["hard_constraints"]["community"])
        self.assertEqual(proof.get("area"), "石桥街道\n华丰\n石桥\n永佳\n半山")
        self.assertNotIn("华丰欣苑", str(result.get("effective_query") or ""))
        self.assertEqual(
            result.get("area_alias_community_stripped", {}).get("removed_communities"),
            ["华丰欣苑"],
        )

    async def test_current_room_query_overrides_stale_inventory_sheet_state(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory_sheet",
                    "rewritten_query": "继续按上一轮房源表任务处理，生成房源表。",
                    "effective_query": "继续按上一轮房源表任务处理，生成房源表。",
                    "query_state": {"intent": "inventory_sheet", "wants_inventory_sheet": True},
                    "needs_clarification": False,
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B", "押一付": "1600"}
                ]

        context = kf_context_memory.empty_context()
        context["active_query_state"] = {"intent": "inventory_sheet", "wants_inventory_sheet": True}
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="荣润府有没有押一付一的？预算1600到1800。",
                context=context,
                signals=main._deterministic_signals("荣润府有没有押一付一的？预算1600到1800。"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(result["intent"], "inventory")
        self.assertFalse(result["query_state"].get("wants_inventory_sheet"))
        self.assertIn("荣润府有没有押一付一的？预算1600到1800。", result["effective_query"])
        self.assertNotIn("棠润府", result["effective_query"])
        self.assertTrue(result["needs_clarification"])
        self.assertIn("你说的是棠润府吗", result["clarification_text"])

    async def test_planner_video_requirement_is_hardened_when_llm_omits_send_video(self) -> None:
        result = await main._plan_actions(
            content="杨家新雅苑49-1102视频有吗？先发我。",
            context=kf_context_memory.empty_context(),
            understanding={
                "intent": "media",
                "effective_query": "杨家新雅苑49-1102视频",
                "query_state": {"intent": "media", "wants_video": True},
                "constraint_proof": {"wants_video": True, "room_refs": ["49-1102"]},
                "structured_task": {
                    "intent": "media",
                    "tool_requirements": {"needs_video": True, "needs_inventory_search": True},
                    "tool_plan": {"actions": ["search_inventory", "generate_reply"], "confidence": 0.9},
                },
                "tool_plan": {"actions": ["search_inventory", "generate_reply"], "confidence": 0.9},
            },
            signals=main._deterministic_signals("杨家新雅苑49-1102视频有吗？先发我。"),
        )

        self.assertIn("send_video", result["actions"])
        self.assertIn("search_inventory", result["actions"])
        self.assertIn("generate_reply", result["actions"])

    async def test_unbound_context_viewing_reference_asks_for_specific_room(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "查询这几套的看房密码",
                    "query_state": {"intent": "viewing"},
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("这几套里面客户今天想看，密码多少？如果打不开门怎么办？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("小区+房号", result["clarification_text"])
        self.assertNotIn("7套", result["clarification_text"])

    async def test_bound_context_viewing_reference_uses_candidate_set(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "查询这几套的看房密码",
                    "effective_query": "查询这几套的看房密码",
                    "query_state": {"intent": "viewing"},
                    "context_reference": True,
                    "needs_clarification": True,
                    "clarification_text": "请问具体哪几套？",
                    "structured_task": {
                        "intent": "viewing",
                        "original_text": "这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                        "tool_requirements": {"needs_viewing_policy": True},
                    },
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以下一室",
            "candidates": [{"小区": "棠润府", "房号": "15-2-801B"}],
            "shown_count": 1,
            "total_count": 1,
        }
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                context=context,
                signals=main._deterministic_signals("这几套里面客户今天想看，密码多少？如果打不开门怎么办？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertTrue(result["context_reference"])

    async def test_bound_viewing_reference_sets_context_reference_without_llm_clarification(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "查询这几套的看房密码",
                    "effective_query": "查询这几套的看房密码",
                    "query_state": {"intent": "viewing"},
                    "needs_clarification": False,
                    "clarification_text": "",
                    "structured_task": {
                        "intent": "viewing",
                        "original_text": "这几套里面客户今天想看，密码多少？",
                        "tool_requirements": {"needs_viewing_policy": True},
                    },
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "北部软件园1800以内单间",
            "candidates": [{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}],
            "shown_count": 1,
            "total_count": 1,
        }
        originals = {"reply_generator": main.reply_generator, "inventory": main.inventory}
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        try:
            result = await main._understand_message(
                content="这几套里面客户今天想看，密码多少？",
                context=context,
                signals=main._deterministic_signals("这几套里面客户今天想看，密码多少？"),
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertFalse(result["needs_clarification"])
        self.assertTrue(result["context_reference"])

    def test_viewing_context_reference_targets_candidate_rows(self) -> None:
        candidates = [
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
        ]
        context = {"last_candidate_set": {"candidates": candidates}}
        rows = main._target_rows_from_understanding(
            {
                "intent": "viewing",
                "context_reference": True,
                "effective_query": "这几套密码多少",
                "structured_task": {
                    "original_text": "这几套里面客户今天想看，密码多少？",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            context,
            [],
        )

        self.assertEqual(rows, candidates)

    async def test_viewing_execute_tools_uses_candidate_rows_even_when_search_misses(self) -> None:
        candidates = [
            {"小区": "棠润府", "房号": "15-2-801B", "看房方式密码": "6.19空出 看房提前联系"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "看房方式密码": "960615#"},
        ]

        class FakeInventory:
            async def search(self, query: str, limit: int = 10) -> list[dict]:
                return [{"小区": "不应使用搜索结果", "房号": "0-000", "看房方式密码": "错误"}]

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "万达2000以下一室",
            "candidates": candidates,
            "shown_count": 2,
            "total_count": 2,
        }
        understanding = {
            "intent": "viewing",
            "effective_query": "查询这几套看房密码",
            "query_state": {"intent": "viewing"},
            "context_reference": False,
            "structured_task": {
                "intent": "viewing",
                "original_text": "这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                "tool_requirements": {"needs_viewing_policy": True},
            },
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"],
                content="这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                context=context,
                understanding=understanding,
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual([row["房号"] for row in evidence["inventory_rows"]], ["15-2-801B", "20-1606A"])
        self.assertEqual([row["房号"] for row in evidence["target_rows"]], ["15-2-801B", "20-1606A"])
        viewing_rooms = evidence["rule_evidence"]["viewing"]["rooms"]
        self.assertEqual([room["room"] for room in viewing_rooms], ["棠润府15-2-801B", "星桥锦绣嘉苑20-1606A"])

    async def test_viewing_execute_tools_recovers_candidate_rows_from_memory_query(self) -> None:
        candidates = [
            {"小区": "棠润府", "房号": "15-2-801B", "看房方式密码": "6.19空出 看房提前联系"},
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "看房方式密码": "960615#"},
        ]

        class FakeInventory:
            async def search(self, query: str, limit: int = 10) -> list[dict]:
                self.last_query = query
                return candidates

        context = kf_context_memory.empty_context()
        context["structured_memory"] = {
            "turn_records": [
                {
                    "turn_id": "t1",
                    "turn_index": 1,
                    "assistant_sent_summary": {
                        "candidate_state": {
                            "candidate_set": {
                                "query": "万达2000以下一室",
                                "shown_count": 2,
                                "total_count": 2,
                            }
                        }
                    },
                }
            ]
        }
        understanding = {
            "intent": "viewing",
            "effective_query": "查询这几套看房密码",
            "query_state": {"intent": "viewing"},
            "structured_task": {
                "intent": "viewing",
                "original_text": "这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                "tool_requirements": {"needs_viewing_policy": True},
            },
        }
        fake_inventory = FakeInventory()
        original_inventory = main.inventory
        main.inventory = fake_inventory
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "explain_unavailable_viewing", "generate_reply"],
                content="这几套里面客户今天想看，密码多少？如果打不开门怎么办？",
                context=context,
                understanding=understanding,
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(fake_inventory.last_query, "万达2000以下一室")
        self.assertEqual([row["房号"] for row in evidence["target_rows"]], ["15-2-801B", "20-1606A"])
        self.assertEqual([row["房号"] for row in context["last_candidate_set"]["candidates"]], ["15-2-801B", "20-1606A"])

    def test_single_exact_room_does_not_overwrite_candidate_set(self) -> None:
        self.assertFalse(
            main._should_remember_candidate_set(
                content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
                understanding={"constraint_proof": {"room_refs": ["20-1606A"]}},
                rows=[{"小区": "星桥锦绣嘉苑", "房号": "20-1606A"}],
            )
        )
        self.assertTrue(
            main._should_remember_candidate_set(
                content="北部软件园附近便宜点的单间还有吗？",
                understanding={},
                rows=[
                    {"小区": "棠润府", "房号": "15-2-801B"},
                    {"小区": "星桥锦绣嘉苑", "房号": "20-1606A"},
                ],
            )
        )

    def test_current_search_community_selection_overrides_stale_confirmed_room(self) -> None:
        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "label": "兴业杨家府4-1502",
            "row": {"小区": "兴业杨家府", "房号": "4-1502"},
        }
        context["last_candidate_set"] = {
            "candidates": [
                {"小区": "兴业杨家府", "房号": "4-1502"},
                {"小区": "兴业杨家府", "房号": "8-1203"},
            ]
        }
        understanding = {
            "intent": "media",
            "effective_query": "杨家新雅苑三室一厅 第一套视频",
            "rewritten_query": "杨家新雅苑三室一厅 第一套视频",
            "selected_indices": [1],
            "constraint_proof": {
                "communities": ["杨家新雅苑"],
                "selected_indices": [1],
                "wants_video": True,
            },
            "structured_task": {
                "original_text": "杨家新雅苑那套也发视频，最好清楚一点。",
                "tool_requirements": {"needs_video": True},
            },
        }
        search_rows = [
            {"小区": "杨家新雅苑", "房号": "15-603"},
            {"小区": "杨家新雅苑", "房号": "36-1102"},
        ]

        rows = main._target_rows_from_understanding(understanding, context, search_rows)

        self.assertEqual([row["小区"] for row in rows], ["杨家新雅苑"])
        self.assertEqual([row["房号"] for row in rows], ["15-603"])

    def test_explicit_media_community_without_room_ref_does_not_bind_multiple_rows(self) -> None:
        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "candidates": [
                {"小区": "兴业杨家府", "房号": "4-1502"},
                {"小区": "兴业杨家府", "房号": "8-1203"},
            ]
        }
        understanding = {
            "intent": "media",
            "effective_query": "杨家新雅苑三室视频",
            "rewritten_query": "杨家新雅苑三室视频",
            "context_reference": True,
            "constraint_proof": {
                "communities": ["杨家新雅苑"],
                "wants_video": True,
            },
            "structured_task": {
                "original_text": "杨家新雅苑那套也发视频，最好清楚一点。",
                "tool_requirements": {"needs_video": True},
            },
        }
        search_rows = [
            {"小区": "杨家新雅苑", "房号": "15-603"},
            {"小区": "杨家新雅苑", "房号": "36-1102"},
        ]

        rows = main._target_rows_from_understanding(understanding, context, search_rows)

        self.assertEqual(rows, [])

    def test_original_video_followup_binds_recent_missing_media_rows(self) -> None:
        context = kf_context_memory.empty_context()
        context["structured_memory"] = {
            "turn_records": [
                {
                    "turn_id": "t1",
                    "assistant_sent_summary": {
                        "final_reply": (
                            "这几套房源我查到了，但本地暂时没找到视频："
                            "长岳王马府4-2002、长浜龙吟轩11-1603。这次没有可发送的视频。"
                        )
                    },
                }
            ],
            "raw_dialog_context": [],
        }
        understanding = {
            "intent": "media",
            "effective_query": "有原视频或者清楚一点的吗",
            "rewritten_query": "有原视频或者清楚一点的吗",
            "context_reference": True,
            "constraint_proof": {
                "wants_video": True,
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "有原视频或者清楚一点的吗？客户嫌转发后有点糊。",
                "tool_requirements": {"needs_video": True},
            },
        }
        search_rows = [
            {"小区": "长岳王马府", "房号": "4-2002"},
            {"小区": "长浜龙吟轩", "房号": "11-1603"},
            {"小区": "嘉樘星绣府", "房号": "9-603"},
            {"小区": "新柠长木府", "房号": "3-1002A"},
        ]

        rows = main._target_rows_from_understanding(understanding, context, search_rows)

        self.assertEqual([main._row_label(row) for row in rows], ["长岳王马府4-2002", "长浜龙吟轩11-1603"])

    def test_original_video_only_flag_is_treated_as_media_request(self) -> None:
        context = kf_context_memory.empty_context()
        context["structured_memory"] = {
            "turn_records": [
                {
                    "turn_id": "t1",
                    "assistant_sent_summary": {
                        "final_reply": (
                            "这几套房源我查到了，但本地暂时没找到视频："
                            "长岳王马府4-2002、长浜龙吟轩11-1603。这次没有可发送的视频。"
                        )
                    },
                }
            ],
            "raw_dialog_context": [],
        }
        understanding = {
            "intent": "media",
            "effective_query": "有原视频或者清楚一点的吗",
            "rewritten_query": "有原视频或者清楚一点的吗",
            "context_reference": True,
            "constraint_proof": {
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "有原视频或者清楚一点的吗？客户嫌转发后有点糊。",
                "tool_requirements": {},
            },
        }
        search_rows = [
            {"小区": "长岳王马府", "房号": "4-2002"},
            {"小区": "长浜龙吟轩", "房号": "11-1603"},
            {"小区": "嘉樘星绣府", "房号": "9-603"},
        ]

        rows = main._target_rows_from_understanding(understanding, context, search_rows)

        self.assertEqual([main._row_label(row) for row in rows], ["长岳王马府4-2002", "长浜龙吟轩11-1603"])

    async def test_pending_missing_video_labels_restore_rows_for_original_followup(self) -> None:
        class FakeInventory:
            async def all_rows(self, limit=1000):
                return [
                    {"小区": "长岳王马府", "房号": "4-2002"},
                    {"小区": "长浜龙吟轩", "房号": "11-1603"},
                    {"小区": "嘉樘星绣府", "房号": "9-603"},
                ]

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["长岳王马府4-2002", "长浜龙吟轩11-1603", "长岳王马府4-2002"],
            reason="missing_or_pending_video",
            requested_count=2,
            sent_count=0,
        )
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            rows = await main._pending_video_label_rows(context)
        finally:
            main.inventory = original_inventory

        self.assertEqual([main._row_label(row) for row in rows], ["长岳王马府4-2002", "长浜龙吟轩11-1603"])

    async def test_selected_indices_without_candidates_do_not_fallback_to_inventory_search(self) -> None:
        class FakeInventory:
            async def search(self, *args, **kwargs):
                return [
                    {"小区": "棠润府", "房号": "10-1004C"},
                    {"小区": "棠润府", "房号": "15-2-801B"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_image", "context_tools", "explain_missing_media"],
                content="第1和第3套图片发我。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "第1和第3套图片发我。",
                    "rewritten_query": "第1和第3套图片发我。",
                    "constraint_proof": {
                        "selected_indices": [1, 3],
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "第1和第3套图片发我。",
                        "tool_requirements": {"needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["selection_error"]["reason"], "missing_current_candidate_set")

    async def test_in_range_selected_indices_without_candidates_do_not_bind_current_search_rows(self) -> None:
        class FakeInventory:
            async def search(self, *args, **kwargs):
                return [
                    {"\u5c0f\u533a": "\u534e\u4e30\u6b23\u82d1", "\u623f\u53f7": "14-2-901"},
                    {"\u5c0f\u533a": "\u77f3\u6865\u94ed\u82d1", "\u623f\u53f7": "6-1102"},
                ]

        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_image", "compact_listing", "generate_reply", "explain_missing_media"],
                content="1\u548c2\u7684\u56fe\u7247\u4e5f\u53d1\u6211\u3002",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "1\u548c2\u7684\u56fe\u7247\u4e5f\u53d1\u6211\u3002",
                    "rewritten_query": "\u53d1\u9001\u524d\u4e24\u5957\u5019\u9009\u623f\u6e90\u7684\u56fe\u7247",
                    "constraint_proof": {
                        "area": "\u77f3\u6865\u8857\u9053\n\u534e\u4e30\n\u77f3\u6865\n\u6c38\u4f73\n\u534a\u5c71",
                        "budget_range": [4500, 5500],
                        "layout": "\u4e24\u5ba4\u4e00\u5385",
                        "selected_indices": [1, 2],
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "1\u548c2\u7684\u56fe\u7247\u4e5f\u53d1\u6211\u3002",
                        "tool_requirements": {"needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["image_rows"], [])
        self.assertEqual(evidence["image_paths"], [])
        self.assertEqual(evidence["selection_error"]["reason"], "missing_current_candidate_set")
        self.assertEqual(evidence["selection_error"]["requested_indices"], [1, 2])
        self.assertEqual(evidence["selection_error"]["candidate_count"], 0)

    async def test_selected_indices_without_candidates_ignore_inherited_room_refs(self) -> None:
        row = {"\u5c0f\u533a": "\u77f3\u6865\u94ed\u82d1", "\u623f\u53f7": "6-1102"}

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return [row]

        context = kf_context_memory.empty_context()
        context["confirmed_room"] = {
            "row": row,
            "label": "\u77f3\u6865\u94ed\u82d16-1102",
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_image", "explain_missing_media"],
                content="\u524d\u4e24\u5957\u56fe\u7247\u4e5f\u53d1\u6211\u3002",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "\u77f3\u6865\u94ed\u82d16-1102\u7684\u524d\u4e24\u5957\u56fe\u7247",
                    "rewritten_query": "\u77f3\u6865\u94ed\u82d16-1102\u7684\u524d\u4e24\u5957\u56fe\u7247",
                    "selected_indices": [1, 2],
                    "constraint_proof": {
                        "room_refs": ["6-1102"],
                        "selected_indices": [1, 2],
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "\u524d\u4e24\u5957\u56fe\u7247\u4e5f\u53d1\u6211\u3002",
                        "tool_requirements": {"needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["image_rows"], [])
        self.assertEqual(evidence["selection_error"]["reason"], "missing_current_candidate_set")
        self.assertEqual(evidence["selection_error"]["candidate_count"], 0)

    async def test_selected_indices_with_candidates_do_not_fallback_to_inherited_search_rows(self) -> None:
        candidate_rows = [
            {"\u5c0f\u533a": "\u68e0\u6da6\u5e9c", "\u623f\u53f7": "15-2-801B"},
            {"\u5c0f\u533a": "\u5408\u5d62\u60a6\u5e9c", "\u623f\u53f7": "6-1-1204B"},
        ]
        search_rows = [
            {"\u5c0f\u533a": "\u534e\u4e30\u6b23\u82d1", "\u623f\u53f7": "14-2-901"},
            {"\u5c0f\u533a": "\u77f3\u6865\u94ed\u82d1", "\u623f\u53f7": "6-1102"},
        ]

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return search_rows

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "\u4e07\u8fbe2000\u4ee5\u5185\u4e00\u5ba4",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_image", "explain_missing_media"],
                content="\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "\u534e\u4e30\u6b23\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                    "rewritten_query": "\u534e\u4e30\u6b23\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                    "selected_indices": [1, 2],
                    "constraint_proof": {
                        "communities": ["\u534e\u4e30\u6b23\u82d1"],
                        "selected_indices": [1, 2],
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                        "tool_requirements": {"needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(
            [main._row_label(row) for row in evidence["target_rows"]],
            [main._row_label(row) for row in candidate_rows],
        )
        self.assertNotIn(
            main._row_label(search_rows[0]),
            [main._row_label(row) for row in evidence["target_rows"]],
        )
        self.assertNotIn("selection_error", evidence)

    async def test_explicit_new_community_selected_indices_do_not_send_stale_candidate_media_on_search_miss(self) -> None:
        candidate_rows = [
            {"\u5c0f\u533a": "\u68e0\u6da6\u5e9c", "\u623f\u53f7": "15-2-801B"},
            {"\u5c0f\u533a": "\u5408\u5d62\u60a6\u5e9c", "\u623f\u53f7": "6-1-1204B"},
        ]
        search_rows = [
            {"\u5c0f\u533a": "\u534e\u4e30\u6b23\u82d1", "\u623f\u53f7": "14-2-901"},
        ]

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return search_rows

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "\u4e07\u8fbe2000\u4ee5\u5185\u4e00\u5ba4",
            "candidates": candidate_rows,
            "shown_count": 2,
            "total_count": 2,
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_image", "explain_missing_media"],
                content="\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                    "rewritten_query": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247",
                    "selected_indices": [1, 2],
                    "constraint_proof": {
                        "communities": ["\u6768\u5bb6\u65b0\u96c5\u82d1"],
                        "selected_indices": [1, 2],
                        "wants_image": True,
                    },
                    "structured_task": {
                        "original_text": "\u6768\u5bb6\u65b0\u96c5\u82d1\u524d\u4e24\u5957\u56fe\u7247\u53d1\u6211\u3002",
                        "tool_requirements": {"needs_image": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["image_rows"], [])
        self.assertEqual(evidence["image_paths"], [])
        self.assertEqual(evidence["selection_error"]["reason"], "current_scope_selection_not_found")
        self.assertNotIn(main._row_label(candidate_rows[0]), [main._row_label(row) for row in evidence["target_rows"]])

    async def test_selected_indices_out_of_range_candidate_context_blocks_fallback(self) -> None:
        row = {"小区": "东新园", "房号": "8-1201"}

        class FakeInventory:
            async def search(self, *args, **kwargs):
                return [row]

        context = kf_context_memory.empty_context()
        context["last_candidate_set"] = {
            "intent": "inventory",
            "query": "东新园",
            "candidates": [row],
            "shown_count": 1,
            "total_count": 1,
        }
        original_inventory = main.inventory
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video", "context_tools", "explain_missing_media"],
                content="前两套视频发我。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "前两套视频发我。",
                    "rewritten_query": "前两套视频发我。",
                    "selected_indices": [1, 2],
                    "constraint_proof": {
                        "selected_indices": [1, 2],
                        "wants_video": True,
                    },
                    "structured_task": {
                        "original_text": "前两套视频发我。",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            main.inventory = original_inventory

        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertEqual(evidence["selection_error"]["reason"], "requested_candidate_index_out_of_range")
        self.assertEqual(evidence["selection_error"]["candidate_count"], 1)

    def test_target_rows_must_match_rewrite_community_constraint(self) -> None:
        target_rows = [{"小区": "兴业杨家府", "房号": "4-1502"}]
        inventory_rows = [{"小区": "杨家新雅苑", "房号": "15-603"}]
        proof = {"communities": ["杨家新雅苑"]}

        rows = main._enforce_target_rows_community_constraints(target_rows, inventory_rows, proof)

        self.assertEqual([main._row_label(row) for row in rows], ["杨家新雅苑15-603"])

    def test_wrong_target_rows_are_cleared_when_no_matching_inventory_rows(self) -> None:
        target_rows = [{"小区": "兴业杨家府", "房号": "4-1502"}]
        proof = {"communities": ["杨家新雅苑"]}

        rows = main._enforce_target_rows_community_constraints(target_rows, [], proof)

        self.assertEqual(rows, [])

    def test_blurry_video_followup_prefers_recent_sent_video_room(self) -> None:
        context = kf_context_memory.empty_context()
        context["structured_memory"] = {
            "turn_records": [
                {
                    "turn_id": "t1",
                    "assistant_sent_summary": {
                        "final_reply": "这是棠润府15-2-801B的视频。",
                        "sent_actions": [{"type": "video", "room": "棠润府15-2-801B", "count": 1}],
                    },
                },
                {
                    "turn_id": "t2",
                    "assistant_sent_summary": {
                        "final_reply": "这几套房源我查到了，但本地暂时没找到图片：瑷颐湾13-1-402A。"
                    },
                },
            ],
            "raw_dialog_context": [],
        }
        understanding = {
            "intent": "media",
            "effective_query": "视频糊 有没有原视频链接",
            "rewritten_query": "视频糊 有没有原视频链接",
            "context_reference": True,
            "constraint_proof": {
                "wants_video": True,
                "wants_original_video": True,
            },
            "structured_task": {
                "original_text": "如果客户说视频糊，有没有原视频链接？",
                "tool_requirements": {"needs_video": True},
            },
        }
        search_rows = [
            {"小区": "瑷颐湾", "房号": "13-1-402A"},
            {"小区": "棠润府", "房号": "15-2-801B"},
            {"小区": "小洋坝家园二区", "房号": "7-1001E"},
        ]

        rows = main._target_rows_from_understanding(understanding, context, search_rows)

        self.assertEqual([main._row_label(row) for row in rows], ["棠润府15-2-801B"])

    async def test_original_video_followup_without_stable_video_target_says_previous_target_unbound(self) -> None:
        class FakeInventory:
            async def search(self, *args, **kwargs):
                return [
                    {"小区": "嘉樘星绣府", "房号": "9-603"},
                    {"小区": "新柠长木府", "房号": "3-1002A"},
                ]

        class FakeMediaStore:
            def list_room_database_videos(self, *args, **kwargs):
                raise AssertionError("没有稳定视频目标时不应收集或发送视频")

            def list_room_database_images(self, *args, **kwargs):
                return []

            def original_video_sources_for_paths(self, paths):
                return {"original_video_paths": [], "original_video_urls": [], "material_page_urls": []}

        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {
            "inventory": main.inventory,
            "media_store": main.media_store,
            "reply_generator": main.reply_generator,
            "agentic_rag": main.agentic_rag,
        }
        main.inventory = FakeInventory()
        main.media_store = FakeMediaStore()
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            for content, effective_query in (
                ("有原视频或者清楚一点的吗？客户嫌转发后有点糊。", "有原视频或者清楚一点的吗"),
                ("第一个原视频有没有？", "第一个原视频有没有"),
            ):
                with self.subTest(content=content):
                    context = kf_context_memory.empty_context()
                    context["structured_memory"] = {
                        "turn_records": [
                            {
                                "turn_id": "t1",
                                "assistant_sent_summary": {
                                    "final_reply": "我这边暂时没稳定匹配到对应素材，不能乱发视频。你回我序号，或者小区名+房号。"
                                },
                            }
                        ],
                        "raw_dialog_context": [],
                    }
                    understanding = {
                        "intent": "media",
                        "effective_query": effective_query,
                        "rewritten_query": effective_query,
                        "constraint_proof": {
                            "intent": "media",
                            "wants_video": True,
                            "wants_original_video": True,
                        },
                        "structured_task": {
                            "intent": "media",
                            "original_text": content,
                            "tool_requirements": {"needs_video": True},
                        },
                    }
                    evidence = await main._execute_tools(
                        actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                        content=content,
                        context=context,
                        understanding=understanding,
                    )
                    result = await main._generate_reply_result(
                        content=content,
                        context=context,
                        understanding=understanding,
                        tool_evidence=evidence,
                        planner_result={
                            "actions": ["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                            "reply_text": "",
                        },
                    )

                    reply = main._reply_for_field_target_error(evidence)
                    self.assertEqual(evidence["field_target_error"]["reason"], "original_video_followup_missing_stable_video_target")
                    self.assertEqual(evidence["target_rows"], [])
                    self.assertEqual(evidence["video_paths"], [])
                    self.assertIn("上一轮没稳定匹配到视频目标", reply)
                    self.assertIn("不能直接给原视频/高清源", reply)
                    self.assertNotIn("这是嘉樘星绣府9-603的视频", reply)
                    self.assertFalse(result["needs_planner_retry"])
                    self.assertIn("上一轮没稳定匹配到视频目标", result["reply"])
                    self.assertEqual(
                        evidence["planner_reply_result"]["source"],
                        "tool_grounded_original_video_target_error",
                    )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

    async def test_original_video_followup_binds_pending_video_labels_without_wrong_send(self) -> None:
        class FakeInventory:
            async def search(self, *args, **kwargs):
                return []

            async def all_rows(self, limit=1000):
                return [
                    {"小区": "小洋坝家园三区", "房号": "12-1003B"},
                    {"小区": "星桥锦绣嘉苑", "房号": "17-503B"},
                    {"小区": "嘉樘星绣府", "房号": "9-603"},
                ]

        class FakeMediaStore:
            def list_room_database_videos(self, query: str, limit: int = 6):
                return []

            def list_room_database_images(self, query: str, limit: int = 6):
                return []

            def original_video_sources_for_paths(self, paths):
                return {"original_video_paths": [], "original_video_urls": [], "material_page_urls": []}

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["小洋坝家园三区12-1003B", "星桥锦绣嘉苑17-503B"],
            reason="missing_or_pending_video",
            requested_count=2,
            sent_count=0,
        )
        originals = {"inventory": main.inventory, "media_store": main.media_store}
        main.inventory = FakeInventory()
        main.media_store = FakeMediaStore()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                content="有原视频或者清楚一点的吗？",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "有原视频或者清楚一点的吗",
                    "constraint_proof": {
                        "intent": "media",
                        "wants_video": True,
                        "wants_original_video": True,
                    },
                    "structured_task": {
                        "intent": "media",
                        "original_text": "有原视频或者清楚一点的吗？",
                        "tool_requirements": {"needs_video": True},
                    },
                },
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertEqual(
            [main._row_label(row) for row in evidence["target_rows"]],
            ["小洋坝家园三区12-1003B", "星桥锦绣嘉苑17-503B"],
        )
        self.assertNotIn("field_target_error", evidence)
        self.assertIn("小洋坝家园三区12-1003B:视频", evidence["missing_media"])
        self.assertIn("星桥锦绣嘉苑17-503B:视频", evidence["missing_media"])
        self.assertNotIn("嘉樘星绣府9-603:视频", evidence["missing_media"])
        self.assertEqual(evidence["video_paths"], [])

    async def test_continue_pending_video_uses_pending_missing_labels(self) -> None:
        class FakeInventory:
            async def search(self, *args, **kwargs):
                raise AssertionError("继续发剩下的视频时不应重新泛搜房源")

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["小洋坝家园三区12-1003B", "星桥锦绣嘉苑17-503B"],
            requested_count=2,
            sent_count=0,
        )
        originals = {"inventory": main.inventory}
        main.inventory = FakeInventory()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                content="继续发剩下的视频。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "继续发剩下的视频",
                    "constraint_proof": {"wants_video": True},
                    "query_state": {"pending_video_action": "continue"},
                    "structured_task": {"intent": "media", "tool_requirements": {"needs_video": True}},
                },
            )
        finally:
            main.inventory = originals["inventory"]

        self.assertIn("小洋坝家园三区12-1003B:视频", evidence["missing_media"])
        self.assertIn("星桥锦绣嘉苑17-503B:视频", evidence["missing_media"])
        self.assertEqual(evidence["media_status"]["video"]["missing_rooms"], ["小洋坝家园三区12-1003B", "星桥锦绣嘉苑17-503B"])

    def test_pending_video_continue_normalizes_rewrite_task(self) -> None:
        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["小洋坝家园三区12-1003B", "星桥锦绣嘉苑17-503B"],
            requested_count=2,
            sent_count=0,
        )
        result = main._force_pending_video_continue_task(
            "剩下的继续发。",
            {
                "intent": "inventory",
                "rewritten_query": "石桥附近4500-5500一室一厅",
                "effective_query": "石桥附近4500-5500一室一厅",
                "query_state": {"intent": "inventory"},
                "constraint_proof": {
                    "intent": "inventory",
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "budget_range": [4500, 5500],
                    "layout": "一室一厅",
                },
                "structured_task": {
                    "intent": "inventory",
                    "effective_query": "石桥附近4500-5500一室一厅",
                    "query_state": {"intent": "inventory"},
                    "constraint_proof": {"intent": "inventory"},
                    "tool_requirements": {"needs_inventory_search": True},
                },
            },
            context,
        )

        self.assertEqual(result["intent"], "media")
        self.assertIn("继续发送上一轮未完成的视频素材", result["effective_query"])
        self.assertTrue(result["constraint_proof"]["wants_video"])
        self.assertEqual(result["constraint_proof"]["pending_video_action"], "continue")
        requirements = result["structured_task"]["tool_requirements"]
        self.assertTrue(requirements["needs_video"])
        self.assertFalse(requirements["needs_inventory_search"])
        self.assertFalse(requirements["needs_viewing_policy"])

    def test_pending_video_send_all_phrase_normalizes_rewrite_task(self) -> None:
        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["兴业杨家府4-1502", "兴业杨家府8-1203"],
            requested_count=5,
            sent_count=3,
        )
        result = main._force_pending_video_continue_task(
            "能发的都发，先不要超过5套。",
            {
                "intent": "inventory",
                "rewritten_query": "石桥附近4500-5500整租",
                "effective_query": "石桥附近4500-5500整租",
                "query_state": {"intent": "inventory"},
                "constraint_proof": {"intent": "inventory"},
                "structured_task": {
                    "intent": "inventory",
                    "tool_requirements": {"needs_inventory_search": True},
                },
            },
            context,
        )

        self.assertEqual(result["intent"], "media")
        self.assertEqual(result.get("selected_indices"), [])
        self.assertEqual(main._possible_community_mentions("能发的都发，先不要超过5套。"), [])
        self.assertEqual(result["query_state"]["pending_video_action"], "continue")
        self.assertNotIn("selected_indices", result["constraint_proof"])
        requirements = result["structured_task"]["tool_requirements"]
        self.assertTrue(requirements["needs_video"])
        self.assertFalse(requirements["needs_inventory_search"])
        self.assertEqual(main._selected_indices_from_understanding(result, "能发的都发，先不要超过5套。"), [])

    async def test_new_exact_video_request_ignores_stale_pending_video_action(self) -> None:
        class FakeInventory:
            def __init__(self) -> None:
                self.searched = False

            async def search(self, *args, **kwargs):
                self.searched = True
                return [{"小区": "华丰欣苑", "房号": "14-2-901", "户型分类": "一室一厅"}]

        class FakeMediaStore:
            def list_room_database_videos(self, query: str, limit: int = 6):
                if "华丰欣苑" in query and "14-2-901" in query:
                    return [Path("room_database/video/华丰欣苑14-2-901/570.mp4")]
                return []

            def list_room_database_images(self, query: str, limit: int = 6):
                return []

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["小洋坝家园三区12-1003B"],
            requested_count=1,
            sent_count=0,
        )
        fake_inventory = FakeInventory()
        originals = {"inventory": main.inventory, "media_store": main.media_store}
        main.inventory = fake_inventory
        main.media_store = FakeMediaStore()
        try:
            evidence = await main._execute_tools(
                actions=["search_inventory", "context_tools", "send_video", "explain_missing_media", "generate_reply"],
                content="华丰欣苑14-2-901视频发我，客户想看看装修。",
                context=context,
                understanding={
                    "intent": "media",
                    "effective_query": "华丰欣苑14-2-901视频客户想看装修",
                    "constraint_proof": {
                        "communities": ["华丰欣苑"],
                        "room_refs": ["14-2-901"],
                        "wants_video": True,
                    },
                    "query_state": {"pending_video_action": "continue", "wants_video": True},
                    "structured_task": {"intent": "media", "tool_requirements": {"needs_video": True}},
                },
            )
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        self.assertTrue(fake_inventory.searched)
        self.assertEqual(evidence["target_rows"][0]["房号"], "14-2-901")
        self.assertEqual(
            [path.replace("\\", "/") for path in evidence["video_paths"]],
            ["room_database/video/华丰欣苑14-2-901/570.mp4"],
        )
        self.assertNotIn("小洋坝家园三区12-1003B:视频", evidence["missing_media"])

    async def test_pending_send_all_phrase_uses_pending_without_inventory_search(self) -> None:
        class FakeInventory:
            def __init__(self) -> None:
                self.searched = False

            async def search(self, *args, **kwargs):
                self.searched = True
                return [{"小区": "杨家新雅苑", "房号": "15-603"}]

        context = kf_context_memory.remember_pending_video_sends(
            kf_context_memory.empty_context(),
            paths=[],
            labels=["兴业杨家府4-1502", "兴业杨家府8-1203"],
            requested_count=5,
            sent_count=3,
        )
        fake_inventory = FakeInventory()
        original_inventory = main.inventory
        main.inventory = fake_inventory
        try:
            understanding = main._force_pending_video_continue_task(
                "能发的都发，先不要超过5套。",
                {
                    "intent": "inventory",
                    "constraint_proof": {"intent": "inventory", "selected_indices": [1, 2, 3, 4, 5]},
                    "structured_task": {
                        "intent": "inventory",
                        "tool_requirements": {"needs_inventory_search": True},
                    },
                },
                context,
            )
            evidence = await main._execute_tools(
                actions=["search_inventory", "send_video", "explain_missing_media", "generate_reply"],
                content="能发的都发，先不要超过5套。",
                context=context,
                understanding=understanding,
            )
        finally:
            main.inventory = original_inventory

        self.assertFalse(fake_inventory.searched)
        self.assertEqual(evidence["inventory_rows"], [])
        self.assertEqual(evidence["target_rows"], [])
        self.assertIn("兴业杨家府4-1502:视频", evidence["missing_media"])
        self.assertIn("兴业杨家府8-1203:视频", evidence["missing_media"])

    async def test_inventory_sheet_hard_rule_keeps_prepared_image_action(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先确认一下房源表。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        inventory_image = self._prepared_inventory_image()
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="房源表也发我一下，客户想自己筛。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory_sheet",
                    "constraint_proof": {"wants_inventory_sheet": True},
                    "structured_task": {"tool_requirements": {"needs_inventory_sheet": True}},
                },
                tool_evidence={
                    "actions": ["send_inventory_sheet"],
                    "inventory_images": [inventory_image],
                },
                planner_result={
                    "actions": ["send_inventory_sheet"],
                    "reply_text": "房源表发你了，你可以让客户先整体看一下。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("房源表发你了", result["reply"])

    async def test_inventory_sheet_missing_image_action_still_requires_retry(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="checking inventory sheet")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        missing_image = str(Path(tempfile.gettempdir()) / "missing_inventory_sheet_m05b.png")
        Path(missing_image).unlink(missing_ok=True)
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="send inventory sheet",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory_sheet",
                    "constraint_proof": {"wants_inventory_sheet": True},
                    "structured_task": {"tool_requirements": {"needs_inventory_sheet": True}},
                },
                tool_evidence={
                    "actions": ["send_inventory_sheet"],
                    "inventory_images": [missing_image],
                },
                planner_result={
                    "actions": ["send_inventory_sheet"],
                    "reply_text": "inventory sheet sent",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertTrue(result["needs_planner_retry"])
        self.assertEqual(result["selfcheck"]["rule"]["source"], "outbound_package_selfcheck")

    def test_inventory_sheet_reply_normalization_removes_unasked_media_clause(self) -> None:
        reply = main._normalize_inventory_sheet_reply_before_selfcheck(
            draft_reply=(
                "房源表发你了，你可以让客户先整体看一下。"
                "按当前条件，暂时没匹配到可直接发视频的具体房源。"
            ),
            understanding={
                "intent": "inventory_sheet",
                "constraint_proof": {"wants_inventory_sheet": True},
                "structured_task": {"tool_requirements": {"needs_inventory_sheet": True}},
            },
            tool_evidence={
                "actions": ["send_inventory_sheet"],
                "inventory_images": ["room_database/inventory_01.png"],
            },
        )

        self.assertEqual(reply, "房源表发你了，你可以让客户先整体看一下。")

    def test_inventory_sheet_reply_normalization_keeps_media_request_clause(self) -> None:
        draft = (
            "房源表发你了，你可以让客户先整体看一下。"
            "这两套视频暂时没找到，我先把表发你。"
        )
        reply = main._normalize_inventory_sheet_reply_before_selfcheck(
            draft_reply=draft,
            understanding={
                "intent": "inventory_sheet",
                "constraint_proof": {"wants_inventory_sheet": True, "wants_video": True},
                "structured_task": {"tool_requirements": {"needs_inventory_sheet": True, "needs_video": True}},
            },
            tool_evidence={
                "actions": ["send_inventory_sheet"],
                "inventory_images": ["room_database/inventory_01.png"],
            },
        )

        self.assertEqual(reply, draft)

    def test_unasked_viewing_tail_normalization_removes_appointment_hint(self) -> None:
        draft = (
            "有的，元宝塘附近一室房源有视频的优先推荐，查到了两套。\n"
            "这两套都有视频，已发送给你了。你可以先看视频，选中想了解的再告诉我房号，我来帮你查具体看房方式或预约。"
        )

        reply = main._normalize_unasked_viewing_tail_before_selfcheck(
            content="元宝塘附近客户想看便宜点的，有视频的优先。",
            draft_reply=draft,
            understanding={"intent": "inventory"},
        )

        self.assertNotIn("预约", reply)
        self.assertNotIn("看房方式", reply)
        self.assertIn("其他细节", reply)

    def test_unasked_viewing_tail_normalization_removes_booking_contact_sentence(self) -> None:
        draft = (
            "有的，查到三套符合条件的房源，视频已发。\n"
            "如果想约看房，可以联系18758141785 / 13282125992 / 19941091943提前预约。"
        )

        reply = main._normalize_unasked_viewing_tail_before_selfcheck(
            content="元宝塘附近客户想看便宜点的，有视频的优先。",
            draft_reply=draft,
            understanding={"intent": "inventory"},
        )

        self.assertNotIn("预约", reply)
        self.assertNotIn("18758141785", reply)
        self.assertIn("视频已发", reply)

    def test_unasked_viewing_tail_normalization_removes_embedded_booking_phrase(self) -> None:
        draft = (
            "这两套都有视频，已经发你了。"
            "你可以先看，如果想看某一套的具体视频或预约看房，直接说小区+房号。"
        )

        reply = main._normalize_unasked_viewing_tail_before_selfcheck(
            content="元宝塘附近客户想看便宜点的，有视频的优先。",
            draft_reply=draft,
            understanding={"intent": "inventory"},
        )

        self.assertNotIn("预约", reply)
        self.assertIn("具体视频", reply)

    def test_unasked_viewing_tail_normalization_keeps_explicit_viewing_request(self) -> None:
        draft = "这套可以看，选中后我来帮你查具体看房方式或预约。"

        reply = main._normalize_unasked_viewing_tail_before_selfcheck(
            content="客户今天想看，怎么预约？",
            draft_reply=draft,
            understanding={"intent": "viewing"},
        )

        self.assertEqual(reply, draft)

    def test_reply_alias_separator_normalization_removes_internal_pipe(self) -> None:
        reply = main._normalize_reply_alias_separators_before_selfcheck(
            "有的，3000以下、一室|一室一厅我查到这些还在租。"
        )

        self.assertEqual(reply, "有的，3000以下、一室、一室一厅我查到这些还在租。")

    def test_customer_visible_reply_normalization_removes_internal_formats(self) -> None:
        reply = main._normalize_customer_visible_reply_text_before_selfcheck(
            "有的，东新园\n杭氧\n新天地、{'min': 0, 'max': 5000}、一室|一室一厅里有新柠长木府2-702-B。"
        )

        self.assertNotIn("{'min'", reply)
        self.assertNotIn("|", reply)
        self.assertNotIn("东新园\n杭氧", reply)
        self.assertIn("东新园、杭氧、新天地", reply)
        self.assertIn("一室、一室一厅", reply)
        self.assertIn("新柠长木府2-702B", reply)

    def test_customer_visible_reply_normalization_preserves_inventory_list_newline(self) -> None:
        reply = main._normalize_customer_visible_reply_text_before_selfcheck(
            "1. 兴业杨家府4-1502，一室一厅，民用水电\n你要视频、图片或者看房方式的话，直接回序号就行。"
        )

        self.assertIn("民用水电\n你要视频", reply)
        self.assertNotIn("民用水电、你要视频", reply)

    def test_customer_visible_format_selfcheck_rejects_internal_leakage(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="万达、东新园两边都可以，3000以内有什么能住的？",
            draft_reply="有的，东新园、{'min': 0, 'max': 5000}、一室|一室一厅我查到这些还在租。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"intent": "inventory", "budget_range": [0, 3000]},
                "structured_task": {"tool_requirements": {"needs_inventory_search": True}},
            },
            tool_evidence={"actions": ["search_inventory"], "inventory_rows": []},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("内部", result["reason"])

    def test_customer_visible_selfcheck_rejects_over_formal_tone(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="石桥附近5000左右有两室吗？最好整租。",
            draft_reply="暂时没查到完全匹配的在租房源。建议您确认房号或放宽预算范围再试。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "intent": "inventory",
                    "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                    "budget_range": [4500, 5500],
                    "layout": "两室",
                },
                "structured_task": {"tool_requirements": {"needs_inventory_search": True}},
            },
            tool_evidence={"actions": ["search_inventory"], "inventory_rows": []},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("口吻", result["reason"])

    def test_original_video_signal_and_selfcheck_require_source_evidence(self) -> None:
        content = "这个视频太糊了，原视频发我，客户要保存转发。"
        signals = main._deterministic_signals(content)
        self.assertTrue(signals["wants_video"])
        self.assertTrue(signals["wants_original_video"])

        result = main._constraint_consistency_selfcheck(
            content=content,
            draft_reply="原视频已发你了，这是棠润府15-2-801B的视频。",
            understanding={
                "intent": "media",
                "constraint_proof": {"intent": "media", "wants_video": True, "wants_original_video": True},
                "structured_task": {"tool_requirements": {"needs_video": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "video_paths": ["room.mp4"],
                "video_rows": [{"小区": "棠润府", "房号": "15-2-801B"}],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("原视频", result["reason"])

    async def test_inventory_sheet_hard_rule_keeps_area_constraint_in_reply(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先确认一下房源表。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        inventory_image = self._prepared_inventory_image()
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="万达附近的房源表发我一下，我给客户先看整体。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory_sheet",
                    "constraint_proof": {
                        "wants_inventory_sheet": True,
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                    },
                    "structured_task": {"tool_requirements": {"needs_inventory_sheet": True}},
                },
                tool_evidence={
                    "actions": ["send_inventory_sheet"],
                    "inventory_images": [inventory_image],
                },
                planner_result={
                    "actions": ["send_inventory_sheet"],
                    "reply_text": "拱墅万达附近的房源表发你了，你可以让客户先整体看一下。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("拱墅万达", result["reply"])
        self.assertIn("房源表发你了", result["reply"])

    def test_constraint_selfcheck_does_not_overrule_prepared_inventory_sheet_action(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="万达附近的房源表发我一下，我给客户先看整体。",
            draft_reply="房源表发你了，你可以让客户先整体看一下。",
            understanding={
                "constraint_proof": {
                    "wants_inventory_sheet": True,
                    "area": "拱墅万达\n北部软件园\n城北万象城",
                }
            },
            tool_evidence={
                "actions": ["send_inventory_sheet"],
                "inventory_images": ["room_database/inventory_01.png"],
                "deterministic_reply_source": "inventory_sheet_hard_rule",
            },
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["scope"], "action_fulfilled_hard_rule")

    def test_constraint_selfcheck_allows_bound_field_followup_without_repeating_filters(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="水电和密码一起发我。",
            draft_reply="白田畈龙吟府4-902B：水电是水30/月，电1元/度；看房方式/密码是902902#。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {
                    "area": "东新园 杭氧 新天地",
                    "budget_range": [2000, 4000],
                    "layout": "一室一厅",
                    "wants_utilities": True,
                },
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "白田畈龙吟府",
                        "房号": "4-902B",
                        "户型分类": "一室一厅",
                        "押一付一": "2100",
                        "押二付一": "1800",
                        "备注": "水30/月，电1元/度",
                        "看房方式密码": "902902#",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_prepared_media_reply_does_not_leak_internal_any_layout(self) -> None:
        reply = main._reply_for_prepared_media(
            {
                "constraint_proof": {
                    "area": "拱墅万达\n北部软件园\n城北万象城",
                    "budget_range": [1000, 2000],
                    "layout": "any",
                    "wants_video": True,
                }
            },
            {
                "video_paths": ["room_database/video/demo.mp4"],
                "video_rows": [{"小区": "大华海派风景", "房号": "2-1-402A"}],
            },
        )

        self.assertIn("拱墅万达", reply)
        self.assertIn("1000-2000左右", reply)
        self.assertNotIn("any", reply)

    async def test_planner_reply_uses_tiered_final_selfcheck_for_inventory_sheet(self) -> None:
        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.selfcheck_calls = 0

            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先确认一下房源表。")

            async def assess_kf_final_reply(self, **kwargs):
                self.selfcheck_calls += 1
                return {"status": "pass", "source": "llm_final_selfcheck"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        fake_reply = FakeReplyGenerator()
        inventory_image = self._prepared_inventory_image()
        main.reply_generator = fake_reply
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="房源表发我一下。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory_sheet",
                    "constraint_proof": {"wants_inventory_sheet": True},
                    "structured_task": {"tool_requirements": {"needs_inventory_sheet": True}},
                },
                tool_evidence={
                    "actions": ["send_inventory_sheet"],
                    "inventory_images": [inventory_image],
                },
                planner_result={
                    "actions": ["send_inventory_sheet"],
                    "reply_text": "房源表发你了，你可以让客户先整体看一下。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("房源表发你了", result["reply"])
        self.assertEqual(result["selfcheck"]["status"], "pass")
        self.assertEqual(result["selfcheck"]["llm"]["source"], "llm_selfcheck_skipped_by_tiered_final_selfcheck")
        self.assertEqual(fake_reply.selfcheck_calls, 0)

    async def test_bad_planner_reply_is_not_replaced_by_tool_reply(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="有的，目前都没视频和图片，我把房源表发你。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="万达有什么2000以下的一室",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "万达2000以下一室",
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "budget_range": [0, 2000],
                        "layout": "一室",
                    },
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": [
                        {
                            "区域": "拱墅万达\n北部软件园\n城北万象城",
                            "小区": "棠润府",
                            "房号": "15-2-801B",
                            "户型分类": "一室一厅",
                            "押一付一": "1600",
                            "押二付一": "1400",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "我先按石桥附近5000左右两室整租查，没匹配到会直接说明。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertTrue(result["needs_planner_retry"])
        self.assertEqual(result["reply"], "")
        self.assertIn("我先按石桥附近5000左右两室整租查", result["draft_reply"])
        self.assertIn("planner_retry_reason", result)

    def test_no_match_reply_with_budget_word_is_not_false_positive(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="杭氧附近有没有带厨房的一室？预算3500左右。",
            draft_reply="我这边暂时没查到东新园、杭氧、新天地附近带独立厨卫的一室，符合3000-4000预算的在租房源。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {
                    "intent": "inventory",
                    "area": "东新园\n杭氧\n新天地",
                    "budget_range": [3000, 4000],
                    "layout": "一室",
                    "features": ["独立厨卫"],
                },
                "structured_task": {"intent": "inventory"},
            },
            tool_evidence={
                "actions": ["search_inventory", "compact_listing", "generate_reply"],
                "inventory_rows": [],
            },
        )

        self.assertEqual(result["status"], "pass", result)

    async def test_new_area_availability_query_drops_unasked_inherited_constraints(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "viewing",
                    "rewritten_query": "东新园 杭氧 新天地 3500 一室 马上空出来的在租房源",
                    "effective_query": "东新园 杭氧 新天地 3500 一室 马上空出来的在租房源",
                    "query_state": {
                        "intent": "viewing",
                        "area": "东新园\n杭氧\n新天地",
                        "budget": "3500",
                        "layout": "一室",
                        "features": ["独立厨卫"],
                    },
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        async def fake_inventory_rows():
            return [
                {
                    "区域": "东新园\n杭氧\n新天地",
                    "小区": "新柠长木府",
                    "房号": "3-1002A",
                    "户型分类": "两室一厅",
                    "押一付一": "4600",
                    "看房方式密码": "转租看房提前联系",
                }
            ]

        originals = {
            "reply_generator": main.reply_generator,
            "_inventory_rows_for_resolution": main._inventory_rows_for_resolution,
        }
        main.reply_generator = FakeReplyGenerator()
        main._inventory_rows_for_resolution = fake_inventory_rows
        try:
            result = await main._understand_message(
                content="东新园有没有马上空出来的？客户比较急。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("东新园有没有马上空出来的？客户比较急。"),
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main._inventory_rows_for_resolution = originals["_inventory_rows_for_resolution"]

        proof = result["constraint_proof"]
        self.assertTrue(result.get("dropped_inherited_constraints"))
        self.assertIn("东新园", proof.get("area", ""))
        self.assertNotIn("budget_range", proof)
        self.assertNotIn("layout", proof)
        self.assertNotIn("features", proof)
        self.assertEqual(result["effective_query"], "东新园有没有马上空出来的？客户比较急。 东新园 杭氧 新天地")

    async def test_explicit_community_query_drops_stale_area_and_layout_context(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "荣润府有没有押一付一的？预算1600到1800。 拱墅万达 北部软件园 城北万象城 棠润府 一室一厅",
                    "effective_query": "荣润府有没有押一付一的？预算1600到1800。 拱墅万达 北部软件园 城北万象城 棠润府 一室一厅",
                    "query_state": {
                        "intent": "inventory",
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "community": "棠润府",
                        "budget": 1700,
                        "layout": "一室一厅",
                    },
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        async def fake_inventory_rows():
            return [
                {
                    "区域": "拱墅万达\n北部软件园\n城北万象城",
                    "小区": "棠润府",
                    "房号": "15-2-801B",
                    "户型分类": "一室一厅",
                    "押一付一": "1600",
                    "押二付一": "1400",
                }
            ]

        originals = {
            "reply_generator": main.reply_generator,
            "_inventory_rows_for_resolution": main._inventory_rows_for_resolution,
        }
        main.reply_generator = FakeReplyGenerator()
        main._inventory_rows_for_resolution = fake_inventory_rows
        try:
            result = await main._understand_message(
                content="荣润府有没有押一付一的？预算1600到1800。",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("荣润府有没有押一付一的？预算1600到1800。"),
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main._inventory_rows_for_resolution = originals["_inventory_rows_for_resolution"]

        proof = result["constraint_proof"]
        self.assertTrue(result.get("dropped_inherited_constraints"))
        self.assertNotIn("拱墅万达", result["effective_query"])
        self.assertNotIn("一室一厅", result["effective_query"])
        self.assertNotIn("area", proof)
        self.assertNotIn("layout", proof)
        self.assertEqual(proof.get("budget_range"), [1600, 1800])
        self.assertEqual(proof.get("communities"), ["棠润府"])

    async def test_new_area_payment_query_drops_stale_layout_context(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "新塘附近有没有押一付一的低价房？",
                    "effective_query": "新塘附近有没有押一付一的低价房？",
                    "query_state": {
                        "intent": "inventory",
                        "area": "闸弄口\n新塘\n元宝塘\n东站",
                        "layout": "一室",
                    },
                    "needs_clarification": False,
                    "clarification_text": "",
                }

        async def fake_inventory_rows():
            return []

        originals = {
            "reply_generator": main.reply_generator,
            "_inventory_rows_for_resolution": main._inventory_rows_for_resolution,
        }
        main.reply_generator = FakeReplyGenerator()
        main._inventory_rows_for_resolution = fake_inventory_rows
        try:
            result = await main._understand_message(
                content="新塘附近有没有押一付一的低价房？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("新塘附近有没有押一付一的低价房？"),
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main._inventory_rows_for_resolution = originals["_inventory_rows_for_resolution"]

        proof = result["constraint_proof"]
        self.assertTrue(result.get("dropped_unasked_llm_inferred_constraints"))
        self.assertIn("新塘", result["effective_query"])
        self.assertNotIn("一室", result["effective_query"])
        self.assertIn("area", proof)
        self.assertNotIn("layout", proof)

    def test_viewing_area_list_requires_viewing_info(self) -> None:
        rows = [
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "长浜龙吟轩",
                "房号": "9-901",
                "户型分类": "三室一厅",
                "押一付一": "5800",
                "押二付一": "5500",
                "看房方式密码": "6.24空出 看房提前联系",
            },
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "长岳王马府",
                "房号": "1-1503",
                "户型分类": "一室一厅",
                "押一付一": "4800",
                "押二付一": "4500",
                "看房方式密码": "6.27空出 看房提前联系",
            },
        ]
        result = main._constraint_consistency_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply="有的，东新园这边我查到这些还在租：1. 长浜龙吟轩9-901，押一付一5800；2. 长岳王马府1-1503，押一付一4800。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"intent": "inventory", "area": "东新园\n杭氧\n新天地"},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("空出", result["reason"])

    def test_viewing_area_list_rejects_generic_viewing_way_prompt_only(self) -> None:
        rows = [
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "长浜龙吟轩",
                "房号": "9-901",
                "户型分类": "三室一厅",
                "押一付一": "5800",
                "押二付一": "5500",
                "看房方式密码": "6.24空出 看房提前联系",
            }
        ]
        result = main._constraint_consistency_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply="有的，东新园这边有房源还在租：1. 长浜龙吟轩9-901。你要看房方式的话直接说房号就行。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"intent": "inventory", "area": "东新园\n杭氧\n新天地"},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("空出", result["reason"])

    def test_viewing_area_availability_list_can_answer_without_specific_room_binding(self) -> None:
        rows = [
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "长浜龙吟轩",
                "房号": "9-901",
                "户型分类": "三室一厅",
                "押一付一": "5800",
                "押二付一": "5500",
                "看房方式密码": "6.24空出 看房提前联系",
            },
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "嘉樘星绣府",
                "房号": "8-2-802B",
                "户型分类": "两室一厅",
                "押一付一": "3300",
                "押二付一": "3000",
                "看房方式密码": "6.22空出 看房提前联系",
            },
        ]
        result = main._constraint_consistency_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply=(
                "有的，东新园这边有几套还在租：\n"
                "1. 长浜龙吟轩9-901，三室一厅，押一付一5800元，押二付一5500元，6月24日空出，看房需提前联系。\n"
                "2. 嘉樘星绣府8-2-802B，两室一厅，押一付一3300元，押二付一3000元，6月22日空出，看房需提前联系。\n"
                "客户比较急的话，建议先联系18758141785 / 13282125992 / 19941091943确认当天能看的时间。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {"intent": "inventory", "area": "东新园\n杭氧\n新天地"},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_viewing_selfcheck_rejects_blanket_already_empty_when_rows_have_empty_dates(self) -> None:
        rows = [
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "长浜龙吟轩",
                "房号": "9-901",
                "户型分类": "三室一厅",
                "押一付一": "5800",
                "押二付一": "5500",
                "看房方式密码": "6.24空出 看房提前联系",
            },
            {
                "区域": "东新园\n杭氧\n新天地",
                "小区": "嘉樘星绣府",
                "房号": "8-2-802B",
                "户型分类": "两室一厅",
                "押一付一": "3300",
                "押二付一": "3000",
                "看房方式密码": "6.22空出 看房提前联系",
            },
        ]
        result = main._constraint_consistency_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply=(
                "有的，东新园这边有几套还在租：\n"
                "1. 长浜龙吟轩9-901，三室一厅，押一付一5800元，押二付一5500元。\n"
                "2. 嘉樘星绣府8-2-802B，两室一厅，押一付一3300元，押二付一3000元。\n"
                "这些房源都已空出，看房需提前联系18758141785 / 13282125992 / 19941091943。"
            ),
            understanding={
                "intent": "inventory",
                "constraint_proof": {"intent": "inventory", "area": "东新园\n杭氧\n新天地"},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": rows,
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能泛称都已空出", result["reason"])

    def test_inventory_sheet_action_can_defer_unbound_viewing_to_outbound_selfcheck(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="东站附近有没有今天能看的？客户预算2500左右，先发房源表和视频。",
            draft_reply="房源表发你了，可以先给客户看整体。视频暂时没找到，等补全后再发。",
            understanding={
                "intent": "inventory_sheet",
                "constraint_proof": {
                    "intent": "inventory_sheet",
                    "area": "闸弄口\n新塘\n元宝塘\n东站",
                    "budget_range": [2000, 3000],
                    "wants_inventory_sheet": True,
                    "wants_video": True,
                },
                "structured_task": {
                    "intent": "inventory_sheet",
                    "tool_requirements": {
                        "needs_inventory_sheet": True,
                        "needs_video": True,
                        "needs_viewing_policy": True,
                    },
                },
            },
            tool_evidence={
                "actions": ["search_inventory", "send_inventory_sheet", "send_video", "explain_unavailable_viewing"],
                "inventory_rows": [],
                "inventory_images": ["inventory-1.png"],
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_payment_selfcheck_does_not_mix_same_community_rows(self) -> None:
        rows = [
            {
                "小区": "长浜龙吟轩",
                "房号": "9-901",
                "押一付一": "5800",
                "押二付一": "5500",
            },
            {
                "小区": "长浜龙吟轩",
                "房号": "11-1603",
                "押一付一": "4200",
                "押二付一": "3900",
            },
        ]
        reply = (
            "1. 长浜龙吟轩9-901，押一付一5800元，押二付一5500元。\n"
            "2. 长浜龙吟轩11-1603，押一付一4200元，押二付一3900元。"
        )

        self.assertEqual(main._payment_field_consistency_failures(reply, rows), [])

    def test_constraint_proof_budget_and_layout_are_hard_filters(self) -> None:
        rows = [
            {"小区": "星桥锦绣嘉苑", "房号": "20-1606A", "户型分类": "一室一厅", "押一付一": "1900"},
            {"小区": "棠润府", "房号": "10-1004C", "户型分类": "一室一厅", "押一付一": "2600"},
            {"小区": "万融城", "房号": "4-1208", "户型分类": "三室一厅", "押一付一": "5200"},
        ]

        filtered = main._filter_rows_by_constraint_proof(
            rows,
            {"budget_range": [0, 2000], "layout": "一室一厅"},
            query_text="拱墅万达一室一厅",
        )

        labels = [f"{row['小区']}{row['房号']}" for row in filtered]
        self.assertEqual(labels, ["星桥锦绣嘉苑20-1606A"])

    def test_constraint_proof_area_is_hard_filter(self) -> None:
        rows = [
            {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅", "押一付一": "1600"},
            {"区域": "东新园 杭氧 新天地", "小区": "新柠长木府", "房号": "3-1002A", "户型分类": "两室一厅", "押一付一": "4600"},
        ]

        filtered = main._filter_rows_by_constraint_proof(
            rows,
            {"area": "石桥街道\n华丰\n石桥\n永佳\n半山"},
            query_text="石桥附近5000左右两室",
        )

        self.assertEqual(filtered, [])

    def test_constraint_proof_community_is_hard_filter(self) -> None:
        rows = [
            {"区域": "拱墅万达", "小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅", "押一付一": "1600"},
            {"区域": "东新园 杭氧 新天地", "小区": "新柠长木府", "房号": "3-1002A", "户型分类": "两室一厅", "押一付一": "4600"},
        ]

        filtered = main._filter_rows_by_constraint_proof(
            rows,
            {"communities": ["石桥铭苑"]},
            query_text="石桥铭苑5000左右两室",
        )

        self.assertEqual(filtered, [])

    def test_constraint_proof_drops_negated_community(self) -> None:
        for content in (
            "我说的是石桥区域，不一定是石桥铭苑。",
            "石桥区域就行，不是只问石桥铭苑。",
        ):
            with self.subTest(content=content):
                proof = main._build_constraint_proof(
                    content=content,
                    effective_query="石桥区域 4500-5500 两室一厅 石桥铭苑",
                    understanding={
                        "intent": "inventory",
                        "query_state": {"area": "石桥街道\n华丰\n石桥\n永佳\n半山"},
                    },
                    entity_resolution={
                        "status": "resolved",
                        "areas": [{"raw_text": "石桥", "canonical": "石桥街道\n华丰\n石桥\n永佳\n半山"}],
                        "communities": [{"raw_text": "石桥铭苑", "canonical": "石桥铭苑"}],
                    },
                    signals={},
                )

                self.assertEqual(proof["area"], "石桥街道\n华丰\n石桥\n永佳\n半山")
                self.assertNotIn("communities", proof)

    def test_utilities_and_viewing_validation_keeps_both_fields(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="水电和密码一起发我",
            draft_reply="白田畈16-1-1003：水电是民用水电；看房方式/密码是336699#。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {"wants_utilities": True},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [
                    {
                        "小区": "白田畈",
                        "房号": "16-1-1003",
                        "备注": "民用水电",
                        "看房方式密码": "336699#",
                    }
                ]
            },
        )

        self.assertEqual(result["status"], "pass")

    def test_inventory_reply_explains_payment_method_budget_match(self) -> None:
        reply = main._reply_for_inventory_search(
            {
                "intent": "inventory",
                "effective_query": "北部软件园1800以内单间",
                "constraint_proof": {
                    "area": "拱墅万达\n北部软件园\n城北万象城",
                    "budget_range": [0, 1800],
                    "layout": "单间",
                },
            },
            {
                "actions": ["search_inventory", "compact_listing"],
                "inventory_rows": [
                    {
                        "小区": "星桥锦绣嘉苑",
                        "房号": "20-1606A",
                        "户型分类": "一室一厅",
                        "押一付一": "1900",
                        "押二付一": "1800",
                        "备注": "水30/月，电1元/度",
                    }
                ],
            },
        )

        self.assertIn("其中一种月租在预算内", reply)
        self.assertIn("押一付一1900", reply)
        self.assertIn("押二付一1800", reply)

    def test_inventory_reply_for_viewing_area_includes_empty_time(self) -> None:
        reply = main._reply_for_inventory_search(
            {
                "intent": "inventory",
                "effective_query": "东新园有没有马上空出来的？客户比较急。",
                "constraint_proof": {"area": "东新园\n杭氧\n新天地"},
                "structured_task": {
                    "original_text": "东新园有没有马上空出来的？客户比较急。",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            {
                "actions": ["search_inventory", "compact_listing"],
                "inventory_rows": [
                    {
                        "小区": "长浜龙吟轩",
                        "房号": "9-901",
                        "户型分类": "三室一厅",
                        "押一付一": "5800",
                        "押二付一": "5500",
                        "看房方式密码": "6.24空出 看房提前联系",
                        "备注": "民用水电",
                    },
                    {
                        "小区": "嘉樘星绣府",
                        "房号": "8-2-802B",
                        "户型分类": "两室一厅",
                        "押一付一": "3300",
                        "押二付一": "3000",
                        "看房方式密码": "6.22空出 看房提前联系",
                        "备注": "水30/月，电1元/度",
                    },
                ],
            },
        )

        self.assertIn("6.24空出", reply)
        self.assertIn("6.22空出", reply)
        self.assertIn("提前联系", reply)
        self.assertIn("18758141785", reply)

    def test_viewing_summary_does_not_leave_password_hash_when_password_hidden(self) -> None:
        summary = main._row_viewing_summary({"看房方式密码": "336699#"})

        self.assertNotIn("#", summary)
        self.assertNotIn("密码", summary)
        self.assertIn("看房方式", summary)

    def test_selfcheck_rejects_unasked_empty_time_in_price_reply(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="东方茂T3-1540是不是一室一厅？价格多少？",
            draft_reply="东方茂商业中心T3-1540是一室一厅，押一付一3800元，押二付一3500元，6.23空出，看房提前联系。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"room_refs": ["T3-1540"]},
                "structured_task": {"intent": "inventory", "tool_requirements": {}},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "inventory_rows": [
                    {
                        "小区": "东方茂商业中心",
                        "房号": "T3-1540",
                        "户型分类": "一室一厅",
                        "押一付一": "3800",
                        "押二付一": "3500",
                        "看房方式密码": "6.23空出 看房提前联系",
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("未问看房", result["reason"])

    def test_selfcheck_rejects_unasked_missing_media_wording(self) -> None:
        result = main._local_human_context_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply="有的，东新园这边有几套空出时间明确。目前暂未匹配到可直接发送视频或图片的具体房源。",
            tool_evidence={"inventory_rows": [{"小区": "长浜龙吟轩", "房号": "9-901"}]},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("素材缺失", result["reason"])

    def test_selfcheck_rejects_unasked_missing_media_wording_with_status_before_media(self) -> None:
        result = main._local_human_context_selfcheck(
            content="新天地这边有没有4000以内的两室？",
            draft_reply=(
                "有的，新天地这边4000以内两室查到几套。"
                "目前暂未查到可直接发送视频的具体房源，建议先选小区+房号。"
            ),
            tool_evidence={"inventory_rows": [{"小区": "诸葛龙吟院", "房号": "10-601A"}]},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("素材缺失", result["reason"])

    def test_viewing_area_list_rejects_password_before_specific_room_selected(self) -> None:
        result = main._constraint_consistency_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply="有的，东新园这边有几套。1. 杨乐府9-604B，看房密码为336699#。",
            understanding={
                "intent": "inventory",
                "constraint_proof": {"area": "东新园\n杭氧\n新天地"},
                "structured_task": {"intent": "inventory", "tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory"],
                "inventory_rows": [
                    {"小区": "杨乐府", "房号": "9-604B", "看房方式密码": "336699#"},
                    {"小区": "长浜龙吟轩", "房号": "9-901", "看房方式密码": "6.24空出 看房提前联系"},
                ],
            },
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能直接给看房密码", result["reason"])

    def test_human_selfcheck_requires_contact_numbers_for_advance_viewing_contact(self) -> None:
        result = main._local_human_context_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply=(
                "有的，东新园这边有几套马上空出的房源。"
                "1. 长浜龙吟轩9-901，6.24空出，看房需提前联系。"
                "你告诉我具体房号，我来帮你安排看房。"
            ),
            tool_evidence={"inventory_rows": [{"小区": "长浜龙吟轩", "房号": "9-901"}]},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("三个联系电话", result["reason"])

    def test_human_selfcheck_allows_advance_viewing_contact_with_phone_numbers(self) -> None:
        result = main._local_human_context_selfcheck(
            content="东新园有没有马上空出来的？客户比较急。",
            draft_reply=(
                "有的，长浜龙吟轩9-901这套6.24空出，看房需提前联系。"
                "客户比较急的话，先联系18758141785 / 13282125992 / 19941091943确认时间。"
            ),
            tool_evidence={"inventory_rows": [{"小区": "长浜龙吟轩", "房号": "9-901"}]},
        )

        self.assertEqual(result["status"], "pass")

    async def test_price_availability_query_is_not_overridden_by_viewing_tool_noise(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "星桥锦绣嘉苑20-1606A还在不在，价格多少",
                    "constraint_proof": {"intent": "inventory"},
                    "structured_task": {"intent": "inventory", "tool_requirements": {"needs_inventory_search": True}},
                },
                tool_evidence={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "inventory_rows": [
                        {
                            "小区": "星桥锦绣嘉苑",
                            "房号": "20-1606A",
                            "户型分类": "一室一厅",
                            "押一付一": "1900",
                            "押二付一": "1800",
                            "看房方式密码": "960615#",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                    "target_rows": [
                        {
                            "小区": "星桥锦绣嘉苑",
                            "房号": "20-1606A",
                            "户型分类": "一室一厅",
                            "押一付一": "1900",
                            "押二付一": "1800",
                            "看房方式密码": "960615#",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                    "rule_evidence": {
                        "viewing": {
                            "rooms": [
                                {
                                    "room": "星桥锦绣嘉苑20-1606A",
                                    "viewing": "960615#",
                                    "has_password": True,
                                    "needs_contact": False,
                                }
                            ]
                        },
                    },
                },
                planner_result={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "reply_text": "星桥锦绣嘉苑20-1606A还在，押一付一1900，押二付一1800，水30/月，电1元/度。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("星桥锦绣嘉苑20-1606A", result["reply"])
        self.assertIn("押一付一1900", result["reply"])
        self.assertNotIn("看房方式是", result["reply"])

    async def test_price_and_viewing_question_keeps_both_replies(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        row = {
            "小区": "合嵣悦府",
            "房号": "6-1-1204B",
            "户型分类": "一室一厅",
            "押一付一": "1500",
            "押二付一": "1300",
            "看房方式密码": "6.19空出 看房提前联系",
            "备注": "水30/月，电1元/度",
        }
        try:
            result = await main._generate_reply_result(
                content="合嵣悦府6-1-1204B是不是1500？今天能看吗？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "viewing",
                    "effective_query": "合嵣悦府6-1-1204B价格和今天看房",
                    "constraint_proof": {
                        "intent": "viewing",
                        "communities": ["合嵣悦府"],
                        "room_refs": ["6-1-1204B"],
                    },
                    "structured_task": {
                        "intent": "viewing",
                        "tool_requirements": {
                            "needs_inventory_search": True,
                            "needs_viewing_policy": True,
                        },
                    },
                },
                tool_evidence={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "inventory_rows": [row],
                    "target_rows": [row],
                    "rule_evidence": {
                        "viewing": {
                            "rooms": [
                                {
                                    "room": "合嵣悦府6-1-1204B",
                                    "viewing": "6.19空出 看房提前联系",
                                    "has_password": False,
                                    "needs_contact": True,
                                }
                            ]
                        },
                        "viewing_contact": ["18758141785", "13282125992", "19941091943"],
                    },
                },
                planner_result={
                    "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                    "reply_text": "合嵣悦府6-1-1204B还在，押一付一1500，押二付一1300；6.19空出，看房需要提前联系。密码不对、打不开门或者还没空出，就联系 18758141785 / 13282125992 / 19941091943 确认。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("还在", result["reply"])
        self.assertIn("押一付一1500", result["reply"])
        self.assertIn("提前联系", result["reply"])
        self.assertIn("18758141785", result["reply"])

    def test_viewing_reply_without_password_request_redacts_password_code(self) -> None:
        row = {
            "小区": "棠润府",
            "房号": "15-2-801B",
            "户型分类": "一室一厅",
            "押一付一": "1600",
            "押二付一": "1400",
            "备注": "水30/月 电1元/度",
            "看房方式密码": "101004# 6.19空出",
        }

        reply = main._reply_for_inventory_search(
            {
                "intent": "viewing",
                "effective_query": "棠润府15-2-801B今天能不能自己看",
                "constraint_proof": {"room_refs": ["15-2-801B"]},
                "structured_task": {
                    "original_text": "这套今天能不能自己看？",
                    "tool_requirements": {"needs_viewing_policy": True},
                },
            },
            {
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "inventory_rows": [row],
            },
        )

        self.assertIn("6.19空出", reply)
        self.assertNotIn("101004#", reply)

    def test_legacy_viewing_direct_reply_is_removed(self) -> None:
        self.assertFalse(hasattr(main, "_reply_for_viewing"))

    def test_viewing_validation_allows_password_only_when_explicitly_requested(self) -> None:
        row = {"小区": "石桥铭苑", "房号": "6-1102", "看房方式密码": "101004# 看房提前联系"}
        result = main._constraint_consistency_selfcheck(
            content="石桥铭苑6-1102密码发我",
            draft_reply="石桥铭苑6-1102：看房方式/密码是101004# 看房提前联系。密码不对就联系 18758141785 / 13282125992 / 19941091943。",
            understanding={
                "intent": "viewing",
                "constraint_proof": {"room_refs": ["6-1102"]},
                "structured_task": {"tool_requirements": {"needs_viewing_policy": True}},
            },
            tool_evidence={
                "actions": ["search_inventory", "explain_unavailable_viewing", "generate_reply"],
                "target_rows": [row],
                "rule_evidence": {
                    "viewing": {
                        "rooms": [
                            {
                                "room": "石桥铭苑6-1102",
                                "viewing": "101004# 看房提前联系",
                                "has_password": True,
                                "needs_contact": True,
                            }
                        ]
                    }
                },
            },
        )

        self.assertEqual(result["status"], "pass")

    async def test_reply_generation_uses_no_match_evidence_when_llm_waits(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="石桥附近5000左右有两室吗？最好整租。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "石桥5000左右两室整租",
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "budget_range": [4500, 5500],
                        "layout": "两室",
                    },
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": [],
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "我这边暂时没查到石桥附近5000左右两室整租完全匹配的在租房源。你可以放宽一点预算、户型或区域，我再帮你筛一轮。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("暂时没查到", result["reply"])
        self.assertIn("石桥", result["reply"])
        self.assertNotIn("稍后给您准确回复", result["reply"])

    async def test_single_inventory_result_does_not_ask_for_sequence_number(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="星桥锦绣嘉苑20-1606A还在不在？价格多少？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "星桥锦绣嘉苑20-1606A房态和价格",
                    "constraint_proof": {
                        "intent": "inventory",
                        "communities": ["星桥锦绣嘉苑"],
                        "room_refs": ["20-1606A"],
                        "budget_range": [0, 1800],
                        "layout": "一室",
                    },
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "generate_reply"],
                    "inventory_rows": [
                        {
                            "小区": "星桥锦绣嘉苑",
                            "房号": "20-1606A",
                            "户型分类": "一室一厅",
                            "押一付一": "1900",
                            "押二付一": "1800",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                    "target_rows": [
                        {
                            "小区": "星桥锦绣嘉苑",
                            "房号": "20-1606A",
                            "户型分类": "一室一厅",
                            "押一付一": "1900",
                            "押二付一": "1800",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                },
                planner_result={
                    "actions": ["search_inventory", "generate_reply"],
                    "reply_text": "还在，星桥锦绣嘉苑20-1606A，一室一厅，押一付一1900，押二付一1800，水30/月，电1元/度。要视频、图片或者看房方式的话，直接说这套就行。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("星桥锦绣嘉苑20-1606A", result["reply"])
        self.assertIn("还在", result["reply"])
        self.assertNotIn("1800以内", result["reply"])
        self.assertIn("直接说这套", result["reply"])
        self.assertNotIn("回序号", result["reply"])

    async def test_inventory_evidence_is_safe_fallback_after_selfcheck_retry(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                if "棠润府15-2-801B" in str(kwargs.get("draft_reply") or ""):
                    return {"status": "pass"}
                return {
                    "status": "fail",
                    "reason": "LLM selfcheck false negative",
                    "fallback_reply": "我先帮您确认一下最新房态，稍后给您准确回复。",
                }

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "北部软件园1800以内单间",
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "拱墅万达\n北部软件园\n城北万象城",
                        "budget_range": [0, 1800],
                        "layout": "未明确",
                    },
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": [
                        {
                            "小区": "棠润府",
                            "房号": "15-2-801B",
                            "户型分类": "一室一厅",
                            "押一付一": "1600",
                            "押二付一": "1400",
                            "备注": "水30/月，电1元/度",
                        }
                    ],
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "万达2000以下一室我按最新房源表查到了。",
                },
                retry_reason="selfcheck_retry",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("有的", result["reply"])
        self.assertIn("棠润府15-2-801B", result["reply"])
        self.assertNotIn("稍后给您准确回复", result["reply"])
        self.assertNotIn("未明确", result["reply"])

    async def test_reply_generation_uses_prepared_video_evidence_when_llm_waits(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
            main.reply_generator = FakeReplyGenerator()
            main.agentic_rag = FakeRag()
            try:
                result = await main._generate_reply_result(
                    content="先把万达2000以下一室里最合适的一套视频发我。",
                    context=kf_context_memory.empty_context(),
                    understanding={
                        "intent": "media",
                        "effective_query": "万达2000以下一室视频",
                        "constraint_proof": {
                            "intent": "media",
                            "area": "拱墅万达\n北部软件园\n城北万象城",
                            "budget_range": [0, 2000],
                            "layout": "一室",
                            "wants_video": True,
                        },
                        "structured_task": {"intent": "media"},
                    },
                    tool_evidence={
                        "actions": ["search_inventory", "send_video", "generate_reply"],
                        "media_request": {"wants_video": True, "requested_count": 1},
                        "inventory_rows": [
                            {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
                        ],
                        "target_rows": [
                            {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
                        ],
                        "video_rows": [
                            {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
                        ],
                        "video_paths": [video.name],
                    },
                    planner_result={
                        "actions": ["search_inventory", "send_video", "generate_reply"],
                        "reply_text": "找到了，这是棠润府15-2-801B的视频。",
                    },
                )
            finally:
                main.reply_generator = originals["reply_generator"]
                main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("找到了", result["reply"])
        self.assertIn("棠润府15-2-801B", result["reply"])
        self.assertNotIn("稍后给您准确回复", result["reply"])

    async def test_missing_video_reply_survives_selfcheck_retry(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                if "暂时没找到视频" in str(kwargs.get("draft_reply") or ""):
                    return {"status": "pass"}
                return {
                    "status": "fail",
                    "reason": "缺素材说明需要重写",
                    "fallback_reply": "我先帮您确认一下最新房态，稍后给您准确回复。",
                }

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="继续发剩下的视频。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "继续发剩下的视频",
                    "constraint_proof": {"wants_video": True},
                    "structured_task": {"intent": "media", "tool_requirements": {"needs_video": True}},
                },
                tool_evidence={
                    "actions": ["search_inventory", "send_video", "explain_missing_media", "generate_reply"],
                    "media_request": {"wants_video": True, "requested_count": 1},
                    "inventory_rows": [{"小区": "小洋坝家园三区", "房号": "12-1003B"}],
                    "target_rows": [{"小区": "小洋坝家园三区", "房号": "12-1003B"}],
                    "missing_media": ["小洋坝家园三区12-1003B:视频"],
                    "media_status": {"video": {"requested_count": 1, "sent_count": 0, "missing_rooms": ["小洋坝家园三区12-1003B"]}},
                },
                planner_result={
                    "actions": ["search_inventory", "send_video", "explain_missing_media", "generate_reply"],
                    "reply_text": "我先查这套视频，有素材就发你；没有会直接说明暂无视频。",
                },
                retry_reason="selfcheck_retry",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("暂时没找到视频", result["reply"])
        self.assertIn("小洋坝家园三区12-1003B", result["reply"])
        self.assertNotIn("找到就发你", result["reply"])
        self.assertNotIn("有的，", result["reply"])

    async def test_planner_missing_reply_after_retry_cannot_send_media(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            row = {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 1},
                "inventory_rows": [row],
                "target_rows": [row],
                "video_rows": [row],
                "video_paths": [video.name],
            }
            originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
            main.reply_generator = FakeReplyGenerator()
            main.agentic_rag = FakeRag()
            try:
                result = await main._generate_reply_result(
                    content="棠润府15-2-801B视频发我。",
                    context=kf_context_memory.empty_context(),
                    understanding={
                        "intent": "media",
                        "effective_query": "棠润府15-2-801B视频",
                        "constraint_proof": {"intent": "media", "wants_video": True},
                        "structured_task": {"intent": "media"},
                    },
                    tool_evidence=tool_evidence,
                    planner_result={
                        "actions": ["search_inventory", "send_video", "generate_reply"],
                        "reply_text": "",
                    },
                    retry_reason="selfcheck_retry",
                )
            finally:
                main.reply_generator = originals["reply_generator"]
                main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertTrue(tool_evidence.get("suppress_actions"))
        self.assertEqual(tool_evidence["outbound_package"]["video_paths"], [])
        self.assertEqual(
            result["selfcheck"]["fallback"]["llm"]["source"],
            "planner_missing_reply_safe_fallback",
        )

    async def test_planner_missing_reply_before_retry_does_not_enter_selfcheck(self) -> None:
        class FakeReplyGenerator:
            pass

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                raise AssertionError("Planner 空 reply_text 不能进入回复检索或最终自检")

            def assess_reply(self, **kwargs):
                raise AssertionError("Planner 空 reply_text 不能进入最终自检")

        row = {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
        originals = {"agentic_rag": main.agentic_rag, "reply_generator": main.reply_generator}
        main.agentic_rag = FakeRag()
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._generate_reply_result(
                content="棠润府15-2-801B视频发我。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "棠润府15-2-801B视频",
                    "constraint_proof": {"intent": "media", "wants_video": True},
                    "structured_task": {"intent": "media"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "media_request": {"wants_video": True, "requested_count": 1},
                    "inventory_rows": [row],
                    "target_rows": [row],
                },
                planner_result={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "reply_text": "",
                },
                retry_reason="",
            )
        finally:
            main.agentic_rag = originals["agentic_rag"]
            main.reply_generator = originals["reply_generator"]

        self.assertTrue(result["needs_planner_retry"])
        self.assertEqual(result["reply"], "")
        self.assertEqual(result["selfcheck"]["rule"]["source"], "planner_output_gate")
        self.assertIn("不能进入最终自检", result["selfcheck"]["rule"]["reason"])

    async def test_inventory_rows_after_planner_retry_use_tool_grounded_reply(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        rows = [
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "4-1502", "户型分类": "一室一厅", "押一付一": "4500", "押二付一": "4200", "备注": "民用水电"},
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "10-1-1205", "户型分类": "两室一厅", "押一付一": "3900", "押二付一": "3700", "备注": "民用水电"},
        ]
        originals = {"agentic_rag": main.agentic_rag, "reply_generator": main.reply_generator}
        main.agentic_rag = FakeRag()
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._generate_reply_result(
                content="兴业杨家府有什么房",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "兴业杨家府有哪些在租房源",
                    "constraint_proof": {"intent": "inventory", "communities": ["兴业杨家府"]},
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": rows,
                    "target_rows": rows,
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "",
                },
                retry_reason="planner_retry_after_empty_reply",
            )
        finally:
            main.agentic_rag = originals["agentic_rag"]
            main.reply_generator = originals["reply_generator"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("兴业杨家府4-1502", result["reply"])
        self.assertIn("兴业杨家府10-1-1205", result["reply"])
        self.assertNotIn("先不乱发", result["reply"])
        self.assertEqual(result["planner_reply_result"]["source"], "tool_grounded_inventory_reply_after_planner_retry")

    async def test_inventory_rows_replace_invalid_planner_clarification_reply(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        rows = [
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "4-1502", "户型分类": "一室一厅", "押一付一": "4500", "押二付一": "4200", "备注": "民用水电"},
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "10-1-1205", "户型分类": "两室一厅", "押一付一": "3900", "押二付一": "3700", "备注": "民用水电"},
        ]
        originals = {"agentic_rag": main.agentic_rag, "reply_generator": main.reply_generator}
        main.agentic_rag = FakeRag()
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._generate_reply_result(
                content="兴业杨家府有什么房",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "兴业杨家府有哪些在租房源",
                    "constraint_proof": {"intent": "inventory", "communities": ["兴业杨家府"]},
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": rows,
                    "target_rows": rows,
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "我这边为了避免发错，先不乱发。你把小区+房号或更具体条件发我一下，我重新按最新房源表查准。",
                },
                retry_reason="",
            )
        finally:
            main.agentic_rag = originals["agentic_rag"]
            main.reply_generator = originals["reply_generator"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("兴业杨家府4-1502", result["reply"])
        self.assertIn("兴业杨家府10-1-1205", result["reply"])
        self.assertNotIn("先不乱发", result["reply"])
        self.assertEqual(result["planner_reply_result"]["source"], "tool_grounded_inventory_reply_replaced_invalid_planner_reply")

    async def test_inventory_rows_final_retry_never_falls_back_to_repeat_room_request(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        rows = [
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "4-1502", "户型分类": "一室一厅", "押一付一": "4500", "押二付一": "4200", "备注": "民用水电"},
            {"区域": "石桥街道 华丰 石桥 永佳 半山", "小区": "兴业杨家府", "房号": "10-1-1205", "户型分类": "两室一厅", "押一付一": "3900", "押二付一": "3700", "备注": "民用水电"},
        ]
        originals = {
            "agentic_rag": main.agentic_rag,
            "reply_generator": main.reply_generator,
            "_outbound_package_selfcheck": main._outbound_package_selfcheck,
        }
        main.agentic_rag = FakeRag()
        main.reply_generator = FakeReplyGenerator()
        main._outbound_package_selfcheck = lambda **kwargs: {
            "status": "retry",
            "action": "retry",
            "reason": "forced_package_retry",
            "source": "test_forced_package_retry",
        }
        try:
            result = await main._generate_reply_result(
                content="兴业杨家府有什么房",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "兴业杨家府有哪些在租房源",
                    "constraint_proof": {"intent": "inventory", "communities": ["兴业杨家府"]},
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "inventory_rows": rows,
                    "target_rows": rows,
                },
                planner_result={
                    "actions": ["search_inventory", "compact_listing", "generate_reply"],
                    "reply_text": "我这边为了避免发错，先不乱发。你把小区+房号或更具体条件发我一下，我重新按最新房源表查准。",
                },
                retry_reason="forced_retry_after_first_selfcheck",
            )
        finally:
            main.agentic_rag = originals["agentic_rag"]
            main.reply_generator = originals["reply_generator"]
            main._outbound_package_selfcheck = originals["_outbound_package_selfcheck"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("兴业杨家府4-1502", result["reply"])
        self.assertIn("兴业杨家府10-1-1205", result["reply"])
        self.assertNotIn("先不乱发", result["reply"])
        self.assertNotIn("小区+房号或更具体条件", result["reply"])
        self.assertEqual(
            result["selfcheck"]["fallback"]["rule"]["source"],
            "tool_grounded_inventory_final_fallback",
        )

    async def test_selfcheck_retry_fallback_preserves_valid_video_actions(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="我先帮您确认一下最新房态，稍后给您准确回复。")

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            video_row = {"小区": "棠润府", "房号": "15-2-801B", "户型分类": "一室一厅"}
            missing_row = {"小区": "小洋坝家园三区", "房号": "12-1003B", "户型分类": "一室一厅"}
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "explain_missing_media", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 2},
                "inventory_rows": [video_row, missing_row],
                "target_rows": [video_row, missing_row],
                "video_rows": [video_row],
                "video_paths": [video.name],
                "missing_media": ["小洋坝家园三区12-1003B:视频"],
                "media_status": {
                    "video": {
                        "requested_count": 2,
                        "sent_count": 1,
                        "missing_rooms": ["小洋坝家园三区12-1003B"],
                    }
                },
            }
            originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
            main.reply_generator = FakeReplyGenerator()
            main.agentic_rag = FakeRag()
            try:
                result = await main._generate_reply_result(
                    content="万达附近1500左右先发几套视频我筛一下。",
                    context=kf_context_memory.empty_context(),
                    understanding={
                        "intent": "media",
                        "effective_query": "万达1500左右视频",
                        "constraint_proof": {
                            "intent": "media",
                            "area": "拱墅万达\n北部软件园\n城北万象城",
                            "budget_range": [1000, 2000],
                            "wants_video": True,
                        },
                        "structured_task": {"intent": "media"},
                    },
                    tool_evidence=tool_evidence,
                    planner_result={
                        "actions": ["search_inventory", "send_video", "explain_missing_media", "generate_reply"],
                        "reply_text": "我先查一下，稍等。",
                    },
                    retry_reason="selfcheck_retry",
                )
            finally:
                main.reply_generator = originals["reply_generator"]
                main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertFalse(bool(tool_evidence.get("suppress_actions")))
        self.assertEqual(tool_evidence["outbound_package"]["video_paths"], [video.name])
        self.assertIn("这是棠润府15-2-801B的视频。", tool_evidence["outbound_package"]["video_explanations"])
        self.assertIn("小洋坝家园三区12-1003B", result["reply"])

    async def test_robotic_tone_fallback_keeps_verified_batch_video_actions(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(
                    action="fallback",
                    status="fallback",
                    reason="robotic_template_reply",
                    fallback_text=kwargs.get("reply_text") or "",
                    fallback_reply=kwargs.get("reply_text") or "",
                )

        rows = [
            {"小区": "杨家新雅苑", "房号": "15-603", "户型分类": "三室一厅"},
            {"小区": "杨家新雅苑", "房号": "49-1102", "户型分类": "一室一厅"},
            {"小区": "兴业杨家府", "房号": "3-601", "户型分类": "一室一厅"},
            {"小区": "石桥铭苑", "房号": "6-1102", "户型分类": "一室一厅"},
        ]
        videos = [tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) for _ in rows]
        for video in videos:
            video.write(b"video")
            video.close()
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 4},
                "inventory_rows": rows,
                "target_rows": rows,
                "video_rows": rows,
                "video_paths": [video.name for video in videos],
                "missing_media": [],
                "media_status": {"video": {"requested_count": 4, "sent_count": 4, "missing_rooms": []}},
            }
            result = await main._generate_reply_result(
                content="石桥和华丰附近5000左右整租视频都发我几套。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "inventory",
                    "effective_query": "石桥和华丰附近5000左右整租视频",
                    "constraint_proof": {
                        "intent": "inventory",
                        "area": "石桥街道\n华丰\n石桥\n永佳\n半山",
                        "budget_range": [4500, 5500],
                        "wants_video": True,
                    },
                    "structured_task": {"intent": "inventory"},
                },
                tool_evidence=tool_evidence,
                planner_result={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "reply_text": "如需看房，请提前联系。视频已准备好，具体如下。",
                },
                retry_reason="robotic_template_reply",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]
            for video in videos:
                Path(video.name).unlink(missing_ok=True)

        self.assertFalse(result["needs_planner_retry"])
        self.assertNotIn("先不乱发", result["reply"], result)
        self.assertIn("按你说的条件先发这4套视频", result["reply"])
        self.assertIn("杨家新雅苑15-603", result["reply"])
        self.assertIn("石桥铭苑6-1102", result["reply"])
        self.assertFalse(bool(tool_evidence.get("suppress_actions")))
        self.assertEqual(len(tool_evidence["outbound_package"]["video_paths"]), 4)
        self.assertEqual(result["selfcheck"]["fallback"]["rule"]["status"], "pass")
        self.assertEqual(result["selfcheck"]["fallback"]["human"]["status"], "pass")

    async def test_original_video_request_explains_sendable_video_is_not_original_source(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        row = {"小区": "嘉樘星绣府", "房号": "9-603", "户型分类": "两室一厅"}
        video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        video.write(b"video")
        video.close()
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 1},
                "inventory_rows": [row],
                "target_rows": [row],
                "video_rows": [row],
                "video_paths": [video.name],
                "missing_media": [],
                "original_video_request": {
                    "requested": True,
                    "has_original_source": False,
                    "has_sendable_video": True,
                    "sendable_video_count": 1,
                    "reason": "当前素材库只提供企业微信可发送视频，没有单独的原视频/高清下载链接证据。",
                },
            }
            result = await main._generate_reply_result(
                content="这套有原视频或者清楚一点的视频吗？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "嘉樘星绣府9-603原视频高清视频",
                    "constraint_proof": {
                        "intent": "media",
                        "wants_video": True,
                        "wants_original_video": True,
                        "room_refs": ["9-603"],
                    },
                    "structured_task": {"intent": "media"},
                },
                tool_evidence=tool_evidence,
                planner_result={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "reply_text": "有的，这套视频我先发你。",
                },
                retry_reason="",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]
            Path(video.name).unlink(missing_ok=True)

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("企业微信可发送视频", result["reply"])
        self.assertIn("可能会压缩", result["reply"])
        self.assertIn("原视频/高清下载链接", result["reply"])
        self.assertIn("original_video_request", tool_evidence["outbound_package"])
        self.assertEqual(tool_evidence["outbound_package"]["video_paths"], [video.name])

    async def test_original_video_request_includes_verified_source_link(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        row = {"小区": "嘉樘星绣府", "房号": "9-603", "户型分类": "两室一厅"}
        video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        video.write(b"video")
        video.close()
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 1},
                "inventory_rows": [row],
                "target_rows": [row],
                "video_rows": [row],
                "video_paths": [video.name],
                "missing_media": [],
                "original_video_urls": ["https://ccn9urs7d60k.feishu.cn/file/source-video"],
                "material_page_urls": ["https://ccn9urs7d60k.feishu.cn/docx/source-doc"],
                "original_video_request": {
                    "requested": True,
                    "has_original_source": True,
                    "has_sendable_video": True,
                    "sendable_video_count": 1,
                },
            }
            result = await main._generate_reply_result(
                content="这个视频太糊了，有没有原视频链接？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "嘉樘星绣府9-603原视频链接",
                    "constraint_proof": {
                        "intent": "media",
                        "wants_video": True,
                        "wants_original_video": True,
                        "room_refs": ["9-603"],
                    },
                    "structured_task": {"intent": "media"},
                },
                tool_evidence=tool_evidence,
                planner_result={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "reply_text": "这套视频我先发你，原视频链接也给你。",
                },
                retry_reason="",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]
            Path(video.name).unlink(missing_ok=True)

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("https://ccn9urs7d60k.feishu.cn/file/source-video", result["reply"])
        self.assertIn("https://ccn9urs7d60k.feishu.cn/docx/source-doc", result["reply"])
        self.assertEqual(tool_evidence["outbound_package"]["original_video_urls"], ["https://ccn9urs7d60k.feishu.cn/file/source-video"])

    def test_missing_original_video_reply_mentions_no_high_resolution_source(self) -> None:
        row = {"小区": "长岳王马府", "房号": "4-2002", "户型分类": "两室一厅"}
        reply = main._reply_for_missing_media(
            {
                "constraint_proof": {
                    "wants_video": True,
                    "wants_original_video": True,
                    "selected_indices": [1, 2],
                }
            },
            {
                "target_rows": [row],
                "missing_media": ["长岳王马府4-2002:视频"],
                "video_paths": [],
                "image_paths": [],
            },
        )

        self.assertIn("长岳王马府4-2002", reply)
        self.assertIn("原视频/高清下载链接", reply)
        self.assertIn("没有可发送的视频", reply)

    async def test_pending_missing_video_reply_overrides_planner_send_claim(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="剩下的继续发。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "继续发送上一轮未完成的视频素材。",
                    "constraint_proof": {
                        "intent": "media",
                        "wants_video": True,
                        "pending_video_action": "continue",
                    },
                    "structured_task": {
                        "intent": "media",
                        "tool_requirements": {"needs_video": True},
                    },
                },
                tool_evidence={
                    "actions": ["send_video", "explain_missing_media", "generate_reply"],
                    "missing_media": ["兴业杨家府4-1502:视频", "兴业杨家府8-1203:视频"],
                    "video_paths": [],
                    "image_paths": [],
                    "media_status": {
                        "video": {
                            "requested_count": 5,
                            "sent_count": 0,
                            "missing_rooms": ["兴业杨家府4-1502", "兴业杨家府8-1203"],
                            "sync_status": {"source": "pending_video_sends"},
                        }
                    },
                },
                planner_result={
                    "actions": ["send_video", "explain_missing_media", "generate_reply"],
                    "reply_text": "好的，剩下的视频继续发你。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("兴业杨家府4-1502", result["reply"])
        self.assertIn("兴业杨家府8-1203", result["reply"])
        self.assertIn("暂时没找到视频", result["reply"])
        self.assertNotIn("小区+房号", result["reply"])

    async def test_original_video_fallback_keeps_video_action_and_explains_no_source_link(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(
                    action="fallback",
                    status="fallback",
                    reason="robotic_template_reply",
                    fallback_text=kwargs.get("reply_text") or "",
                    fallback_reply=kwargs.get("reply_text") or "",
                )

        row = {"小区": "新柠长木府", "房号": "3-1002A", "户型分类": "两室一厅"}
        video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        video.write(b"video")
        video.close()
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            tool_evidence = {
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 1},
                "inventory_rows": [row],
                "target_rows": [row],
                "video_rows": [row],
                "video_paths": [video.name],
                "missing_media": [],
                "original_video_request": {
                    "requested": True,
                    "has_original_source": False,
                    "has_sendable_video": True,
                    "sendable_video_count": 1,
                },
            }
            result = await main._generate_reply_result(
                content="有原视频或者高清一点的吗？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "新柠长木府3-1002A原视频高清视频",
                    "constraint_proof": {
                        "intent": "media",
                        "wants_video": True,
                        "wants_original_video": True,
                    },
                    "structured_task": {"intent": "media"},
                },
                tool_evidence=tool_evidence,
                planner_result={
                    "actions": ["search_inventory", "send_video", "generate_reply"],
                    "reply_text": "视频已准备好。",
                },
                retry_reason="robotic_template_reply",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]
            Path(video.name).unlink(missing_ok=True)

        self.assertFalse(result["needs_planner_retry"])
        self.assertFalse(bool(tool_evidence.get("suppress_actions")))
        self.assertIn("新柠长木府3-1002A", result["reply"])
        self.assertIn("可能会压缩", result["reply"])
        self.assertIn("原视频/高清下载链接", result["reply"])
        self.assertEqual(tool_evidence["outbound_package"]["video_paths"], [video.name])

    async def test_action_explanation_retry_keeps_verified_batch_media_actions(self) -> None:
        class FakeReplyGenerator:
            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="")

        rows = [
            {"小区": "昌运里三区", "房号": "3-1403", "户型分类": "两室一厅"},
            {"小区": "棠润府", "房号": "15-2-1901B", "户型分类": "两室一厅"},
        ]
        videos = [tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) for _ in rows]
        images = [tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) for _ in rows]
        for file in [*videos, *images]:
            file.write(b"media")
            file.close()
        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            tool_evidence = {
                "actions": ["search_inventory", "send_image", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "wants_image": True, "requested_count": 2},
                "inventory_rows": rows,
                "target_rows": rows,
                "video_rows": rows,
                "image_rows": rows,
                "video_paths": [video.name for video in videos],
                "image_paths": [image.name for image in images],
                "missing_media": [],
            }
            result = await main._generate_reply_result(
                content="这两套图片和视频都发我。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "media",
                    "effective_query": "这两套图片和视频",
                    "constraint_proof": {"wants_video": True, "wants_image": True, "selected_indices": [1, 2]},
                    "structured_task": {"intent": "media"},
                },
                tool_evidence=tool_evidence,
                planner_result={
                    "actions": ["search_inventory", "send_image", "send_video", "generate_reply"],
                    "reply_text": "有的，这是昌运里三区3-1403和棠润府15-2-1901B的图片和视频。",
                },
                retry_reason="多套视频动作回复必须逐套说明",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]
            for file in [*videos, *images]:
                Path(file.name).unlink(missing_ok=True)

        self.assertFalse(result["needs_planner_retry"])
        self.assertNotIn("先不乱发", result["reply"], result)
        self.assertIn("这是昌运里三区3-1403的视频", result["reply"])
        self.assertIn("这是棠润府15-2-1901B的图片", result["reply"])
        self.assertFalse(bool(tool_evidence.get("suppress_actions")))

    async def test_contract_reply_does_not_invent_room_details(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(
                    text=(
                        "客户看中了直接联系 18758141785 / 13282125992 / 19941091943 定房。"
                        "比如星桥锦绣嘉苑20-1606A这套，密码是960615#，可以马上安排。"
                    )
                )

            async def assess_kf_final_reply(self, **kwargs):
                return {"status": "pass"}

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="客户看中了怎么定房？定金和合同怎么弄？",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "contract",
                    "structured_task": {
                        "intent": "contract",
                        "tool_requirements": {"needs_contract_contact": True},
                    },
                },
                tool_evidence={"actions": ["send_contract_contact", "generate_reply"]},
                planner_result={
                    "actions": ["send_contract_contact", "generate_reply"],
                    "reply_text": "客户看中了可以联系 18758141785 / 13282125992 / 19941091943 定房和签电子合同。联系时带上小区+房号，方便确认房态和定金。",
                },
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("18758141785", result["reply"])
        self.assertIn("签电子合同", result["reply"])
        self.assertIn("小区+房号", result["reply"])
        self.assertNotIn("比如星桥锦绣嘉苑", result["reply"])
        self.assertNotIn("密码是", result["reply"])

    async def test_deposit_utilities_reply_survives_selfcheck_retry(self) -> None:
        class FakeReplyGenerator:
            async def generate(self, *args, **kwargs):
                return ReplyPlan(text="免押走支付宝无忧住芝麻信用评估，服务费按租期5.5%-8%。")

            async def assess_kf_final_reply(self, **kwargs):
                if "水电要按具体房源备注查" in str(kwargs.get("draft_reply") or ""):
                    return {"status": "pass"}
                return {
                    "status": "fail",
                    "reason": "遗漏水电追问",
                    "fallback_reply": "免押走支付宝无忧住芝麻信用评估，服务费按租期5.5%-8%。",
                }

        class FakeRag:
            async def retrieve_for_reply(self, **kwargs):
                return SimpleNamespace(context_text="", dynamic_evidence=[])

            def assess_reply(self, **kwargs):
                return SimpleNamespace(action="pass", status="pass", reason="", fallback_text="")

        originals = {"reply_generator": main.reply_generator, "agentic_rag": main.agentic_rag}
        main.reply_generator = FakeReplyGenerator()
        main.agentic_rag = FakeRag()
        try:
            result = await main._generate_reply_result(
                content="免押金要什么条件？服务费怎么算？顺便说下这几套水电怎么收。",
                context=kf_context_memory.empty_context(),
                understanding={
                    "intent": "deposit",
                    "structured_task": {
                        "intent": "deposit",
                        "tool_requirements": {"needs_deposit_policy": True, "needs_utilities": True},
                    },
                },
                tool_evidence={"actions": ["send_deposit_policy", "generate_reply"]},
                planner_result={
                    "actions": ["send_deposit_policy", "generate_reply"],
                    "reply_text": "免押是支付宝无忧住信用免押服务，需要符合芝麻信用风控并支付免押服务费。",
                },
                retry_reason="selfcheck_retry",
            )
        finally:
            main.reply_generator = originals["reply_generator"]
            main.agentic_rag = originals["agentic_rag"]

        self.assertFalse(result["needs_planner_retry"])
        self.assertIn("免押走支付宝无忧住", result["reply"])
        self.assertIn("水电要按具体房源备注查", result["reply"])
        self.assertIn("小区+房号", result["reply"])

    async def test_contract_question_is_forced_to_contract_intent(self) -> None:
        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "unclear",
                    "rewritten_query": "这两套具体是哪两套？",
                    "query_state": {"intent": "unclear"},
                    "needs_clarification": True,
                    "clarification_text": "请问是哪两套？",
                }

        original = main.reply_generator
        main.reply_generator = FakeReplyGenerator()
        try:
            result = await main._understand_message(
                content="这两套客户看中了怎么定房？",
                context=kf_context_memory.empty_context(),
                signals=main._deterministic_signals("这两套客户看中了怎么定房？"),
            )
        finally:
            main.reply_generator = original

        self.assertEqual(result["intent"], "contract")
        self.assertFalse(result["needs_clarification"])
        self.assertTrue(result["query_state"]["wants_contract_contact"])

    def test_outbound_package_selfcheck_rejects_payment_field_semantic_error(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="这套押一付一押金是3500，押二付一押金是3200。",
            tool_evidence={
                "actions": ["search_inventory", "generate_reply"],
                "inventory_rows": [
                    {
                        "小区": "新柠长木府",
                        "房号": "3-1002B",
                        "押一付一": "3500",
                        "押二付一": "3200",
                    }
                ],
            },
            outbound_package={"text": "这套押一付一押金是3500，押二付一押金是3200。"},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("押一付一/押二付一", result["reason"])

    def test_outbound_package_selfcheck_rejects_robot_phone_call_claim(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="水电费我这边直接电话跟您核对一下。",
            tool_evidence={
                "actions": ["send_deposit_policy", "generate_reply"],
                "rule_evidence": {"deposit_policy": main._deposit_policy_evidence()},
            },
            outbound_package={"text": "水电费我这边直接电话跟您核对一下。"},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("不能声称自己会打电话", result["reason"])

    def test_outbound_package_selfcheck_rejects_unexplained_missing_video(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="我先帮您确认一下最新房态，稍后给您准确回复。",
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 2},
                "target_rows": [{"小区": "杨乐府", "房号": "9-604B"}],
            },
            outbound_package={"text": "我先帮您确认一下最新房态，稍后给您准确回复。"},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("视频请求", result["reason"])

    def test_outbound_package_selfcheck_rejects_video_claim_without_action(self) -> None:
        result = main._outbound_package_selfcheck(
            draft_reply="诸葛龙吟院10-601A的视频已找到，稍后发给您。",
            tool_evidence={
                "actions": ["search_inventory", "send_video", "generate_reply"],
                "media_request": {"wants_video": True, "requested_count": 1},
            },
            outbound_package={"text": "诸葛龙吟院10-601A的视频已找到，稍后发给您。"},
        )

        self.assertEqual(result["status"], "retry")
        self.assertIn("待发送包没有视频动作", result["reason"])

    def test_outbound_package_selfcheck_allows_sheet_sent_with_unbound_video_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inventory_image = Path(directory) / "inventory.png"
            inventory_image.write_bytes(b"png")
            result = main._outbound_package_selfcheck(
                draft_reply=(
                    "房源表发你了，你可以先整体看一下；按当前区域和预算暂时没匹配到"
                    "可直接发视频的具体房源，建议你从表里选小区+房号后，我再帮你查视频或看房方式。"
                ),
                tool_evidence={
                    "actions": ["send_inventory_sheet", "send_video", "generate_reply"],
                    "media_request": {"wants_video": True, "requested_count": 1},
                },
                outbound_package={
                    "text": (
                        "房源表发你了，你可以先整体看一下；按当前区域和预算暂时没匹配到"
                        "可直接发视频的具体房源，建议你从表里选小区+房号后，我再帮你查视频或看房方式。"
                    ),
                    "inventory_images": [str(inventory_image)],
                    "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。",
                },
            )

        self.assertEqual(result["status"], "pass")

    def test_safe_deposit_fallback_contains_fee_tiers(self) -> None:
        fallback = main._safe_fallback_for_intent(
            {"intent": "deposit"},
            "免押不是免费，是支付宝芝麻信用无忧住服务。",
        )

        self.assertIn("芝麻", fallback)
        self.assertIn("5.5", fallback)
        self.assertIn("7%", fallback)
        self.assertIn("8%", fallback)
        self.assertIn("服务费", fallback)

    def test_safe_contract_fallback_contains_contact_numbers(self) -> None:
        fallback = main._safe_fallback_for_intent(
            {"intent": "contract"},
            "我先帮您确认一下最新房态，稍后给您准确回复。",
        )

        self.assertIn("18758141785", fallback)
        self.assertIn("13282125992", fallback)
        self.assertIn("19941091943", fallback)
        self.assertIn("电子合同", fallback)

    def test_safe_inventory_sheet_fallback_does_not_claim_send_without_image(self) -> None:
        fallback = main._safe_fallback_for_intent(
            {"intent": "inventory_sheet"},
            "我先帮您确认一下最新房态，稍后给您准确回复。",
        )

        self.assertIn("房源表图片", fallback)
        self.assertIn("暂时没生成成功", fallback)
        self.assertNotIn("发你", fallback)

    def test_clarification_raw_mention_strips_query_tail(self) -> None:
        self.assertEqual(
            main._clean_clarification_raw_mention("\u6768\u5bb6\u5e9c\u8fd8\u5b50"),
            "\u6768\u5bb6\u5e9c",
        )
        self.assertEqual(
            main._clean_clarification_raw_mention("\u6768\u5bb6\u5e9c\u8fd8\u6709\u623f\u5b50\u5417"),
            "\u6768\u5bb6\u5e9c",
        )

    async def test_clarification_reply_passes_final_selfcheck_before_send(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            async def rewrite_kf_message(self, **kwargs):
                return {
                    "intent": "inventory",
                    "rewritten_query": "\u6768\u5bb6\u5e9c\u6709\u623f\u5b50\u5417",
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "\u4f60\u8bf4\u7684\u201c\u6768\u5bb6\u5e9c\u201d\u6211\u8fd9\u8fb9\u6709\u51e0\u4e2a\u76f8\u8fd1\u5c0f\u533a\uff1a\u5174\u4e1a\u6768\u5bb6\u5e9c\u3001\u6768\u4e50\u5e9c\u3002\u4f60\u786e\u8ba4\u4e0b\u662f\u54ea\u4e00\u4e2a\uff0c\u6211\u518d\u6309\u6700\u65b0\u623f\u6e90\u8868\u67e5\u3002",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return [
                    {"小区": "兴业杨家府", "房号": "3-601"},
                    {"小区": "杨乐府", "房号": "1-101"},
                ]

        calls: list[dict] = []

        async def fake_generate_reply_result(**kwargs):
            calls.append(kwargs)
            return {
                "reply": "\u6211\u5148\u786e\u8ba4\u4e00\u4e0b\uff0c\u4f60\u8bf4\u7684\u662f\u5174\u4e1a\u6768\u5bb6\u5e9c\uff0c\u8fd8\u662f\u6768\u4e50\u5e9c\uff1f\u786e\u8ba4\u540e\u6211\u518d\u5e2e\u4f60\u67e5\u8fd8\u6709\u54ea\u4e9b\u5728\u79df\u3002",
                "draft_reply": str((kwargs.get("planner_result") or {}).get("reply_text") or ""),
                "context": kwargs["context"],
                "selfcheck": {"status": "pass"},
                "needs_planner_retry": False,
                "planner_retry_reason": "",
            }

        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "_generate_reply_result": main._generate_reply_result,
            "kf_turn_generations": dict(main.kf_turn_generations),
        }
        fake_wecom = FakeWeComKf()
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = FakeContextStore()
        main.reply_generator = FakeReplyGenerator()
        main.inventory = FakeInventory()
        main._generate_reply_result = fake_generate_reply_result
        main.kf_turn_generations[main._conversation_key("kf", "wm")] = 0
        try:
            await main._process_text_turn(
                open_kfid="kf",
                external_userid="wm",
                pending_items=[
                    {
                        "msgid": "msg-clarify",
                        "content": "\u6768\u5bb6\u5e9c\u8fd8\u6709\u623f\u5b50\u5417\uff1f",
                    }
                ],
                generation=0,
            )
        finally:
            for name, value in originals.items():
                if name == "kf_turn_generations":
                    main.kf_turn_generations.clear()
                    main.kf_turn_generations.update(value)
                else:
                    setattr(main, name, value)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_evidence"]["actions"], ["clarification"])
        self.assertEqual(
            fake_wecom.texts,
            ["\u6211\u5148\u786e\u8ba4\u4e00\u4e0b\uff0c\u4f60\u8bf4\u7684\u662f\u5174\u4e1a\u6768\u5bb6\u5e9c\uff0c\u8fd8\u662f\u6768\u4e50\u5e9c\uff1f\u786e\u8ba4\u540e\u6211\u518d\u5e2e\u4f60\u67e5\u8fd8\u6709\u54ea\u4e9b\u5728\u79df\u3002"],
        )
        self.assertEqual(fake_wecom.state_store.processed, {"msg-clarify"})

    async def test_send_final_actions_suppresses_all_media_when_selfcheck_fallback(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.images: list[str] = []
                self.videos: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

            def send_image(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
                self.images.append(media_id)
                return {"errcode": 0}

            def send_video(self, open_kfid: str, external_userid: str, media_id: str, title: str = "") -> dict:
                self.videos.append(media_id)
                return {"errcode": 0}

        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            result = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=kf_context_memory.empty_context(),
                final_reply="这条我需要人工再确认一下，避免发错资料。",
                tool_evidence={
                    "suppress_actions": True,
                    "inventory_images": ["sheet.png"],
                    "image_paths": ["room.jpg"],
                    "video_paths": ["room.mp4"],
                    "outbound_package": {
                        "inventory_explanation": "房源表发你了。",
                        "image_explanations": ["这是新柠长木府3-1002B的图片。"],
                        "video_explanations": ["这是新柠长木府3-1002B的视频。"],
                    },
                },
            )
        finally:
            main.wecom_kf = original_wecom

        self.assertEqual(fake.texts, ["这条我需要人工再确认一下，避免发错资料。"])
        self.assertEqual(fake.images, [])
        self.assertEqual(fake.videos, [])
        self.assertEqual(result["sent_actions"], [{"type": "text", "count": 1}])

    async def test_send_final_actions_does_not_duplicate_inventory_explanation(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.images: list[str] = []
                self.videos: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

            def send_image(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
                self.images.append(str(media_id))
                return {"errcode": 0}

            def send_video(self, open_kfid: str, external_userid: str, media_id: str, title: str = "") -> dict:
                self.videos.append(str(media_id))
                return {"errcode": 0}

        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                sheet_path = str(Path(directory) / "sheet.png")
                Path(sheet_path).write_bytes(b"png")
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=kf_context_memory.empty_context(),
                    final_reply="房源表发你了，可以先给客户看整体。",
                    tool_evidence={
                        "inventory_images": [sheet_path],
                        "outbound_package": {
                            "inventory_images": [sheet_path],
                            "inventory_explanation": "房源表发你了，你可以让客户先整体看一下。",
                        },
                    },
                )
        finally:
            main.wecom_kf = original_wecom

        self.assertEqual(fake.texts, ["房源表发你了，可以先给客户看整体。"])
        self.assertEqual(fake.images, [sheet_path])
        self.assertEqual(fake.videos, [])
        self.assertEqual(
            result["sent_actions"],
            [{"type": "text", "count": 1}, {"type": "image", "path": sheet_path, "count": 1}],
        )

    def test_stale_deposit_selfcheck_is_ignored_for_inventory_sheet(self) -> None:
        sanitized = main._sanitize_rule_selfcheck_for_intent(
            {
                "status": "fallback",
                "action": "fallback",
                "reason": "deposit_reply_missing_platform",
                "fallback_reply": "免押是支付宝无忧住服务。",
            },
            content="房源表发我",
            understanding={"intent": "inventory_sheet"},
        )

        self.assertEqual(sanitized["status"], "pass")
        fallback = main._safe_fallback_for_intent(
            {"intent": "inventory_sheet"},
            "免押是支付宝无忧住服务。",
        )
        self.assertIn("房源表", fallback)
        self.assertNotIn("免押", fallback)

    async def test_batch_customer_followups_are_merged_before_rewrite(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def is_processed(self, msgid: str) -> bool:
                return msgid in self.processed

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.contents: list[str] = []

            async def rewrite_kf_message(self, **kwargs):
                self.contents.append(kwargs["content"])
                return {
                    "intent": "inventory",
                    "rewritten_query": kwargs["content"],
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "我把你刚才补充的问题一起看了，按你给的预算和视频需求处理。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        fake_wecom = FakeWeComKf()
        fake_reply = FakeReplyGenerator()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "kf_turn_tasks": dict(main.kf_turn_tasks),
            "kf_turn_generations": dict(main.kf_turn_generations),
            "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
        }
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = FakeContextStore()
        main.reply_generator = fake_reply
        main.inventory = FakeInventory()
        main.kf_turn_tasks.clear()
        main.kf_turn_generations.clear()
        main.kf_turn_pending_messages.clear()
        try:
            await main._handle_text_messages_batch(
                [
                    {
                        "msgid": "msg-1",
                        "msgtype": "text",
                        "origin": 3,
                        "open_kfid": "kf",
                        "external_userid": "wm",
                        "text": {"content": "万达1500左右有哪些"},
                    },
                    {
                        "msgid": "msg-2",
                        "msgtype": "text",
                        "origin": 3,
                        "open_kfid": "kf",
                        "external_userid": "wm",
                        "text": {"content": "先发几套视频我筛一下"},
                    },
                ]
            )
        finally:
            main.wecom_kf = originals["wecom_kf"]
            main.wecom_kf_context_store = originals["wecom_kf_context_store"]
            main.reply_generator = originals["reply_generator"]
            main.inventory = originals["inventory"]
            main.kf_turn_tasks.clear()
            main.kf_turn_tasks.update(originals["kf_turn_tasks"])
            main.kf_turn_generations.clear()
            main.kf_turn_generations.update(originals["kf_turn_generations"])
            main.kf_turn_pending_messages.clear()
            main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])

        self.assertEqual(len(fake_reply.contents), 1)
        self.assertIn("万达1500左右有哪些", fake_reply.contents[0])
        self.assertIn("先发几套视频我筛一下", fake_reply.contents[0])
        self.assertEqual(len(fake_wecom.texts), 1)
        self.assertNotIn("发具体小区或预算", fake_wecom.texts[0])
        self.assertEqual(fake_wecom.state_store.processed, {"msg-1", "msg-2"})

    async def test_new_followup_cancels_running_turn_and_rewrites_all_questions(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def is_processed(self, msgid: str) -> bool:
                return msgid in self.processed

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.contents: list[str] = []
                self.first_started = asyncio.Event()

            async def rewrite_kf_message(self, **kwargs):
                self.contents.append(kwargs["content"])
                if len(self.contents) == 1:
                    self.first_started.set()
                    await asyncio.Event().wait()
                return {
                    "intent": "inventory",
                    "rewritten_query": kwargs["content"],
                    "query_state": {"intent": "inventory"},
                    "needs_clarification": True,
                    "clarification_text": "我把你连续补充的问题一起看了，按新的完整需求处理。",
                }

        class FakeInventory:
            async def all_rows(self, **kwargs):
                return []

        fake_wecom = FakeWeComKf()
        fake_reply = FakeReplyGenerator()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "reply_generator": main.reply_generator,
            "inventory": main.inventory,
            "kf_turn_tasks": dict(main.kf_turn_tasks),
            "kf_turn_generations": dict(main.kf_turn_generations),
            "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
        }
        main.wecom_kf = fake_wecom
        main.wecom_kf_context_store = FakeContextStore()
        main.reply_generator = fake_reply
        main.inventory = FakeInventory()
        main.kf_turn_tasks.clear()
        main.kf_turn_generations.clear()
        main.kf_turn_pending_messages.clear()
        try:
            first = asyncio.create_task(
                main._handle_text_message(
                    {
                        "msgid": "msg-running-1",
                        "msgtype": "text",
                        "origin": 3,
                        "open_kfid": "kf",
                        "external_userid": "wm",
                        "text": {"content": "万达1500左右有哪些"},
                    }
                )
            )
            await fake_reply.first_started.wait()
            second = asyncio.create_task(
                main._handle_text_message(
                    {
                        "msgid": "msg-running-2",
                        "msgtype": "text",
                        "origin": 3,
                        "open_kfid": "kf",
                        "external_userid": "wm",
                        "text": {"content": "视频也发我几套"},
                    }
                )
            )
            await asyncio.gather(first, second)
        finally:
            main.wecom_kf = originals["wecom_kf"]
            main.wecom_kf_context_store = originals["wecom_kf_context_store"]
            main.reply_generator = originals["reply_generator"]
            main.inventory = originals["inventory"]
            main.kf_turn_tasks.clear()
            main.kf_turn_tasks.update(originals["kf_turn_tasks"])
            main.kf_turn_generations.clear()
            main.kf_turn_generations.update(originals["kf_turn_generations"])
            main.kf_turn_pending_messages.clear()
            main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])

        self.assertEqual(len(fake_reply.contents), 2)
        self.assertEqual(fake_reply.contents[0], "万达1500左右有哪些")
        self.assertIn("万达1500左右有哪些", fake_reply.contents[-1])
        self.assertIn("视频也发我几套", fake_reply.contents[-1])
        self.assertEqual(len(fake_wecom.texts), 1)
        self.assertNotIn("重新查", fake_wecom.texts[0])
        self.assertEqual(fake_wecom.state_store.processed, {"msg-running-1", "msg-running-2"})

    async def test_enter_session_welcome_is_limited_to_ten_minutes(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.sent_at = 0.0

            def last_welcome_sent_at(self, key: str) -> float:
                return self.sent_at

            def mark_welcome_sent(self, key: str, sent_at: float | None = None) -> None:
                self.sent_at = sent_at or 1.0

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.sent: list[str] = []

            async def send_welcome_text_on_event(self, welcome_code: str, content: str) -> dict:
                self.sent.append(content)
                return {"errcode": 0}

        fake = FakeWeComKf()
        original = main.wecom_kf
        original_time = main.time.time
        original_audit = main._record_kf_welcome_audit
        main.wecom_kf = fake
        main.time.time = lambda: 1000.0
        main._record_kf_welcome_audit = lambda event: None
        try:
            message = {
                "msgtype": "event",
                "event": {
                    "welcome_code": "welcome-code",
                    "open_kfid": "kf",
                    "external_userid": "wm",
                },
            }
            await main._handle_enter_session(message)
            await main._handle_enter_session(message)
        finally:
            main.wecom_kf = original
            main.time.time = original_time
            main._record_kf_welcome_audit = original_audit

        self.assertEqual(len(fake.sent), 1)
        self.assertIn("找房、要视频、看密码、发房源表都可以直接说", fake.sent[0])
        self.assertIn("万达1500左右还有哪些", fake.sent[0])
        self.assertIn("小区名、房号记不清也没事", fake.sent[0])

    async def test_enter_session_welcome_falls_back_when_code_expired(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.sent_at = 0.0
                self.marked: list[tuple[str, float]] = []

            def last_welcome_sent_at(self, key: str) -> float:
                return self.sent_at

            def mark_welcome_sent(self, key: str, sent_at: float | None = None) -> None:
                self.sent_at = sent_at or 1.0
                self.marked.append((key, self.sent_at))

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.event_codes: list[str] = []
                self.texts: list[tuple[str, str, str]] = []

            async def send_welcome_text_on_event(self, welcome_code: str, content: str) -> dict:
                self.event_codes.append(welcome_code)
                raise RuntimeError("code expired")

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append((open_kfid, external_userid, content))
                return {"errcode": 0}

        fake = FakeWeComKf()
        audits: list[dict] = []
        original = main.wecom_kf
        original_time = main.time.time
        original_audit = main._record_kf_welcome_audit
        main.wecom_kf = fake
        main.time.time = lambda: 2000.0
        main._record_kf_welcome_audit = audits.append
        try:
            await main._handle_enter_session(
                {
                    "msgtype": "event",
                    "event": {
                        "event_type": "enter_session",
                        "welcome_code": "expired-code",
                        "open_kfid": "kf-1",
                        "external_userid": "wm-1",
                    },
                }
            )
        finally:
            main.wecom_kf = original
            main.time.time = original_time
            main._record_kf_welcome_audit = original_audit

        self.assertEqual(fake.event_codes, ["expired-code"])
        self.assertEqual(len(fake.texts), 1)
        self.assertEqual(fake.texts[0][0:2], ("kf-1", "wm-1"))
        self.assertEqual(fake.state_store.marked, [(main._conversation_key("kf-1", "wm-1"), 2000.0)])
        self.assertEqual(audits[-1]["status"], "sent")
        self.assertEqual(audits[-1]["method"], "send_text_fallback")

    async def test_enter_session_welcome_without_code_uses_text_fallback(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.sent_at = 0.0

            def last_welcome_sent_at(self, key: str) -> float:
                return self.sent_at

            def mark_welcome_sent(self, key: str, sent_at: float | None = None) -> None:
                self.sent_at = sent_at or 1.0

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.texts: list[tuple[str, str, str]] = []

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append((open_kfid, external_userid, content))
                return {"errcode": 0}

        fake = FakeWeComKf()
        audits: list[dict] = []
        original = main.wecom_kf
        original_audit = main._record_kf_welcome_audit
        main.wecom_kf = fake
        main._record_kf_welcome_audit = audits.append
        try:
            await main._handle_enter_session(
                {
                    "msgtype": "event",
                    "open_kfid": "kf-1",
                    "external_userid": "wm-1",
                    "event": {"event_type": "enter_session"},
                }
            )
        finally:
            main.wecom_kf = original
            main._record_kf_welcome_audit = original_audit

        self.assertEqual(len(fake.texts), 1)
        self.assertEqual(fake.texts[0][0:2], ("kf-1", "wm-1"))
        self.assertEqual(audits[-1]["status"], "sent")
        self.assertEqual(audits[-1]["method"], "send_text_fallback")

    async def test_wecom_kf_status_masks_welcome_state(self) -> None:
        class FakeStateStore:
            def load(self) -> dict:
                return {
                    "cursor": "cursor-1",
                    "processed_msgids": ["msg-1", "msg-2"],
                    "welcome_sent_at": {"kf-open-id:wm-external-id": 2000.0},
                }

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.last_next_cursor = "cursor-next"

        original = main.wecom_kf
        original_recent = main._recent_kf_welcome_audits
        main.wecom_kf = FakeWeComKf()
        main._recent_kf_welcome_audits = lambda limit=30: [{"status": "sent"}]
        try:
            status = await main.wecom_kf_status()
        finally:
            main.wecom_kf = original
            main._recent_kf_welcome_audits = original_recent

        self.assertTrue(status["ok"])
        self.assertTrue(status["cursor_present"])
        self.assertEqual(status["processed_msgid_count"], 2)
        self.assertEqual(status["welcome_sent_count"], 1)
        self.assertNotIn("wm-external-id", status["recent_welcome_sent"][0]["key"])
        self.assertEqual(status["recent_welcome_audits"], [{"status": "sent"}])

    async def test_kf_callback_schedules_message_event_background(self) -> None:
        class FakeRequest:
            async def body(self) -> bytes:
                return b"<xml />"

        class FakeWeComKf:
            def parse_callback_event(self, body: str, msg_signature: str, timestamp: str, nonce: str) -> dict:
                return {"Event": "kf_msg_or_event", "Token": "token-1", "OpenKfId": "kf-1"}

        scheduled: list[str] = []

        def fake_schedule(coro, *, label: str):
            scheduled.append(label)
            coro.close()
            return None

        original_wecom = main.wecom_kf
        original_schedule = main._schedule_background_task
        main.wecom_kf = FakeWeComKf()
        main._schedule_background_task = fake_schedule
        try:
            result = await main.receive_wecom_kf_callback(
                FakeRequest(),
                msg_signature="sig",
                timestamp="ts",
                nonce="nonce",
            )
        finally:
            main.wecom_kf = original_wecom
            main._schedule_background_task = original_schedule

        self.assertEqual(result, "success")
        self.assertEqual(scheduled, ["KF callback message event"])

    async def test_kf_callback_schedules_direct_enter_session_background(self) -> None:
        class FakeRequest:
            async def body(self) -> bytes:
                return b"<xml />"

        class FakeWeComKf:
            def parse_callback_event(self, body: str, msg_signature: str, timestamp: str, nonce: str) -> dict:
                return {
                    "MsgType": "event",
                    "Event": "enter_session",
                    "WelcomeCode": "welcome-code",
                    "OpenKfId": "kf-1",
                    "ExternalUserID": "wm-1",
                }

        scheduled: list[str] = []

        def fake_schedule(coro, *, label: str):
            scheduled.append(label)
            coro.close()
            return None

        original_wecom = main.wecom_kf
        original_schedule = main._schedule_background_task
        main.wecom_kf = FakeWeComKf()
        main._schedule_background_task = fake_schedule
        try:
            result = await main.receive_wecom_kf_callback(
                FakeRequest(),
                msg_signature="sig",
                timestamp="ts",
                nonce="nonce",
            )
        finally:
            main.wecom_kf = original_wecom
            main._schedule_background_task = original_schedule

        self.assertEqual(result, "success")
        self.assertEqual(scheduled, ["KF callback enter_session event"])

    async def test_kf_event_awaits_async_sync_messages(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.called = False
                self.args: tuple[str, str] | None = None

            async def sync_messages(self, open_kfid: str, token: str) -> list[dict]:
                self.called = True
                self.args = (open_kfid, token)
                return []

        fake = FakeWeComKf()
        original = main.wecom_kf
        main.wecom_kf = fake
        try:
            await main._handle_kf_event({"OpenKfId": "kf-1", "Token": "token-1"})
        finally:
            main.wecom_kf = original

        self.assertTrue(fake.called)
        self.assertEqual(fake.args, ("kf-1", "token-1"))

    async def test_send_text_awaits_async_wecom_client(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.sent: list[tuple[str, str, str]] = []

            async def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.sent.append((open_kfid, external_userid, text))
                return {"errcode": 0}

        fake = FakeWeComKf()
        original = main.wecom_kf
        main.wecom_kf = fake
        try:
            await main._send_text("kf", "wm", "  你好  ")
        finally:
            main.wecom_kf = original

        self.assertEqual(fake.sent, [("kf", "wm", "你好")])

    async def test_send_final_actions_skips_duplicate_text_for_same_msgid(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0, "msgid": f"provider-{len(self.texts)}"}

        context = {
            "structured_memory": {
                "current_turn_id": "turn-1",
                "turn_records": [{"turn_id": "turn-1", "msgids": ["msg-dup"]}],
            }
        }
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=context,
                final_reply="这条我需要人工再确认一下，避免发错资料。",
                tool_evidence={"suppress_actions": True},
                msgids=["msg-dup"],
            )
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=first["context"],
                final_reply="这条我需要人工再确认一下，避免发错资料。",
                tool_evidence={"suppress_actions": True},
                msgids=["msg-dup"],
            )
        finally:
            main.wecom_kf = original_wecom

        self.assertEqual(fake.texts, ["这条我需要人工再确认一下，避免发错资料。"])
        self.assertEqual(first["sent_actions"], [{"type": "text", "count": 1}])
        self.assertEqual(second["sent_actions"], [])
        statuses = [item["status"] for item in second["context"]["send_receipts"]["receipts"]]
        self.assertEqual(statuses, ["sent", "skipped_duplicate"])

    async def test_send_final_actions_skips_duplicate_video_transaction_for_same_msgid(self) -> None:
        class FakeWeComKf:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.videos: list[str] = []

            def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
                self.texts.append(text)
                return {"errcode": 0}

            def send_video(self, open_kfid: str, external_userid: str, media_id: str, title: str = "") -> dict:
                self.videos.append(str(media_id))
                return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

        context = {
            "structured_memory": {
                "current_turn_id": "turn-1",
                "turn_records": [{"turn_id": "turn-1", "msgids": ["msg-video"]}],
            }
        }
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = str(Path(directory) / "room.mp4")
                Path(video_path).write_bytes(b"video")
                first = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=context,
                    final_reply="",
                    tool_evidence={
                        "video_paths": [video_path],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video"],
                )
                second = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=first["context"],
                    final_reply="",
                    tool_evidence={
                        "video_paths": [video_path],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video"],
                )
        finally:
            main.wecom_kf = original_wecom

        self.assertEqual(len(fake.texts), 1)
        self.assertIn("视频", fake.texts[0])
        self.assertEqual(fake.videos, [video_path])
        self.assertEqual(len(first["sent_actions"]), 1)
        self.assertEqual(second["sent_actions"], [])
        statuses = [item["status"] for item in second["context"]["send_receipts"]["receipts"]]
        self.assertEqual(statuses, ["sent", "skipped_duplicate"])

    async def test_interleaved_customers_do_not_share_candidate_context(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: set[str] = set()

            def mark_processed(self, msgid: str) -> None:
                self.processed.add(msgid)

        class FakeWeComKf:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()

        class FakeContextStore:
            def __init__(self) -> None:
                self.data: dict[str, dict] = {}

            def get(self, key: str) -> dict | None:
                return self.data.get(key)

            def save(self, key: str, context: dict) -> None:
                self.data[key] = context

        class FakeInventoryReadContext:
            def __init__(self, turn_id: str) -> None:
                self.turn_id = turn_id

            def to_log_dict(self) -> dict:
                return {
                    "request_id": "test-request",
                    "turn_id": self.turn_id,
                    "source_kind": "legacy",
                    "source_hash": "test-hash",
                    "decision_id": f"decision-{self.turn_id}",
                    "selection_mode": "disabled",
                }

        rows_by_customer = {
            "wm-a": {"community": "AlphaCourt", "room_no": "A-101", "listing_id": "lst-a"},
            "wm-b": {"community": "BetaCourt", "room_no": "B-202", "listing_id": "lst-b"},
        }
        observed_context: list[tuple[str, str, list[str]]] = []
        sent_replies: list[tuple[str, str]] = []

        def labels_from_context(context: dict) -> list[str]:
            return [
                main._row_label(row)
                for row in ((context.get("last_candidate_set") or {}).get("candidates") or [])
            ]

        async def fake_understand_message(**kwargs):
            content = kwargs["content"]
            context = kwargs["context"]
            observed_context.append((kwargs.get("inventory_read_context").turn_id, content, labels_from_context(context)))
            wants_video = "video" in content.lower()
            return {
                "intent": "media" if wants_video else "inventory",
                "rewritten_query": content,
                "effective_query": content,
                "query_state": {"intent": "media" if wants_video else "inventory"},
                "needs_clarification": False,
                "constraint_proof": {"wants_video": wants_video},
                "structured_task": {"tool_requirements": {}},
            }

        async def fake_plan_actions(**kwargs):
            content = kwargs["content"]
            actions = (
                ["send_video", "generate_reply"]
                if "video" in content.lower()
                else ["search_inventory", "compact_listing", "generate_reply"]
            )
            return {"actions": actions, "source": "test_interleaved_customers"}

        async def fake_execute_tools(**kwargs):
            context = kwargs["context"]
            content = kwargs["content"]
            if "customer A" in content:
                row = rows_by_customer["wm-a"]
                context["last_candidate_set"] = {"candidates": [row], "shown_count": 1}
                return {"actions": kwargs["actions"], "inventory_rows": [row], "target_rows": [row]}
            if "customer B" in content:
                row = rows_by_customer["wm-b"]
                context["last_candidate_set"] = {"candidates": [row], "shown_count": 1}
                return {"actions": kwargs["actions"], "inventory_rows": [row], "target_rows": [row]}
            candidates = (context.get("last_candidate_set") or {}).get("candidates") or []
            return {
                "actions": kwargs["actions"],
                "target_rows": candidates,
                "video_rows": candidates,
                "video_paths": [],
            }

        async def fake_generate_reply_result(**kwargs):
            labels = labels_from_context(kwargs["context"])
            reply = f"current candidate: {labels[0]}" if labels else "current candidate: none"
            return {
                "reply": reply,
                "draft_reply": reply,
                "context": kwargs["context"],
                "selfcheck": {"status": "pass"},
                "needs_planner_retry": False,
                "planner_retry_reason": "",
            }

        async def fake_send_final_actions(**kwargs):
            sent_replies.append((kwargs["external_userid"], kwargs["final_reply"]))
            return {
                "context": kwargs["context"],
                "sent_actions": [{"type": "text", "count": 1}],
            }

        def fake_create_inventory_read_context(**kwargs):
            return FakeInventoryReadContext(f"{kwargs['external_userid']}-{kwargs['generation']}")

        fake_store = FakeContextStore()
        originals = {
            "wecom_kf": main.wecom_kf,
            "wecom_kf_context_store": main.wecom_kf_context_store,
            "_create_inventory_read_context": main._create_inventory_read_context,
            "_understand_message": main._understand_message,
            "_plan_actions": main._plan_actions,
            "_execute_tools": main._execute_tools,
            "_generate_reply_result": main._generate_reply_result,
            "_send_final_actions": main._send_final_actions,
            "_build_orchestrator_shadow_artifact": main._build_orchestrator_shadow_artifact,
            "kf_turn_generations": dict(main.kf_turn_generations),
            "kf_turn_tasks": dict(main.kf_turn_tasks),
            "kf_turn_pending_messages": dict(main.kf_turn_pending_messages),
        }
        main.wecom_kf = FakeWeComKf()
        main.wecom_kf_context_store = fake_store
        main._create_inventory_read_context = fake_create_inventory_read_context
        main._understand_message = fake_understand_message
        main._plan_actions = fake_plan_actions
        main._execute_tools = fake_execute_tools
        main._generate_reply_result = fake_generate_reply_result
        main._send_final_actions = fake_send_final_actions
        main._build_orchestrator_shadow_artifact = lambda **kwargs: None
        try:
            sequence = [
                ("wm-a", "msg-a-1", "customer A wants AlphaCourt one room"),
                ("wm-b", "msg-b-1", "customer B wants BetaCourt two room"),
                ("wm-a", "msg-a-2", "send this video"),
                ("wm-b", "msg-b-2", "send this video"),
            ]
            for generation, (external_userid, msgid, content) in enumerate(sequence, start=1):
                key = main._conversation_key("kf", external_userid)
                main.kf_turn_generations[key] = generation
                await main._process_text_turn(
                    open_kfid="kf",
                    external_userid=external_userid,
                    pending_items=[{"msgid": msgid, "content": content}],
                    generation=generation,
                )
        finally:
            main.wecom_kf = originals["wecom_kf"]
            main.wecom_kf_context_store = originals["wecom_kf_context_store"]
            main._create_inventory_read_context = originals["_create_inventory_read_context"]
            main._understand_message = originals["_understand_message"]
            main._plan_actions = originals["_plan_actions"]
            main._execute_tools = originals["_execute_tools"]
            main._generate_reply_result = originals["_generate_reply_result"]
            main._send_final_actions = originals["_send_final_actions"]
            main._build_orchestrator_shadow_artifact = originals["_build_orchestrator_shadow_artifact"]
            main.kf_turn_generations.clear()
            main.kf_turn_generations.update(originals["kf_turn_generations"])
            main.kf_turn_tasks.clear()
            main.kf_turn_tasks.update(originals["kf_turn_tasks"])
            main.kf_turn_pending_messages.clear()
            main.kf_turn_pending_messages.update(originals["kf_turn_pending_messages"])

        key_a = main._conversation_key("kf", "wm-a")
        key_b = main._conversation_key("kf", "wm-b")
        self.assertEqual(labels_from_context(fake_store.data[key_a]), ["AlphaCourtA-101"])
        self.assertEqual(labels_from_context(fake_store.data[key_b]), ["BetaCourtB-202"])
        a_followup = [item for item in observed_context if item[0] == "wm-a-3"][0]
        b_followup = [item for item in observed_context if item[0] == "wm-b-4"][0]
        self.assertEqual(a_followup[2], ["AlphaCourtA-101"])
        self.assertEqual(b_followup[2], ["BetaCourtB-202"])
        self.assertIn(("wm-a", "current candidate: AlphaCourtA-101"), sent_replies)
        self.assertIn(("wm-b", "current candidate: BetaCourtB-202"), sent_replies)
        self.assertNotIn(("wm-a", "current candidate: BetaCourtB-202"), sent_replies)
        self.assertNotIn(("wm-b", "current candidate: AlphaCourtA-101"), sent_replies)


if __name__ == "__main__":
    unittest.main()
