from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Any

from pydantic import ValidationError

from app.services.inventory_query import ROOM_TYPE_GROUPS
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
from app.services.kf_dual_llm_shadow import ROW_ALIASES
from app.services.region_inventory_constants import active_area_alias_groups


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
    action_by_id: dict[str, SendAction]
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
UTILITY_MARKERS = ("utility", "utilities", "水电", "水费", "电费", "水电费", "民用水电")
UTILITY_MISSING_MARKERS = ("暂无", "暂时没", "暂未", "没有", "未提供", "没写", "没标", "备注里暂时没有", "具体房源")
DEPOSIT_MARKERS = ("deposit", "免押", "无忧住", "押金", "芝麻信用")
DEPOSIT_CONDITION_MARKERS = ("能免押", "能不能免押", "可以免押", "支不支持免押", "免押条件", "免押金要什么条件")
DEPOSIT_SELFCHECK_MARKERS = ("自查", "信用额度", "租房板块申请额度", "申请额度", "支付宝：我的", "支付宝-我的", "支付宝 我的")

# 户型/区域声称核验（L3）：只拦"回复声明了且与证据行字段直接矛盾"，不拦"未声明"——
# LLM2 自检提示词明文允许回复不逐字复述约束，因此否定/回声句段整体豁免，
# 且声称只在"肯定推荐上下文"（同句含证据行标签或"有一套/这套"类措辞）里计入。
# 词表单一事实源：户型口语映射=inventory_query.ROOM_TYPE_GROUPS，
# 区域别名=region_inventory_constants.active_area_alias_groups，
# 证据行字段键名=kf_dual_llm_shadow.ROW_ALIASES；本模块不得新增同源规则拷贝。
# 逗号/顿号必须纳入分句:否则"这套三室的，其他房型暂时没有"这类客服高频
# "有A没有B"逗号句式会整段命中否定豁免,幻觉户型/区域(三室)随否定词(没有)
# 一起被跳过而漏拦(2026-07-05 审计实证:H1)。分子句后否定只豁免其所在子句。
CLAIM_SENTENCE_SPLIT_PATTERN = re.compile(r"[。；;！!？?，,、\n\r]+")
CLAIM_NEGATION_MARKERS = (
    "暂无",
    "没有",
    "没得",
    "没找到",
    "没查到",
    "查不到",
    "找不到",
    "暂时没",
    "暂未",
    "没能",
    "不满足",
    "不符合",
    "不是",
    "已剔除",
    "剔除",
)
CLAIM_ECHO_MARKERS = (
    "你要",
    "您要",
    "你说",
    "您说",
    "你想",
    "您想",
    "你之前",
    "您之前",
    "你问",
    "您问",
    "要求",
    "需求",
    "按你",
    "按您",
)
CLAIM_AFFIRMATIVE_MARKERS = (
    "有一套",
    "有套",
    "有一间",
    "有两套",
    "有三套",
    "有几套",
    "还有",
    "这套",
    "这一套",
    "这间",
    "这几套",
    "这边有",
    "现在有",
    "查到",
    "找到",
    "匹配到",
    "筛出",
    "筛选",
    "推荐",
)
LAYOUT_FIELD_KEYS = ROW_ALIASES["layout"] + ROW_ALIASES["layout_description"]
LAYOUT_LABEL_FIELD_KEYS = ROW_ALIASES["layout"]
AREA_FIELD_KEYS = ROW_ALIASES["area"]
ROW_LABEL_FIELD_KEYS = ROW_ALIASES["community"] + ROW_ALIASES["room_no"] + ROW_ALIASES["title"]
LISTING_HINT_FIELD_KEYS = ROW_LABEL_FIELD_KEYS + LAYOUT_FIELD_KEYS + AREA_FIELD_KEYS + ROW_ALIASES["listing_id"]


def _layout_claim_alias_table() -> tuple[tuple[str, str], ...]:
    # 长别名在前，避免"两室一厅"被"两室"截断；比对统一收敛到 broad 标签（一室/两室/...），
    # 使"两室"对"两室一厅/两室两厅"按包含关系放行（误伤面一：泛称包含）。
    pairs: dict[str, str] = {}
    for label, query_aliases, match_aliases, broad_label in ROOM_TYPE_GROUPS:
        for alias in (label, *query_aliases, *match_aliases):
            pairs.setdefault(alias, broad_label)
    return tuple(sorted(pairs.items(), key=lambda item: (-len(item[0]), item[0])))


