from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.export_real_dialogue_fixture import (
    build_fixture_payload,
    export_fixture,
    sanitize_text,
    validate_fixture_payload,
)


REAL_DIALOGUE_FIXTURE = Path("tests/fixtures/qa/real_server_dialogues_sanitized.json")


def test_export_real_dialogue_fixture_redacts_sensitive_values_and_groups_user_turns(tmp_path: Path) -> None:
    fake_phone = "1" + "3800138000"
    fake_password = "123" + "456#"
    fake_key = "sk-" + "test_" + ("a" * 24)
    raw = tmp_path / "kf_dialogue_events.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "external_userid": "wm_" + ("b" * 40),
                        "msgtype": "text",
                        "text": {
                            "content": f"客户想看万达附近两室，电话{fake_phone}，密码{fake_password}",
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "external_userid": "wm_" + ("b" * 40),
                        "reply_texts": [f"客服回复不应该进用户回放，{fake_key}"],
                    },
                    ensure_ascii=False,
                ),
                "客户: 这两套视频都发一下，最好原视频",
                "客服: 好的",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_fixture_payload(raw, window_size=10)
    joined = "\n".join(turn for window in payload["windows"] for turn in window["turns"])

    assert payload["window_count"] == 2
    assert "万达附近两室" in joined
    assert "这两套视频都发一下" in joined
    assert "客服回复不应该进用户回放" not in joined
    assert fake_phone not in joined
    assert fake_password not in joined
    assert fake_key not in joined
    assert "<PHONE>" in joined
    assert "<VIEWING_PASSWORD>" in joined
    assert validate_fixture_payload(payload) == []


def test_export_real_dialogue_fixture_writes_valid_json(tmp_path: Path) -> None:
    raw = tmp_path / "events.jsonl"
    out = tmp_path / "real_server_dialogues_sanitized.json"
    raw.write_text(
        json.dumps(
            {
                "conversation_id": "conv_" + ("c" * 36),
                "user_text": "客户问石桥附近有没有一室一厅，预算两千左右",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    payload = export_fixture(raw, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))

    assert loaded == payload
    assert loaded["schema"] == "real_server_dialogues_sanitized.v1"
    assert loaded["windows"][0]["turns"] == ["客户问石桥附近有没有一室一厅，预算两千左右"]
    assert validate_fixture_payload(loaded) == []


def test_sanitize_text_masks_common_runtime_sensitive_shapes() -> None:
    fake_phone = "1" + "9900000000"
    fake_key = "sk-" + ("z" * 32)
    text = sanitize_text(f"手机号{fake_phone} token={fake_key} 门锁码888888#")

    assert fake_phone not in text
    assert fake_key not in text
    assert "888888#" not in text
    assert "<PHONE>" in text
    assert "<REDACTED_SECRET>" in text
    assert "<VIEWING_PASSWORD>" in text


def test_real_server_dialogue_fixture_is_safe_when_present() -> None:
    if not REAL_DIALOGUE_FIXTURE.exists():
        if os.environ.get("REQUIRE_REAL_DIALOGUE_FIXTURE") == "1":
            pytest.fail(
                "上线级真实对话 fixture 缺失；L4 必须提供 tests/fixtures/qa/real_server_dialogues_sanitized.json，"
                "或显式使用 -AllowMissingRealDialogues。"
            )
        pytest.skip(
            "本地离线单测允许缺少真实对话 fixture；上线级 L4 由 scripts/rag-v2-test-gates.ps1 "
            "强制要求该 fixture，除非显式 -AllowMissingRealDialogues。"
        )

    payload = json.loads(REAL_DIALOGUE_FIXTURE.read_text(encoding="utf-8"))
    errors = validate_fixture_payload(payload)
    turns = [turn for window in payload.get("windows", []) for turn in window.get("turns", [])]

    assert errors == []
    assert turns
    assert sum(1 for char in "\n".join(turns) if "\u4e00" <= char <= "\u9fff") > 20
