from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import settings
from app.services.llm import ReplyGenerator


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
    assert "最终话术只能在工具执行后生成" in prompt
    assert "15-2-801B 不能生成 801预算" in system_prompt
    assert "不能输出“又问杨家新雅苑”" in prompt


def test_plan_kf_reply_text_uses_tool_evidence_after_tools(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_planner_model", "planner-model")

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"reply_text":"还在的，棠润府15-2-801B还在，押一付一1600。","need_rewrite_clarification":false,"reason":"ok"}'
                        )
                    )
                ]
            )

    generator = ReplyGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    result = asyncio.run(
        generator.plan_kf_reply_text(
            content="荣润府15-2-801B还在吗",
            structured_task={"intent": "inventory", "effective_query": "棠润府15-2-801B还在吗"},
            entity_resolution={"status": "resolved", "canonical_community": "棠润府"},
            constraint_proof={"room_refs": ["15-2-801B"]},
            planner_result={"actions": ["search_inventory", "generate_reply"]},
            tool_evidence={
                "target_rows": [
                    {"小区": "棠润府", "房号": "15-2-801B", "押一付一": "1600", "押二付一": "1400"}
                ]
            },
        )
    )

    assert result["source"] == "llm_planner_reply_from_tools"
    assert result["selfcheck"]["status"] == "pass"
    assert "棠润府15-2-801B" in result["reply_text"]
    prompt = captured["messages"][1]["content"]
    assert "工具结果证据 ToolEvidence" in prompt
    assert "selfcheck" in prompt
    assert "棠润府" in prompt
    assert "pre_tool_reply_text" in prompt
    system_prompt = captured["messages"][0]["content"]
    assert "还在吗" in system_prompt
    assert "还在，" in system_prompt
    assert "不能说“稍后发你”" in system_prompt
    assert "不要主动报看房密码" in system_prompt
    assert "ToolEvidence 里有看房方式密码" in system_prompt
    assert "RetryPacket 要求去掉看房密码" in captured["messages"][1]["content"]
    assert "同时回答押一付一和押二付一" in system_prompt
    assert "不要主动说暂时没找到视频" in system_prompt or "不要主动说暂时没找到视频/图片素材" in system_prompt


def test_plan_kf_reply_text_treats_inventory_images_as_sheet_ready(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_planner_model", "planner-model")

    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"reply_text":"房源表发你了，你可以让客户先整体看一下。","need_rewrite_clarification":false,"reason":"ok"}'
                        )
                    )
                ]
            )

    generator = ReplyGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    result = asyncio.run(
        generator.plan_kf_reply_text(
            content="房源表发我",
            structured_task={"intent": "inventory_sheet", "effective_query": "发送房源表"},
            entity_resolution={"status": "resolved"},
            constraint_proof={"wants_inventory_sheet": True},
            planner_result={"actions": ["send_inventory_sheet"]},
            tool_evidence={"inventory_row_count": 0, "inventory_image_count": 2},
        )
    )

    assert "房源表发你了" in result["reply_text"]
    system_prompt = captured["messages"][0]["content"]
    assert "inventory_image_count" in system_prompt
    assert "不能说没查到房源表" in system_prompt


def test_reply_generator_routes_each_rag_stage_to_configured_model(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_rewrite_provider", "dashscope")
    monkeypatch.setattr(settings, "llm_planner_provider", "dashscope")
    monkeypatch.setattr(settings, "llm_selfcheck_provider", "dashscope")
    monkeypatch.setattr(settings, "llm_retry_provider", "dashscope")
    monkeypatch.setattr(settings, "dashscope_rewrite_model", "rewrite-model")
    monkeypatch.setattr(settings, "dashscope_planner_model", "planner-model")
    monkeypatch.setattr(settings, "dashscope_selfcheck_model", "selfcheck-model")
    monkeypatch.setattr(settings, "dashscope_retry_model", "retry-model")

    captured_models: list[str] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            model = kwargs["model"]
            captured_models.append(model)
            if model in {"planner-model", "retry-model"}:
                content = '{"reply_text":"有的，我这边查到了。","selfcheck":{"status":"pass","reason":"ok"},"reason":"ok"}'
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
        generator.plan_kf_reply_text(
            content="万达一室",
            structured_task={"intent": "inventory"},
            entity_resolution={"status": "resolved"},
            constraint_proof={"layout": "一室"},
            planner_result={"actions": ["search_inventory", "generate_reply"]},
            tool_evidence={"target_rows": [{"小区": "万融城", "房号": "1-101"}]},
        )
    )
    asyncio.run(
        generator.plan_kf_reply_text(
            content="万达一室",
            structured_task={"intent": "inventory"},
            entity_resolution={"status": "resolved"},
            constraint_proof={"layout": "一室"},
            planner_result={"actions": ["search_inventory", "generate_reply"]},
            tool_evidence={"target_rows": [{"小区": "万融城", "房号": "1-101"}]},
            planner_retry_reason="自检失败，重新规划",
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
        "planner-model",
        "retry-model",
        "selfcheck-model",
    ]


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