def _area_claim_alias_table() -> tuple[tuple[str, str], ...]:
    pairs = {alias: " ".join(parts) for alias, parts in active_area_alias_groups().items()}
    return tuple(sorted(pairs.items(), key=lambda item: (-len(item[0]), item[0])))


LAYOUT_CLAIM_ALIAS_TABLE = _layout_claim_alias_table()
AREA_CLAIM_ALIAS_TABLE = _area_claim_alias_table()

URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
PASSWORD_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{3,8}#(?![A-Za-z0-9])")
INTERNAL_LEAK_PATTERN = re.compile(
    r"\b(?:listing_id|candidate_number|candidate_set_id|evidence_id|field_values|raw_tool_result|schema_version|"
    r"PreparedOutboundPackage|ToolEvidenceBundle|StructuredTaskPacket|inventory\.search|dual_llm_shadow|LLM2|Planner)\b",
    re.IGNORECASE,
)
TEMPLATE_PATTERN = re.compile(r"(?:XX|某某|某小区|某房号|TODO|\{\{|\}\}|<小区>|<房号>|例如某套|示例房源)")
IMMEDIATE_MEDIA_SEND_CLAIM = (
    r"(?:马上|现在|这就|立即).{0,24}(?:(?:发|发送|传).{0,12}(?:视频|图片|照片|素材)|(?:视频|图片|照片|素材).{0,8}(?:发|发送|传))"
)
FUTURE_SEND_PATTERN = re.compile(
    rf"(?:稍后|等下|待会|晚点|之后).{{0,8}}(?:发|传)|(?:可以|能|会).{{0,4}}发你|{IMMEDIATE_MEDIA_SEND_CLAIM}"
)
PAST_SENT_PATTERN = re.compile(
    rf"(?:已发|已发送|已经发|已经发送|发你了|发给你了|给你发过去了|正在发送|发送中|这是.{{0,16}}(?:视频|图片|房源表)|{IMMEDIATE_MEDIA_SEND_CLAIM})"
)
MISSING_MEDIA_PATTERN = re.compile(r"(?:暂无|没有|没找到|暂时没|暂未(?:找到)?).{0,8}(?:视频|图片|照片)")
GENERIC_WAITING_PATTERN = re.compile(
    r"(?:我先帮[你您]?(?:确认|核实)|先(?:帮[你您]?)?(?:确认|核实)|稍后(?:再)?(?:给[你您])?(?:回复|答复)|"
    r"晚点(?:再)?(?:给[你您])?(?:回复|答复)|避免发错|确认一下最新房态)"
)
HARD_FORBIDDEN_HUMAN_PATTERN = re.compile(
    r"(?:作为(?:一个)?AI|系统显示|根据上下文|无法完成该请求|马上通知[你您]|稍后(?:会)?通知[你您]|稍后(?:会)?为[你您]推送|"
    r"稍后(?:会|将)?.{0,16}(?:发|发送|给|安排)|稍等.{0,12}(?:同步|确认|回复|发)|"
    r"有新(?:房源|资源)(?:会)?第一时间通知|通过系统|受控通道|受控渠道|专属联系通道|联系通道)",
)
OUTBOUND_FORBIDDEN_INCIDENT_PATTERN = re.compile(r"(?:工具未绑定|上一轮只有\s*\d+\s*套候选|客户(?:要查询|选择了))")
RAW_CANDIDATE_ROW_PATTERN = re.compile(
    r"(?:^|[\n\r。；;])\s*候选\s*\d+\s+[^，。；;\n\r:：]{2,40}\s+[\w一二三四五六七八九十栋幢单元座楼室-]+"
    r"(?:\s|，|,|、).{0,60}(?:租金|押一付一|押二付一|价格|月租)\s*\d+",
    re.IGNORECASE,
)
MISSING_MEDIA_TEMPLATE_PATTERN = re.compile(
    r"[^，。；;\n\r:：]{2,40}[:：]\s*(?:图片|照片|视频)\s*(?:暂无|没有|没找到|暂时没|暂未(?:找到)?).{0,12}(?:可发送)?(?:视频|图片|照片)"
)
FILTER_CONTRADICTION_PATTERN = re.compile(
    r"(?:匹配到|查到|有)\s*(?:这|以下|共|约)?\s*[\d一二三四五六七八九十]+\s*套.{0,200}(?:不满足|已剔除|剔除)"
)


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
    action_by_id = {action.action_id: action for action in package.send_actions if action.action_id}
    candidate_numbers = {item.candidate_number for item in _candidate_items(package.candidate_set)}
    for evidence in evidence_items:
        candidate_numbers.update(_candidate_numbers_from_model(evidence))
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
        action_by_id=action_by_id,
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

    caption_action_ids_seen: set[str] = set()
    for index, caption in enumerate(package.action_captions):
        path = f"action_captions[{index}]"
        if not caption.action_id:
            issues.append(_issue(ValidationLevel.L0, "l0.missing_action_ref", "Action caption must reference a send action.", path))
            continue
        if caption.action_id in caption_action_ids_seen:
            issues.append(
                _issue(
                    ValidationLevel.L0,
                    "l0.duplicate_action_caption",
                    "Duplicate action caption for the same action_id is not allowed.",
                    f"{path}.action_id",
                    caption.action_id,
                )
            )
        caption_action_ids_seen.add(caption.action_id)
        action = indexes.action_by_id.get(caption.action_id)
        if action is None:
            issues.append(
                _issue(
                    ValidationLevel.L0,
                    "l0.unknown_action_ref",
                    "Action caption references a send action that does not exist.",
                    f"{path}.action_id",
                    caption.action_id,
                )
            )
            continue
        if caption.action_type and caption.action_type != action.action_type:
            issues.append(
                _issue(
                    ValidationLevel.L0,
                    "l0.action_caption_type_mismatch",
                    "Action caption action_type must match the referenced send action.",
                    f"{path}.action_type",
                    caption.action_id,
                )
            )

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
    issues.extend(_validate_task_answered(package, indexes, context))
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
    has_media_action = any(_is_media_action(action) for action in package.send_actions)
    allow_missing_media_text = _package_has_missing_media_evidence(package)
    if text:
        issues.extend(
            _validate_l3_text(
                text,
                "reply_text",
                context=context,
                has_media_action=has_media_action,
                allow_missing_media_text=allow_missing_media_text,
            )
        )
        issues.extend(_validate_reply_claims_against_evidence(package))
    for index, caption in enumerate(package.action_captions):
        action = _action_for_caption(caption.action_id, package)
        caption_has_media_action = _is_media_action(action) if action is not None else False
        issues.extend(
            _validate_l3_text(
                caption.text,
                f"action_captions[{index}].text",
                context=None,
                has_media_action=caption_has_media_action,
                allow_missing_media_text=False,
            )
        )
    return issues


