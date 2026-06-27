from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from app.services.kf_contracts import SendAction, SendReceipt, safe_artifact_payload


SEND_RECEIPT_SCHEMA_VERSION = "kf_send_receipts.v1"
SEND_RECEIPT_CONTEXT_KEY = "send_receipts"
SEND_RECEIPT_CONTEXT_LIMIT = 100
SENT_STATUS = "sent"
FAILED_STATUS = "failed"
SKIPPED_DUPLICATE_STATUS = "skipped_duplicate"
_SUCCESS_STATUSES = {SENT_STATUS}
_LEDGER_INTERNAL_MATCH_KEYS = {"receipt_id", "idempotency_key", "duplicate_of"}
_SENSITIVE_ERROR_MARKERS = (
    "access_token",
    "authorization",
    "corpsecret",
    "credential",
    "secret",
    "signature",
    "token",
    "鉴权",
    "凭证",
    "密钥",
)
_AUTH_ERROR_MARKERS = (
    "auth",
    "credential",
    "forbidden",
    "permission",
    "unauthorized",
    "鉴权",
    "权限",
    "凭证",
)


def build_send_action(
    *,
    open_kfid: str,
    external_userid: str,
    context: dict[str, Any] | None,
    action_id: str,
    action_type: str,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    msgids: list[str] | None = None,
    listing_id: str = "",
    evidence_id: str = "",
    inventory_snapshot_id: str = "",
    source_hash: str = "",
    candidate_set_id: str = "",
    media_id: str = "",
    sha256: str = "",
) -> SendAction:
    scope = current_turn_scope(context, msgids=msgids)
    conversation_id = _conversation_digest(open_kfid, external_userid)
    payload = dict(payload or {})
    metadata = dict(metadata or {})
    source_hash = _first_text(source_hash, metadata.get("source_hash"), payload.get("source_hash"))
    media_id = _first_text(media_id, metadata.get("media_id"), payload.get("media_id"))
    sha256 = _first_text(sha256, metadata.get("sha256"), payload.get("sha256"))
    action_metadata = {
        "idempotency_profile": SEND_RECEIPT_SCHEMA_VERSION,
        "turn_scope_source": scope["source"],
        "turn_scope_id": scope["scope_id"],
        **metadata,
    }
    return SendAction(
        conversation_id=conversation_id,
        turn_id=scope["turn_id"],
        listing_id=listing_id,
        evidence_id=evidence_id,
        inventory_snapshot_id=inventory_snapshot_id,
        source_hash=source_hash,
        candidate_set_id=candidate_set_id,
        media_id=media_id,
        sha256=sha256,
        action_id=action_id,
        action_type=action_type,
        payload=payload,
        metadata=action_metadata,
    )


def current_turn_scope(context: dict[str, Any] | None, *, msgids: list[str] | None = None) -> dict[str, str]:
    clean_msgids = sorted({str(item).strip() for item in msgids or [] if str(item).strip()})
    if not clean_msgids and isinstance(context, dict):
        memory = context.get("structured_memory")
        if isinstance(memory, dict):
            current_turn_id = str(memory.get("current_turn_id") or "").strip()
            for record in reversed([item for item in memory.get("turn_records") or [] if isinstance(item, dict)]):
                if current_turn_id and str(record.get("turn_id") or "").strip() != current_turn_id:
                    continue
                clean_msgids = sorted({str(item).strip() for item in record.get("msgids") or [] if str(item).strip()})
                if clean_msgids:
                    break
    if clean_msgids:
        scope_id = _stable_digest({"msgids": clean_msgids})
        return {"source": "msgids", "scope_id": scope_id, "turn_id": f"msgids:{scope_id}"}

    turn_id = ""
    if isinstance(context, dict):
        memory = context.get("structured_memory")
        if isinstance(memory, dict):
            turn_id = str(memory.get("current_turn_id") or "").strip()
    if turn_id:
        return {"source": "turn_id", "scope_id": _stable_digest({"turn_id": turn_id}), "turn_id": turn_id}
    return {"source": "unspecified", "scope_id": "unspecified", "turn_id": "unspecified"}


