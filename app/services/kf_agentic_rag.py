from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from app.services.fuzzy_match import COMMUNITY_DISPLAY_ALIASES, canonical_community_display


KNOWLEDGE_TOPICS: dict[str, tuple[str, ...]] = {
    "deposit_waiver": ("免押", "押金", "芝麻", "无忧住", "服务费", "信用免押"),
    "contract_booking": (
        "合同",
        "签约",
        "订房",
        "定房",
        "定金",
        "订金",
        "付款",
        "电子合同",
        "预定",
        "预订",
        "锁房",
        "想定",
        "怎么订",
        "订下来",
        "定下来",
        "半个月",
    ),
    "refund_cancel": (
        "退租",
        "转租",
        "退定",
        "退订",
        "退押金",
        "押金退",
        "剩余租金",
        "中途退",
        "提前退",
        "不租了",
    ),
    "maintenance": ("维修", "损坏", "坏了", "自然损坏", "人为损坏", "易损件", "材料费", "工费", "家电", "漏水", "堵塞"),
    "utilities": ("水电", "电费", "水费", "民用水电", "网络", "宽带", "物业费", "物业", "网费", "备注"),
    "viewing": ("看房", "密码", "动态密码", "预约", "空出", "现场", "门锁"),
    "media": ("视频", "图片", "照片", "笔记", "原视频", "不清楚", "发不了", "素材"),
    "owner_price": ("房东", "便宜", "优惠", "讲价", "价格谈", "能不能少"),
    "handoff_exception": ("人工", "联系", "电话", "号码", "不对", "错误", "失败", "争议", "投诉", "特殊约定"),
}
INVENTORY_FACT_WORDS = ("房源", "价格", "房租", "多少钱", "还在", "在租", "空房", "房号")
ROOM_REFERENCE_PATTERN = re.compile(r"\d+(?:[-－—][a-zA-Z0-9]+)+")


@dataclass(frozen=True)
class ReferenceConfirmation:
    status: str
    kind: str
    raw_text: str
    suggested_text: str = ""
    rewritten_query: str = ""
    options: tuple[str, ...] = ()
    confidence: str = "medium"
    reason: str = ""


@dataclass(frozen=True)
class UserNeedRewrite:
    original: str
    normalized_query: str
    topics: list[str]
    needs_knowledge: bool
    needs_inventory: bool
    needs_media: bool
    reason: str
    reference_confirmation: ReferenceConfirmation | None = None


@dataclass(frozen=True)
class KnowledgeChunk:
    doc_id: str
    title: str
    source: str
    content: str
    topics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RagEvidence:
    title: str
    source: str
    content: str
    score: float
    topics: list[str] = field(default_factory=list)
    kind: str = "knowledge"


@dataclass(frozen=True)
class AgenticRagResult:
    enabled: bool
    used: bool
    need: UserNeedRewrite
    evidence: list[RagEvidence]
    context_text: str
    trace: list[str]
    dynamic_evidence: list[RagEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class SelfCheckReport:
    passed: bool
    score: int = 100
    hard_fail: bool = False
    fail_reasons: tuple[str, ...] = ()
    missing_actions: tuple[str, ...] = ()
    retry_instruction: str = ""
    risk: str = "low"

    def to_retry_reason(self) -> str:
        parts = [
            f"selfcheck_pass={self.passed}",
            f"score={self.score}",
            f"hard_fail={self.hard_fail}",
            f"risk={self.risk}",
        ]
        if self.fail_reasons:
            parts.append("fail_reasons=" + "；".join(self.fail_reasons))
        if self.missing_actions:
            parts.append("missing_actions=" + ",".join(self.missing_actions))
        if self.retry_instruction:
            parts.append("retry_instruction=" + self.retry_instruction)
        return "\n".join(parts)


@dataclass(frozen=True)
class AgenticRagAssessment:
    action: str
    reason: str = ""
    fallback_text: str = ""
    status: str = ""
    fallback_reply: str = ""
    report: SelfCheckReport | None = None

    def __post_init__(self) -> None:
        status = (self.status or self.action or "pass").strip()
        fallback_text = (self.fallback_text or self.fallback_reply or "").strip()
        object.__setattr__(self, "action", (self.action or status).strip())
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "fallback_text", fallback_text)
        object.__setattr__(self, "fallback_reply", fallback_text)


