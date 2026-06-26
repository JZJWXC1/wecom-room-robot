from __future__ import annotations

from enum import Enum
import re
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "kf_rag_contracts.v1"
ORCHESTRATOR_SHADOW_SCHEMA_VERSION = "rag_v2_orchestrator_shadow.v1"
REDACTED = "[REDACTED]"
SENSITIVE_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "phone",
    "mobile",
    "viewing",
    "raw_viewing",
    "private",
    "看房方式密码",
    "看房密码",
    "密码",
    "手机号",
)
SAFE_OUTPUT_OMIT_KEYS = {"raw_tool_result", "sensitive_metadata", "sensitive_payload"}
SAFE_ARTIFACT_OMIT_KEYS = SAFE_OUTPUT_OMIT_KEYS | {
    "raw_customer_content",
    "customer_content",
    "message_content",
    "original_content",
    "raw_message",
}
ORCHESTRATOR_SHADOW_TOP_LEVEL_KEYS = (
    "schema_version",
    "mode",
    "artifact_id",
    "created_at",
    "baseline_commit",
    "turn",
    "inventory_read",
    "legacy_pipeline",
    "shadow_a",
    "integration_notes",
)
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PASSWORD_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{3,8}#(?![A-Za-z0-9])")
TOKEN_CONTEXT_PATTERN = re.compile(r"\b(token|secret|password)\s*[:=]\s*[^\s,;]+", re.IGNORECASE)


class ConstraintOperation(str, Enum):
    INHERIT = "inherit"
    REPLACE = "replace"
    EXCLUDE = "exclude"
    CLEAR = "clear"


class ResponseStrategy(str, Enum):
    ANSWER = "answer"
    ASK_CLARIFICATION = "ask_clarification"
    TOOL_FIRST = "tool_first"
    SEND_MEDIA = "send_media"
    HANDOFF = "handoff"
    SAFE_FALLBACK = "safe_fallback"
    RETRY = "retry"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=True)

    schema_version: str = SCHEMA_VERSION
    prompt_version: str = ""
    conversation_id: str = ""
    turn_id: str = ""
    case_id: str = ""
    audience: str = "broker"
    inventory_snapshot_id: str = ""
    candidate_set_id: str = ""
    listing_id: str = ""
    evidence_id: str = ""
    legacy_unknown_fields: dict[str, Any] = Field(default_factory=dict, repr=False)

    legacy_field_aliases: ClassVar[dict[str, str]] = {}

    @field_validator(
        "schema_version",
        "prompt_version",
        "conversation_id",
        "turn_id",
        "case_id",
        "audience",
        "inventory_snapshot_id",
        "candidate_set_id",
        "listing_id",
        "evidence_id",
        mode="before",
    )
    @classmethod
    def _coerce_string(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @classmethod
    def from_legacy_dict(cls, payload: dict[str, Any]) -> Self:
        if not isinstance(payload, dict):
            raise TypeError(f"{cls.__name__}.from_legacy_dict expects dict")
        known_fields = set(cls.model_fields)
        aliases = dict(cls.legacy_field_aliases)
        data: dict[str, Any] = {}
        unknown: dict[str, Any] = {}
        for key, value in payload.items():
            field_name = aliases.get(str(key), str(key))
            if field_name in known_fields and field_name != "legacy_unknown_fields":
                data[field_name] = value
            else:
                unknown[str(key)] = _redact_sensitive(value, key=str(key))
        if unknown:
            data["legacy_unknown_fields"] = unknown
        return cls.model_validate(data)

    def to_legacy_dict(self) -> dict[str, Any]:
        return self.to_safe_dict()

    def to_safe_dict(self) -> dict[str, Any]:
        return _redact_sensitive(_omit_non_loggable(self.model_dump(mode="json")))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.to_safe_dict()!r})"

    __str__ = __repr__


class TaskAtom(ContractModel):
    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "id": "task_id",
        "type": "task_type",
        "intent": "task_type",
        "text": "user_text",
        "strategy": "response_strategy",
        "operation": "constraint_operation",
    }

    task_id: str
    task_type: str
    user_text: str = ""
    constraint_operation: ConstraintOperation = ConstraintOperation.INHERIT
    constraints: dict[str, Any] = Field(default_factory=dict)
    response_strategy: ResponseStrategy = ResponseStrategy.TOOL_FIRST
    depends_on_task_ids: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)

    @field_validator("task_id", "task_type", mode="before")
    @classmethod
    def _require_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("constraints", mode="before")
    @classmethod
    def _safe_constraints(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))


class StructuredTaskPacket(ContractModel):
    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "strategy": "response_strategy",
        "structured_tasks": "tasks",
        "task_atoms": "tasks",
    }

    response_strategy: ResponseStrategy = ResponseStrategy.TOOL_FIRST
    tasks: list[TaskAtom] = Field(default_factory=list)
    inherited_constraints: dict[str, Any] = Field(default_factory=dict)
    replaced_constraints: dict[str, Any] = Field(default_factory=dict)
    excluded_constraints: dict[str, Any] = Field(default_factory=dict)
    cleared_constraint_keys: list[str] = Field(default_factory=list)
    rewritten_query: str = ""

    @model_validator(mode="after")
    def _must_have_task(self) -> Self:
        if not self.tasks:
            raise ValueError("StructuredTaskPacket requires at least one task")
        return self


class CandidateItem(ContractModel):
    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "number": "candidate_number",
        "candidate_no": "candidate_number",
        "room": "room_no",
        "community_name": "community",
    }

    candidate_number: int
    community: str = ""
    room_no: str = ""
    title: str = ""
    rent_pay1: int | None = None
    rent_pay2: int | None = None
    score: float | None = None
    source_kind: str = ""

    @field_validator("candidate_number")
    @classmethod
    def _positive_candidate_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError("candidate_number must be positive")
        return value


