from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from app.services import kf_send_receipts
from app.services.kf_contracts import SendAction, SendReceipt, safe_artifact_payload


OUTBOX_SCHEMA_VERSION = "kf_outbox.v1"
OUTBOX_RECORD_RESERVED = "reserved"
OUTBOX_RECORD_RECEIPT = "receipt"
DEFAULT_OUTBOX_PATH = Path("data/kf_send_outbox.jsonl")

_OUTBOX_INTERNAL_KEYS = {"idempotency_key", "outbox_id", "receipt_id"}
_RECEIPT_INTERNAL_KEYS = {"duplicate_of", "idempotency_key", "receipt_id"}
_BLOCKING_RECEIPT_STATUSES = {
    kf_send_receipts.SENT_STATUS,
    kf_send_receipts.SEND_UNCERTAIN_STATUS,
}
_TERMINAL_RECEIPT_STATUSES = {
    kf_send_receipts.SENT_STATUS,
    kf_send_receipts.FAILED_STATUS,
    kf_send_receipts.SEND_UNCERTAIN_STATUS,
}
_UNCERTAIN_ERROR_CLASS_MARKERS = (
    "connecterror",
    "networkerror",
    "pooltimeout",
    "readerror",
    "remoteprotocolerror",
    "timeout",
    "writeerror",
)
_UNCERTAIN_ERROR_TEXT_MARKERS = (
    "connection aborted",
    "connection reset",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "remote protocol",
    "server disconnected",
    "timed out",
    "timeout",
)


@dataclass(frozen=True)
class OutboxDecision:
    should_send: bool
    idempotency_key: str
    outbox_id: str = ""
    attempt: int = 1
    reason: str = ""
    duplicate_of: str = ""
    existing_receipt: SendReceipt | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxDiagnostics:
    path: str
    record_count: int = 0
    corruption_count: int = 0
    corrupted_line_numbers: tuple[int, ...] = ()
    fail_closed: bool = False

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "record_count": self.record_count,
            "corruption_count": self.corruption_count,
            "corrupted_line_numbers": list(self.corrupted_line_numbers),
            "fail_closed": self.fail_closed,
        }


