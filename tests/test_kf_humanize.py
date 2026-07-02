from __future__ import annotations

from app.services.kf_humanize import (
    BUBBLE_SEP,
    merge_pending_text,
    pending_batch_ready,
    sanitize_reply,
    split_bubbles,
    typing_delay_seconds,
)


def test_pending_batch_ready_waits_for_silence() -> None:
    now = 1000.0
    items = [
        {"msgid": "a", "content": "东新园", "created_at": now - 5.0},
        {"msgid": "b", "content": "有两室吗", "created_at": now - 1.0},
    ]

    assert pending_batch_ready(items, now=now, debounce_seconds=2.5) is False
    assert pending_batch_ready(items, now=now + 1.6, debounce_seconds=2.5) is True


def test_pending_batch_ready_max_wait_prevents_starvation() -> None:
    now = 1000.0
    items = [
        {"msgid": "a", "content": "在吗", "created_at": now - 9.0},
        {"msgid": "b", "content": "价格多少", "created_at": now - 0.1},
    ]

    assert pending_batch_ready(items, now=now, max_wait_seconds=8.0) is True


def test_merge_pending_text_joins_fragments() -> None:
    items = [
        {"content": "东新园"},
        {"content": ""},
        {"content": "预算4000左右 两室"},
    ]

    assert merge_pending_text(items) == "东新园\n预算4000左右 两室"


def test_split_bubbles_short_text_single_bubble() -> None:
    assert split_bubbles("好的，稍等哈") == ["好的，稍等哈"]


def test_split_bubbles_explicit_separator() -> None:
    text = f"长浜龙吟轩11-1603在的{BUBBLE_SEP}押二付一3800，整租两室{BUBBLE_SEP}要看视频吗"

    assert split_bubbles(text) == ["长浜龙吟轩11-1603在的", "押二付一3800，整租两室", "要看视频吗"]


def test_split_bubbles_sentence_fallback_and_cap() -> None:
    text = "这套是整租两室一厅，65方精装。押二付一3800一个月，押一付一4000。支持芝麻信用免押金~水电是民水民电。空出时间是7月8号，看房要提前跟我说。还有别的想了解的吗？"

    bubbles = split_bubbles(text, max_bubbles=3, max_chars=45)

    assert 1 <= len(bubbles) <= 3
    assert "".join(bubbles).replace(" ", "") == text.replace(" ", "")


def test_split_bubbles_strips_markdown() -> None:
    assert split_bubbles("**押二付一3800**，`民水民电`") == ["押二付一3800，民水民电"]


def test_split_bubbles_splits_overlong_first_piece_on_soft_break() -> None:
    text = "押二付一3800一个月；押一付一4000一个月；水电民水民电"

    bubbles = split_bubbles(text, max_bubbles=3, max_chars=12)

    assert len(bubbles) == 3
    assert "".join(bubbles).replace(" ", "") == text.replace(" ", "")


def test_sanitize_reply_removes_list_markers() -> None:
    assert sanitize_reply("- 押二付一3800\n- 民水民电") == "押二付一3800\n民水民电"


def test_typing_delay_monotonic_and_capped() -> None:
    short = typing_delay_seconds("好的", index=1)
    long = typing_delay_seconds("这套是整租两室一厅精装修带阳台朝南采光很好" * 3, index=1)

    assert short < long <= 3.5
    assert typing_delay_seconds("好的", index=0) > short
