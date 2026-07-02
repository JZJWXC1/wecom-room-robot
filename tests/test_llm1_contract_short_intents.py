from __future__ import annotations

import app.main as main


def _assert_missing_action_retry(content: str, missing_action: str) -> None:
    needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
        content,
        {"actions": ["generate_reply"]},
    )

    assert needs_retry
    assert missing_action in reason


def test_inventory_sheet_short_explicit_requests_require_send_inventory_sheet() -> None:
    for content in ("房源表", "表发一下"):
        _assert_missing_action_retry(content, "send_inventory_sheet")


def test_deposit_short_explicit_request_requires_send_deposit_policy() -> None:
    _assert_missing_action_retry("能免押吗", "send_deposit_policy")


def test_inventory_field_request_rejects_media_missing_actions() -> None:
    needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
        "这个水电",
        {"actions": ["search_inventory", "context_tools", "explain_missing_media", "generate_reply"]},
    )

    assert needs_retry
    assert "must not include" in reason


def test_viewing_request_rejects_media_missing_actions() -> None:
    needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
        "看房密码多少，今天可以看吗？",
        {"actions": ["search_inventory", "context_tools", "explain_missing_media", "generate_reply"]},
    )

    assert needs_retry
    assert "must not include" in reason


def test_contract_booking_and_deposit_payment_requests_require_contract_contact() -> None:
    for content in ("合同联系方式", "订房联系方式", "交定金联系方式"):
        _assert_missing_action_retry(content, "send_contract_contact")


def test_password_and_today_viewing_requests_require_unavailable_viewing_explanation() -> None:
    for content in ("密码多少", "今天能看吗", "客户想约今天晚上看", "约个时间看下可以吗"):
        _assert_missing_action_retry(content, "explain_unavailable_viewing")


def test_short_acknowledgements_cannot_inherit_inventory_sheet_or_video_actions() -> None:
    for content, inherited_action in (
        ("嗯", "send_inventory_sheet"),
        ("好的", "send_video"),
        ("谢谢，嗯", "send_video"),
        ("好的谢谢", "send_inventory_sheet"),
    ):
        needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
            content,
            {"actions": [inherited_action, "generate_reply"]},
        )

        assert needs_retry
        assert "Short acknowledgement" in reason


def test_short_acknowledgement_without_send_action_does_not_retry() -> None:
    for content in ("嗯", "谢谢，嗯", "好的谢谢"):
        needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
            content,
            {"actions": ["generate_reply"]},
        )

        assert not needs_retry
        assert reason == ""


def test_short_acknowledgement_does_not_swallow_real_media_request() -> None:
    needs_retry, reason = main._llm1_tool_plan_needs_contract_retry(
        "谢谢，视频也发我",
        {"actions": ["generate_reply"]},
    )

    assert needs_retry
    assert "send_video" in reason