class LocalKfOutboxLedger:
    def __init__(self, path: Path | str | None = None) -> None:
        configured = path or os.environ.get("KF_SEND_OUTBOX_PATH") or DEFAULT_OUTBOX_PATH
        self.path = Path(configured)
        self._process_lock = threading.RLock()
        self._last_diagnostics = OutboxDiagnostics(path=str(self.path))
        # 增量缓存:reserve 只解析上次同步后新追加的字节,避免每个出站动作
        # 全量重读台账(台账随运行时间线性增长,全量重读会拖垮发送阶段)。
        self._cache_offset = 0
        self._cache_line_number = 0
        self._cache_signature: tuple[int, int] | None = None
        self._cache_key_records: dict[str, list[dict[str, Any]]] = {}
        self._cache_record_count = 0
        self._cache_corrupted_line_numbers: list[int] = []
        self._cache_full_resync_count = 0

    def reserve(self, action: SendAction, *, idempotency_key: str | None = None) -> OutboxDecision:
        key = idempotency_key or kf_send_receipts.build_idempotency_key(action)
        with self._locked_records():
            self._sync_cache_unlocked()
            diagnostics = self._last_diagnostics
            if diagnostics.corruption_count:
                return OutboxDecision(
                    should_send=False,
                    idempotency_key=key,
                    reason="outbox_corruption_blocks_send",
                    metadata={
                        "blocking_status": "outbox_corruption",
                        **diagnostics.to_safe_dict(),
                    },
                )
            state = _summarize_key(self._cache_key_records.get(key) or [], key)
            blocking = state.get("blocking_receipt")
            if isinstance(blocking, SendReceipt):
                return OutboxDecision(
                    should_send=False,
                    idempotency_key=key,
                    reason="receipt_blocks_duplicate",
                    duplicate_of=blocking.receipt_id,
                    existing_receipt=blocking,
                    metadata={"blocking_status": blocking.status},
                )
            pending = state.get("pending_record")
            if isinstance(pending, dict):
                return OutboxDecision(
                    should_send=False,
                    idempotency_key=key,
                    reason="pending_outbox_blocks_duplicate",
                    duplicate_of=str(pending.get("outbox_id") or ""),
                    attempt=_coerce_attempt(pending.get("attempt")),
                    metadata={"blocking_status": "pending_outbox"},
                )

            attempt = _coerce_attempt(state.get("attempt_count")) + 1
            outbox_id = _outbox_id(key, action.action_id, attempt)
            self._append_record_unlocked(_reserved_record(action, key, outbox_id, attempt))
            return OutboxDecision(
                should_send=True,
                idempotency_key=key,
                outbox_id=outbox_id,
                attempt=attempt,
                reason="reserved",
            )

    def record_receipt(
        self,
        receipt: SendReceipt,
        *,
        action: SendAction | None = None,
        idempotency_key: str | None = None,
        outbox_id: str = "",
    ) -> None:
        key = idempotency_key or receipt.idempotency_key or (kf_send_receipts.build_idempotency_key(action) if action else "")
        with self._locked_records():
            self._append_record_unlocked(_receipt_record(receipt, key, outbox_id=outbox_id, action=action))

    def records(self) -> list[dict[str, Any]]:
        with self._locked_records():
            return self._read_records_unlocked()

    def diagnostics(self) -> OutboxDiagnostics:
        with self._locked_records():
            self._read_records_unlocked()
            return self._last_diagnostics

    @contextmanager
    def _locked_records(self) -> Iterator[None]:
        with self._process_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f"{self.path.name}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_handle:
                _lock_file(lock_handle)
                try:
                    yield
                finally:
                    _unlock_file(lock_handle)

    def _reset_cache_unlocked(self) -> None:
        self._cache_offset = 0
        self._cache_line_number = 0
        self._cache_key_records = {}
        self._cache_record_count = 0
        self._cache_corrupted_line_numbers = []

    def _sync_cache_unlocked(self) -> None:
        # 台账为加锁追加写,持锁期间按字节偏移续读是安全的;文件被替换
        # (dev/ino 变化)或变短(截断/轮转)时整体重建,保证跨进程一致。
        if not self.path.exists():
            self._reset_cache_unlocked()
            self._cache_signature = None
            self._last_diagnostics = OutboxDiagnostics(path=str(self.path))
            return
        stat = self.path.stat()
        signature = (stat.st_dev, stat.st_ino)
        if signature != self._cache_signature or stat.st_size < self._cache_offset:
            self._reset_cache_unlocked()
            self._cache_signature = signature
            self._cache_full_resync_count += 1
        pending_tail_corruption: list[int] = []
        if stat.st_size > self._cache_offset:
            with self.path.open("rb") as handle:
                handle.seek(self._cache_offset)
                chunk = handle.read()
            complete = chunk
            if complete and not complete.endswith(b"\n"):
                # 写入方持锁完成整行写入后才释放,残缺尾行只可能来自写入中
                # 途崩溃;按损坏行 fail-closed,不消费偏移,等待人工修复。
                cut = complete.rfind(b"\n") + 1
                complete = complete[:cut]
                pending_tail_corruption.append(
                    self._cache_line_number + complete.count(b"\n") + 1
                )
            if complete:
                self._consume_cache_lines_unlocked(complete)
                self._cache_offset += len(complete)
        corrupted = list(self._cache_corrupted_line_numbers) + pending_tail_corruption
        self._last_diagnostics = OutboxDiagnostics(
            path=str(self.path),
            record_count=self._cache_record_count,
            corruption_count=len(corrupted),
            corrupted_line_numbers=tuple(corrupted),
            fail_closed=bool(corrupted),
        )

    def _consume_cache_lines_unlocked(self, chunk: bytes) -> None:
        for raw_line in chunk.split(b"\n")[:-1]:
            self._cache_line_number += 1
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                self._cache_corrupted_line_numbers.append(self._cache_line_number)
                continue
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self._cache_corrupted_line_numbers.append(self._cache_line_number)
                continue
            if not isinstance(record, dict):
                continue
            safe_record = _safe_outbox_record(record)
            if not isinstance(safe_record, dict):
                continue
            self._cache_record_count += 1
            key = str(safe_record.get("idempotency_key") or "")
            self._cache_key_records.setdefault(key, []).append(_trimmed_record(safe_record))

    def _read_records_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            self._last_diagnostics = OutboxDiagnostics(path=str(self.path))
            return []
        records: list[dict[str, Any]] = []
        corrupted_line_numbers: list[int] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    corrupted_line_numbers.append(line_number)
                    continue
                if isinstance(record, dict):
                    records.append(_safe_outbox_record(record))
        safe_records = [record for record in records if isinstance(record, dict)]
        self._last_diagnostics = OutboxDiagnostics(
            path=str(self.path),
            record_count=len(safe_records),
            corruption_count=len(corrupted_line_numbers),
            corrupted_line_numbers=tuple(corrupted_line_numbers),
            fail_closed=bool(corrupted_line_numbers),
        )
        return safe_records

    def _append_record_unlocked(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe_record = _safe_outbox_record(record)
        line = json.dumps(safe_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def send_error_is_uncertain(error: BaseException) -> bool:
    name = error.__class__.__name__.lower()
    if any(marker in name for marker in _UNCERTAIN_ERROR_CLASS_MARKERS):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _UNCERTAIN_ERROR_TEXT_MARKERS)


def _reserved_record(action: SendAction, key: str, outbox_id: str, attempt: int) -> dict[str, Any]:
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "record_type": OUTBOX_RECORD_RESERVED,
        "outbox_id": outbox_id,
        "idempotency_key": key,
        "attempt": attempt,
        "status": "pending",
        "created_at": _utc_now(),
        "created_at_epoch": time.time(),
        "action": _action_binding(action),
    }