def _package_has_missing_media_evidence(package: PreparedOutboundPackage) -> bool:
    if package.missing_items:
        return True
    return any(evidence.evidence_type == "missing_media" for evidence in _evidence_items(package.evidence_bundle))


def _validate_l3_text(
    text: str,
    path: str,
    *,
    context: OutboundValidationContext | None,
    has_media_action: bool,
    allow_missing_media_text: bool,
) -> list[ValidationIssue]:
    if not text:
        return []
    issues: list[ValidationIssue] = []
    if INTERNAL_LEAK_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.internal_name_leak", "Reply leaks internal field/tool names; regenerate wording only.", path)
        )
    if HARD_FORBIDDEN_HUMAN_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.forbidden_human_phrase", "Reply contains a hard-forbidden customer-service phrase; regenerate wording only.", path)
        )
    if (
        OUTBOUND_FORBIDDEN_INCIDENT_PATTERN.search(text)
        or RAW_CANDIDATE_ROW_PATTERN.search(text)
        or MISSING_MEDIA_TEMPLATE_PATTERN.search(text)
    ):
        issues.append(
            _issue(
                ValidationLevel.L3,
                "l3.outbound_forbidden_incident_phrase",
                "Reply contains forbidden internal incident wording; regenerate wording only.",
                path,
            )
        )
    if TEMPLATE_PATTERN.search(text):
        issues.append(_issue(ValidationLevel.L3, "l3.template_talk", "Reply contains placeholder or template wording; regenerate wording only.", path))
    if FILTER_CONTRADICTION_PATTERN.search(text):
        issues.append(
            _issue(
                ValidationLevel.L3,
                "l3.filter_contradiction",
                "Reply says rooms match while also saying one listed room is excluded; regenerate wording only.",
                path,
            )
        )
    if GENERIC_WAITING_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.generic_waiting_reply", "Reply is a generic waiting/confirmation fallback; regenerate wording only.", path)
        )
    if context is not None and _asks_known_condition_again(text, context):
        issues.append(
            _issue(ValidationLevel.L3, "l3.repeats_known_condition", "Reply asks again for conditions already known in context.", path)
        )
    if has_media_action and FUTURE_SEND_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply uses future tense although media action is already prepared.", path)
        )
    if not has_media_action and PAST_SENT_PATTERN.search(text):
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply claims a media action was sent but no media action is prepared.", path)
        )
    if has_media_action and MISSING_MEDIA_PATTERN.search(text) and not allow_missing_media_text:
        issues.append(
            _issue(ValidationLevel.L3, "l3.action_tense_error", "Reply says media is missing while a media send action is prepared.", path)
        )
    return issues