class KfAgenticRagService:
    def __init__(
        self,
        *,
        knowledge_dir: Path,
        enabled: bool = True,
        max_evidence: int = 5,
        min_score: float = 2.0,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.enabled = enabled
        self.max_evidence = max_evidence
        self.min_score = min_score
        self._cache_signature: tuple[tuple[str, float], ...] | None = None
        self._chunks: list[KnowledgeChunk] = []

    async def retrieve_for_reply(
        self,
        *,
        content: str,
        conversation_context: str = "",
        rooms: list[dict[str, Any]] | None = None,
        inventory_snapshot: str = "",
        media_images: list[str] | None = None,
        media_videos: list[str] | None = None,
        row_video_paths: list[Path] | None = None,
        row_image_paths: list[Path] | None = None,
        recent_context: dict[str, Any] | None = None,
        inventory_rows: list[dict[str, Any]] | None = None,
        retry_reason: str = "",
        original_content: str = "",
    ) -> AgenticRagResult:
        rooms = rooms or []
        need = self.rewrite_user_need(
            content,
            original_content=original_content,
            conversation_context=conversation_context,
            rooms=rooms,
            inventory_rows=inventory_rows,
            retry_reason=retry_reason,
        )
        dynamic_evidence = self.build_dynamic_evidence(
            content=content,
            need=need,
            rooms=rooms,
            inventory_snapshot=inventory_snapshot,
            media_images=media_images or [],
            media_videos=media_videos or [],
            row_video_paths=row_video_paths or [],
            row_image_paths=row_image_paths or [],
            recent_context=recent_context,
        )
        trace = [
            f"rewrite: topics={','.join(need.topics) or 'none'} knowledge={need.needs_knowledge} reason={need.reason}"
        ]
        if not self.enabled:
            return AgenticRagResult(False, False, need, [], "", trace + ["disabled"], dynamic_evidence)

        evidence: list[RagEvidence] = []
        if need.needs_knowledge:
            query = self._build_query(need, conversation_context=conversation_context, retry_reason=retry_reason)
            evidence = self.retrieve(query, topics=need.topics)
            trace.append(f"retrieve[1]: query={query[:120]} evidence={len(evidence)}")
            if not evidence and retry_reason:
                expanded_query = f"{query}\n{self._topic_keywords_text(need.topics)}"
                evidence = self.retrieve(
                    expanded_query,
                    topics=need.topics,
                    min_score=max(1.0, self.min_score - 1.0),
                )
                trace.append(f"retrieve[2]: expanded evidence={len(evidence)}")
        else:
            trace.append("knowledge retrieval skipped: deterministic or no static topic")

        context_text = self.format_evidence_context(
            evidence,
            dynamic_evidence=dynamic_evidence,
            need=need,
            inventory_snapshot=inventory_snapshot,
        )
        used = bool(evidence or dynamic_evidence)
        return AgenticRagResult(
            enabled=True,
            used=used,
            need=need,
            evidence=evidence,
            context_text=context_text,
            trace=trace + [f"dynamic evidence={len(dynamic_evidence)}"],
            dynamic_evidence=dynamic_evidence,
        )

    def rewrite_user_need(
        self,
        content: str,
        *,
        conversation_context: str = "",
        rooms: list[dict[str, Any]] | None = None,
        inventory_rows: list[dict[str, Any]] | None = None,
        retry_reason: str = "",
        original_content: str = "",
    ) -> UserNeedRewrite:
        # Static knowledge topics must be triggered by the current task, not by
        # stale dialogue history. Otherwise a previous deposit question can make
        # the next inventory reply fail with a deposit fallback.
        topic_source = original_content or content
        text = f"{topic_source}\n{retry_reason}".strip()
        topics = [
            topic
            for topic, keywords in KNOWLEDGE_TOPICS.items()
            if any(keyword in text for keyword in keywords)
        ]
        has_room_reference = bool(ROOM_REFERENCE_PATTERN.search(content))
        needs_inventory = bool(rooms) or any(word in content for word in INVENTORY_FACT_WORDS) or has_room_reference
        needs_media = any(word in content for word in ("视频", "图片", "照片", "原视频", "素材")) or any(
            word in content for word in ("清楚一点", "更清楚", "高清", "原版")
        )
        only_deterministic_media = needs_media and not any(
            word in content for word in ("不清楚", "发不了", "为什么", "笔记", "失败")
        )
        needs_knowledge = bool(topics) and not (only_deterministic_media and not retry_reason)
        if needs_inventory and not topics and not retry_reason:
            needs_knowledge = False
        reason = "retry" if retry_reason else ("knowledge_topic" if needs_knowledge else "no_knowledge_needed")
        normalized_query = " ".join(part for part in (content.strip(), retry_reason.strip()) if part)
        reference_confirmation = self.reference_confirmation_for_message(
            content,
            inventory_rows or rooms or [],
        )
        return UserNeedRewrite(
            original=content,
            normalized_query=normalized_query,
            topics=topics,
            needs_knowledge=needs_knowledge,
            needs_inventory=needs_inventory,
            needs_media=needs_media,
            reason=reason,
            reference_confirmation=reference_confirmation,
        )

    def reference_confirmation_for_message(
        self,
        content: str,
        inventory_rows: list[dict[str, Any]],
    ) -> ReferenceConfirmation | None:
        rows = [row for row in inventory_rows if isinstance(row, dict)]
        if not rows or not content.strip():
            return None

        room_check = self._room_reference_confirmation(content, rows)
        if room_check is not None:
            return room_check
        return self._community_reference_confirmation(content, rows)

    def _community_reference_confirmation(
        self,
        content: str,
        rows: list[dict[str, Any]],
    ) -> ReferenceConfirmation | None:
        communities = self._unique_communities(rows)
        alias_candidates = self._explicit_community_alias_candidates(content, communities)
        if alias_candidates:
            raw_text, options = alias_candidates
            if len(options) == 1:
                suggested = options[0]
                return ReferenceConfirmation(
                    status="needs_confirmation",
                    kind="community",
                    raw_text=raw_text,
                    suggested_text=suggested,
                    rewritten_query=content.replace(raw_text, suggested, 1),
                    confidence="medium",
                    reason="single_fuzzy_community",
                )
            return ReferenceConfirmation(
                status="ambiguous",
                kind="community",
                raw_text=raw_text,
                options=tuple(options[:5]),
                confidence="low",
                reason="multiple_fuzzy_communities",
            )
        normalized_content = self._normalize_text(content)
        if any(self._normalize_text(community) in normalized_content for community in communities):
            return None

        candidates = self._community_candidates(content, communities)
        if not candidates:
            return None

        raw_text, options = candidates
        if len(options) == 1:
            suggested = options[0]
            return ReferenceConfirmation(
                status="needs_confirmation",
                kind="community",
                raw_text=raw_text,
                suggested_text=suggested,
                rewritten_query=content.replace(raw_text, suggested, 1),
                confidence="medium",
                reason="single_fuzzy_community",
            )
        return ReferenceConfirmation(
            status="ambiguous",
            kind="community",
            raw_text=raw_text,
            options=tuple(options[:5]),
            confidence="low",
            reason="multiple_fuzzy_communities",
        )

    def _room_reference_confirmation(
        self,
        content: str,
        rows: list[dict[str, Any]],
    ) -> ReferenceConfirmation | None:
        room_refs = ROOM_REFERENCE_PATTERN.findall(content)
        if not room_refs:
            return None

        room_index: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            room_no = self._row_value(row, "房号", "房间号", "门牌")
            normalized = self._normalize_room_no(room_no)
            if normalized:
                room_index.setdefault(normalized, []).append(row)

        for room_ref in room_refs:
            normalized_ref = self._normalize_room_no(room_ref)
            if not normalized_ref or normalized_ref in room_index:
                continue
            matches = self._similar_room_rows(normalized_ref, rows)
            if len(matches) == 1:
                row = matches[0]
                community = self._row_value(row, "小区", "社区", "楼盘")
                room_no = self._row_value(row, "房号", "房间号", "门牌")
                suggested = f"{community}{room_no}".strip()
                return ReferenceConfirmation(
                    status="needs_confirmation",
                    kind="room",
                    raw_text=room_ref,
                    suggested_text=suggested,
                    rewritten_query=content.replace(room_ref, room_no, 1),
                    confidence="medium",
                    reason="single_fuzzy_room_no",
                )
            if len(matches) > 1:
                options = tuple(self._room_label(row) for row in matches[:5] if self._room_label(row))
                return ReferenceConfirmation(
                    status="ambiguous",
                    kind="room",
                    raw_text=room_ref,
                    options=options,
                    confidence="low",
                    reason="multiple_fuzzy_room_no",
                )
            if len(room_refs) == 1 and self._looks_like_specific_room_question(content):
                return ReferenceConfirmation(
                    status="not_found",
                    kind="room",
                    raw_text=room_ref,
                    confidence="low",
                    reason="room_no_not_found",
                )
        return None

    def assess_action(
        self,
        *,
        content: str,
        action: str,
        rag_result: AgenticRagResult | None = None,
    ) -> AgenticRagAssessment:
        text = self._normalize_text(content)
        action = str(action or "").strip()
        supported_actions = {
            "send_image",
            "send_video",
            "send_inventory_sheet",
            "send_contract_contact",
            "send_price_negotiation_contact",
            "reply_inventory_fact",
        }
        if action not in supported_actions:
            return AgenticRagAssessment(action="pass")

        need = rag_result.need if rag_result is not None else self.rewrite_user_need(content)

        if action == "send_inventory_sheet":
            if any(word in content for word in ("房源表", "表格", "表发", "发一下表", "总表")):
                return AgenticRagAssessment(action="pass")
            return AgenticRagAssessment(
                action="fallback",
                reason="inventory_sheet_action_without_sheet_intent",
            )

        if action == "send_contract_contact":
            if "contract_booking" in need.topics and any(
                word in content for word in ("联系", "谁", "找谁", "订", "定", "签", "合同", "定金", "订金")
            ):
                return AgenticRagAssessment(action="pass")
            return AgenticRagAssessment(
                action="fallback",
                reason="contract_contact_action_without_booking_intent",
            )

        if action == "send_price_negotiation_contact":
            if "owner_price" in need.topics or any(
                word in content for word in ("最低价", "能不能谈", "少点", "便宜点", "优惠", "砍价", "谈价")
            ):
                return AgenticRagAssessment(action="pass")
            return AgenticRagAssessment(
                action="fallback",
                reason="price_contact_action_without_negotiation_intent",
            )

        if action == "reply_inventory_fact":
            if need.needs_inventory or any(
                word in content for word in ("同小区", "这小区", "这个小区", "最便宜", "最低价", "房号", "哪套")
            ):
                return AgenticRagAssessment(action="pass")
            return AgenticRagAssessment(
                action="fallback",
                reason="inventory_fact_action_without_inventory_intent",
            )

        if self._has_higher_priority_non_media_intent(content):
            return AgenticRagAssessment(
                action="fallback",
                reason=f"{action}_conflicts_with_non_media_intent",
            )

        if self._looks_like_action_confirmation(content):
            return AgenticRagAssessment(action="pass")

        if action == "send_video" and self._has_explicit_video_intent(content):
            return AgenticRagAssessment(action="pass")
        if action == "send_image" and self._has_explicit_image_intent(content):
            return AgenticRagAssessment(action="pass")

        if need.needs_media and not self._has_higher_priority_non_media_intent(content):
            return AgenticRagAssessment(action="pass")

        if any(word in text for word in ("这套", "那套", "第一套", "第一个", "发我", "都发")):
            return AgenticRagAssessment(action="pass")
        return AgenticRagAssessment(
            action="fallback",
            reason=f"{action}_without_clear_media_intent",
        )

    def _has_explicit_video_intent(self, content: str) -> bool:
        return any(word in content for word in ("视频", "原视频", "笔记", "素材"))

    def _has_explicit_image_intent(self, content: str) -> bool:
        return any(word in content for word in ("图片", "照片", "实拍图", "房间图", "素材"))

    def _looks_like_action_confirmation(self, content: str) -> bool:
        normalized = self._normalize_text(content)
        return normalized in {
            "是",
            "是的",
            "对",
            "对的",
            "嗯",
            "嗯嗯",
            "好的",
            "好",
            "可以",
            "行",
            "发",
            "发我",
            "先发",
        }

    def _has_higher_priority_non_media_intent(self, content: str) -> bool:
        return any(
            word in content
            for word in (
                "免押",
                "押金",
                "芝麻",
                "无忧住",
                "服务费",
                "费率",
                "怎么算",
                "怎么申请",
                "多少钱",
                "价格",
                "房租",
                "押一付",
                "押二付",
                "密码",
                "看房",
                "预约",
                "还在",
                "空出",
                "合同",
                "签约",
                "订房",
                "定房",
                "想定",
                "定金",
                "订金",
            )
        )

    def _unique_communities(self, rows: list[dict[str, Any]]) -> list[str]:
        communities: list[str] = []
        seen: set[str] = set()
        for row in rows:
            community = self._row_value(row, "小区", "社区", "楼盘")
            if not community:
                continue
            normalized = self._normalize_text(community)
            if normalized in seen:
                continue
            seen.add(normalized)
            communities.append(community)
        return communities

    def _community_candidates(self, content: str, communities: list[str]) -> tuple[str, list[str]] | None:
        alias_candidates = self._explicit_community_alias_candidates(content, communities)
        if alias_candidates:
            return alias_candidates

        best_raw = ""
        scored: dict[str, int] = {}
        terms = self._candidate_terms(content)
        for raw in terms:
            raw_norm = self._normalize_text(raw)
            if len(raw_norm) < 2:
                continue
            for community in communities:
                community_norm = self._normalize_text(community)
                if not community_norm or raw_norm == community_norm:
                    continue
                score = self._community_similarity(raw_norm, community_norm)
                if score < 55:
                    continue
                scored[community] = max(scored.get(community, 0), score)
                if not best_raw or len(raw_norm) > len(self._normalize_text(best_raw)):
                    best_raw = raw
        if not scored:
            return None
        ordered = sorted(scored.items(), key=lambda item: item[1], reverse=True)
        top_score = ordered[0][1]
        options = [community for community, score in ordered if score >= max(55, top_score - 18)]
        if not best_raw:
            best_raw = options[0]
        return best_raw, options

    def _explicit_community_alias_candidates(
        self,
        content: str,
        communities: list[str],
    ) -> tuple[str, list[str]] | None:
        aliases = {
            **{wrong: (right,) for wrong, right in COMMUNITY_DISPLAY_ALIASES.items()},
            "杨家府": ("兴业杨家府", "杨家新雅苑", "杨乐府"),
        }
        available = {self._normalize_text(community): community for community in communities}
        for raw, suggestions in aliases.items():
            if raw not in content:
                continue
            if raw == "杨家府" and any(suggestion in content for suggestion in suggestions):
                continue
            options = [
                canonical_community_display(available[self._normalize_text(suggestion)])
                for suggestion in suggestions
                if self._normalize_text(suggestion) in available
            ]
            if options:
                return raw, list(dict.fromkeys(options))
        return None

    def _candidate_terms(self, content: str) -> list[str]:
        compact = re.sub(r"[^一-鿿A-Za-z0-9-]+", " ", content)
        stop_words = {
            "现在",
            "还有",
            "哪些",
            "有房",
            "视频",
            "图片",
            "照片",
            "客户",
            "预算",
            "价格",
            "能看",
            "看房",
            "密码",
            "多少",
            "附近",
            "左右",
        }
        terms: list[str] = []
        for token in compact.split():
            token = token.strip()
            if len(self._normalize_text(token)) < 2:
                continue
            if any(word == token for word in stop_words):
                continue
            terms.append(token)
            normalized = self._normalize_text(token)
            for size in range(min(6, len(normalized)), 1, -1):
                for start in range(0, len(normalized) - size + 1):
                    term = normalized[start : start + size]
                    if term not in stop_words:
                        terms.append(term)
        return list(dict.fromkeys(terms))

    def _community_similarity(self, raw: str, community: str) -> int:
        if raw in community or community in raw:
            return 95 if len(raw) >= 3 else 62
        distance = self._bounded_levenshtein(raw, community, 1)
        if distance == 1:
            return 88
        common = len(set(raw) & set(community))
        if common < 2:
            return 0
        score = common * 18
        if raw[:1] and community.startswith(raw[:1]):
            score += 12
        if raw[-1:] and community.endswith(raw[-1:]):
            score += 12
        return score

    def _similar_room_rows(self, normalized_ref: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        matches: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            room_no = self._row_value(row, "房号", "房间号", "门牌")
            normalized_room = self._normalize_room_no(room_no)
            if not normalized_room:
                continue
            score = 0
            if normalized_ref in normalized_room or normalized_room in normalized_ref:
                score = 90
            elif self._bounded_levenshtein(normalized_ref, normalized_room, 2) is not None:
                score = 82
            elif self._room_tail(normalized_ref) and self._room_tail(normalized_ref) == self._room_tail(normalized_room):
                score = 70
            if score:
                matches.append((score, row))
        matches.sort(key=lambda item: item[0], reverse=True)
        if not matches:
            return []
        top_score = matches[0][0]
        return [row for score, row in matches if score >= top_score - 8][:5]

    def _looks_like_specific_room_question(self, content: str) -> bool:
        return any(
            word in content
            for word in ("密码", "视频", "图片", "照片", "还在", "空不空", "现在空", "看房", "多少钱")
        )

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z一-鿿]", "", str(value or "")).lower()

    def _normalize_room_no(self, value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z]", "", str(value or "")).lower()

    def _room_tail(self, normalized_room_no: str) -> str:
        match = re.search(r"(\d+[a-z]?)$", normalized_room_no)
        return match.group(1) if match else ""

    def _bounded_levenshtein(self, left: str, right: str, max_distance: int) -> int | None:
        if abs(len(left) - len(right)) > max_distance:
            return None
        previous = list(range(len(right) + 1))
        for left_index, left_char in enumerate(left, start=1):
            current = [left_index]
            row_min = current[0]
            for right_index, right_char in enumerate(right, start=1):
                cost = 0 if left_char == right_char else 1
                value = min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + cost,
                )
                current.append(value)
                row_min = min(row_min, value)
            if row_min > max_distance:
                return None
            previous = current
        distance = previous[-1]
        return distance if distance <= max_distance else None

    def retrieve(
        self,
        query: str,
        *,
        topics: list[str],
        min_score: float | None = None,
    ) -> list[RagEvidence]:
        min_score = self.min_score if min_score is None else min_score
        query_terms = self._terms(query)
        topic_terms = set(self._topic_keywords_text(topics).split())
        scored: list[RagEvidence] = []
        for chunk in self._load_chunks():
            chunk_terms = self._terms(chunk.content + "\n" + chunk.title + "\n" + " ".join(chunk.topics))
            overlap = query_terms & chunk_terms
            if not overlap and not (set(chunk.topics) & set(topics)):
                continue
            score = float(len(overlap))
            if set(chunk.topics) & set(topics):
                score += 4.0
            if topic_terms and topic_terms & chunk_terms:
                score += 1.0
            if score < min_score:
                continue
            scored.append(
                RagEvidence(
                    title=chunk.title,
                    source=chunk.source,
                    content=chunk.content,
                    score=score,
                    topics=chunk.topics,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: self.max_evidence]

    def format_evidence_context(
        self,
        evidence: list[RagEvidence],
        *,
        dynamic_evidence: list[RagEvidence] | None = None,
        need: UserNeedRewrite,
        inventory_snapshot: str,
    ) -> str:
        dynamic_evidence = dynamic_evidence or []
        if not evidence and not dynamic_evidence:
            return ""
        lines = [
            "Agentic RAG 证据包：",
            "使用规则：动态证据来自本轮房源表、素材库和上下文，静态证据来自客服知识库；房源、价格、房态、密码、视频/图片是否存在只能引用动态证据，不得补编。需要真正发送图片/视频时仍交给素材发送工具执行。",
            f"需求重写：{need.normalized_query}",
            f"主题：{', '.join(need.topics) or '未识别'}",
        ]
        if dynamic_evidence:
            lines.append("\n动态工具证据：")
            for index, item in enumerate(dynamic_evidence, start=1):
                lines.append(f"\n[D{index}] {item.title}（{item.source}）")
                lines.append(item.content.strip())
        if inventory_snapshot.strip():
            lines.append("\n库存快照说明：本轮回复涉及房源事实时，以动态工具证据和实时房源库存为准。")
        if evidence:
            lines.append("\n静态知识库证据：")
        for index, item in enumerate(evidence, start=1):
            lines.append(f"\n[K{index}] {item.title}（{item.source}，score={item.score:.1f}）")
            lines.append(item.content.strip())
        return "\n".join(lines).strip()

    def build_dynamic_evidence(
        self,
        *,
        content: str,
        need: UserNeedRewrite,
        rooms: list[dict[str, Any]],
        inventory_snapshot: str,
        media_images: list[str],
        media_videos: list[str],
        row_video_paths: list[Path],
        row_image_paths: list[Path],
        recent_context: dict[str, Any] | None,
    ) -> list[RagEvidence]:
        evidence: list[RagEvidence] = []
        if rooms:
            evidence.extend(
                self._room_dynamic_evidence(
                    content=content,
                    need=need,
                    rooms=rooms,
                )
            )
        elif inventory_snapshot.strip():
            evidence.append(
                RagEvidence(
                    title="实时房源库存快照",
                    source="inventory_snapshot",
                    content=self._compact_snapshot(inventory_snapshot),
                    score=10.0,
                    topics=["inventory"],
                    kind="dynamic",
                )
            )

        media_content = self._media_dynamic_content(
            media_images=media_images,
            media_videos=media_videos,
            row_video_paths=row_video_paths,
            row_image_paths=row_image_paths,
        )
        if media_content:
            evidence.append(
                RagEvidence(
                    title="实时素材库匹配结果",
                    source="media_store",
                    content=media_content,
                    score=9.0,
                    topics=["media"],
                    kind="dynamic",
                )
            )

        recent_content = self._recent_context_dynamic_content(recent_context)
        if recent_content:
            evidence.append(
                RagEvidence(
                    title="最近对话上下文素材状态",
                    source="wecom_kf_context",
                    content=recent_content,
                    score=7.0,
                    topics=["context"],
                    kind="dynamic",
                )
            )
        return evidence[: self.max_evidence + 3]

    def _room_dynamic_evidence(
        self,
        *,
        content: str,
        need: UserNeedRewrite,
        rooms: list[dict[str, Any]],
    ) -> list[RagEvidence]:
        include_password = self._should_include_password(content, need)
        evidence: list[RagEvidence] = []
        for index, row in enumerate(rooms[: self.max_evidence], start=1):
            community = self._row_value(row, "小区", "社区", "楼盘")
            room_no = self._row_value(row, "房号", "房间号", "门牌")
            title = f"实时房源表匹配 {index}"
            label = f"{community}{room_no}".strip()
            if label:
                title = f"{title}：{label}"

            parts = [
                self._field_text("区域", self._row_value(row, "区域")),
                self._field_text("小区", community),
                self._field_text("房号", room_no),
                self._field_text("户型描述", self._row_value(row, "户型", "户型描述")),
                self._field_text("户型分类", self._row_value(row, "户型分类")),
                self._field_text("押一付一", self._row_value(row, "押一付一", "押一付")),
                self._field_text("押二付一", self._row_value(row, "押二付一", "押二付")),
                self._field_text("房态", self._row_value(row, "房态", "状态", "空置状态")),
                self._field_text("备注", self._row_value(row, "备注", "水电", "说明")),
            ]
            password = self._row_value(row, "看房方式密码", "看房密码", "密码", "门锁密码")
            if password:
                if include_password:
                    parts.append(self._field_text("看房方式密码", password))
                else:
                    parts.append("看房方式字段：有（客户明确问看房或密码时再引用具体内容）")
            content_text = "；".join(part for part in parts if part)
            if not content_text:
                content_text = "房源表命中该记录，但可展示字段为空。"
            evidence.append(
                RagEvidence(
                    title=title,
                    source="inventory",
                    content=content_text,
                    score=10.0,
                    topics=["inventory"],
                    kind="dynamic",
                )
            )
        return evidence

    def _media_dynamic_content(
        self,
        *,
        media_images: list[str],
        media_videos: list[str],
        row_video_paths: list[Path],
        row_image_paths: list[Path],
    ) -> str:
        lines: list[str] = []
        lines.append(f"本地房间视频：{len(row_video_paths)} 个")
        lines.append(f"本地房间图片：{len(row_image_paths)} 个")
        lines.append(f"可发送图片链接：{len(media_images)} 个")
        lines.append(f"可发送视频链接：{len(media_videos)} 个")
        if row_video_paths:
            lines.append("视频文件示例：" + "、".join(path.name for path in row_video_paths[:3]))
        if row_image_paths:
            lines.append("图片文件示例：" + "、".join(path.name for path in row_image_paths[:3]))
        if media_videos:
            lines.append("视频链接示例：" + "、".join(media_videos[:2]))
        if media_images:
            lines.append("图片链接示例：" + "、".join(media_images[:2]))
        if not any((media_images, media_videos, row_video_paths, row_image_paths)):
            return ""
        lines.append("发送规则：只能发送上述已存在素材；没有素材时不要承诺马上发。")
        return "\n".join(lines)

    def _recent_context_dynamic_content(self, recent_context: dict[str, Any] | None) -> str:
        if not recent_context:
            return ""
        video_paths = list(recent_context.get("video_paths") or [])
        video_urls = list(recent_context.get("video_urls") or [])
        image_paths = list(recent_context.get("image_paths") or [])
        pending_video_sends = recent_context.get("pending_video_sends") or {}
        pending_video_paths = list(pending_video_sends.get("paths") or [])
        pending_video_labels = list(pending_video_sends.get("labels") or [])
        candidate_set = recent_context.get("last_candidate_set") or {}
        candidates = list(candidate_set.get("candidates") or [])
        lines: list[str] = []
        if video_paths or video_urls:
            lines.append(f"最近上下文有视频素材：文件 {len(video_paths)} 个，链接 {len(video_urls)} 个。")
        if image_paths:
            lines.append(f"最近上下文有图片素材：{len(image_paths)} 个。")
        if pending_video_paths:
            labels = "、".join(str(label) for label in pending_video_labels if str(label).strip())
            reason = str(pending_video_sends.get("reason") or "send_pending")
            sent_count = int(pending_video_sends.get("sent_count") or 0)
            requested_count = int(pending_video_sends.get("requested_count") or len(pending_video_paths))
            pending_text = f"待补发视频：{len(pending_video_paths)} 个；原因：{reason}；本轮已发：{sent_count}/{requested_count}。"
            if labels:
                pending_text += f" 待补发房源：{labels}。"
            lines.append(pending_text)
        if candidates:
            try:
                shown_count = int(candidate_set.get("shown_count") or 0)
            except (TypeError, ValueError):
                shown_count = 0
            shown_count = min(max(0, shown_count), len(candidates))
            labels = [self._room_label(row) for row in candidates[:10]]
            lines.append("最近待确认候选：" + "、".join(label for label in labels if label))
            lines.append(f"上一轮已展示候选数：{shown_count}；候选总数：{len(candidates)}。")
            if shown_count < len(candidates):
                remainder_labels = [self._room_label(row) for row in candidates[shown_count:]]
                lines.append("上一轮未展示候选：" + "、".join(label for label in remainder_labels if label))
        return "\n".join(lines)

    def _compact_snapshot(self, inventory_snapshot: str, *, limit: int = 8) -> str:
        lines = [line.strip() for line in inventory_snapshot.splitlines() if line.strip()]
        compact = "\n".join(lines[:limit])
        if len(lines) > limit:
            compact += f"\n... 还有 {len(lines) - limit} 行库存快照未展示"
        return compact[:1200]

    def _should_include_password(self, content: str, need: UserNeedRewrite) -> bool:
        if "viewing" in need.topics:
            return True
        return any(
            word in content
            for word in (
                "密码",
                "看房",
                "怎么去",
                "怎么开",
                "门锁",
                "动态密码",
                "预约",
                "现场",
                "钥匙",
            )
        )

    def _room_label(self, row: dict[str, Any]) -> str:
        community = canonical_community_display(self._row_value(row, "小区", "社区", "楼盘"))
        return f"{community}{self._row_value(row, '房号', '房间号', '门牌')}".strip()

    def _row_value(self, row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _field_text(self, name: str, value: str) -> str:
        return f"{name}：{value}" if value else ""

    def assess_reply(
        self,
        *,
        content: str,
        reply_text: str,
        rag_result: AgenticRagResult | None,
        retry_attempted: bool,
    ) -> AgenticRagAssessment:
        basic_assessment = self._assess_basic_reply_quality(
            content=content,
            reply_text=reply_text,
            rag_result=rag_result,
            retry_attempted=retry_attempted,
        )
        if basic_assessment is not None:
            return basic_assessment
        if not rag_result or not rag_result.enabled or not rag_result.need.needs_knowledge:
            return AgenticRagAssessment("pass")
        if not rag_result.evidence:
            return self._retry_or_fallback(
                retry_attempted,
                "knowledge_evidence_missing",
                "这个我需要再确认一下准确口径。你也可以把具体小区和房号发我，我先按房源表查准。",
            )
        if "refund_cancel" in rag_result.need.topics and any(word in content for word in ("退租", "提前退", "中途退", "转租")):
            if "押金" not in reply_text or ("转租" not in reply_text and "未找到" not in reply_text):
                return self._retry_or_fallback(
                    retry_attempted,
                    "refund_cancel_missing_two_cases",
                    "合同期内退租分两种：未找到转租直接退租，押金不退；找到转租后退租，押金全退，剩余租金按天退还。具体办理请联系：\n18758141785\n13282125992\n19941091943",
                )
        if "deposit_waiver" in rag_result.need.topics:
            if any(word in reply_text for word in ("完全免费", "不用付任何费用", "免费免押")):
                return self._retry_or_fallback(
                    retry_attempted,
                    "deposit_reply_claims_free",
                    "免押不是免费，是支付宝芝麻信用无忧住服务。符合风控后，可以不直接付押金，但需要按支付宝页面支付免押服务费。",
                )
            if not any(word in reply_text for word in ("支付宝", "芝麻", "无忧住")):
                return self._retry_or_fallback(
                    retry_attempted,
                    "deposit_reply_missing_platform",
                    "免押是支付宝芝麻信用无忧住服务，不是平台自己免押。符合条件后按支付宝页面办理。",
                )
        if "contract_booking" in rag_result.need.topics and any(
            word in content for word in ("联系", "订房", "定房", "预定", "预订", "定金", "订金", "合同", "签约")
        ):
            if not any(number in reply_text for number in ("18758141785", "13282125992", "19941091943")):
                return self._retry_or_fallback(
                    retry_attempted,
                    "contract_reply_missing_contact",
                    "订房、签合同、交定金建议直接联系：\n18758141785\n13282125992\n19941091943",
                )
        if "contract_booking" in rag_result.need.topics and any(word in content for word in ("退定", "退订", "定金退", "订金退")):
            if any(word in reply_text for word in ("可以退", "能退", "可退")) and "不退" not in reply_text:
                return self._retry_or_fallback(
                    retry_attempted,
                    "booking_deposit_refund_wrong",
                    "定金/订金至少半个月房租；因个人原因退定，定金不退。具体订房和签约请联系：\n18758141785\n13282125992\n19941091943",
                )
        if "maintenance" in rag_result.need.topics and any(word in content for word in ("维修", "损坏", "坏了", "易损件")):
            if not any(word in reply_text for word in ("租客原因", "自然原因", "人为")):
                return self._retry_or_fallback(
                    retry_attempted,
                    "maintenance_reply_missing_responsibility_split",
                    "合同期内，因租客原因导致的损坏由租客负责维修好并承担全部维修费用；自然原因损坏由我方承担工费，租客承担易损件材料费。",
                )
        if "笔记" in content and "笔记" in reply_text:
            return self._retry_or_fallback(
                retry_attempted,
                "note_word_not_allowed",
                "我们这边没有单独的笔记，客户说笔记时按房间视频或详细信息处理。你把小区和房号发我，我先帮你查。",
            )
        return AgenticRagAssessment("pass")

    def _assess_basic_reply_quality(
        self,
        *,
        content: str,
        reply_text: str,
        rag_result: AgenticRagResult | None,
        retry_attempted: bool,
    ) -> AgenticRagAssessment | None:
        text = str(reply_text or "").strip()
        if not text:
            return self._retry_or_fallback(
                retry_attempted,
                "empty_reply",
                "我这边没生成出有效回复。你把小区名和房号再发我一下，我重新按房源表查。",
            )

        fixed_fact_text = self._fix_wrong_canonical_room_names(text, rag_result)
        if fixed_fact_text != text:
            return self._retry_or_fallback(
                retry_attempted,
                "canonical_room_name_mismatch",
                fixed_fact_text,
            )

        if self._known_wanda_area_query(content) and any(word in text for word in ("哪个城市", "哪座城市", "万达广场")):
            return self._retry_or_fallback(
                retry_attempted,
                "known_area_city_hallucination",
                "有的，我按拱墅万达/北部软件园/城北万象城这片给你查，不用再确认城市。",
            )

        action_followup = any(
            word in content
            for word in (
                "视频",
                "图片",
                "照片",
                "原视频",
                "高清",
                "水电",
                "水费",
                "电费",
                "密码",
                "看房",
                "今天看",
                "定房",
                "订房",
                "合同",
                "免押",
                "服务费",
                "第",
                "前两套",
                "这套",
                "这几套",
            )
        )
        budget = self._budget_constraint(content)
        if (
            budget
            and not action_followup
            and not self._looks_like_disambiguation_reply(text)
            and self._looks_like_inventory_list_reply(text)
            and not self._reply_mentions_budget(text, budget)
        ):
            return self._retry_or_fallback(
                retry_attempted,
                "budget_constraint_omitted",
                f"我按你说的{budget}预算查，优先只看这个价格附近的在租房源；超出太多的我就不混进去。",
            )

        if (
            self._asks_availability(content)
            and not self._looks_like_candidate_or_reference_confirmation(text)
            and not self._directly_answers_availability(text)
        ):
            return self._retry_or_fallback(
                retry_attempted,
                "availability_question_not_answered",
                "我先按房源表确认：如果你问的是具体某套，把小区名和房号发我，我直接回你还在不在；如果是找房，我按预算和区域给你筛。",
            )

        pending_video_assessment = self._assess_pending_video_reply(
            content=content,
            reply_text=text,
            rag_result=rag_result,
            retry_attempted=retry_attempted,
        )
        if pending_video_assessment is not None:
            return pending_video_assessment

        if self._has_orphan_sequence_instruction(text, rag_result):
            return self._retry_or_fallback(
                retry_attempted,
                "orphan_sequence_instruction",
                self._remove_orphan_sequence_instruction(text),
            )

        if self._asks_room_again_despite_single_fact(text, rag_result):
            return self._retry_or_fallback(
                retry_attempted,
                "asks_room_again_with_confirmed_fact",
                self._remove_redundant_room_reference_request(text),
            )

        if self._looks_like_robotic_template(text):
            return self._retry_or_fallback(
                retry_attempted,
                "robotic_template_reply",
                self._humanize_short_reply(text),
            )
        return None

    def _assess_pending_video_reply(
        self,
        *,
        content: str,
        reply_text: str,
        rag_result: AgenticRagResult | None,
        retry_attempted: bool,
    ) -> AgenticRagAssessment | None:
        if not rag_result or "待补发视频" not in rag_result.context_text:
            return None
        pending_line = self._pending_video_context_line(rag_result.context_text)
        reply_mentions_pending = any(
            word in reply_text
            for word in (
                "没发完",
                "限制",
                "补发",
                "剩下",
                "待补",
                "继续发",
                "找一下",
                "找到直接发",
            )
        )
        misleading_claim = any(
            word in reply_text
            for word in ("都发完", "已发完", "已直接发送相关视频", "视频发你了", "把视频发你", "已经发你")
        )
        explicit_video_request = self._has_explicit_video_intent(content)
        if misleading_claim or (explicit_video_request and not reply_mentions_pending):
            fallback = "刚才微信限制没发完，我会优先把剩下的视频补给你。"
            if pending_line:
                fallback = f"{fallback}\n{pending_line}"
            return self._retry_or_fallback(
                retry_attempted,
                "video_send_pending",
                fallback,
                missing_actions=("send_video",),
            )
        return None

    def _pending_video_context_line(self, context_text: str) -> str:
        for line in context_text.splitlines():
            if "待补发视频" in line:
                return line.strip()
        return ""

    def _fix_wrong_canonical_room_names(
        self,
        reply_text: str,
        rag_result: AgenticRagResult | None,
    ) -> str:
        fixed = reply_text
        context_text = self._rag_context_text(rag_result)
        fact_rooms = self._fact_rooms_from_context(context_text)

        for wrong, right in COMMUNITY_DISPLAY_ALIASES.items():
            if wrong in fixed and right in context_text:
                fixed = fixed.replace(wrong, right)

        for community, room_no in fact_rooms:
            if not community or not room_no or community in fixed or room_no not in fixed:
                continue
            pattern = re.compile(rf"([一-鿿]{{2,10}})\s*{re.escape(room_no)}")
            for match in list(pattern.finditer(fixed)):
                mentioned = match.group(1)
                if mentioned == community:
                    continue
                if self._similar_community_name(mentioned, community):
                    fixed = fixed.replace(mentioned + room_no, community + room_no, 1)
                    fixed = fixed.replace(f"{mentioned} {room_no}", f"{community}{room_no}", 1)
                    break
        return fixed

    def _rag_context_text(self, rag_result: AgenticRagResult | None) -> str:
        if rag_result is None:
            return ""
        parts = [rag_result.context_text or ""]
        parts.extend(item.content for item in rag_result.dynamic_evidence)
        return "\n".join(part for part in parts if part)

    def _fact_rooms_from_context(self, context_text: str) -> list[tuple[str, str]]:
        rooms: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for match in re.finditer(r"小区：([^；\n]+).*?房号：([^；\n]+)", context_text):
            community = canonical_community_display(match.group(1).strip())
            room_no = match.group(2).strip()
            key = (community, room_no)
            if community and room_no and key not in seen:
                seen.add(key)
                rooms.append(key)
        return rooms

    def _similar_community_name(self, left: str, right: str) -> bool:
        left = canonical_community_display(str(left or "").strip())
        right = canonical_community_display(str(right or "").strip())
        if not left or not right or left == right:
            return left == right
        if len(left) == len(right) and self._bounded_levenshtein(left, right, 1) is not None:
            return True
        return self._normalize_text(left) == self._normalize_text(right)

    def _asks_availability(self, content: str) -> bool:
        if any(word in content for word in ("视频", "图片", "照片", "素材", "原视频")):
            return False
        if any(word in content for word in ("清楚一点", "更清楚", "高清", "原版")):
            return False
        return any(
            word in content
            for word in (
                "有没有",
                "有吗",
                "有哪些",
                "哪些",
                "还有吗",
                "还在吗",
                "还在",
                "还有",
                "空吗",
                "空不空",
            )
        )

    def _known_wanda_area_query(self, content: str) -> bool:
        return "万达" in str(content or "")

    def _budget_constraint(self, content: str) -> str:
        text = str(content or "")
        range_match = re.search(
            r"(\d{3,5})\s*(?:-|~|～|到|至)\s*(\d{3,5})\s*(?:左右|上下|以内|以下|元)?",
            text,
        )
        if range_match:
            return f"{range_match.group(1)}-{range_match.group(2)}"
        match = re.search(r"(\d{3,5})\s*(左右|上下|以内|以下|元以内|元以下)?", text)
        if not match:
            return ""
        return f"{match.group(1)}{match.group(2) or ''}"

    def _looks_like_inventory_list_reply(self, reply_text: str) -> bool:
        return bool(re.search(r"(?m)(?:^|\n)\s*\d+\s*[.、．]", reply_text)) or any(
            word in reply_text for word in ("查到", "筛到", "匹配到", "在租房源", "这几套")
        )

    def _reply_mentions_budget(self, reply_text: str, budget: str) -> bool:
        if budget and budget in reply_text:
            return True
        number_match = re.search(r"\d{3,5}", budget)
        if number_match and number_match.group(0) in reply_text:
            return True
        return any(word in reply_text for word in ("预算", "左右", "上下", "以内", "以下"))

    def _directly_answers_availability(self, reply_text: str) -> bool:
        return any(
            word in reply_text
            for word in (
                "有的",
                "有，",
                "还有",
                "还在",
                "在的",
                "在租",
                "查到",
                "查到了",
                "筛到",
                "匹配到",
                "没有",
                "暂无",
                "暂时没",
                "没找到",
                "已租",
                "下架",
            )
        )

    def _looks_like_candidate_or_reference_confirmation(self, reply_text: str) -> bool:
        if any(
            phrase in reply_text
            for phrase in (
                "是不是",
                "我怕选错",
                "下面哪",
                "哪一套",
                "可能是",
                "相近小区",
                "确认下是哪一个",
                "确认一下小区",
                "避免房源或素材发错",
            )
        ):
            return True
        return bool(re.search(r"(?m)(?:^|\n)\s*\d+\s*[.、．]", reply_text))

    def _looks_like_disambiguation_reply(self, reply_text: str) -> bool:
        return any(
            phrase in reply_text
            for phrase in (
                "是不是",
                "我怕选错",
                "下面哪",
                "哪一套",
                "哪个小区",
                "先确认",
                "确认一下",
                "你要哪",
                "回序号",
                "回复序号",
            )
        )

    def _has_orphan_sequence_instruction(
        self,
        reply_text: str,
        rag_result: AgenticRagResult | None,
    ) -> bool:
        if "序号" not in reply_text and "第几" not in reply_text:
            return False
        has_numbered_list = bool(re.search(r"(?m)(?:^|\n)\s*\d+\s*[.、．]", reply_text))
        if not has_numbered_list:
            return True
        return len(self._fact_rooms_from_context(self._rag_context_text(rag_result))) == 1

    def _remove_orphan_sequence_instruction(self, reply_text: str) -> str:
        text = reply_text
        replacements = {
            "，你回复序号": "",
            "，回序号": "",
            "你回复序号，": "",
            "回序号，": "",
            "或者序号": "",
            "或序号": "",
            "回复序号": "直接把房号发我",
            "回有视频那套的序号": "直接说要哪套视频",
            "你要视频的话，直接回序号或说都发。": "要视频的话，直接说要哪套，或者说都发。",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return re.sub(r"\s+", " ", text).strip()

    def _asks_room_again_despite_single_fact(
        self,
        reply_text: str,
        rag_result: AgenticRagResult | None,
    ) -> bool:
        if len(self._fact_rooms_from_context(self._rag_context_text(rag_result))) != 1:
            return False
        return any(
            phrase in reply_text
            for phrase in (
                "把准确的小区名+房号发我",
                "把小区名+房号发我",
                "你说的是哪套",
                "确认是哪一套",
            )
        )

    def _remove_redundant_room_reference_request(self, reply_text: str) -> str:
        text = reply_text
        for phrase in (
            "。你把准确的小区名+房号发我，我再帮你确认",
            "。把准确的小区名+房号发我，我再帮你确认",
            "。你把小区名+房号发我，我再帮你确认",
            "，你把准确的小区名+房号发我",
            "，把准确的小区名+房号发我",
        ):
            text = text.replace(phrase, "")
        return text.strip()

    def _looks_like_robotic_template(self, reply_text: str) -> bool:
        has_concrete_inventory_list = bool(
            re.search(r"(?m)^\s*\d+[\.、]\s*\S{2,30}\d", reply_text)
            and any(
                marker in reply_text
                for marker in ("押一付一", "押二付一", "民用水电", "水30/月", "电1元/度")
            )
        )
        broad_guidance_phrases = () if has_concrete_inventory_list else ("请回复", "如需", "若需要", "您可以")
        robotic_phrases = (
            "请提供",
            "感谢您的咨询",
            "希望能帮到你",
            *broad_guidance_phrases,
        )
        return any(phrase in reply_text for phrase in robotic_phrases)

    def _humanize_short_reply(self, reply_text: str) -> str:
        text = reply_text.strip()
        replacements = {
            "请提供": "发我",
            "请回复": "直接回",
            "如需": "要是想",
            "若需要": "要是想",
            "您可以": "你可以",
            "感谢您的咨询。": "",
            "希望能帮到你。": "",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return re.sub(r"\s+", " ", text).strip()

    def _retry_or_fallback(
        self,
        retry_attempted: bool,
        reason: str,
        fallback_text: str,
        *,
        missing_actions: tuple[str, ...] = (),
    ) -> AgenticRagAssessment:
        report = SelfCheckReport(
            passed=False,
            score=55 if retry_attempted else 70,
            hard_fail=True,
            fail_reasons=(reason,),
            missing_actions=missing_actions,
            retry_instruction=fallback_text,
            risk="high" if retry_attempted else "medium",
        )
        if retry_attempted:
            return AgenticRagAssessment(
                "fallback",
                reason=reason,
                fallback_text=fallback_text,
                report=report,
            )
        return AgenticRagAssessment("retry", reason=reason, report=report)

    def _build_query(self, need: UserNeedRewrite, *, conversation_context: str, retry_reason: str) -> str:
        parts = [
            need.normalized_query,
            self._topic_keywords_text(need.topics),
            conversation_context[-500:],
            retry_reason,
        ]
        return "\n".join(part for part in parts if part.strip())

    def _topic_keywords_text(self, topics: list[str]) -> str:
        keywords: list[str] = []
        for topic in topics:
            keywords.extend(KNOWLEDGE_TOPICS.get(topic, ()))
        return " ".join(dict.fromkeys(keywords))

    def _load_chunks(self) -> list[KnowledgeChunk]:
        signature = self._knowledge_signature()
        if signature == self._cache_signature:
            return self._chunks
        chunks: list[KnowledgeChunk] = []
        if self.knowledge_dir.exists():
            for path in sorted(self.knowledge_dir.rglob("*.md")):
                if "rules" in path.relative_to(self.knowledge_dir).parts:
                    continue
                chunks.extend(self._chunks_from_markdown(path))
        self._cache_signature = signature
        self._chunks = chunks
        return chunks

    def _knowledge_signature(self) -> tuple[tuple[str, float], ...]:
        if not self.knowledge_dir.exists():
            return ()
        return tuple(
            (str(path.relative_to(self.knowledge_dir)), path.stat().st_mtime)
            for path in sorted(self.knowledge_dir.rglob("*.md"))
            if "rules" not in path.relative_to(self.knowledge_dir).parts
        )

    def _chunks_from_markdown(self, path: Path) -> list[KnowledgeChunk]:
        text = path.read_text(encoding="utf-8")
        title = self._markdown_title(text) or path.stem
        sections = self._split_markdown_sections(text)
        chunks: list[KnowledgeChunk] = []
        for index, section in enumerate(sections, start=1):
            clean = section.strip()
            if not clean:
                continue
            topics = [
                topic
                for topic, keywords in KNOWLEDGE_TOPICS.items()
                if any(keyword in clean or keyword in title or keyword in path.stem for keyword in keywords)
            ]
            chunks.append(
                KnowledgeChunk(
                    doc_id=f"{path.stem}-{index}",
                    title=title,
                    source=str(path.as_posix()),
                    content=clean[:1200],
                    topics=topics,
                )
            )
        return chunks

    def _markdown_title(self, text: str) -> str:
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return ""

    def _split_markdown_sections(self, text: str) -> list[str]:
        sections: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            if line.startswith("## ") and current:
                sections.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current))
        return sections

    def _terms(self, text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        terms = set(re.findall(r"[a-z0-9]+", normalized))
        for topic_keywords in KNOWLEDGE_TOPICS.values():
            for keyword in topic_keywords:
                if keyword in normalized:
                    terms.add(keyword)
        for chunk in re.findall(r"[一-鿿]+", normalized):
            if len(chunk) == 1:
                terms.add(chunk)
                continue
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
            if len(chunk) >= 3:
                terms.update(chunk[index : index + 3] for index in range(len(chunk) - 2))
        return {term for term in terms if term.strip()}