def build_idempotency_key(action: SendAction, *, channel: str = "wecom_kf") -> str:
    payload = {
        "profile": SEND_RECEIPT_SCHEMA_VERSION,
        "channel": channel,
        "conversation_id": action.conversation_id,
        "turn_id": action.turn_id,
        "action_id": action.action_id,
        "action_type": action.action_type,
        "listing_id": action.listing_id,
        "evidence_id": action.evidence_id,
        "inventory_snapshot_id": action.inventory_snapshot_id,
        "source_hash": _action_fact_value(action, "source_hash"),
        "candidate_set_id": action.candidate_set_id,
        "media_id": _action_fact_value(action, "media_id"),
        "sha256": _action_fact_value(action, "sha256"),
        "payload": safe_artifact_payload(action.payload),
        "metadata": {
            "turn_scope_source": action.metadata.get("turn_scope_source"),
            "turn_scope_id": action.metadata.get("turn_scope_id"),
            "material_hash": action.metadata.get("material_hash"),
            "text_hash": action.metadata.get("text_hash"),
        },
    }
    return f"send:{_stable_digest(payload)}"


def find_successful_receipt(context: dict[str, Any] | None, idempotency_key: str) -> SendReceipt | None:
    for item in reversed(_receipt_payloads(context)):
        if str(item.get("idempotency_key") or "") != idempotency_key:
            continue
        if str(item.get("status") or "") in _SUCCESS_STATUSES:
            return SendReceipt.from_legacy_dict(item)
    return None