def _action_for_caption(action_id: str, package: PreparedOutboundPackage) -> SendAction | None:
    for action in package.send_actions:
        if action.action_id == action_id:
            return action
    return None


@dataclass(frozen=True)
class _ReplyClaimSupport:
    layout_broads: frozenset[str]
    layout_labels: tuple[str, ...]
    area_groups: frozenset[str]
    row_labels: tuple[str, ...]
    evidence_text: str


def _validate_reply_claims_against_evidence(package: PreparedOutboundPackage) -> list[ValidationIssue]:
    text = str(package.reply_text or "")
    if not text.strip():
        return []
    support = _reply_claim_support(package)
    # 证据行没有对应字段（或字段值不在共享词表内）时该维度不校验：核验只能以证据为基准，fail-open。
    if not support.layout_broads and not support.area_groups:
        return []
    conflicting_layouts: dict[str, str] = {}
    conflicting_areas: dict[str, str] = {}
    for sentence in CLAIM_SENTENCE_SPLIT_PATTERN.split(text):
        segment = sentence.strip()
        if not segment:
            continue
        if _contains_any(segment, CLAIM_NEGATION_MARKERS) or _contains_any(segment, CLAIM_ECHO_MARKERS):
            continue
        if not _sentence_presents_evidence_rows(segment, support):
            continue
        if support.layout_broads:
            for alias, broad_label in _claim_tokens(segment, LAYOUT_CLAIM_ALIAS_TABLE):
                if broad_label in support.layout_broads or alias in support.evidence_text:
                    continue
                conflicting_layouts[alias] = broad_label
        if support.area_groups:
            for alias, area_group in _claim_tokens(segment, AREA_CLAIM_ALIAS_TABLE):
                if area_group in support.area_groups or alias in support.evidence_text:
                    continue
                conflicting_areas[alias] = area_group
    issues: list[ValidationIssue] = []
    if conflicting_layouts:
        claimed = "/".join(sorted(conflicting_layouts))
        supported = "/".join(sorted(set(support.layout_labels))[:5]) or "unknown"
        issues.append(
            _issue(
                ValidationLevel.L3,
                "l3.layout_claim_mismatch",
                f"Reply claims layout '{claimed}' but evidence rows only support '{supported}'; rewrite using evidence layout only.",
                "reply_text",
            )
        )
    if conflicting_areas:
        claimed = "/".join(sorted(conflicting_areas))
        supported = "/".join(sorted(support.area_groups)[:4]) or "unknown"
        issues.append(
            _issue(
                ValidationLevel.L3,
                "l3.area_claim_mismatch",
                f"Reply claims area '{claimed}' but evidence rows only support '{supported}'; rewrite using evidence area only.",
                "reply_text",
            )
        )
    return issues


