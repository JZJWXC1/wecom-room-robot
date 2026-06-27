from __future__ import annotations

from enum import Enum
import re
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "kf_rag_contracts.v1"
ORCHESTRATOR_SHADOW_SCHEMA_VERSION = "rag_v2_orchestrator_shadow.v1"
REDACTED = "[REDACTED]"
SENSITIVE_KEY_MARKERS = (
    "access_token",
    "app_secret",
    "appsecret",
    "authorization",
    "bearer",
    "corpsecret",
    "cursor",
    "feishu",
    "external_userid",
    "external_user_id",
    "media_id",
    "msg_signature",
    "openid",
    "open_id",
    "password",
    "refresh_token",
    "secret",
    "signature",
    "tenant_access_token",
    "token",
    "unionid",
    "union_id",
    "welcome_code",
    "phone",
    "mobile",
    "电话",
    "飞书",
    "密钥",
    "手机",
    "手机号",
    "密码",
    "看房密码",
    "看房方式密码",
    "viewing",
    "raw_viewing",
    "private",
    "看房方式密码",
    "看房密码",
    "密码",
    "手机号",
)
SAFE_SENSITIVE_SUMMARY_KEYS = {
    "has_password",
    "password_available",
    "password_match",
    "password_policy",
    "has_viewing_text",
    "viewing_mode",
    "viewing_summary",
    "availability_summary",
    "availability_status",
    "needs_contact",
    "contact_required",
    "source_hash",
}
SAFE_LONG_HASH_KEYS = {
    "content_hash",
    "material_hash",
    "source_hash",
    "text_hash",
    "turn_scope_id",
}
SAFE_GIT_COMMIT_KEYS = {
    "baseline_commit",
    "commit",
    "commit_hash",
    "git_commit",
    "git_commit_hash",
}
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
FLEX_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)")
PASSWORD_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{3,8}#(?![A-Za-z0-9])")
PASSWORD_CONTEXT_PATTERN = re.compile(
    r"((?:看房方式|看房|门锁|门禁|房门|密码)[^0-9A-Za-z#]{0,12})([A-Za-z0-9][A-Za-z0-9_#-]{2,31})"
)
TOKEN_CONTEXT_PATTERN = re.compile(
    r"\b(access[_-]?token|tenant[_-]?access[_-]?token|refresh[_-]?token|api[_-]?key|app[_-]?secret|corpsecret|secret|token|password|passwd|pwd|authorization|bearer|msg[_-]?signature|signature|welcome[_-]?code|media[_-]?id|cursor|external[_-]?userid|openid|unionid)"
    r"\s*[:=]\s*[^\s,;，。\"'<>]+",
    re.IGNORECASE,
)
SECRET_CONTEXT_PATTERN = re.compile(
    r"((?:飞书)?密钥|app[_ -]?secret|corpsecret|tenant[_ -]?access[_ -]?token|access[_ -]?token|refresh[_ -]?token|authorization|msg[_ -]?signature|signature|welcome[_ -]?code|media[_ -]?id|external[_ -]?userid|openid|unionid|cursor)\s*[:=：]?\s*[^\s,;，。\"'<>]+",
    re.IGNORECASE,
)
LONG_HEX_PATTERN = re.compile(r"\b[0-9a-f]{32,128}\b", re.IGNORECASE)
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
LONG_RUNTIME_ID_PATTERN = re.compile(
    r"\b(?:wm|wo|open|union|media|msg|cursor|welcome)[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
GENERIC_LONG_ID_PATTERN = re.compile(r"\b(?=[A-Za-z0-9_-]{40,}\b)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_-]{40,}\b")


class ConstraintOperation(str, Enum):
    INHERIT = "inherit"
    REPLACE = "replace"
    EXCLUDE = "exclude"
    CLEAR = "clear"


class ResponseStrategyMode(str, Enum):
    ANSWER = "answer"
    ASK_CLARIFICATION = "ask_clarification"
    TOOL_FIRST = "tool_first"
    SEND_MEDIA = "send_media"
    HANDOFF = "handoff"
    SAFE_FALLBACK = "safe_fallback"
    RETRY = "retry"


class ResponseStrategy(BaseModel):
    """LLM2 回复策略；兼容旧字符串/枚举 mode 输入。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=True)

    ANSWER: ClassVar["ResponseStrategy"]
    ASK_CLARIFICATION: ClassVar["ResponseStrategy"]
    TOOL_FIRST: ClassVar["ResponseStrategy"]
    SEND_MEDIA: ClassVar["ResponseStrategy"]
    HANDOFF: ClassVar["ResponseStrategy"]
    SAFE_FALLBACK: ClassVar["ResponseStrategy"]
    RETRY: ClassVar["ResponseStrategy"]

    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "strategy": "mode",
        "response_strategy": "mode",
        "require_direct_answer": "direct_answer_required",
        "ack_context": "acknowledge_context",
        "avoid_repeats": "avoid_repeat_fields",
        "tense": "action_tense",
    }

    mode: ResponseStrategyMode = ResponseStrategyMode.ANSWER
    detail_level: str = "normal"
    direct_answer_required: bool = False
    acknowledge_context: bool = True
    max_sentences: int = Field(default=2, ge=0)
    max_questions: int = Field(default=1, ge=0)
    avoid_repeat_fields: list[str] = Field(default_factory=list)
    action_tense: str = "present"
    legacy_unknown_fields: dict[str, Any] = Field(default_factory=dict, repr=False)

    def __init__(self, value: Any = None, **data: Any) -> None:
        if value is not None:
            if isinstance(value, ResponseStrategy):
                data = value.to_safe_dict() | data
            elif isinstance(value, dict) and not data:
                data = dict(value)
            else:
                data.setdefault("mode", value)
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_value(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if isinstance(value, ResponseStrategyMode):
            return {"mode": value.value}
        if isinstance(value, str):
            return {"mode": value}
        if isinstance(value, dict):
            known_fields = set(cls.model_fields)
            data: dict[str, Any] = {}
            unknown: dict[str, Any] = {}
            for key, item_value in value.items():
                field_name = cls.legacy_field_aliases.get(str(key), str(key))
                if field_name in known_fields and field_name != "legacy_unknown_fields":
                    data[field_name] = item_value
                else:
                    unknown[str(key)] = _redact_sensitive(item_value, key=str(key))
            if unknown:
                data["legacy_unknown_fields"] = unknown
            return data
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, value: Any) -> str:
        if isinstance(value, ResponseStrategy):
            return value.mode
        if isinstance(value, ResponseStrategyMode):
            return value.value
        text = "" if value is None else str(value).strip()
        return text or ResponseStrategyMode.ANSWER.value

    @field_validator("detail_level", "action_tense", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("avoid_repeat_fields", mode="before")
    @classmethod
    def _coerce_repeat_fields(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @classmethod
    def from_legacy_dict(cls, payload: dict[str, Any]) -> Self:
        if not isinstance(payload, dict):
            raise TypeError("ResponseStrategy.from_legacy_dict expects dict")
        return cls.model_validate(payload)

    @classmethod
    def from_legacy_value(cls, value: Any) -> "ResponseStrategy":
        if isinstance(value, cls):
            return value
        return cls.model_validate(value)

    def to_legacy_dict(self) -> dict[str, Any]:
        return self.to_safe_dict()

    def to_safe_dict(self) -> dict[str, Any]:
        return _redact_sensitive(self.model_dump(mode="json"))

    def __str__(self) -> str:
        return str(self.mode)

    def __repr__(self) -> str:
        return f"ResponseStrategy({self.to_safe_dict()!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ResponseStrategy):
            return self.to_safe_dict() == other.to_safe_dict()
        if isinstance(other, ResponseStrategyMode):
            return str(self.mode) == other.value
        if isinstance(other, str):
            return str(self.mode) == other
        return False

    def __hash__(self) -> int:
        return hash(
            (
                str(self.mode),
                self.detail_level,
                self.direct_answer_required,
                self.acknowledge_context,
                self.max_sentences,
                self.max_questions,
                tuple(self.avoid_repeat_fields),
                self.action_tense,
            )
        )


ResponseStrategy.ANSWER = ResponseStrategy(ResponseStrategyMode.ANSWER)
ResponseStrategy.ASK_CLARIFICATION = ResponseStrategy(ResponseStrategyMode.ASK_CLARIFICATION)
ResponseStrategy.TOOL_FIRST = ResponseStrategy(ResponseStrategyMode.TOOL_FIRST)
ResponseStrategy.SEND_MEDIA = ResponseStrategy(ResponseStrategyMode.SEND_MEDIA)
ResponseStrategy.HANDOFF = ResponseStrategy(ResponseStrategyMode.HANDOFF)
ResponseStrategy.SAFE_FALLBACK = ResponseStrategy(ResponseStrategyMode.SAFE_FALLBACK)
ResponseStrategy.RETRY = ResponseStrategy(ResponseStrategyMode.RETRY)


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
    response_strategy: ResponseStrategy = Field(
        default_factory=lambda: ResponseStrategy.from_legacy_value(ResponseStrategyMode.TOOL_FIRST)
    )
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

    response_strategy: ResponseStrategy = Field(
        default_factory=lambda: ResponseStrategy.from_legacy_value(ResponseStrategyMode.TOOL_FIRST)
    )
    tasks: list[TaskAtom] = Field(default_factory=list)
    inherited_constraints: dict[str, Any] = Field(default_factory=dict)
    replaced_constraints: dict[str, Any] = Field(default_factory=dict)
    excluded_constraints: dict[str, Any] = Field(default_factory=dict)
    cleared_constraint_keys: list[str] = Field(default_factory=list)
    rewritten_query: str = ""

    @field_validator("tasks", mode="before")
    @classmethod
    def _coerce_tasks(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, TaskAtom)

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

    @field_validator("candidates", mode="before")
    @classmethod
    def _coerce_candidates(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, CandidateItem)

    @model_validator(mode="after")
    def _candidate_numbers_are_unique_and_sequential(self) -> Self:
        numbers = [item.candidate_number for item in self.candidates]
        if len(numbers) != len(set(numbers)):
            raise ValueError("candidate_number must be unique")
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("candidate_number must be sequential from 1")
        return self


class EvidenceItem(ContractModel):
    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "record_id": "source_record_id",
        "source_id": "source_record_id",
        "fields": "field_values",
        "values": "field_values",
        "sensitive_level": "sensitivity",
    }

    evidence_type: str
    summary: str = ""
    source_kind: str = ""
    source_record_id: str = ""
    field_values: dict[str, Any] = Field(default_factory=dict)
    sensitivity: str = "public"
    fetched_at: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sensitive_metadata: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("metadata", "field_values", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))

    @field_validator("evidence_type", "summary", "source_kind", "source_record_id", "sensitivity", "fetched_at", mode="before")
    @classmethod
    def _safe_evidence_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())


class ToolEvidenceBundle(ContractModel):
    tool_name: str = ""
    source_record_id: str = ""
    field_values: dict[str, Any] = Field(default_factory=dict)
    sensitivity: str = "public"
    fetched_at: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    candidate_set: CandidateSet | None = None
    raw_tool_result: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("field_values", mode="before")
    @classmethod
    def _safe_bundle_field_values(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))

    @field_validator("tool_name", "source_record_id", "sensitivity", "fetched_at", mode="before")
    @classmethod
    def _safe_bundle_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, EvidenceItem)

    @field_validator("candidate_set", mode="before")
    @classmethod
    def _coerce_candidate_set(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return CandidateSet.from_legacy_dict(value)
        return value

    def to_safe_dict(self) -> dict[str, Any]:
        payload = super().to_safe_dict()
        payload.pop("raw_tool_result", None)
        return payload


class Claim(ContractModel):
    legacy_field_aliases: ClassVar[dict[str, str]] = {
        "evidence": "evidence_ref",
        "evidence_refs": "support",
        "evidence_reference": "evidence_ref",
        "source_evidence_id": "evidence_ref",
    }

    claim_id: str
    task_id: str = ""
    field: str = ""
    value: Any = None
    evidence_ref: str = ""
    text_span: dict[str, int] = Field(default_factory=dict)
    sensitivity: str = "public"
    text: str = ""
    status: str = "supported"
    support: list[str] = Field(default_factory=list)
    risk: str = "low"

    @field_validator("claim_id", mode="before")
    @classmethod
    def _require_claim_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        return _redact_text(text)

    @field_validator("task_id", "field", "evidence_ref", "sensitivity", "text", "status", "risk", mode="before")
    @classmethod
    def _safe_claim_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())

    @field_validator("support", mode="before")
    @classmethod
    def _coerce_support(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [_redact_text(str(item).strip()) for item in value if str(item).strip()]
        return []

    @field_validator("text_span", mode="before")
    @classmethod
    def _coerce_text_span(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, int] = {}
        for key, item_value in value.items():
            try:
                result[str(key)] = int(item_value)
            except (TypeError, ValueError):
                continue
        return result

    @model_validator(mode="after")
    def _normalize_field_claim(self) -> Self:
        safe_value = _redact_sensitive(self.value, key=self.field)
        object.__setattr__(self, "value", safe_value)
        evidence_ref = self.evidence_ref
        evidence_id = self.evidence_id
        if evidence_ref and not evidence_id:
            evidence_id = evidence_ref
            object.__setattr__(self, "evidence_id", evidence_id)
        if evidence_id and not evidence_ref:
            evidence_ref = evidence_id
            object.__setattr__(self, "evidence_ref", evidence_ref)
        support = list(self.support)
        if evidence_ref and evidence_ref not in support:
            support.append(evidence_ref)
            object.__setattr__(self, "support", support)
        if not self.text:
            if self.field:
                object.__setattr__(
                    self,
                    "text",
                    _redact_text(f"{self.field}: {self.value if self.value is not None else ''}".strip()),
                )
            else:
                raise ValueError("Claim requires text or field")
        return self


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


class ActionCaption(ContractModel):
    caption_id: str = ""
    action_id: str
    action_type: str = ""
    text: str = ""
    display_order: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("caption_id", "action_id", "action_type", "text", mode="before")
    @classmethod
    def _safe_caption_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_caption_metadata(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))


class PreparedOutboundPackage(ContractModel):
    reply_text: str
    response_strategy: ResponseStrategy = Field(
        default_factory=lambda: ResponseStrategy.from_legacy_value(ResponseStrategyMode.ANSWER)
    )
    answered_task_ids: list[str] = Field(default_factory=list)
    candidate_set: CandidateSet | None = None
    evidence_bundle: ToolEvidenceBundle | None = None
    claims: list[Claim] = Field(default_factory=list)
    action_captions: list[ActionCaption] = Field(default_factory=list)
    send_actions: list[SendAction] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)
    self_review: dict[str, Any] = Field(default_factory=dict)
    selfcheck_profile: str = ""
    reply_source: str = "rag"

    @field_validator("reply_text", mode="before")
    @classmethod
    def _safe_reply_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))

    @field_validator("answered_task_ids", "missing_items", mode="before")
    @classmethod
    def _coerce_string_items(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [_redact_text(str(item).strip()) for item in value if str(item).strip()]
        return []

    @field_validator("candidate_set", mode="before")
    @classmethod
    def _coerce_outbound_candidate_set(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return CandidateSet.from_legacy_dict(value)
        return value

    @field_validator("evidence_bundle", mode="before")
    @classmethod
    def _coerce_outbound_evidence_bundle(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return ToolEvidenceBundle.from_legacy_dict(value)
        return value

    @field_validator("claims", mode="before")
    @classmethod
    def _coerce_claims(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, Claim)

    @field_validator("action_captions", mode="before")
    @classmethod
    def _coerce_action_captions(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, ActionCaption)

    @field_validator("send_actions", mode="before")
    @classmethod
    def _coerce_send_actions(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, SendAction)

    @field_validator("self_review", mode="before")
    @classmethod
    def _safe_self_review(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))

    @field_validator("selfcheck_profile", mode="before")
    @classmethod
    def _safe_selfcheck_profile(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())


class RetryPacket(ContractModel):
    reason: str
    failed_claims: list[Claim] = Field(default_factory=list)
    retry_instruction: str = ""
    previous_package: PreparedOutboundPackage | None = None

    @field_validator("reason", "retry_instruction", mode="before")
    @classmethod
    def _safe_retry_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))

    @field_validator("failed_claims", mode="before")
    @classmethod
    def _coerce_failed_claims(cls, value: Any) -> list[Any]:
        return _coerce_contract_list(value, Claim)

    @field_validator("previous_package", mode="before")
    @classmethod
    def _coerce_previous_package(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return PreparedOutboundPackage.from_legacy_dict(value)
        return value


class SendReceipt(ContractModel):
    action_id: str
    action_type: str
    status: str
    receipt_id: str = ""
    idempotency_key: str = ""
    duplicate_of: str = ""
    provider: str = "wecom_kf"
    attempt: int = 1
    sent_at: str = ""
    provider_message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    send_result: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _INTERNAL_MATCH_FIELDS: ClassVar[set[str]] = {"receipt_id", "idempotency_key", "duplicate_of"}

    @field_validator(
        "action_id",
        "action_type",
        "status",
        "provider",
        "sent_at",
        "error_code",
        mode="before",
    )
    @classmethod
    def _safe_receipt_text(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())

    @field_validator("receipt_id", "idempotency_key", "duplicate_of", mode="before")
    @classmethod
    def _preserve_internal_match_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("provider_message_id", mode="before")
    @classmethod
    def _safe_provider_message_id(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value).strip())

    @field_validator("attempt", mode="before")
    @classmethod
    def _coerce_attempt(cls, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 1
        return max(1, number)

    @field_validator("error_message", mode="before")
    @classmethod
    def _safe_error_message(cls, value: Any) -> str:
        return _redact_text("" if value is None else str(value))

    @field_validator("send_result", "metadata", mode="before")
    @classmethod
    def _safe_receipt_payload(cls, value: Any) -> dict[str, Any]:
        return _redact_sensitive(dict(value or {}))

    def to_ledger_dict(self) -> dict[str, Any]:
        payload = self.to_safe_dict()
        raw = self.model_dump(mode="json")
        for key in self._INTERNAL_MATCH_FIELDS:
            value = str(raw.get(key) or "").strip()
            if value:
                payload[key] = value
        return payload


def _redact_sensitive(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, BaseModel):
        return _redact_sensitive(value.model_dump(mode="json"), key=key)
    if isinstance(value, dict):
        return {str(item_key): _redact_sensitive(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        if _is_safe_git_commit_key(key) and _is_git_commit_hash(value.strip()):
            return value.strip()
        return _redact_text(value, allow_long_hash=_is_safe_long_hash_key(key))
    return value


def _omit_non_loggable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _omit_non_loggable(value.model_dump(mode="json"))
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


def _redact_text(text: str, *, allow_long_hash: bool = False) -> str:
    result = FLEX_PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    result = PHONE_PATTERN.sub("[REDACTED_PHONE]", result)
    result = PASSWORD_CODE_PATTERN.sub(REDACTED, result)
    result = PASSWORD_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
    result = TOKEN_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", result)
    result = SECRET_CONTEXT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", result)
    result = LONG_RUNTIME_ID_PATTERN.sub("[REDACTED_ID]", result)
    result = GENERIC_LONG_ID_PATTERN.sub("[REDACTED_ID]", result)
    if not allow_long_hash:
        result = LONG_HEX_PATTERN.sub("[REDACTED_HASH]", result)
    return result


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SAFE_SENSITIVE_SUMMARY_KEYS:
        return False
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _is_safe_long_hash_key(key: str) -> bool:
    return key.lower() in SAFE_LONG_HASH_KEYS


def _is_safe_git_commit_key(key: str) -> bool:
    return key.lower() in SAFE_GIT_COMMIT_KEYS


def _is_git_commit_hash(value: str) -> bool:
    return bool(GIT_COMMIT_PATTERN.fullmatch(value))


def redact_sensitive_text(value: Any) -> str:
    return _redact_text("" if value is None else str(value))


def redact_sensitive_value(value: Any) -> Any:
    return _redact_sensitive(value)


def safe_artifact_payload(value: Any) -> Any:
    return _redact_sensitive(_omit_shadow_unsafe(value))


def _omit_shadow_unsafe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _omit_shadow_unsafe(value.model_dump(mode="json"))
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


def _coerce_contract_list(value: Any, model_cls: type[ContractModel]) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        return value
    result: list[Any] = []
    for item in value:
        if isinstance(item, model_cls):
            result.append(item)
        elif isinstance(item, dict):
            result.append(model_cls.from_legacy_dict(item))
        else:
            result.append(item)
    return result


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

    @field_validator("artifact_id", "created_at", mode="before")
    @classmethod
    def _require_artifact_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        return _redact_text(text)

    @field_validator("baseline_commit", mode="before")
    @classmethod
    def _require_baseline_commit(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("field must not be empty")
        if _is_git_commit_hash(text):
            return text
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
