from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import settings
from app.services.llm import ReplyGenerator


def test_build_kf_task_packet_production_missing_key_is_hard_gate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_rewrite_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_api_key", "")

    generator = ReplyGenerator()

    try:
        asyncio.run(generator.build_kf_task_packet(content="你好", mode="production"))
    except RuntimeError as exc:
        assert "LLM1 production rewrite API key is missing" in str(exc)
    else:
        raise AssertionError("production LLM1 must not fall back to legacy packet when rewrite key is missing")


def test_build_kf_task_packet_production_prompt_excludes_legacy_summary(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_rewrite_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_rewrite_model", "rewrite-model")

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"rewritten_query":"你好",'
                                '"task_atoms":[{"task_id":"task-1-reply","task_type":"reply_compose_signal"}],'
                                '"tool_plan":{"actions":["generate_reply"]}}'
                            )
                        )
                    )
                ]
            )

    generator = ReplyGenerator(rule_knowledge=SimpleNamespace(retrieve_text=lambda **kwargs: "无"))
    generator._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    packet = asyncio.run(
        generator.build_kf_task_packet(
            content="你好",
            legacy_rewrite={"rewritten_query": "LEGACY_ONLY_REWRITE"},
            legacy_planner={"actions": ["search_inventory", "send_video"], "reason": "LEGACY_ONLY_PLAN"},
            mode="production",
        )
    )
    prompt = captured["messages"][1]["content"]

    assert packet.legacy_unknown_fields["llm1_production"]["prompt_artifact"]["source"] == "production"
    assert "脱敏 production 输入" in prompt
    assert "legacy_rewrite_summary" not in prompt
    assert "legacy_planner_summary" not in prompt
    assert "LEGACY_ONLY_REWRITE" not in prompt
    assert "LEGACY_ONLY_PLAN" not in prompt
    assert "reply_compose_signal" in prompt
    assert "clarification|reply_text" not in prompt
    assert "Planner 回流证据" not in prompt


def test_rewrite_kf_message_returns_orchestrator_tool_plan_contract(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_rewrite_model", "rewrite-model")

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"rewritten_query":"万达1500左右一室",'
                                '"intent":"inventory",'
                                '"tool_plan":{"actions":["search_inventory","generate_reply"],"confidence":0.9}}'
                            )
                        )
                    )
                ]
            )

    generator = ReplyGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    result = asyncio.run(generator.rewrite_kf_message(content="万达1500左右有哪些"))

    assert result["tool_plan"]["actions"] == ["search_inventory", "generate_reply"]
    prompt = captured["messages"][1]["content"]
    system_prompt = captured["messages"][0]["content"]
    assert "tool_plan" in prompt
    assert "工具前阶段" in system_prompt
    assert "最终客户可见话术只能由 LLM2 在工具取证后生成" in prompt
    assert "15-2-801B 不能生成 801预算" in system_prompt
    assert "不能输出“又问杨家新雅苑”" in prompt


def test_legacy_plan_kf_reply_text_method_removed() -> None:
    generator = ReplyGenerator()

    assert not hasattr(generator, "plan_kf_reply_text")


def test_reply_generator_routes_each_rag_stage_to_configured_model(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_rewrite_provider", "dashscope")
    monkeypatch.setattr(settings, "llm_reply_provider", "dashscope")
    monkeypatch.setattr(settings, "llm_selfcheck_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_rewrite_model", "rewrite-model")
    monkeypatch.setattr(settings, "dashscope_reply_model", "reply-model")
    monkeypatch.setattr(settings, "dashscope_selfcheck_model", "selfcheck-model")

    captured_models: list[str] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            model = kwargs["model"]
            captured_models.append(model)
            if model == "reply-model":
                content = '{"reply_text":"有的，我这边查到了。","self_review":{"status":"pass","reason":"ok"}}'
            elif model == "selfcheck-model":
                content = '{"status":"pass","reason":""}'
            else:
                content = '{"rewritten_query":"万达2000以下一室","intent":"inventory","tool_plan":{"actions":["search_inventory","generate_reply"],"confidence":0.9}}'
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content)
                    )
                ]
            )

    generator = ReplyGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    asyncio.run(generator.rewrite_kf_message(content="万达一室"))
    asyncio.run(
        generator.compose_kf_outbound_shadow(
            task_packet={"tasks": [{"task_id": "task-1", "task_type": "reply_text"}]},
            evidence_bundle={"evidence": [{"evidence_id": "evd-1", "summary": "万融城1-101"}]},
            response_strategy={"mode": "answer"},
        )
    )
    asyncio.run(
        generator.assess_kf_final_reply(
            content="万达一室",
            draft_reply="有的，我这边查到了。",
        )
    )
    assert captured_models == [
        "rewrite-model",
        "reply-model",
        "selfcheck-model",
    ]


def test_compose_kf_outbound_shadow_prompt_requires_oralized_media_tense(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_reply_model", "reply-model")

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"reply_text":"这是棠润府15-2-801B房间的视频。","self_review":{"status":"pass"}}'
                        )
                    )
                ]
            )

    generator = ReplyGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    result = asyncio.run(
        generator.compose_kf_outbound_shadow(
            task_packet={"tasks": [{"task_id": "task-video", "task_type": "send_video"}]},
            evidence_bundle={"evidence": [{"evidence_id": "evd-video", "summary": "棠润府15-2-801B 视频"}]},
            response_strategy={"mode": "send_media"},
        )
    )

    system_prompt = captured["messages"][0]["content"]
    user_prompt = captured["messages"][1]["content"]

    assert result["reply_text"].startswith("这是棠润府")
    assert captured["model"] == "reply-model"
    assert "话术要像真实租房客服" in system_prompt
    assert "不要说“稍后发、等下发、会发你、素材已准备好”" in system_prompt
    assert "确定性 inventory/media/deposit/contract fallback 只允许进入 ToolEvidenceBundle 或 error code" in system_prompt
    assert "Sender 只执行已验证 send action 和授权槽位追加，不生成客服话术" in system_prompt
    assert "客户可见话术必须口语化" in user_prompt
    assert "已有媒体 send action" in user_prompt
    assert "evidence_type=missing_media" in user_prompt
    assert "只改话术，不改 claims/send action/action_id/evidence_ref" in user_prompt


def test_settings_support_deepseek_provider_without_changing_stage_contract(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_planner_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_retry_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "deepseek_planner_model", "deepseek-chat")
    monkeypatch.setattr(settings, "deepseek_retry_model", "deepseek-reasoner")

    provider = settings.llm_provider_for("planner")

    assert provider == "deepseek"
    assert settings.llm_api_key_for(provider) == "deepseek-key"
    assert settings.llm_base_url_for(provider) == "https://api.deepseek.com"
    assert settings.llm_model_for("planner") == "deepseek-chat"
    assert settings.llm_model_for("planner", retry=True) == "deepseek-reasoner"
