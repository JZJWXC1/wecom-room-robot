from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "deposit_waiver": ("免押", "免押金", "押金", "芝麻", "无忧住", "服务费", "信用免押"),
    "contract_booking": ("合同", "签约", "签合同", "定房", "订房", "预定", "预订", "定金", "订金", "看中了"),
    "owner_price_sop": ("最低价", "少点", "便宜点", "优惠", "砍价", "谈价", "价格申请"),
    "refund_cancel": ("退租", "转租", "押金怎么退", "取消", "退定"),
    "maintenance": ("维修", "坏了", "报修", "漏水", "损坏"),
    "utilities": ("水电", "电费", "水费", "民水民电"),
    "viewing_sop": ("看房", "约看", "密码", "门锁", "门禁", "自助看"),
    "escalation": ("投诉", "人工", "客服", "找人", "联系"),
}


@dataclass(frozen=True)
class BusinessKnowledgeCard:
    id: str
    source: str
    score: float
    content: str


class KfBusinessKnowledgeService:
    """Lightweight business FAQ retriever for LangGraph production business_qa."""

    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self._cache_signature: tuple[tuple[str, float], ...] | None = None
        self._docs: list[tuple[str, Path, str]] = []

    def retrieve(
        self,
        *,
        query_text: str,
        intent: str = "",
        signals: dict[str, Any] | None = None,
        limit: int = 4,
    ) -> list[BusinessKnowledgeCard]:
        query = _normalize_text(query_text)
        intent = _normalize_token(intent)
        signals = signals or {}
        scored: list[BusinessKnowledgeCard] = []
        for doc_id, path, content in self._load_docs():
            score = self._score_doc(
                doc_id,
                content,
                query=query,
                intent=intent,
                signals=signals,
            )
            if score <= 0:
                continue
            scored.append(
                BusinessKnowledgeCard(
                    id=doc_id,
                    source=str(path.as_posix()),
                    score=round(score, 3),
                    content=_clip_text(content, limit=900),
                )
            )
        scored.sort(key=lambda card: (-card.score, card.id))
        return scored[: max(1, int(limit or 1))]

    def format_cards(self, cards: list[BusinessKnowledgeCard]) -> str:
        lines: list[str] = []
        for card in cards:
            lines.append(
                "\n".join(
                    [
                        f"- id: {card.id}",
                        f"  source: {card.source}",
                        f"  content: {card.content}",
                    ]
                )
            )
        return "\n".join(lines)

    def _score_doc(
        self,
        doc_id: str,
        content: str,
        *,
        query: str,
        intent: str,
        signals: dict[str, Any],
    ) -> float:
        topic_keywords = TOPIC_KEYWORDS.get(doc_id, ())
        score = 0.0
        if intent and intent in {doc_id, doc_id.replace("_waiver", ""), doc_id.replace("_booking", "")}:
            score += 8.0
        if intent == "deposit" and doc_id == "deposit_waiver":
            score += 8.0
        if intent == "contract" and doc_id == "contract_booking":
            score += 8.0
        if intent == "price_negotiation" and doc_id == "owner_price_sop":
            score += 8.0
        if signals.get("wants_deposit") and doc_id == "deposit_waiver":
            score += 8.0
        if signals.get("wants_contract_contact") and doc_id == "contract_booking":
            score += 8.0
        if signals.get("wants_price_negotiation") and doc_id == "owner_price_sop":
            score += 8.0
        normalized_content = _normalize_text(content)
        for keyword in topic_keywords:
            normalized = _normalize_text(keyword)
            if normalized and normalized in query:
                score += 3.0
            elif normalized and normalized in normalized_content and normalized in query:
                score += 1.0
        return score

    def _load_docs(self) -> list[tuple[str, Path, str]]:
        signature = self._knowledge_signature()
        if signature == self._cache_signature:
            return self._docs
        docs: list[tuple[str, Path, str]] = []
        if self.knowledge_dir.exists():
            for path in sorted(self.knowledge_dir.rglob("*.md")):
                if "rules" in path.relative_to(self.knowledge_dir).parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                content = _strip_markdown_title(text).strip()
                if content:
                    docs.append((path.stem, path, content))
        self._cache_signature = signature
        self._docs = docs
        return docs

    def _knowledge_signature(self) -> tuple[tuple[str, float], ...]:
        if not self.knowledge_dir.exists():
            return ()
        return tuple(
            (str(path.relative_to(self.knowledge_dir)), path.stat().st_mtime)
            for path in sorted(self.knowledge_dir.rglob("*.md"))
            if "rules" not in path.relative_to(self.knowledge_dir).parts
        )


def _normalize_token(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _strip_markdown_title(text: str) -> str:
    return re.sub(r"^\s*# .*$", "", text, count=1, flags=re.M).strip()


def _clip_text(text: str, *, limit: int) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."
