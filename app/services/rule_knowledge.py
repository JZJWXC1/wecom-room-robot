from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from app.config import settings


DEFAULT_RULES_DIR = settings.kf_agentic_rag_knowledge_dir / "rules"


@dataclass(frozen=True)
class RuleKnowledgeCard:
    id: str
    stage: str
    intents: tuple[str, ...]
    triggers: tuple[str, ...]
    priority: int
    content: str
    must_enforce_by_code: bool = False
    source: str = ""


class RuleKnowledgeService:
    def __init__(self, rules_dir: Path | None = None) -> None:
        self.rules_dir = rules_dir or DEFAULT_RULES_DIR
        self._cache_signature: tuple[tuple[str, float], ...] | None = None
        self._cards: list[RuleKnowledgeCard] = []

    def retrieve(
        self,
        *,
        stage: str,
        intent: str = "",
        query_text: str = "",
        query_state: dict[str, Any] | None = None,
        constraint_proof: dict[str, Any] | None = None,
        retry_packet: str = "",
        tool_result_summary: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> list[RuleKnowledgeCard]:
        stage = _normalize_token(stage)
        intent = _normalize_token(intent)
        haystack = _normalize_text(
            "\n".join(
                [
                    query_text,
                    _jsonish_text(query_state),
                    _jsonish_text(constraint_proof),
                    retry_packet,
                    _jsonish_text(tool_result_summary),
                ]
            )
        )
        scored: list[tuple[float, RuleKnowledgeCard]] = []
        for card in self._load_cards():
            score = self._score_card(card, stage=stage, intent=intent, haystack=haystack)
            if score > 0:
                scored.append((score, card))
        scored.sort(key=lambda item: (-item[0], -item[1].priority, item[1].id))
        return [card for _, card in scored[:limit]]

    def format_cards(self, cards: list[RuleKnowledgeCard]) -> str:
        if not cards:
            return ""
        lines: list[str] = []
        for card in cards:
            enforce = "是" if card.must_enforce_by_code else "否"
            lines.append(
                "\n".join(
                    [
                        f"- id: {card.id}",
                        f"  stage: {card.stage}",
                        f"  intents: {', '.join(card.intents) or '通用'}",
                        f"  hard_rule: {enforce}",
                        f"  content: {card.content}",
                    ]
                )
            )
        return "\n".join(lines)

    def retrieve_text(self, **kwargs: Any) -> str:
        return self.format_cards(self.retrieve(**kwargs))

    def _score_card(self, card: RuleKnowledgeCard, *, stage: str, intent: str, haystack: str) -> float:
        if card.stage and card.stage not in {stage, "common", "all"}:
            return 0.0
        score = 10.0 if card.stage == stage else 2.0
        if intent and intent in card.intents:
            score += 8.0
        elif card.intents and "all" not in card.intents and intent:
            score -= 1.0
        trigger_hits = 0
        for trigger in card.triggers:
            normalized = _normalize_text(trigger)
            if normalized and normalized in haystack:
                trigger_hits += 1
        if card.triggers and not trigger_hits and intent not in card.intents:
            return 0.0
        score += trigger_hits * 3.0
        score += min(max(card.priority, 0), 100) / 100.0
        if card.must_enforce_by_code:
            score += 0.5
        return score

    def _load_cards(self) -> list[RuleKnowledgeCard]:
        signature = self._rules_signature()
        if signature == self._cache_signature:
            return self._cards
        cards: list[RuleKnowledgeCard] = []
        if self.rules_dir.exists():
            for path in sorted(self.rules_dir.rglob("*.md")):
                card = self._card_from_markdown(path)
                if card:
                    cards.append(card)
        self._cache_signature = signature
        self._cards = cards
        return cards

    def _rules_signature(self) -> tuple[tuple[str, float], ...]:
        if not self.rules_dir.exists():
            return ()
        return tuple(
            (str(path.relative_to(self.rules_dir)), path.stat().st_mtime)
            for path in sorted(self.rules_dir.rglob("*.md"))
        )

    def _card_from_markdown(self, path: Path) -> RuleKnowledgeCard | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = _split_front_matter(text)
        content = _strip_markdown_title(body).strip()
        if not content:
            return None
        card_id = str(meta.get("id") or path.stem).strip()
        return RuleKnowledgeCard(
            id=card_id,
            stage=_normalize_token(str(meta.get("stage") or "common")),
            intents=tuple(_split_csv(str(meta.get("intents") or ""))),
            triggers=tuple(_split_csv(str(meta.get("triggers") or ""))),
            priority=_safe_int(meta.get("priority"), default=50),
            content=content[:900],
            must_enforce_by_code=_as_bool(meta.get("hard_rule") or meta.get("must_enforce_by_code")),
            source=str(path.as_posix()),
        )


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_text, body = parts[1], parts[2]
    meta: dict[str, str] = {}
    for line in meta_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


def _strip_markdown_title(text: str) -> str:
    return re.sub(r"^\s*# .*$", "", text, count=1, flags=re.M).strip()


def _split_csv(value: str) -> list[str]:
    return [
        _normalize_token(item)
        for item in re.split(r"[,，|/、\s]+", value)
        if _normalize_token(item)
    ]


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _normalize_token(value: str) -> str:
    return value.strip().lower()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _jsonish_text(value: Any) -> str:
    if not value:
        return ""
    return str(value)