def _reply_claim_support(package: PreparedOutboundPackage) -> _ReplyClaimSupport:
    layout_broads: set[str] = set()
    layout_labels: list[str] = []
    area_groups: set[str] = set()
    row_labels: list[str] = []
    evidence_chunks: list[str] = []
    for values, summary in _listing_like_evidence_payloads(package):
        layout_text = " ".join(_field_text_values(values, LAYOUT_FIELD_KEYS))
        if layout_text:
            for alias, broad_label in LAYOUT_CLAIM_ALIAS_TABLE:
                if alias in layout_text:
                    layout_broads.add(broad_label)
            layout_labels.extend(_field_text_values(values, LAYOUT_LABEL_FIELD_KEYS))
        area_text = " ".join(_field_text_values(values, AREA_FIELD_KEYS))
        if area_text:
            for alias, area_group in AREA_CLAIM_ALIAS_TABLE:
                if alias in area_text:
                    area_groups.add(area_group)
        for label in _field_text_values(values, ROW_LABEL_FIELD_KEYS):
            if len(label) >= 2:
                row_labels.append(label)
        evidence_chunks.append(" ".join(str(item or "").strip() for item in _flatten_values(values) if str(item or "").strip()))
        if summary:
            evidence_chunks.append(summary)
    for item in _candidate_items(package.candidate_set):
        for label in (item.community, item.room_no, item.title):
            label_text = str(label or "").strip()
            if len(label_text) >= 2:
                row_labels.append(label_text)
                evidence_chunks.append(label_text)
    return _ReplyClaimSupport(
        layout_broads=frozenset(layout_broads),
        layout_labels=tuple(dict.fromkeys(layout_labels)),
        area_groups=frozenset(area_groups),
        row_labels=tuple(dict.fromkeys(row_labels)),
        evidence_text=" ".join(chunk for chunk in evidence_chunks if chunk),
    )


def _listing_like_evidence_payloads(package: PreparedOutboundPackage) -> Iterable[tuple[Mapping[str, Any], str]]:
    # 支持面只取"像房源行"的证据（有 listing_id 或带行字段键），规则卡/错误码类证据
    # 不得为户型/区域声称背书；task_packet 约束更不算证据——那正是幻觉的来源。
    for evidence in _evidence_items(package.evidence_bundle):
        values = _evidence_field_values(evidence)
        if not evidence.listing_id and not any(key in values for key in LISTING_HINT_FIELD_KEYS):
            continue
        yield values, str(evidence.summary or "")


def _sentence_presents_evidence_rows(sentence: str, support: _ReplyClaimSupport) -> bool:
    if any(label in sentence for label in support.row_labels):
        return True
    return _contains_any(sentence, CLAIM_AFFIRMATIVE_MARKERS)