def _receipt_record(
    receipt: SendReceipt,
    key: str,
    *,
    outbox_id: str = "",
    action: SendAction | None = None,
) -> dict[str, Any]:
    payload = receipt.to_ledger_dict()
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "record_type": OUTBOX_RECORD_RECEIPT,
        "outbox_id": outbox_id,
        "idempotency_key": key or receipt.idempotency_key,
        "receipt_id": receipt.receipt_id,
        "status": receipt.status,
        "created_at": _utc_now(),
        "receipt": payload,
        "action": _action_binding(action) if action else {},
    }


def _action_binding(action: SendAction) -> dict[str, Any]:
    return safe_artifact_payload(
        {
            "conversation_id": action.conversation_id,
            "turn_id": action.turn_id,
            "action_id": action.action_id,
            "action_type": action.action_type,
            "listing_id": action.listing_id,
            "evidence_id": action.evidence_id,
            "inventory_snapshot_id": action.inventory_snapshot_id,
            "candidate_set_id": action.candidate_set_id,
            "source_hash": action.source_hash,
            "material_hash": kf_send_receipts.material_hash(action.media_id) if action.media_id else "",
            "sha256": action.sha256,
            "payload_hash": kf_send_receipts.payload_hash(action),
            "turn_scope_source": action.metadata.get("turn_scope_source"),
            "turn_scope_id": action.metadata.get("turn_scope_id"),
        }
    )


def _summarize_key(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    key_records = [record for record in records if str(record.get("idempotency_key") or "") == key]
    terminal_by_outbox: dict[str, str] = {}
    blocking_receipt: SendReceipt | None = None
    attempt_count = 0

    for record in key_records:
        attempt_count = max(attempt_count, _coerce_attempt(record.get("attempt")))
        if str(record.get("record_type") or "") != OUTBOX_RECORD_RECEIPT:
            continue
        status = str(record.get("status") or "").strip()
        outbox_id = str(record.get("outbox_id") or "").strip()
        if outbox_id and status in _TERMINAL_RECEIPT_STATUSES:
            terminal_by_outbox[outbox_id] = status
        receipt_payload = record.get("receipt")
        if status in _BLOCKING_RECEIPT_STATUSES and isinstance(receipt_payload, dict):
            blocking_receipt = SendReceipt.from_legacy_dict(receipt_payload)

    pending_record: dict[str, Any] | None = None
    for record in key_records:
        if str(record.get("record_type") or "") != OUTBOX_RECORD_RESERVED:
            continue
        outbox_id = str(record.get("outbox_id") or "").strip()
        if outbox_id and outbox_id in terminal_by_outbox:
            continue
        pending_record = record
        attempt_count = max(attempt_count, _coerce_attempt(record.get("attempt")))

    return {
        "attempt_count": attempt_count,
        "blocking_receipt": blocking_receipt,
        "pending_record": pending_record,
    }


def _outbox_id(idempotency_key: str, action_id: str, attempt: int) -> str:
    digest = _stable_digest({"idempotency_key": idempotency_key, "action_id": action_id, "attempt": attempt})
    return f"outbox:{digest[:24]}"


_TRIMMED_RECORD_KEYS = ("idempotency_key", "record_type", "status", "outbox_id", "attempt")


def _trimmed_record(record: dict[str, Any]) -> dict[str, Any]:
    # 常驻缓存只保留 _summarize_key 及其下游消费的字段;receipt 载荷只在
    # 阻断状态(SENT/UNCERTAIN)时需要还原 SendReceipt,其余一律丢弃。
    trimmed = {key: record.get(key) for key in _TRIMMED_RECORD_KEYS if key in record}
    if (
        str(record.get("record_type") or "") == OUTBOX_RECORD_RECEIPT
        and str(record.get("status") or "").strip() in _BLOCKING_RECEIPT_STATUSES
        and isinstance(record.get("receipt"), dict)
    ):
        trimmed["receipt"] = record["receipt"]
    return trimmed


def _safe_outbox_record(record: dict[str, Any]) -> dict[str, Any]:
    safe = safe_artifact_payload(record)
    if not isinstance(safe, dict):
        return {}
    for key in _OUTBOX_INTERNAL_KEYS:
        value = str(record.get(key) or "").strip()
        if value:
            safe[key] = value
    receipt = record.get("receipt")
    safe_receipt = safe.get("receipt")
    if isinstance(receipt, dict) and isinstance(safe_receipt, dict):
        for key in _RECEIPT_INTERNAL_KEYS:
            value = str(receipt.get(key) or "").strip()
            if value:
                safe_receipt[key] = value
        safe["receipt"] = safe_receipt
    return safe


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _coerce_attempt(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
