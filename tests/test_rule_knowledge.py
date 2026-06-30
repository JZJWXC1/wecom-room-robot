from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import settings
from app.services.llm import ReplyGenerator
from app.services.rule_knowledge import RuleKnowledgeService


def _write_card(path, *, stage: str, intents: str, triggers: str, content: str) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {path.stem}",
                f"stage: {stage}",
                f"intents: {intents}",
                f"triggers: {triggers}",
                "priority: 90",
                "hard_rule: true",
                "---",
                f"# {path.stem}",
                "",
                content,
            ]
        ),
        encoding="utf-8",
    )


def test_rule_knowledge_retrieves_by_stage_intent_and_trigger(tmp_path) -> None:
    _write_card(
        tmp_path / "rewrite_inventory_sheet.md",
        stage="rewrite",
        intents="inventory_sheet",
        triggers="房源表 表格",
        content="房源表请求直接发总表，不追问小区或价位。",
    )
    _write_card(
        tmp_path / "planner_media.md",
        stage="planner",
        intents="media",
        triggers="视频 图片",
        content="视频请求必须规划素材工具。",
    )

    service = RuleKnowledgeService(tmp_path)
    rewrite_cards = service.retrieve(stage="rewrite", intent="inventory_sheet", query_text="房源表发一下")
    plain_inventory_cards = service.retrieve(stage="rewrite", intent="inventory", query_text="万达1500左右有哪些")
    planner_cards = service.retrieve(stage="planner", intent="media", query_text="1和5视频")

    assert [card.id for card in rewrite_cards] == ["rewrite_inventory_sheet"]
    assert plain_inventory_cards == []
    assert [card.id for card in planner_cards] == ["planner_media"]
    assert "视频请求" in service.format_cards(planner_cards)


def test_builtin_rule_cards_cover_room_scope_and_unbound_context() -> None:
    service = RuleKnowledgeService()

    room_scope = service.retrieve(
        stage="rewrite",
        intent="inventory",
        query_text="万达有什么2000以下的一室",
    )
    unbound_context = service.retrieve(
        stage="rewrite",
        intent="viewing",
        query_text="这几套里面客户今天想看，密码多少？",
    )

    assert any(card.id == "rewrite_room_type_scope" for card in room_scope)
    assert any(card.id == "rewrite_unbound_context_reference" for card in unbound_context)


def test_builtin_rule_cards_cover_llm2_tool_grounded_reply() -> None:
    service = RuleKnowledgeService()

    cards = service.retrieve(
        stage="llm2",
        intent="inventory",
        query_text="万达有什么2000以下的一室，稍后确认房态",
    )

    assert any(card.id == "reply_tool_grounded" for card in cards)


def test_builtin_rule_cards_cover_tool_resolver_context_binding() -> None:
    service = RuleKnowledgeService()

    cards = service.retrieve(
        stage="tool_resolver",
        intent="inventory",
        query_text="石桥附近5000左右有两室吗？最好整租。",
        query_state={"area": "石桥街道 华丰 石桥 永佳 半山", "budget": 5000, "layout": "两室"},
    )

    assert any(card.id == "planner_context_binding" for card in cards)


def test_builtin_rule_cards_cover_llm2_budget_payment_scope() -> None:
    service = RuleKnowledgeService()

    cards = service.retrieve(
        stage="llm2",
        intent="inventory",
        query_text="北部软件园附近便宜点的单间还有吗？客户预算1800以内。",
        query_state={"budget": 1800, "layout": "一室"},
    )

    assert any(card.id == "planner_budget_payment_scope" for card in cards)


def test_builtin_rule_cards_do_not_restore_legacy_planner_stage() -> None:
    service = RuleKnowledgeService()

    cards = service.retrieve(
        stage="planner",
        intent="inventory",
        query_text="万达有什么2000以下的一室，稍后确认房态",
    )

    assert not any(card.id in {"reply_tool_grounded", "planner_tool_mapping"} for card in cards)


def test_rewrite_prompt_includes_stage_rule_cards_without_full_context(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_rewrite_model", "rewrite-model")
    _write_card(
        tmp_path / "rewrite_batch_video.md",
        stage="rewrite",
        intents="media",
        triggers="1和5 视频",
        content="1和5视频必须绑定候选编号，不能重新解释用户意图。",
    )
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"rewritten_query":"发送第1和第5套视频",'
                                '"intent":"media",'
                                '"tool_plan":{"actions":["search_inventory","context_tools","send_video","generate_reply"],"confidence":0.9}}'
                            )
                        )
                    )
                ]
            )

    generator = ReplyGenerator(rule_knowledge=RuleKnowledgeService(tmp_path))
    generator._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    result = asyncio.run(
        generator.rewrite_kf_message(
            content="1和5视频",
            structured_memory={
                "last_turn_record": {
                    "query_state": {"intent": "media", "selected_indices": [1, 5]}
                }
            },
            inventory_index={"communities": ["棠润府"], "room_keys": ["棠润府15-2-801B"]},
        )
    )

    prompt = captured["messages"][1]["content"]
    assert "send_video" in result["tool_plan"]["actions"]
    assert "问题重写相关规则卡片" in prompt
    assert "1和5视频必须绑定候选编号" in prompt
    assert "raw_dialog_context" not in prompt


def test_selfcheck_prompt_includes_outbound_rule_cards(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    monkeypatch.setattr(settings, "dashscope_selfcheck_model", "selfcheck-model")
    _write_card(
        tmp_path / "selfcheck_outbound_package.md",
        stage="selfcheck",
        intents="media",
        triggers="视频",
        content="自检不通过时必须拦住所有动作并按分级回流。",
    )
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"status":"pass"}'))]
            )

    generator = ReplyGenerator(rule_knowledge=RuleKnowledgeService(tmp_path))
    generator._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    result = asyncio.run(
        generator.assess_kf_final_reply(
            content="前两套视频发我",
            structured_task={"intent": "media", "query_state": {"wants_video": True}},
            constraint_proof={"wants_video": True},
            outbound_package={"videos": [{"room": "荣润府15-2-801B"}]},
            draft_reply="这是荣润府15-2-801B的视频。",
        )
    )

    prompt = captured["messages"][1]["content"]
    assert result["status"] == "pass"
    assert "最终自检相关规则卡片" in prompt
    assert "自检不通过时必须拦住所有动作并按分级回流" in prompt