class CandidateSet(ContractModel):
    candidates: list[CandidateItem] = Field(default_factory=list)
    query_state: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _candidate_numbers_are_unique_and_sequential(self) -> Self:
        numbers = [item.candidate_number for item in self.candidates]
        if len(numbers) != len(set(numbers)):
            raise ValueError("candidate_number must be unique")
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("candidate_number must be sequential from 1")
        return self


class EvidenceItem(ContractModel):
    evidence_type: str
    summary: str = ""
    source_kind: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sensitive_metadata: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))


class ToolEvidenceBundle(ContractModel):
    tool_name: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    candidate_set: CandidateSet | None = None
    raw_tool_result: dict[str, Any] = Field(default_factory=dict, repr=False)

    def to_safe_dict(self) -> dict[str, Any]:
        payload = super().to_safe_dict()
        payload.pop("raw_tool_result", None)
        return payload


class Claim(ContractModel):
    claim_id: str
    text: str
    status: str = "supported"
    support: list[str] = Field(default_factory=list)
    risk: str = "low"

    @field_validator("claim_id", "text", mode="before")
    @classmethod
    def _require_claim_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        return _redact_text(text)


class SendAction(ContractModel):
    action_id: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sensitive_payload: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("payload", "metadata", mode="before")
    @classmethod
    def _safe_payload(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))


class PreparedOutboundPackage(ContractModel):
    reply_text: str
    response_strategy: ResponseStrategy = ResponseStrategy.ANSWER
    candidate_set: CandidateSet | None = None
    evidence_bundle: ToolEvidenceBundle | None = None
    claims: list[Claim] = Field(default_factory=list)
    send_actions: list[SendAction] = Field(default_factory=list)
    reply_source: str = "rag"

    @field_validator("reply_text", mode="before")
    @classmethod
    def _safe_reply_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))


class RetryPacket(ContractModel):
    reason: str
    failed_claims: list[Claim] = Field(default_factory=list)
    retry_instruction: str = ""
    previous_package: PreparedOutboundPackage | None = None

    @field_validator("reason", "retry_instruction", mode="before")
    @classmethod
    def _safe_retry_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))


class SendReceipt(ContractModel):
    action_id: str
    action_type: str
    status: str
    sent_at: str = ""
    provider_message_id: str = ""
    error_code: str = ""
    error_message: str = ""

    @field_validator("error_message", mode="before")
    @classmethod
    def _safe_error_message(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))


def _redact_sensitive(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): _redact_sensitive(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _omit_non_loggable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _omit_non_loggable(item_value)
            for item_key, item_value in value.items()
            if str(item_key) not in SAFE_OUTPUT_OMIT_KEYS
        }
    if isinstance(value, list):
        return [_omit_non_loggable(item) for item in value]
    if isinstance(value, tuple):
        return [_omit_non_loggable(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    result = PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    result = PASSWORD_CODE_PATTERN.sub(REDACTED, result)
    result = TOKEN_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", result)
    return result


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def safe_artifact_payload(value: Any) -> Any:
    return _redact_sensitive(_omit_shadow_unsafe(value))


def _omit_shadow_unsafe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _omit_shadow_unsafe(item_value)
            for item_key, item_value in value.items()
            if str(item_key).lower() not in SAFE_ARTIFACT_OMIT_KEYS
        }
    if isinstance(value, list):
        return [_omit_shadow_unsafe(item) for item in value]
    if isinstance(value, tuple):
        return [_omit_shadow_unsafe(item) for item in value]
    return value


class OrchestratorShadowArtifact(ContractModel):
    schema_version: str = ORCHESTRATOR_SHADOW_SCHEMA_VERSION
    mode: str = "shadow"
    artifact_id: str
    created_at: str
    baseline_commit: str
    turn: dict[str, Any] = Field(default_factory=dict)
    inventory_read: dict[str, Any] = Field(default_factory=dict)
    legacy_pipeline: dict[str, Any] = Field(default_factory=dict)
    shadow_a: dict[str, Any] = Field(default_factory=dict)
    integration_notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _require_shadow_schema(cls, value: str) -> str:
        if value != ORCHESTRATOR_SHADOW_SCHEMA_VERSION:
            raise ValueError("invalid orchestrator shadow schema_version")
        return value

    @field_validator("mode")
    @classmethod
    def _require_shadow_mode(cls, value: str) -> str:
        mode = str(value or "").strip()
        if mode != "shadow":
            raise ValueError("orchestrator shadow artifact mode must be shadow")
        return mode

    @field_validator("artifact_id", "created_at", "baseline_commit", mode="before")
    @classmethod
    def _require_artifact_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        return _redact_text(text)

    @field_validator("turn", "inventory_read", "legacy_pipeline", "shadow_a", mode="before")
    @classmethod
    def _safe_artifact_dict(cls, value: Any) -> dict[str, Any]:
        return safe_artifact_payload(dict(value or {}))

    @field_validator("integration_notes", mode="before")
    @classmethod
    def _safe_integration_notes(cls, value: Any) -> list[str]:
        return [
            _redact_text(str(item).strip())
            for item in (value or [])
            if str(item).strip()
        ]

    def to_safe_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", include=set(ORCHESTRATOR_SHADOW_TOP_LEVEL_KEYS))
        return safe_artifact_payload(
            {
                key: payload.get(key)
                for key in ORCHESTRATOR_SHADOW_TOP_LEVEL_KEYS
                if key in payload
            }
        )
