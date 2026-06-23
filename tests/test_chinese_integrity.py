import json
from pathlib import Path

from app.services.kf_context_memory import (
    append_dialog_message,
    record_structured_assistant_output,
    rewrite_memory_view,
    start_structured_turn,
    structured_memory_summary,
)
from app.services.rule_knowledge import RuleKnowledgeService
from app.services.wecom_kf import WeComKfContextStore


def test_structured_memory_preserves_chinese_without_escape_or_question_marks() -> None:
    user_text = "万达附近1500左右还有哪些？客户想今天先看两套。"
    rewrite_result = {
        "rewritten_query": "拱墅万达/北部软件园/城北万象城区域，1500左右，在租一室房源，客户今天想先看两套",
        "intent": "inventory",
        "query_state": {
            "area": "拱墅万达 北部软件园 城北万象城",
            "budget": "1500左右",
            "layout": "一室",
        },
        "needs_clarification": False,
    }
    reply_text = "有的，万达附近1500左右我这边先看这两套：合峙悦府6-1-1204B、棠润府15-2-801B。"

    context = append_dialog_message(None, role="user", content=user_text)
    context = start_structured_turn(
        context,
        state={},
        user_input={"content": user_text},
        rewrite_result=rewrite_result,
    )
    context = record_structured_assistant_output(
        context,
        final_reply=reply_text,
        sent_action={
            "type": "text",
            "count": 1,
            "room_keys": ["合峙悦府6-1-1204B", "棠润府15-2-801B"],
        },
    )
    context = append_dialog_message(context, role="assistant", content=reply_text)

    payload = {
        "summary": structured_memory_summary(context),
        "rewrite_view": rewrite_memory_view(context),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    assert "万达附近1500左右" in text
    assert "拱墅万达" in text
    assert "合峙悦府" in text
    assert "\\u4e07" not in text
    assert "?" not in text


def test_rule_knowledge_loads_and_matches_chinese_utf8(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "deposit.md").write_text(
        "\n".join(
            [
                "---",
                "id: deposit",
                "stage: planner",
                "intents: deposit",
                "triggers: 免押金,芝麻信用,服务费",
                "priority: 90",
                "---",
                "# 免押规则",
                "免押金需要走支付宝无忧住，服务费按 5.5%-8%。",
            ]
        ),
        encoding="utf-8",
    )

    cards = RuleKnowledgeService(rules_dir).retrieve_text(
        stage="planner",
        intent="deposit",
        query_text="免押金要什么条件？服务费怎么算？",
    )

    assert "免押金" in cards
    assert "支付宝无忧住" in cards
    assert "\\u514d" not in cards
    assert "?" not in cards


def test_wecom_context_store_round_trips_chinese_json(tmp_path: Path) -> None:
    path = tmp_path / "kf_context.json"
    store = WeComKfContextStore(path=path)
    context = {
        "recent_messages": [
            {"role": "user", "content": "新天地4000左右两室还有吗？"},
            {"role": "assistant", "content": "有的，我这边查一下新天地附近4000左右的两室。"},
        ],
        "structured_memory": {
            "raw_dialog_context": [
                {"role": "user", "content": "新天地4000左右两室还有吗？"},
            ],
            "turn_records": [
                {
                    "turn_id": "turn-1",
                    "turn_index": 1,
                    "user_raw": "新天地4000左右两室还有吗？",
                    "rewritten_query": "东新园/杭氧/新天地区域，4000左右，两室在租房源",
                    "intent": "inventory",
                }
            ],
        },
    }

    store.save("kf:user", context)
    stored_text = path.read_text(encoding="utf-8")
    loaded = store.get("kf:user")

    assert "新天地4000左右两室" in stored_text
    assert "\\u65b0" not in stored_text
    assert loaded["recent_messages"][0]["content"] == "新天地4000左右两室还有吗？"
    assert loaded["structured_memory"]["turn_records"][0]["rewritten_query"].startswith("东新园")
