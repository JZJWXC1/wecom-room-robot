from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "real_server_dialogues_sanitized.v1"

USER_TEXT_KEYS = {
    "user",
    "user_text",
    "customer_text",
    "incoming_text",
    "question",
    "query",
    "message_text",
    "raw_text",
}
BOT_OR_SYSTEM_KEYS = {
    "bot",
    "reply",
    "reply_text",
    "reply_texts",
    "response",
    "answer",
    "assistant",
    "客服",
    "机器人",
    "system",
    "debug",
    "trace",
    "stage_timings",
}
CONVERSATION_KEYS = {
    "conversation_id",
    "external_userid",
    "external_user_id",
    "openid",
    "unionid",
    "conv",
    "customer_id",
}
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
VIEWING_PASSWORD_RE = re.compile(r"(?<!\d)\d{4,8}#")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|access_token|app_secret|encoding_aes_key|api_key)\b\s*[:=]\s*\S+"
)
API_KEY_RE = re.compile(r"(?i)\b(sk-[a-z0-9_-]{8,}|ak-[a-z0-9_-]{12,})\b")
LONG_IDENTIFIER_RE = re.compile(r"\b[a-zA-Z0-9_-]{32,}\b")
TRANSCRIPT_USER_RE = re.compile(r"^\s*(客户|用户|客人|user|customer)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
TRANSCRIPT_BOT_RE = re.compile(r"^\s*(客服|机器人|assistant|bot|system)\s*[:：]", re.IGNORECASE)


def sanitize_text(text: str, *, max_chars: int = 280) -> str:
    cleaned = " ".join(str(text or "").replace("\u0000", " ").split())
    cleaned = SECRET_ASSIGNMENT_RE.sub("<REDACTED_SECRET>", cleaned)
    cleaned = API_KEY_RE.sub("<REDACTED_SECRET>", cleaned)
    cleaned = PHONE_RE.sub("<PHONE>", cleaned)
    cleaned = VIEWING_PASSWORD_RE.sub("<VIEWING_PASSWORD>", cleaned)
    cleaned = LONG_IDENTIFIER_RE.sub("<ID>", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned.strip()


def sensitive_findings(text: str) -> list[str]:
    findings: list[str] = []
    checks = (
        ("phone", PHONE_RE),
        ("viewing_password", VIEWING_PASSWORD_RE),
        ("secret_assignment", SECRET_ASSIGNMENT_RE),
        ("api_key", API_KEY_RE),
        ("long_identifier", LONG_IDENTIFIER_RE),
    )
    for name, pattern in checks:
        if pattern.search(text):
            findings.append(name)
    return findings


def validate_fixture_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != SCHEMA:
        errors.append("schema")
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return errors + ["windows"]
    for window_index, window in enumerate(windows, start=1):
        turns = window.get("turns") if isinstance(window, dict) else None
        if not isinstance(turns, list) or not turns:
            errors.append(f"windows[{window_index}].turns")
            continue
        for turn_index, text in enumerate(turns, start=1):
            if not isinstance(text, str) or not text.strip():
                errors.append(f"windows[{window_index}].turns[{turn_index}]")
                continue
            for finding in sensitive_findings(text):
                errors.append(f"windows[{window_index}].turns[{turn_index}].{finding}")
    return errors


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _load_json_or_raw(line: str) -> Any:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _extract_transcript_user_lines(text: str) -> list[str]:
    extracted: list[str] = []
    for raw_line in str(text or "").splitlines():
        if TRANSCRIPT_BOT_RE.match(raw_line):
            continue
        match = TRANSCRIPT_USER_RE.match(raw_line)
        if match:
            extracted.append(match.group(2))
    return extracted


def _looks_like_bot_path(path: tuple[str, ...]) -> bool:
    return any(part.lower() in BOT_OR_SYSTEM_KEYS for part in path)


def _message_text_from_wecom_shape(item: dict[str, Any]) -> str:
    msgtype = str(item.get("msgtype") or "").lower()
    if msgtype != "text":
        return ""
    text_payload = item.get("text")
    if isinstance(text_payload, dict):
        return str(text_payload.get("content") or "")
    if isinstance(text_payload, str):
        return text_payload
    return ""


def iter_user_texts(item: Any, path: tuple[str, ...] = ()) -> Iterable[str]:
    if item is None:
        return
    if isinstance(item, str):
        transcript_lines = _extract_transcript_user_lines(item)
        if transcript_lines:
            yield from transcript_lines
        elif not _looks_like_bot_path(path) and path and path[-1].lower() in USER_TEXT_KEYS:
            yield item
        return
    if isinstance(item, list):
        for index, child in enumerate(item):
            yield from iter_user_texts(child, (*path, str(index)))
        return
    if not isinstance(item, dict):
        return

    wecom_text = _message_text_from_wecom_shape(item)
    if wecom_text and not _looks_like_bot_path(path):
        yield wecom_text

    for key, value in item.items():
        key_text = str(key)
        lowered = key_text.lower()
        child_path = (*path, lowered)
        if lowered in BOT_OR_SYSTEM_KEYS:
            continue
        if lowered in USER_TEXT_KEYS and isinstance(value, str):
            yield value
            continue
        if lowered == "text" and isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str) and not _looks_like_bot_path(path):
                yield content
                continue
        yield from iter_user_texts(value, child_path)


def _first_value_for_keys(item: Any, keys: set[str]) -> str:
    if isinstance(item, dict):
        for key, value in item.items():
            lowered = str(key).lower()
            if lowered in keys and isinstance(value, (str, int)):
                return str(value)
            found = _first_value_for_keys(value, keys)
            if found:
                return found
    elif isinstance(item, list):
        for child in item:
            found = _first_value_for_keys(child, keys)
            if found:
                return found
    return ""


def _append_unique_turn(turns: list[str], text: str) -> None:
    if not text or len(text) < 2:
        return
    if turns and turns[-1] == text:
        return
    turns.append(text)


def build_fixture_payload(
    input_path: Path,
    *,
    window_size: int = 10,
    limit_windows: int | None = None,
) -> dict[str, Any]:
    conversations: "OrderedDict[str, list[str]]" = OrderedDict()
    line_count = 0
    extracted_count = 0

    for raw_line in input_path.read_text(encoding="utf-8", errors="replace").splitlines():
        item = _load_json_or_raw(raw_line)
        if item is None:
            continue
        line_count += 1
        raw_conversation = _first_value_for_keys(item, CONVERSATION_KEYS)
        conversation_key = _stable_hash(raw_conversation) if raw_conversation else "unknown"
        turns = conversations.setdefault(conversation_key, [])
        seen_in_event: set[str] = set()
        for raw_text in iter_user_texts(item):
            text = sanitize_text(raw_text)
            if not text or text in seen_in_event:
                continue
            seen_in_event.add(text)
            _append_unique_turn(turns, text)
            extracted_count += 1

    windows: list[dict[str, Any]] = []
    for conversation_hash, turns in conversations.items():
        for offset in range(0, len(turns), window_size):
            selected = turns[offset : offset + window_size]
            if not selected:
                continue
            windows.append(
                {
                    "id": f"real_server_{len(windows) + 1:03d}",
                    "source": "server_log_sanitized",
                    "source_conversation_hash": conversation_hash,
                    "turns": selected,
                }
            )
            if limit_windows is not None and len(windows) >= limit_windows:
                break
        if limit_windows is not None and len(windows) >= limit_windows:
            break

    payload = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_file": input_path.name,
        "source_note": "Sanitized from server dialogue/event logs. Raw ids, phones, viewing passwords, and secrets are not stored.",
        "redaction": {
            "phone": "<PHONE>",
            "viewing_password": "<VIEWING_PASSWORD>",
            "secret": "<REDACTED_SECRET>",
            "long_identifier": "<ID>",
        },
        "line_count": line_count,
        "extracted_turn_count": extracted_count,
        "window_count": len(windows),
        "windows": windows,
    }
    errors = validate_fixture_payload(payload)
    if errors:
        raise RuntimeError("exported fixture failed safety validation: " + ", ".join(errors))
    return payload


def export_fixture(
    input_path: Path,
    output_path: Path,
    *,
    window_size: int = 10,
    limit_windows: int | None = None,
) -> dict[str, Any]:
    payload = build_fixture_payload(
        input_path,
        window_size=window_size,
        limit_windows=limit_windows,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export sanitized server dialogue logs into an offline QA replay fixture."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/qa/real_server_dialogues_sanitized.json"),
    )
    parser.add_argument("--window-size", type=int, default=10)
    parser.add_argument("--limit-windows", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = export_fixture(
        args.input,
        args.output,
        window_size=args.window_size,
        limit_windows=args.limit_windows,
    )
    print(
        "exported "
        f"windows={payload['window_count']} turns={payload['extracted_turn_count']} "
        f"to={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
