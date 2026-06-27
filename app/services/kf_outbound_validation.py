from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Any

from pydantic import ValidationError

from app.services.kf_contracts import (
    SCHEMA_VERSION,
    CandidateItem,
    CandidateSet,
    Claim,
    EvidenceItem,
    PreparedOutboundPackage,
    SendAction,
    StructuredTaskPacket,
    ToolEvidenceBundle,
)


class ValidationLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ValidationStatus(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    REWRITE_REQUIRED = "rewrite_required"


@dataclass(frozen=True)
class ValidationIssue:
    level: ValidationLevel
    code: str
    message: str
    path: str = ""
    subject_id: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "level": self.level.value,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            payload["path"] = self.path
        if self.subject_id:
            payload["subject_id"] = self.subject_id
        return payload


@dataclass(frozen=True)
class OutboundValidationContext:
    task_packet: StructuredTaskPacket | None = None
    user_asked_password: bool | None = None
    answered_task_ids: tuple[str, ...] = ()
    known_constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundValidationResult:
    issues: tuple[ValidationIssue, ...]

    @property
    def blocking_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level in {ValidationLevel.L0, ValidationLevel.L1, ValidationLevel.L2})

    @property
    def l3_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == ValidationLevel.L3)

    @property
    def l3_rewrite_reasons(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.l3_issues)

    @property
    def facts_passed(self) -> bool:
        return not self.blocking_issues

    @property
    def requires_rewrite(self) -> bool:
        return self.facts_passed and bool(self.l3_issues)

    @property
    def send_allowed(self) -> bool:
        return self.facts_passed and not self.l3_issues

    @property
    def passed(self) -> bool:
        return self.send_allowed

    @property
    def status(self) -> ValidationStatus:
        if self.blocking_issues:
            return ValidationStatus.BLOCKED
        if self.l3_issues:
            return ValidationStatus.REWRITE_REQUIRED
        return ValidationStatus.PASS

    def issues_for_level(self, level: ValidationLevel | str) -> tuple[ValidationIssue, ...]:
        normalized = ValidationLevel(level)
        return tuple(issue for issue in self.issues if issue.level == normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "passed": self.passed,
            "facts_passed": self.facts_passed,
            "send_allowed": self.send_allowed,
            "requires_rewrite": self.requires_rewrite,
            "l3_rewrite_reasons": list(self.l3_rewrite_reasons),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class _PackageIndexes:
    evidence_by_id: dict[str, EvidenceItem]
    action_ids: set[str]
    candidate_numbers: set[int]
    listing_ids: set[str]


PASSWORD_MARKERS = (
    "password",
    "passcode",
    "door_code",
    "doorcode",
    "viewing_password",
    "密码",
    "门锁",
    "门禁",
    "看房密码",
)
LINK_KEY_MARKERS = ("url", "uri", "link", "href", "链接")
FIELD_VALUE_KEYS = ("field_values", "fields", "values")
CLAIM_VALUE_KEYS = ("value", "claim_value", "field_value", "expected_value")
CLAIM_FIELD_KEYS = ("field", "field_name", "column", "column_name")
EVIDENCE_REF_KEYS = ("evidence_id", "evidence_ref", "evidence_refs", "support", "supports")
ACTION_REF_KEYS = ("action_id", "action_ref", "action_refs", "depends_on_action_ids")
CANDIDATE_NUMBER_KEYS = ("candidate_number", "candidate_no", "candidate_index", "candidate_numbers")
MEDIA_VIDEO_MARKERS = ("video", "send_video", "room_video", "视频")
MEDIA_IMAGE_MARKERS = ("image", "photo", "picture", "send_image", "图片", "照片")
MEDIA_SHEET_MARKERS = ("sheet", "inventory_sheet", "房源表", "表格")

URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
PASSWORD_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{3,8}#(?![A-Za-z0-9])")
INTERNAL_LEAK_PATTERN = re.compile(
    r"\b(?:listing_id|candidate_number|candidate_set_id|evidence_id|field_values|raw_tool_result|schema_version|"
    r"PreparedOutboundPackage|ToolEvidenceBundle|StructuredTaskPacket|inventory\.search|dual_llm_shadow|LLM2|Planner)\b",
    re.IGNORECASE,
)
TEMPLATE_PATTERN = re.compile(r"(?:XX|某某|某小区|某房号|TODO|\{\{|\}\}|<小区>|<房号>|例如某套|示例房源)")
FUTURE_SEND_PATTERN = re.compile(r"(?:稍后|等下|待会|晚点|之后).{0,8}(?:发|传)|(?:可以|能|会).{0,4}发你")
PAST_SENT_PATTERN = re.compile(r"(?:已发|发你了|已经发|给你发过去了|这是.{0,16}(?:视频|图片|房源表))")
MISSING_MEDIA_PATTERN = re.compile(r"(?:暂无|没有|没找到|暂时没).{0,8}(?:视频|图片|照片)")


def validate_prepared_outbound_package(
    package: PreparedOutboundPackage | Mapping[str, Any],
    *,
    task_packet: StructuredTaskPacket | None = None,
    context: OutboundValidationContext | None = None,
) -> OutboundValidationResult:
    """Validate a PreparedOutboundPackage before any real send side effect.

    L0-L2 issues block facts/actions. L3 issues only return rewrite reasons and
    intentionally do not include a rewritten reply or changed facts.
    """

    validation_context = _merge_context(context, task_packet)
    parsed_package, issues = _coerce_package(package)
    if parsed_package is None:
        return OutboundValidationResult(tuple(issues))

    indexes = _build_indexes(parsed_package)
    issues.extend(_validate_l0(parsed_package, indexes))
    issues.extend(_validate_l1(parsed_package, indexes, validation_context))
    issues.extend(_validate_l2(parsed_package, indexes, validation_context))
    issues.extend(_validate_l3(parsed_package, validation_context))
    return OutboundValidationResult(tuple(issues))


def _merge_context(
    context: OutboundValidationContext | None,
    task_packet: StructuredTaskPacket | None,
) -> OutboundValidationContext:
    if context is None:
        return OutboundValidationContext(task_packet=task_packet)
    if task_packet is None or context.task_packet is task_packet:
        return context
    return OutboundValidationContext(
        task_packet=task_packet,
        user_asked_password=context.user_asked_password,
        answered_task_ids=context.answered_task_ids,
        known_constraints=context.known_constraints,
    )


def _coerce_package(
    package: PreparedOutboundPackage | Mapping[str, Any],
) -> tuple[PreparedOutboundPackage | None, list[ValidationIssue]]:
    if isinstance(package, PreparedOutboundPackage):
        return package, []
    if isinstance(package, Mapping):
        try:
            return PreparedOutboundPackage.model_validate(dict(package)), []
        except ValidationError:
            return None, [
                _issue(
                    ValidationLevel.L0,
                    "l0.invalid_schema_type",
                    "PreparedOutboundPackage schema validation failed.",
                    "package",
                )
            ]
    return None, [
        _issue(
            ValidationLevel.L0,
            "l0.invalid_schema_type",
            "Outbound validator expects PreparedOutboundPackage or a package mapping.",
            "package",
        )
    ]


def _build_indexes(package: PreparedOutboundPackage) -> _PackageIndexes:
    evidence_items = _evidence_items(package.evidence_bundle)
    evidence_by_id = {item.evidence_id: item for item in evidence_items if item.evidence_id}
    candidate_numbers = {item.candidate_number for item in _candidate_items(package.candidate_set)}
    listing_ids = {
        listing_id
        for listing_id in (
            [item.listing_id for item in _candidate_items(package.candidate_set)]
            + [item.listing_id for item in evidence_items]
        )
        if listing_id
    }
    action_ids = {action.action_id for action in package.send_actions if action.action_id}
    return _PackageIndexes(
        evidence_by_id=evidence_by_id,
        action_ids=action_ids,
        candidate_numbers=candidate_numbers,
        listing_ids=listing_ids,
    )


def _validate_l0(package: PreparedOutboundPackage, indexes: _PackageIndexes) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if package.schema_version != SCHEMA_VERSION:
        issues.append(
            _issue(ValidationLevel.L0, "l0.schema_version", "Package schema_version is not the current contract version.", "schema_version")
        )

    evidence_ids_seen: set[str] = set()
    for index, evidence in enumerate(_evidence_items(package.evidence_bundle)):
        path = f"evidence_bundle.evidence[{index}]"
        if not evidence.evidence_id:
            issues.append(_issue(ValidationLevel.L0, "l0.missing_evidence_id", "Evidence item must have evidence_id.", path))
        elif evidence.evidence_id in evidence_ids_seen:
            issues.append(
                _issue(ValidationLevel.L0, "l0.duplicate_evidence_id", "Duplicate evidence_id is not allowed.", f"{path}.evidence_id", evidence.evidence_id)
            )
        evidence_ids_seen.add(evidence.evidence_id)
        if evidence.listing_id and indexes.listing_ids and evidence.listing_id not in indexes.listing_ids:
            issues.append(
                _issue(ValidationLevel.L0, "l0.unknown_listing_ref", "Evidence listing_id is not present in the candidate/evidence index.", f"{path}.listing_id")
            )

    action_ids_seen: set[str] = set()
    action_signatures_seen: set[str] = set()
    for index, action in enumerate(package.send_actions):
        path = f"send_actions[{index}]"
        if not action.action_id:
            issues.append(_issue(ValidationLevel.L0, "l0.missing_action_id", "Send action must have action_id.", path))
        elif action.action_id in action_ids_seen:
            issues.append(
                _issue(ValidationLevel.L0, "l0.duplicate_action", "Duplicate action_id is not allowed.", f"{path}.action_id", action.action_id)
            )
        action_ids_seen.add(action.action_id)
        if not action.action_type:
            issues.append(_issue(ValidationLevel.L0, "l0.missing_action_type", "Send action must have action_type.", f"{path}.action_type", action.action_id))
        signature = _action_signature(action)
        if signature in action_signatures_seen:
            issues.append(_issue(ValidationLevel.L0, "l0.duplicate_action", "Duplicate send action payload is not allowed.", path, action.action_id))
        action_signatures_seen.add(signature)
        issues.extend(_validate_listing_ref(action.listing_id, indexes, f"{path}.listing_id", action.action_id, ValidationLevel.L0))
        issues.extend(_validate_evidence_refs(_action_evidence_refs(action), indexes, path, action.action_id))
        issues.extend(_validate_action_refs(action, indexes, path))
        issues.extend(_validate_candidate_ref_types(action, path))

    for index, claim in enumerate(package.claims):
        path = f"claims[{index}]"
        issues.extend(_validate_listing_ref(claim.listing_id, indexes, f"{path}.listing_id", claim.claim_id, ValidationLevel.L0))
        refs = _claim_evidence_refs(claim)
        if not refs:
            issues.append(_issue(ValidationLevel.L0, "l0.missing_evidence_ref", "Claim must reference at least one evidence item.", path, claim.claim_id))
        issues.extend(_validate_evidence_refs(refs, indexes, path, claim.claim_id))
        issues.extend(_validate_candidate_ref_types(claim, path))

    issues.extend(_validate_package_level_action_refs(package, indexes))
    return issues


def _validate_l1(
    package: PreparedOutboundPackage,
    indexes: _PackageIndexes,
    context: OutboundValidationContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, claim in enumerate(package.claims):
        path = f"claims[{index}]"
        for evidence in _referenced_evidence(claim, indexes):
            if claim.listing_id and evidence.listing_id and claim.listing_id != evidence.listing_id:
                issues.append(
                    _issue(ValidationLevel.L1, "l1.listing_mismatch", "Claim listing_id does not match referenced evidence.", f"{path}.listing_id", claim.claim_id)
                )
            if claim.inventory_snapshot_id and evidence.inventory_snapshot_id and claim.inventory_snapshot_id != evidence.inventory_snapshot_id:
                issues.append(
                    _issue(
                        ValidationLevel.L1,
                        "l1.snapshot_mismatch",
                        "Claim snapshot_id does not match referenced evidence.",
                        f"{path}.inventory_snapshot_id",
                        claim.claim_id,
                    )
                )
        issues.extend(_validate_claim_value_against_evidence(claim, indexes, path))

    for path, model in _snapshot_checked_models(package):
        if model.inventory_snapshot_id and package.inventory_snapshot_id and model.inventory_snapshot_id != package.inventory_snapshot_id:
            issues.append(
                _issue(
                    ValidationLevel.L1,
                    "l1.snapshot_mismatch",
                    "Nested contract snapshot_id does not match package snapshot_id.",
                    f"{path}.inventory_snapshot_id",
                    getattr(model, "action_id", "") or getattr(model, "claim_id", "") or getattr(model, "evidence_id", ""),
                )
            )

    issues.extend(_validate_sensitive_values_are_evidence_only(package, indexes, context))
    return issues


def _validate_l2(
    package: PreparedOutboundPackage,
    indexes: _PackageIndexes,
    context: OutboundValidationContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(_validate_task_answered(package, context))
    issues.extend(_validate_candidate_ref_bounds(package, indexes))

    if _is_video_only_request(context):
        for index, action in enumerate(package.send_actions):
            if _is_image_action(action):
                issues.append(
                    _issue(
                        ValidationLevel.L2,
                        "l2.video_only_cannot_send_image",
                        "Video-only customer request must not include image send action.",
                        f"send_actions[{index}]",
                        action.action_id,
                    )
                )

    if not _user_asked_password(context):
        for index, claim in enumerate(package.claims):
            if _mentions_password(_claim_visible_payload(claim)):
                issues.append(
                    _issue(
                        ValidationLevel.L2,
                        "l2.password_not_requested",
                        "Password claim is not allowed when the customer did not ask for password.",
                        f"claims[{index}]",
                        claim.claim_id,
                    )
                )
        for index, action in enumerate(package.send_actions):
            if _is_password_action(action):
                issues.append(
                    _issue(
                        ValidationLevel.L2,
                        "l2.password_not_requested",
                        "Password send action is not allowed when the customer did not ask for password.",
                        f"send_actions[{index}]",
                        action.action_id,
                    )
                )
    else:
        for index, claim in enumerate(package.claims):
            if _mentions_password(_claim_visible_payload(claim)) and not _is_evidence_bound_password_claim(claim, indexes):
                issues.append(
                    _issue(
                        ValidationLevel.L2,
                        "l2.password_not_evidence_bound",
                        "Password claim is only allowed when it is bound to password evidence.",
                        f"claims[{index}]",
                        claim.claim_id,
                    )
                )
        for index, action in enumerate(package.send_actions):
            if _is_password_action(action) and not _is_evidence_bound_password_action(action, indexes):
                issues.append(
                    _issue(
                        ValidationLevel.L2,
                        "l2.password_not_evidence_bound",
                        "Password send action is only allowed when it is bound to password evidence.",
                        f"send_actions[{index}]",
                        action.action_id,
                    )
                )
    return issues


def _validate_l3(package: PreparedOutboundPackage, context: OutboundValidationContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    text = package.reply_text or ""
    if not text:
        return issues
    if INTERNAL_LEAK_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.internal_name_leak", "Reply leaks internal field/tool names; regenerate wording only.", "reply_text")
        )
    if TEMPLATE_PATTERN.search(text):
        issues.append(_issue(ValidationLevel.L3, "l3.template_talk", "Reply contains placeholder or template wording; regenerate wording only.", "reply_text"))
    if _asks_known_condition_again(text, context):
        issues.append(
            _issue(ValidationLevel.L3, "l3.repeats_known_condition", "Reply asks again for conditions already known in context.", "reply_text")
        )
    has_media_action = any(_is_media_action(action) for action in package.send_actions)
    if has_media_action and FUTURE_SEND_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply uses future tense although media action is already prepared.", "reply_text")
        )
    if not has_media_action and PAST_SENT_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply claims a media action was sent but no media action is prepared.", "reply_text")
        )
    if has_media_action and MISSING_MEDIA_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply says media is missing while a media send action is prepared.", "reply_text")
        )
    return issues