def _claim_tokens(text: str, alias_table: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    occupied: list[tuple[int, int]] = []
    for alias, mapped in alias_table:
        start = 0
        while True:
            index = text.find(alias, start)
            if index < 0:
                break
            end = index + len(alias)
            start = end
            if any(index < taken_end and taken_start < end for taken_start, taken_end in occupied):
                continue
            occupied.append((index, end))
            found.append((alias, mapped))
    return found


def _field_text_values(values: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    if not isinstance(values, Mapping):
        return result
    for key in keys:
        for item in _flatten_values(values.get(key)):
            text = str(item or "").strip()
            if text:
                result.append(text)
    return result


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
    # H3(2026-07-05 审计):本校验只应拦"LLM2 编造的结构化事实值不在证据里",不得核验
    # 控制/兜底类 claim(field=missing_target/deposit_policy/viewing_contact/selection_error
    # 等,value 是话术不是事实)——否则话术不在证据 field_values 里会被误判、send_allowed=False
    # 致对客无回复(naive 回退一等属性曾实证 9 项测试红)。
    # 事实 claim 判别位(两条同时满足):
    #   1) claim 声称的 field 经 ROW_ALIASES 归一命中"已知库存行字段"(area/layout/rent_pay1/
    #      community/room_no/utilities/candidate_number 等)——控制 claim 的 field
    #      (missing_target/deposit_policy/viewing_contact/selection_error 等)不在 ROW_ALIASES,
    #      归一为空即豁免。注意:控制证据的 field_values 会带同名状态标志键(如 missing_target=True),
    #      故不能只靠"字段名是证据键"判别(2026-07-05 审计 H3 修复(2)之所以仍红的根因)。
    #   2) 该行字段被引用证据的 field_values 结构化承载(经 ROW_ALIASES 归一到同键)——只在证据
    #      确有该字段结构化值时才做字段作用域值核验,证据没有该字段则无从比对,豁免。
    # 值/字段读取兼容一等属性(生产/规范 schema:claim.value/claim.field)与 legacy_unknown_fields(旧形态)。
    field_name = _claim_fact_field(claim)
    if not _canonical_field_key(field_name):
        return []
    raw_value = _claim_fact_value(claim)
    if raw_value is None:
        return []
    referenced_evidence = _referenced_evidence(claim, indexes)
    if not referenced_evidence:
        return []
    fact_evidence = [
        evidence for evidence in referenced_evidence if _evidence_carries_claim_field(evidence, field_name)
    ]
    if not fact_evidence:
        return []
    issues: list[ValidationIssue] = []
    for value in _as_sequence(raw_value):
        normalized_value = _normalize_fact_value(value)
        if not normalized_value:
            continue
        if not any(_evidence_field_scoped_contains(evidence, normalized_value, field_name) for evidence in fact_evidence):
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


def _validate_task_answered(
    package: PreparedOutboundPackage,
    indexes: _PackageIndexes,
    context: OutboundValidationContext,
) -> list[ValidationIssue]:
    task_packet = context.task_packet
    if task_packet is None:
        return []
    explicit_answered = set(context.answered_task_ids) | set(_string_values(package.legacy_unknown_fields.get("answered_task_ids")))
    issues: list[ValidationIssue] = []
    for index, task in enumerate(task_packet.tasks):
        if task.task_id in explicit_answered:
            continue
        if not _task_is_answered(task, package, indexes):
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


def _task_is_answered(task: Any, package: PreparedOutboundPackage, indexes: _PackageIndexes) -> bool:
    task_text = _task_search_text(task)
    task_type = str(getattr(task, "task_type", "") or "").strip()
    if task_type == "reply_compose_signal":
        return bool((package.reply_text or "").strip()) or bool(package.claims) or bool(package.send_actions)
    if _contains_any(task_text, MEDIA_VIDEO_MARKERS):
        return any(_is_video_action(action) for action in package.send_actions) or _has_missing_media_answer(
            package,
            expected_kind="video",
        ) or _has_target_error_answer(package, indexes)
    if _contains_any(task_text, MEDIA_IMAGE_MARKERS):
        return any(_is_image_action(action) for action in package.send_actions) or _has_missing_media_answer(
            package,
            expected_kind="image",
        ) or _has_target_error_answer(package, indexes)
    if _contains_any(task_text, MEDIA_SHEET_MARKERS):
        return any(_is_sheet_action(action) for action in package.send_actions)
    if _contains_any(task_text, UTILITY_MARKERS):
        return _has_utility_answer(package, indexes)
    if _contains_any(task_text, DEPOSIT_MARKERS):
        return _has_deposit_answer(task_text, package)
    if _mentions_password(task_text):
        return any(_is_password_action(action) for action in package.send_actions) or any(
            _mentions_password(_claim_visible_payload(claim)) for claim in package.claims
        ) or any(_is_evidence_bound_viewing_contact_action(action, indexes) for action in package.send_actions) or _has_target_error_answer(
            package,
            indexes,
        )
    return bool((package.reply_text or "").strip()) or bool(package.claims) or bool(package.send_actions)


def _has_deposit_answer(task_text: str, package: PreparedOutboundPackage) -> bool:
    visible_text = "\n".join(
        item
        for item in [
            str(package.reply_text or "").strip(),
            *(str(_claim_visible_payload(claim) or "") for claim in package.claims),
        ]
        if item
    )
    if not visible_text:
        return False
    if not _contains_any(visible_text, DEPOSIT_MARKERS):
        return False
    if _contains_any(task_text, DEPOSIT_CONDITION_MARKERS):
        return "支付宝" in visible_text and _contains_any(visible_text, DEPOSIT_SELFCHECK_MARKERS)
    return True


def _has_utility_answer(package: PreparedOutboundPackage, indexes: _PackageIndexes) -> bool:
    visible_text = "\n".join(
        item
        for item in [
            str(package.reply_text or "").strip(),
            *(str(_claim_visible_payload(claim) or "") for claim in package.claims),
        ]
        if item
    )
    if not visible_text:
        return False
    if not any(marker in visible_text for marker in ("水电", "水费", "电费", "水", "电")):
        return False
    evidence_texts: list[str] = []
    for evidence in indexes.evidence_by_id.values():
        field_values = _evidence_field_values(evidence)
        for key, value in field_values.items():
            key_text = str(key or "")
            value_text = str(value or "").strip()
            if not value_text:
                continue
            if any(marker in key_text for marker in ("水电", "水费", "电费", "备注", "utility")):
                evidence_texts.append(value_text)
        summary = str(evidence.summary or "").strip()
        if any(marker in summary for marker in UTILITY_MARKERS):
            evidence_texts.append(summary)
    evidence_texts = list(dict.fromkeys(text for text in evidence_texts if text))
    if not evidence_texts:
        return any(marker in visible_text for marker in UTILITY_MISSING_MARKERS)
    for value in evidence_texts:
        if value in visible_text:
            return True
        numbers = re.findall(r"\d+(?:\.\d+)?", value)
        if numbers and all(number in visible_text for number in numbers[:3]):
            return True
    return any(marker in visible_text for marker in UTILITY_MISSING_MARKERS)


def _has_missing_media_answer(package: PreparedOutboundPackage, *, expected_kind: str) -> bool:
    text = package.reply_text or ""
    if not text or not MISSING_MEDIA_PATTERN.search(text):
        return False
    if expected_kind == "video" and "视频" not in text:
        return False
    if expected_kind == "image" and not any(token in text for token in ("图片", "照片")):
        return False
    for evidence in _evidence_items(package.evidence_bundle):
        if evidence.evidence_type != "missing_media":
            continue
        media_kind = str((evidence.field_values or {}).get("media_kind") or "").strip().lower()
        if media_kind in {expected_kind, "media", "video_and_image"} or expected_kind in media_kind:
            return True
        if expected_kind == "video" and "视频" in evidence.summary:
            return True
        if expected_kind == "image" and any(token in evidence.summary for token in ("图片", "照片")):
            return True
    return False


def _has_target_error_answer(package: PreparedOutboundPackage, indexes: _PackageIndexes) -> bool:
    if not str(package.reply_text or "").strip() and not package.claims:
        return False
    for evidence in indexes.evidence_by_id.values():
        evidence_type = str(evidence.evidence_type or "").strip().lower()
        metadata = evidence.metadata if isinstance(evidence.metadata, Mapping) else {}
        field_values = evidence.field_values if isinstance(evidence.field_values, Mapping) else {}
        code = str(metadata.get("controlled_error_code") or field_values.get("error_code") or "").strip().lower()
        if evidence_type in {"selection_error", "field_target_error", "missing_target"}:
            return True
        if code in {"selection_error", "field_target_error", "missing_target", "original_video_target_error"}:
            return True
    return False


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


# 事实 claim 判别用的字段规范化(H3):把 claim.field 与证据 field_values 键统一到
# kf_dual_llm_shadow.ROW_ALIASES 的行字段规范键(单一事实源,不新增同源拷贝,硬规则5)。
# 惰性构建(依赖后定义的 _normalize_fact_value,避免模块加载期前向引用)。
_ROW_ALIAS_CANONICAL: dict[str, str] = {}


def _canonical_field_key(name: Any) -> str:
    if not _ROW_ALIAS_CANONICAL:
        for canonical, aliases in ROW_ALIASES.items():
            for alias in aliases:
                _ROW_ALIAS_CANONICAL[_normalize_fact_value(alias)] = canonical
    return _ROW_ALIAS_CANONICAL.get(_normalize_fact_value(name), "")


def _claim_fact_field(claim: Claim) -> Any:
    field = str(getattr(claim, "field", "") or "").strip()
    if field:
        return field
    return _first_legacy_value(claim, CLAIM_FIELD_KEYS)


def _claim_fact_value(claim: Claim) -> Any:
    value = getattr(claim, "value", None)
    if value is not None:
        return value
    return _first_legacy_value(claim, CLAIM_VALUE_KEYS)


def _evidence_field_keys_for(field_values: Mapping[str, Any], field_name: Any) -> list[str]:
    """证据 field_values 中与 claim 声称字段对应的键:直接同名(归一后)或经 ROW_ALIASES 同规范键。"""
    target_norm = _normalize_fact_value(field_name)
    if not target_norm or not field_values:
        return []
    target_canonical = _canonical_field_key(field_name)
    matches: list[str] = []
    for key in field_values:
        key_norm = _normalize_fact_value(key)
        if key_norm and key_norm == target_norm:
            matches.append(key)
            continue
        if target_canonical and _canonical_field_key(key) == target_canonical:
            matches.append(key)
    return matches


def _evidence_carries_claim_field(evidence: EvidenceItem, field_name: Any) -> bool:
    return bool(_evidence_field_keys_for(_evidence_field_values(evidence), field_name))


def _evidence_field_scoped_contains(evidence: EvidenceItem, expected: str, field_name: Any) -> bool:
    field_values = _evidence_field_values(evidence)
    for key in _evidence_field_keys_for(field_values, field_name):
        if expected in {_normalize_fact_value(item) for item in _flatten_values(field_values[key])}:
            return True
    return False


def _evidence_field_values(evidence: EvidenceItem) -> Mapping[str, Any]:
    if isinstance(evidence.field_values, Mapping) and evidence.field_values:
        return evidence.field_values
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
    if isinstance(model, EvidenceItem):
        return {
            "field_values": model.field_values,
            "metadata": model.metadata,
            "legacy_unknown_fields": model.legacy_unknown_fields,
        }
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


def _candidate_numbers_from_model(model: Any) -> set[int]:
    numbers: set[int] = set()
    for _, raw_ref in _iter_candidate_number_refs(model):
        for value in _as_sequence(raw_ref):
            number = _coerce_candidate_number(value)
            if number is not None:
                numbers.add(number)
    return numbers


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
    if action.action_type == "viewing_contact":
        return False
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


def _is_evidence_bound_viewing_contact_action(action: SendAction, indexes: _PackageIndexes) -> bool:
    refs = [ref for ref in _action_evidence_refs(action) if ref in indexes.evidence_by_id]
    if not refs or not _is_viewing_contact_action(action):
        return False
    return any(_evidence_requires_viewing_contact(indexes.evidence_by_id[ref]) for ref in refs)


def _evidence_allows_password(evidence: EvidenceItem) -> bool:
    evidence_type = str(evidence.evidence_type or "").strip().lower()
    metadata = evidence.metadata if isinstance(evidence.metadata, Mapping) else {}
    channel = str(metadata.get("controlled_channel") or "").strip().lower()
    return evidence_type in {"viewing_password", "password", "viewing"} or channel == "viewing_password"


def _evidence_requires_viewing_contact(evidence: EvidenceItem) -> bool:
    evidence_type = str(evidence.evidence_type or "").strip().lower()
    metadata = evidence.metadata if isinstance(evidence.metadata, Mapping) else {}
    field_values = evidence.field_values if isinstance(evidence.field_values, Mapping) else {}
    channel = str(metadata.get("controlled_channel") or "").strip().lower()
    return (
        evidence_type == "viewing_contact"
        or channel == "viewing_contact"
        or bool(field_values.get("needs_contact"))
    )


def _is_media_action(action: SendAction) -> bool:
    return _is_video_action(action) or _is_image_action(action) or _is_sheet_action(action)


def _is_video_action(action: SendAction) -> bool:
    if str(action.action_type or "").strip().lower() in {"video", "send_video", "room_video"}:
        return True
    return _contains_any(_action_search_text(action), MEDIA_VIDEO_MARKERS)


def _is_image_action(action: SendAction) -> bool:
    if str(action.action_type or "").strip().lower() in {"image", "send_image", "photo", "picture"}:
        return True
    return _contains_any(_action_search_text(action), MEDIA_IMAGE_MARKERS)


def _is_sheet_action(action: SendAction) -> bool:
    if str(action.action_type or "").strip().lower() in {"inventory_sheet", "send_inventory_sheet"}:
        return True
    return _contains_any(_action_search_text(action), MEDIA_SHEET_MARKERS)


def _is_viewing_contact_action(action: SendAction) -> bool:
    metadata = action.metadata if isinstance(action.metadata, Mapping) else {}
    channel = str(metadata.get("controlled_channel") or "").strip().lower()
    return str(action.action_type or "").strip().lower() == "viewing_contact" or channel == "viewing_contact"


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
            _stable_json(_task_search_constraints(getattr(task, "constraints", {}))),
            _stable_json(getattr(task, "required_tools", [])),
        ]
    )


def _task_search_constraints(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    blocked = {
        "confirmed_room",
        "candidate_set",
        "candidates",
        "inventory_rows",
        "target_rows",
        "rows",
        "row",
    }
    return {str(key): item for key, item in value.items() if str(key) not in blocked}


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
