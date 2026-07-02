"""Humanized pacing helpers for WeCom customer-service replies.

These helpers do not create facts or choose actions. They only shape when
fragmented user messages are considered ready, and how an already-approved
reply is displayed as customer-visible text bubbles.
"""
from __future__ import annotations

import re
import time
from typing import Any

BUBBLE_SEP = "|||"

_SENT_END = re.compile(r"(?<=[。！？!?~])")
_SOFT_END = re.compile(r"(?<=[；;\n])")

_MD_PATTERNS = (
    (re.compile(r"[*_#`]{1,3}"), ""),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
)


def sanitize_reply(text: str) -> str:
    """Remove lightweight Markdown artifacts before sending plain WeCom text."""
    out = str(text or "")
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return out.strip()


def pending_batch_ready(
    items: list[dict[str, Any]],
    *,
    now: float | None = None,
    debounce_seconds: float = 2.5,
    max_wait_seconds: float = 8.0,
) -> bool:
    """Return true after a quiet window, or after max wait prevents starvation."""
    if not items:
        return False
    ts = time.time() if now is None else now
    created = [float(item.get("created_at") or 0.0) for item in items]
    newest, oldest = max(created), min(created)
    return (ts - newest) >= debounce_seconds or (ts - oldest) >= max_wait_seconds


def merge_pending_text(items: list[dict[str, Any]]) -> str:
    """Merge fragmented pending messages into one text block for understanding."""
    parts = [str(item.get("content") or "").strip() for item in items]
    return "\n".join(part for part in parts if part)


def split_bubbles(
    text: str,
    *,
    max_bubbles: int = 3,
    max_chars: int = 90,
) -> list[str]:
    """Split a final reply into WeCom bubbles without dropping content."""
    max_bubbles = max(1, int(max_bubbles or 1))
    max_chars = max(1, int(max_chars or 1))
    text = sanitize_reply(text)
    if not text:
        return []
    if BUBBLE_SEP in text:
        bubbles: list[str] = []
        for piece in (part.strip() for part in text.split(BUBBLE_SEP)):
            if not piece:
                continue
            bubbles.extend(_split_overlong_piece(piece, max_chars=max_chars))
        return _cap_bubbles(bubbles, max_bubbles=max_bubbles)
    if len(text) <= max_chars:
        return [text]

    bubbles: list[str] = []
    for piece in (part.strip() for part in _SENT_END.split(text) if part.strip()):
        for sub in _split_overlong_piece(piece, max_chars=max_chars):
            _append_or_merge(bubbles, sub, max_chars)
    return _cap_bubbles(bubbles, max_bubbles=max_bubbles)


def _split_overlong_piece(piece: str, *, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]
    soft_parts = [part.strip() for part in _SOFT_END.split(piece) if part.strip()]
    if len(soft_parts) > 1:
        return soft_parts
    return [piece]


def _cap_bubbles(bubbles: list[str], *, max_bubbles: int) -> list[str]:
    if len(bubbles) <= max_bubbles:
        return bubbles
    head = bubbles[: max_bubbles - 1]
    tail = " ".join(bubbles[max_bubbles - 1 :])
    return head + [tail]


def _append_or_merge(bubbles: list[str], piece: str, max_chars: int) -> None:
    if bubbles and len(bubbles[-1]) + len(piece) + 1 <= max_chars:
        if bubbles[-1].endswith(("。", "！", "？", "!", "?", "~")):
            bubbles[-1] = f"{bubbles[-1]}{piece}"
        else:
            bubbles[-1] = f"{bubbles[-1]} {piece}"
        return
    bubbles.append(piece)


def typing_delay_seconds(
    text: str,
    *,
    index: int = 0,
    base: float = 0.6,
    per_char: float = 0.05,
    cap: float = 3.5,
) -> float:
    """Estimate a conservative typing delay for an already-approved bubble."""
    think = 0.8 if index == 0 else 0.0
    return min(float(cap), float(base) + think + float(per_char) * len(str(text or "")))