def _validate_listing_ref(
    listing_id: str,
    indexes: _PackageIndexes,
    path: str,
    subject_id: str,
    level: ValidationLevel,
) -> list[ValidationIssue]:
    if listing_id and indexes.listing_ids and listing_id not in indexes.listing_ids:
        return [_issue(level, "l0.unknown_listing_ref", "listing_id reference is not present in candidate/evidence index.", path, subject_id)]
    return []


def _validate_evidence_refs(
    refs: Iterable[str],
    indexes: _PackageIndexes,
    path: str,
    subject_id: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ref in refs:
        if ref and ref not in indexes.evidence_by_id:
            issues.append(
                _issue(ValidationLevel.L0, "l0.unknown_evidence_ref", "Evidence reference does not exist in package evidence bundle.", path, subject_id)
            )
    return issues


def _validate_action_refs(action: SendAction, indexes: _PackageIndexes, path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ref_path, raw_ref in _iter_keyed_values(_action_ref_payload(action), ACTION_REF_KEYS):
        for ref in _string_values(raw_ref):
            if ref and ref != action.action_id and ref not in indexes.action_ids:
                issues.append(
                    _issue(
                        ValidationLevel.L0,
                        "l0.unknown_action_ref",
                        "Action reference does not exist in package send actions.",
                        f"{path}.{ref_path}",
                        action.action_id,
                    )
                )
    return issues


def _validate_package_level_action_refs(package: PreparedOutboundPackage, indexes: _PackageIndexes) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ref_path, raw_ref in _iter_keyed_values(package.legacy_unknown_fields, ACTION_REF_KEYS):
        for ref in _string_values(raw_ref):
            if ref and ref not in indexes.action_ids:
                issues.append(
                    _issue(
                        ValidationLevel.L0,
                        "l0.unknown_action_ref",
                        "Package-level action reference does not exist in package send actions.",
                        f"legacy_unknown_fields.{ref_path}",
                    )
                )
    return issues


def _validate_candidate_ref_types(model: Any, path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ref_path, raw_ref in _iter_candidate_number_refs(model):
        for value in _as_sequence(raw_ref):
            if _coerce_candidate_number(value) is None:
                issues.append(
                    _issue(
                        ValidationLevel.L0,
                        "l0.invalid_candidate_number",
                        "candidate_number reference must be an integer.",
                        f"{path}.{ref_path}",
                        getattr(model, "action_id", "") or getattr(model, "claim_id", ""),
                    )
                )
    return issues


def _validate_candidate_ref_bounds(package: PreparedOutboundPackage, indexes: _PackageIndexes) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for model_path, model in _candidate_ref_models(package):
        for ref_path, raw_ref in _iter_candidate_number_refs(model):
            for value in _as_sequence(raw_ref):
                number = _coerce_candidate_number(value)
                if number is None:
                    continue
                if not indexes.candidate_numbers or number not in indexes.candidate_numbers:
                    issues.append(
                        _issue(
                            ValidationLevel.L2,
                            "l2.candidate_number_out_of_range",
                            "candidate_number reference is outside the candidate set.",
                            f"{model_path}.{ref_path}",
                            getattr(model, "action_id", "") or getattr(model, "claim_id", ""),
                        )
                    )
    return issues


def _validate_claim_value_against_evidence(claim: Claim, indexes: _PackageIndexes, path: str) -> list[ValidationIssue]:
    raw_value = _first_legacy_value(claim, CLAIM_VALUE_KEYS)
    if raw_value is None:
        return []
    field_name = _first_legacy_value(claim, CLAIM_FIELD_KEYS)
    issues: list[ValidationIssue] = []
    referenced_evidence = _referenced_evidence(claim, indexes)
    if not referenced_evidence:
        return issues
    for value in _as_sequence(raw_value):
        normalized_value = _normalize_fact_value(value)
        if not normalized_value:
            continue
        if not any(_evidence_field_values_contain(evidence, normalized_value, field_name) for evidence in referenced_evidence):
            issues.append(
                _issue(
                    ValidationLevel.L1,
                    "l1.claim_value_not_in_evidence",
                    "Claim value is not present in referenced evidence field_values.",
                    path,
                    claim.claim_id,
                )
            )
    return issues


def _validate_sensitive_values_are_evidence_only(
    package: PreparedOutboundPackage,
    indexes: _PackageIndexes,
    context: OutboundValidationContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if _contains_sensitive_value(package.reply_text):
        issues.append(
            _issue(
                ValidationLevel.L1,
                "l1.sensitive_outside_evidence",
                "Password or link value appears outside evidence slots.",
                "reply_text",
            )
        )
    for index, claim in enumerate(package.claims):
        payload = _claim_visible_payload(claim)
        if _contains_sensitive_value(payload) and not (
            _contains_password_value(payload)
            and not _contains_link_value(payload)
            and _user_asked_password(context)
            and _is_evidence_bound_password_claim(claim, indexes)
        ):
            issues.append(
                _issue(
                    ValidationLevel.L1,
                    "l1.sensitive_outside_evidence",
                    "Password or link value appears outside evidence slots.",
                    f"claims[{index}]",
                    claim.claim_id,
            )
        )
    for index, action in enumerate(package.send_actions):
        payload = {"payload": action.payload, "metadata": action.metadata, "sensitive_payload": action.sensitive_payload}
        if _contains_sensitive_value(payload) and not (
            _contains_password_value(payload)
            and not _contains_link_value(payload)
            and _user_asked_password(context)
            and _is_evidence_bound_password_action(action, indexes)
        ):
            issues.append(
                _issue(
                    ValidationLevel.L1,
                    "l1.sensitive_outside_evidence",
                    "Password or link value appears outside evidence slots.",
                    f"send_actions[{index}]",
                    action.action_id,
                )
            )
    return issues


def _validate_task_answered(package: PreparedOutboundPackage, context: OutboundValidationContext) -> list[ValidationIssue]:
    task_packet = context.task_packet
    if task_packet is None:
        return []
    explicit_answered = set(context.answered_task_ids) | set(_string_values(package.legacy_unknown_fields.get("answered_task_ids")))
    issues: list[ValidationIssue] = []
    for index, task in enumerate(task_packet.tasks):
        if task.task_id in explicit_answered:
            continue
        if not _task_is_answered(task, package):
            issues.append(
                _issue(
                    ValidationLevel.L2,
                    "l2.task_not_answered",
                    "Task atom has no matching answer in reply_text, claims, or send actions.",
                    f"task_packet.tasks[{index}]",
                    task.task_id,
                )
            )
    return issues


def _task_is_answered(task: Any, package: PreparedOutboundPackage) -> bool:
    task_text = _task_search_text(task)
    if _contains_any(task_text, MEDIA_VIDEO_MARKERS):
        return any(_is_video_action(action) for action in package.send_actions)
    if _contains_any(task_text, MEDIA_IMAGE_MARKERS):
        return any(_is_image_action(action) for action in package.send_actions)
    if _contains_any(task_text, MEDIA_SHEET_MARKERS):
        return any(_is_sheet_action(action) for action in package.send_actions)
    if _mentions_password(task_text):
        return any(_is_password_action(action) for action in package.send_actions) or any(
            _mentions_password(_claim_visible_payload(claim)) for claim in package.claims
        )
    return bool((package.reply_text or "").strip()) or bool(package.claims) or bool(package.send_actions)


def _is_video_only_request(context: OutboundValidationContext) -> bool:
    task_packet = context.task_packet
    if task_packet is None or not task_packet.tasks:
        return False
    task_text = " ".join(_task_search_text(task) for task in task_packet.tasks)
    asks_video = _contains_any(task_text, MEDIA_VIDEO_MARKERS)
    asks_image = _contains_any(task_text, MEDIA_IMAGE_MARKERS)
    asks_sheet = _contains_any(task_text, MEDIA_SHEET_MARKERS)
    return asks_video and not asks_image and not asks_sheet


def _user_asked_password(context: OutboundValidationContext) -> bool:
    if context.user_asked_password is not None:
        return context.user_asked_password
    task_packet = context.task_packet
    if task_packet is None:
        return False
    return any(_mentions_password(_task_search_text(task)) for task in task_packet.tasks)


def _asks_known_condition_again(text: str, context: OutboundValidationContext) -> bool:
    constraints = _known_constraints(context)
    if not constraints:
        return False
    if _has_known_key(constraints, ("community", "小区", "楼盘")) and re.search(r"(?:哪个|哪一个|什么).{0,4}小区|小区.{0,4}(?:哪里|哪个)", text):
        return True
    if _has_known_key(constraints, ("room_no", "room", "房号", "房间")) and re.search(r"(?:哪个|哪一间|什么).{0,4}(?:房号|房间)", text):
        return True
    if _has_known_key(constraints, ("budget", "price", "rent", "预算", "价格", "租金")) and re.search(r"(?:预算|价位|租金).{0,8}(?:多少|是啥|什么)", text):
        return True
    if _has_known_key(constraints, ("area", "district", "区域", "板块")) and re.search(r"(?:区域|板块|哪里|哪边).{0,8}(?:想看|要看|方便)", text):
        return True
    return False


def _known_constraints(context: OutboundValidationContext) -> dict[str, Any]:
    result: dict[str, Any] = {str(key): value for key, value in context.known_constraints.items() if value not in (None, "", [], {})}
    task_packet = context.task_packet
    if task_packet is None:
        return result
    for source in (task_packet.inherited_constraints, task_packet.replaced_constraints):
        for key, value in source.items():
            if value not in (None, "", [], {}):
                result[str(key)] = value
    for task in task_packet.tasks:
        for key, value in task.constraints.items():
            if value not in (None, "", [], {}):
                result[str(key)] = value
    return result


def _has_known_key(constraints: Mapping[str, Any], keys: Sequence[str]) -> bool:
    normalized_keys = [str(key).lower() for key in constraints]
    return any(any(marker.lower() in key for marker in keys) for key in normalized_keys)


def _snapshot_checked_models(package: PreparedOutboundPackage) -> Iterable[tuple[str, Any]]:
    if package.candidate_set is not None:
        yield "candidate_set", package.candidate_set
        for index, candidate in enumerate(package.candidate_set.candidates):
            yield f"candidate_set.candidates[{index}]", candidate
    if package.evidence_bundle is not None:
        yield "evidence_bundle", package.evidence_bundle
        for index, evidence in enumerate(package.evidence_bundle.evidence):
            yield f"evidence_bundle.evidence[{index}]", evidence
    for index, claim in enumerate(package.claims):
        yield f"claims[{index}]", claim
    for index, action in enumerate(package.send_actions):
        yield f"send_actions[{index}]", action


def _candidate_ref_models(package: PreparedOutboundPackage) -> Iterable[tuple[str, Any]]:
    for index, claim in enumerate(package.claims):
        yield f"claims[{index}]", claim
    for index, action in enumerate(package.send_actions):
        yield f"send_actions[{index}]", action
    yield "legacy_unknown_fields", package.legacy_unknown_fields


def _candidate_items(candidate_set: CandidateSet | None) -> list[CandidateItem]:
    return list(candidate_set.candidates) if candidate_set is not None else []


def _evidence_items(evidence_bundle: ToolEvidenceBundle | None) -> list[EvidenceItem]:
    return list(evidence_bundle.evidence) if evidence_bundle is not None else []


def _claim_evidence_refs(claim: Claim) -> list[str]:
    refs = []
    if claim.evidence_id:
        refs.append(claim.evidence_id)
    refs.extend(_string_values(claim.support))
    for key in EVIDENCE_REF_KEYS:
        refs.extend(_string_values(claim.legacy_unknown_fields.get(key)))
    return _dedupe(refs)


def _action_evidence_refs(action: SendAction) -> list[str]:
    refs = []
    if action.evidence_id:
        refs.append(action.evidence_id)
    for ref_path, raw_ref in _iter_keyed_values(_action_ref_payload(action), EVIDENCE_REF_KEYS):
        if ref_path:
            refs.extend(_string_values(raw_ref))
    return _dedupe(refs)


def _referenced_evidence(claim: Claim, indexes: _PackageIndexes) -> list[EvidenceItem]:
    return [indexes.evidence_by_id[ref] for ref in _claim_evidence_refs(claim) if ref in indexes.evidence_by_id]


def _first_legacy_value(model: Any, keys: Sequence[str]) -> Any:
    legacy = getattr(model, "legacy_unknown_fields", {}) or {}
    for key in keys:
        if key in legacy:
            return legacy[key]
    return None


def _evidence_field_values_contain(evidence: EvidenceItem, expected: str, field_name: Any) -> bool:
    values = _evidence_field_values(evidence)
    if not values:
        return False
    field_text = str(field_name or "").strip()
    if field_text and field_text in values:
        return expected in {_normalize_fact_value(item) for item in _flatten_values(values[field_text])}
    haystack = {_normalize_fact_value(item) for item in _flatten_values(values)}
    return expected in haystack


def _evidence_field_values(evidence: EvidenceItem) -> Mapping[str, Any]:
    for source in (evidence.metadata, evidence.legacy_unknown_fields):
        for key in FIELD_VALUE_KEYS:
            raw_values = source.get(key) if isinstance(source, Mapping) else None
            if isinstance(raw_values, Mapping):
                return raw_values
    return {}


def _iter_candidate_number_refs(model: Any) -> Iterable[tuple[str, Any]]:
    payload = _candidate_ref_payload(model)
    yield from _iter_keyed_values(payload, CANDIDATE_NUMBER_KEYS)


def _candidate_ref_payload(model: Any) -> Any:
    if isinstance(model, Claim):
        return {"legacy_unknown_fields": model.legacy_unknown_fields}
    if isinstance(model, SendAction):
        return {
            "payload": model.payload,
            "metadata": model.metadata,
            "legacy_unknown_fields": model.legacy_unknown_fields,
        }
    return model


def _action_ref_payload(action: SendAction) -> dict[str, Any]:
    return {
        "payload": action.payload,
        "metadata": action.metadata,
        "legacy_unknown_fields": action.legacy_unknown_fields,
    }


def _iter_keyed_values(value: Any, keys: Sequence[str], prefix: str = "") -> Iterable[tuple[str, Any]]:
    normalized_keys = {key.lower() for key in keys}
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if key.lower() in normalized_keys:
                yield path, item
            yield from _iter_keyed_values(item, keys, path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from _iter_keyed_values(item, keys, path)


def _coerce_candidate_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _string_values(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_sequence(value) if str(item).strip()]


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _flatten_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _flatten_values(item)
    elif isinstance(value, list | tuple | set):
        for item in value:
            yield from _flatten_values(item)
    else:
        yield value


def _normalize_fact_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value).strip().lower())


def _contains_sensitive_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if _key_mentions_password(key) or _key_mentions_link(key):
                return True
            if _contains_sensitive_value(item):
                return True
        return False
    if isinstance(value, list | tuple | set):
        return any(_contains_sensitive_value(item) for item in value)
    text = str(value or "")
    return bool(URL_PATTERN.search(text) or PASSWORD_CODE_PATTERN.search(text))


def _contains_password_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_key_mentions_password(str(key).lower()) or _contains_password_value(item) for key, item in value.items())
    if isinstance(value, list | tuple | set):
        return any(_contains_password_value(item) for item in value)
    return bool(PASSWORD_CODE_PATTERN.search(str(value or "")))


def _contains_link_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_key_mentions_link(str(key).lower()) or _contains_link_value(item) for key, item in value.items())
    if isinstance(value, list | tuple | set):
        return any(_contains_link_value(item) for item in value)
    return bool(URL_PATTERN.search(str(value or "")))


def _claim_visible_payload(claim: Claim) -> dict[str, Any]:
    return {
        "text": claim.text,
        "legacy_unknown_fields": claim.legacy_unknown_fields,
    }


def _mentions_password(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_mentions_password(key) or _mentions_password(item) for key, item in value.items())
    if isinstance(value, list | tuple | set):
        return any(_mentions_password(item) for item in value)
    return _key_mentions_password(str(value or "").lower())


def _key_mentions_password(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in PASSWORD_MARKERS)


def _key_mentions_link(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in LINK_KEY_MARKERS)


def _is_password_action(action: SendAction) -> bool:
    return _mentions_password(
        {
            "action_type": action.action_type,
            "payload": action.payload,
            "metadata": action.metadata,
            "sensitive_payload": action.sensitive_payload,
            "legacy_unknown_fields": action.legacy_unknown_fields,
        }
    )


def _is_evidence_bound_password_claim(claim: Claim, indexes: _PackageIndexes) -> bool:
    refs = [ref for ref in _claim_evidence_refs(claim) if ref in indexes.evidence_by_id]
    if not refs:
        return False
    return any(_evidence_allows_password(indexes.evidence_by_id[ref]) for ref in refs)


def _is_evidence_bound_password_action(action: SendAction, indexes: _PackageIndexes) -> bool:
    refs = [ref for ref in _action_evidence_refs(action) if ref in indexes.evidence_by_id]
    if not refs:
        return False
    if not _mentions_password(
        {
            "action_type": action.action_type,
            "metadata": action.metadata,
            "legacy_unknown_fields": action.legacy_unknown_fields,
        }
    ):
        return False
    return any(_evidence_allows_password(indexes.evidence_by_id[ref]) for ref in refs)


def _evidence_allows_password(evidence: EvidenceItem) -> bool:
    evidence_type = str(evidence.evidence_type or "").strip().lower()
    metadata = evidence.metadata if isinstance(evidence.metadata, Mapping) else {}
    channel = str(metadata.get("controlled_channel") or "").strip().lower()
    return evidence_type in {"viewing_password", "password", "viewing"} or channel == "viewing_password"


def _is_media_action(action: SendAction) -> bool:
    return _is_video_action(action) or _is_image_action(action) or _is_sheet_action(action)


def _is_video_action(action: SendAction) -> bool:
    return _contains_any(_action_search_text(action), MEDIA_VIDEO_MARKERS)


def _is_image_action(action: SendAction) -> bool:
    return _contains_any(_action_search_text(action), MEDIA_IMAGE_MARKERS)


def _is_sheet_action(action: SendAction) -> bool:
    return _contains_any(_action_search_text(action), MEDIA_SHEET_MARKERS)


def _action_search_text(action: SendAction) -> str:
    return " ".join(
        [
            action.action_type,
            _stable_json(action.payload),
            _stable_json(action.metadata),
            _stable_json(action.legacy_unknown_fields),
        ]
    )


def _task_search_text(task: Any) -> str:
    return " ".join(
        [
            str(getattr(task, "task_id", "")),
            str(getattr(task, "task_type", "")),
            str(getattr(task, "user_text", "")),
            _stable_json(getattr(task, "constraints", {})),
            _stable_json(getattr(task, "required_tools", [])),
        ]
    )


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _action_signature(action: SendAction) -> str:
    if action.evidence_id:
        return f"{action.action_type}|evidence:{action.evidence_id}"
    return f"{action.action_type}|payload:{_stable_json(action.payload)}|metadata:{_stable_json(action.metadata)}"


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def _issue(level: ValidationLevel, code: str, message: str, path: str = "", subject_id: str = "") -> ValidationIssue:
    return ValidationIssue(level=level, code=code, message=message, path=path, subject_id=subject_id)