def build_sent_receipt(
    action: SendAction,
    *,
    idempotency_key: str | None = None,
    provider_result: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SendReceipt:
    key = idempotency_key or build_idempotency_key(action)
    result = dict(provider_result or {})
    return SendReceipt(
        conversation_id=action.conversation_id,
        turn_id=action.turn_id,
        listing_id=action.listing_id,
        evidence_id=action.evidence_id,
        inventory_snapshot_id=action.inventory_snapshot_id,
        source_hash=action.source_hash,
        candidate_set_id=action.candidate_set_id,
        media_id=action.media_id,
        sha256=action.sha256,
        action_id=action.action_id,
        action_type=action.action_type,
        status=SENT_STATUS,
        receipt_id=_receipt_id(key, action.action_id, SENT_STATUS),
        idempotency_key=key,
        sent_at=_utc_now(),
        provider_message_id=_provider_message_id(result),
        send_result=result,
        metadata=_receipt_metadata(action, metadata),
    )


def build_failed_receipt(
    action: SendAction,
    *,
    error: BaseException,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SendReceipt:
    key = idempotency_key or build_idempotency_key(action)
    return SendReceipt(
        conversation_id=action.conversation_id,
        turn_id=action.turn_id,
        listing_id=action.listing_id,
        evidence_id=action.evidence_id,
        inventory_snapshot_id=action.inventory_snapshot_id,
        source_hash=action.source_hash,
        candidate_set_id=action.candidate_set_id,
        media_id=action.media_id,
        sha256=action.sha256,
        action_id=action.action_id,
        action_type=action.action_type,
        status=FAILED_STATUS,
        receipt_id=_receipt_id(key, action.action_id, FAILED_STATUS),
        idempotency_key=key,
        error_code=error.__class__.__name__,
        error_message=safe_failure_reason(error),
        metadata=_receipt_metadata(action, metadata),
    )


def build_duplicate_receipt(action: SendAction, existing: SendReceipt, *, idempotency_key: str | None = None) -> SendReceipt:
    key = idempotency_key or existing.idempotency_key or build_idempotency_key(action)
    return SendReceipt(
        conversation_id=action.conversation_id,
        turn_id=action.turn_id,
        listing_id=action.listing_id,
        evidence_id=action.evidence_id,
        inventory_snapshot_id=action.inventory_snapshot_id,
        source_hash=action.source_hash,
        candidate_set_id=action.candidate_set_id,
        media_id=action.media_id,
        sha256=action.sha256,
        action_id=action.action_id,
        action_type=action.action_type,
        status=SKIPPED_DUPLICATE_STATUS,
        receipt_id=_receipt_id(key, action.action_id, SKIPPED_DUPLICATE_STATUS),
        idempotency_key=key,
        duplicate_of=existing.receipt_id,
        sent_at=_utc_now(),
        metadata=_receipt_metadata(action, {"duplicate_of": existing.receipt_id}),
    )


def append_receipt(context: dict[str, Any] | None, receipt: SendReceipt) -> dict[str, Any]:
    current = context or {}
    ledger = normalize_receipt_ledger(current)
    receipts = list(ledger["receipts"])
    receipts.append(receipt.to_ledger_dict())
    ledger["receipts"] = receipts[-SEND_RECEIPT_CONTEXT_LIMIT:]
    current[SEND_RECEIPT_CONTEXT_KEY] = ledger
    return current


def normalize_receipt_ledger(context: dict[str, Any] | None) -> dict[str, Any]:
    ledger = (context or {}).get(SEND_RECEIPT_CONTEXT_KEY)
    if not isinstance(ledger, dict):
        return {"schema_version": SEND_RECEIPT_SCHEMA_VERSION, "receipts": []}
    receipts = [
        _safe_ledger_receipt_payload(item)
        for item in ledger.get("receipts") or []
        if isinstance(item, dict)
    ]
    return {
        "schema_version": str(ledger.get("schema_version") or SEND_RECEIPT_SCHEMA_VERSION),
        "receipts": receipts[-SEND_RECEIPT_CONTEXT_LIMIT:],
    }


def material_hash(value: Any) -> str:
    return _stable_digest({"material": str(value or "")})


def text_hash(value: str) -> str:
    return _stable_digest({"text": value or ""})


def safe_failure_reason(error: BaseException | str) -> str:
    text = str(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_ERROR_MARKERS):
        if any(marker in lowered for marker in _AUTH_ERROR_MARKERS):
            return "provider_auth_error_redacted"
        return "provider_sensitive_error_redacted"
    return str(safe_artifact_payload(text))


def _receipt_payloads(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for item in normalize_receipt_ledger(context).get("receipts") or [] if isinstance(item, dict)]


def _safe_ledger_receipt_payload(item: dict[str, Any]) -> dict[str, Any]:
    safe = safe_artifact_payload(item)
    if not isinstance(safe, dict):
        return {}
    for key in _LEDGER_INTERNAL_MATCH_KEYS:
        value = str(item.get(key) or "").strip()
        if value:
            safe[key] = value
    return safe


def _receipt_metadata(action: SendAction, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return safe_artifact_payload(
        {
            "idempotency_profile": SEND_RECEIPT_SCHEMA_VERSION,
            "turn_scope_source": action.metadata.get("turn_scope_source"),
            "turn_scope_id": action.metadata.get("turn_scope_id"),
            "source_hash": action.source_hash,
            "media_id": action.media_id,
            "sha256": action.sha256,
            **dict(metadata or {}),
        }
    )


def _conversation_digest(open_kfid: str, external_userid: str) -> str:
    return f"kf:{_stable_digest({'open_kfid': open_kfid, 'external_userid': external_userid})[:16]}"


def _receipt_id(idempotency_key: str, action_id: str, status: str) -> str:
    return f"rcpt:{_stable_digest({'key': idempotency_key, 'action_id': action_id, 'status': status})[:24]}"


def _provider_message_id(result: dict[str, Any]) -> str:
    for key in ("msgid", "message_id", "provider_message_id"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return ""


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _action_fact_value(action: SendAction, key: str) -> str:
    return _first_text(
        getattr(action, key, ""),
        action.metadata.get(key),
        action.payload.get(key),
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
