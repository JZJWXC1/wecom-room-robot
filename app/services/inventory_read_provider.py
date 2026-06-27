from __future__ import annotations

import re
from typing import Any, Callable, Protocol

from app.config import settings
from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text
from app.services.inventory import InventoryService
from app.services.inventory_query import (
    filter_scored_by_hard_constraints,
    parse_inventory_query,
    row_matches_hard_constraints,
)
from app.services.inventory_read_models import (
    SOURCE_KIND_LEGACY,
    SOURCE_KIND_SNAPSHOT,
    InventoryListingEvidence,
    InventoryReadContext,
    InventoryReadError,
    InventoryReadHealth,
    REASON_CONTEXT_SNAPSHOT_MISMATCH,
    REASON_SNAPSHOT_READ_FAILED,
    assert_evidence_consistency,
    ensure_provider_context,
    make_evidence_id,
    stable_safe_hash,
)
from app.services.inventory_snapshot_builder import (
    FIELD_ALIASES,
    IMAGE_FIELD_ALIASES,
    VIDEO_FIELD_ALIASES,
    build_availability_summary,
    build_utility_summary,
    parse_media_bool,
    parse_monthly_rent,
)
from app.services.inventory_snapshot_models import (
    InventoryListing,
    InventorySnapshot,
    generate_listing_id,
    now_utc_iso,
    sanitize_for_log,
)
from app.services.inventory_snapshot_reader import SnapshotReader
from app.services.region_inventory_constants import area_alias_index_entries
from app.services.rewrite_inventory_index import load_rewrite_inventory_index, sanitize_rewrite_inventory_index


class InventoryReadProvider(Protocol):
    source_kind: str

    async def search_inventory(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> list[InventoryListingEvidence]:
        ...

    async def search_inventory_rows(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ...

    async def all_inventory_rows(
        self,
        context: InventoryReadContext,
        *,
        limit: int = 500,
        refresh_if_needed: bool = True,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ...

    async def get_listing(
        self,
        listing_id: str,
        context: InventoryReadContext,
    ) -> InventoryListingEvidence | None:
        ...

    async def get_listings(
        self,
        listing_ids: list[str],
        context: InventoryReadContext,
    ) -> list[InventoryListingEvidence]:
        ...

    async def get_rewrite_index(self, context: InventoryReadContext) -> dict[str, Any]:
        ...

    async def get_inventory_metadata(self, context: InventoryReadContext) -> dict[str, Any]:
        ...

    def health(self) -> InventoryReadHealth:
        ...


class LegacyInventoryReadProvider:
    """Adapter around the existing InventoryService; it does not duplicate CSV search."""

    source_kind = SOURCE_KIND_LEGACY

    def __init__(
        self,
        inventory_service: InventoryService | None = None,
        *,
        rewrite_index_loader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.inventory_service = inventory_service or InventoryService()
        self.rewrite_index_loader = rewrite_index_loader or load_rewrite_inventory_index

    async def search_inventory(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> list[InventoryListingEvidence]:
        _rows, evidence = await self.search_inventory_rows(query_state, context, limit=limit)
        return evidence

    async def search_inventory_rows(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ensure_provider_context(self.source_kind, context)
        rows = list(await self.inventory_service.search(_query_text(query_state), limit=limit) or [])
        evidence = [_legacy_row_to_evidence(row, context, index=index) for index, row in enumerate(rows)]
        assert_evidence_consistency(context, evidence)
        return rows, evidence

    async def all_inventory_rows(
        self,
        context: InventoryReadContext,
        *,
        limit: int = 500,
        refresh_if_needed: bool = True,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ensure_provider_context(self.source_kind, context)
        try:
            rows = await self.inventory_service.all_rows(
                limit=limit,
                refresh_if_needed=refresh_if_needed,
            )
        except TypeError:
            rows = await self.inventory_service.all_rows(limit=limit)
        rows = list(rows or [])
        evidence = [_legacy_row_to_evidence(row, context, index=index) for index, row in enumerate(rows)]
        assert_evidence_consistency(context, evidence)
        return rows, evidence

    async def get_listing(
        self,
        listing_id: str,
        context: InventoryReadContext,
    ) -> InventoryListingEvidence | None:
        ensure_provider_context(self.source_kind, context)
        rows = await self.inventory_service.all_rows(limit=2000)
        for index, row in enumerate(rows):
            evidence = _legacy_row_to_evidence(row, context, index=index)
            if evidence.listing_id == listing_id:
                assert_evidence_consistency(context, [evidence])
                return evidence
        return None

    async def get_listings(
        self,
        listing_ids: list[str],
        context: InventoryReadContext,
    ) -> list[InventoryListingEvidence]:
        ensure_provider_context(self.source_kind, context)
        wanted = list(dict.fromkeys(str(item) for item in listing_ids if str(item or "").strip()))
        if not wanted:
            return []
        rows = await self.inventory_service.all_rows(limit=2000)
        indexed: dict[str, InventoryListingEvidence] = {}
        for index, row in enumerate(rows):
            evidence = _legacy_row_to_evidence(row, context, index=index)
            indexed.setdefault(evidence.listing_id, evidence)
        result = [indexed[item] for item in wanted if item in indexed]
        assert_evidence_consistency(context, result)
        return result

    async def get_rewrite_index(self, context: InventoryReadContext) -> dict[str, Any]:
        ensure_provider_context(self.source_kind, context)
        return sanitize_rewrite_inventory_index(dict(self.rewrite_index_loader() or {}))

    async def get_inventory_metadata(self, context: InventoryReadContext) -> dict[str, Any]:
        ensure_provider_context(self.source_kind, context)
        return _strip_sensitive_payload(_legacy_cache_meta(self.inventory_service))

    def health(self) -> InventoryReadHealth:
        meta = _strip_sensitive_payload(_legacy_cache_meta(self.inventory_service))
        source_hash = str(meta.get("hash") or meta.get("source_hash") or stable_safe_hash(meta))
        return InventoryReadHealth(
            status=str(meta.get("status") or "unknown"),
            source_kind=self.source_kind,
            message="legacy inventory service metadata",
            details={
                "source_hash": source_hash,
                "schema_version": "legacy_inventory_service.v1",
                "row_count": meta.get("row_count", 0),
                "cache_mtime": meta.get("cache_mtime", 0),
            },
        )


class SnapshotInventoryReadProvider:
    """Read evidence only from the snapshot pinned in InventoryReadContext."""

    source_kind = SOURCE_KIND_SNAPSHOT

    def __init__(self, reader: SnapshotReader | None = None) -> None:
        self.reader = reader or _configured_snapshot_reader()

    async def search_inventory(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> list[InventoryListingEvidence]:
        _rows, evidence = await self.search_inventory_rows(query_state, context, limit=limit)
        return evidence

    async def search_inventory_rows(
        self,
        query_state: Any,
        context: InventoryReadContext,
        *,
        limit: int = 8,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        scored = _score_snapshot_listings(snapshot, _query_text(query_state))
        listings = [listing for _score, _index, listing in scored[:limit]]
        rows = [_listing_as_query_row(listing) for listing in listings]
        evidence = [_snapshot_listing_to_evidence(listing, context) for listing in listings]
        assert_evidence_consistency(context, evidence)
        return rows, evidence

    async def all_inventory_rows(
        self,
        context: InventoryReadContext,
        *,
        limit: int = 500,
        refresh_if_needed: bool = True,
    ) -> tuple[list[dict[str, Any]], list[InventoryListingEvidence]]:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        listings = list(snapshot.listings[:limit])
        rows = [_listing_as_query_row(listing) for listing in listings]
        evidence = [_snapshot_listing_to_evidence(listing, context) for listing in listings]
        assert_evidence_consistency(context, evidence)
        return rows, evidence

    async def get_listing(
        self,
        listing_id: str,
        context: InventoryReadContext,
    ) -> InventoryListingEvidence | None:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        for listing in snapshot.listings:
            if listing.listing_id == listing_id:
                evidence = _snapshot_listing_to_evidence(listing, context)
                assert_evidence_consistency(context, [evidence])
                return evidence
        return None

    async def get_listings(
        self,
        listing_ids: list[str],
        context: InventoryReadContext,
    ) -> list[InventoryListingEvidence]:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        indexed = {listing.listing_id: listing for listing in snapshot.listings}
        result = [
            _snapshot_listing_to_evidence(indexed[listing_id], context)
            for listing_id in list(dict.fromkeys(listing_ids))
            if listing_id in indexed
        ]
        assert_evidence_consistency(context, result)
        return result

    async def get_rewrite_index(self, context: InventoryReadContext) -> dict[str, Any]:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        index = _strip_sensitive_payload(snapshot.rewrite_index)
        _assert_snapshot_rewrite_index_headers(index, context)
        return index

    async def get_inventory_metadata(self, context: InventoryReadContext) -> dict[str, Any]:
        ensure_provider_context(self.source_kind, context)
        snapshot = self._snapshot_for_context(context)
        return {
            "schema_version": snapshot.schema_version,
            "snapshot_id": snapshot.snapshot_id,
            "source_hash": snapshot.source_hash,
            "generated_at": snapshot.generated_at,
            "row_count": len(snapshot.listings),
        }

    def health(self) -> InventoryReadHealth:
        health = self.reader.health()
        return InventoryReadHealth(
            status=health.status,
            source_kind=self.source_kind,
            code="" if health.status in {"ok", "stale"} else health.status,
            message=health.message,
            checked_at=health.checked_at,
            details={
                "snapshot_id": health.snapshot_id,
                "age_seconds": health.age_seconds,
                "issues": [issue.to_dict() for issue in health.issues],
            },
        )

    def _snapshot_for_context(self, context: InventoryReadContext) -> InventorySnapshot:
        if not context.snapshot_id:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "snapshot provider requires a context snapshot_id",
            )
        result = self.reader.get_snapshot(context.snapshot_id)
        if not result.ok:
            raise InventoryReadError(
                REASON_SNAPSHOT_READ_FAILED,
                result.message,
                details={"read_code": result.code, "status": result.status},
            )
        snapshot = result.value
        if snapshot.snapshot_id != context.snapshot_id:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "loaded snapshot_id does not match context",
                details={"loaded": snapshot.snapshot_id, "context": context.snapshot_id},
            )
        if snapshot.source_hash != context.source_hash:
            raise InventoryReadError(
                REASON_CONTEXT_SNAPSHOT_MISMATCH,
                "loaded source_hash does not match context",
                details={"loaded": snapshot.source_hash, "context": context.source_hash},
            )
        return snapshot


def _legacy_row_to_evidence(
    row: dict[str, Any],
    context: InventoryReadContext,
    *,
    index: int,
) -> InventoryListingEvidence:
    community = _row_value(row, FIELD_ALIASES["community"])
    room_no = _row_value(row, FIELD_ALIASES["room_no"])
    listing_id = generate_listing_id(community or "unknown", room_no or f"row-{index}")
    rent_pay1, _ = parse_monthly_rent(_row_value(row, FIELD_ALIASES["rent_monthly_pay1"]))
    rent_pay2, _ = parse_monthly_rent(_row_value(row, FIELD_ALIASES["rent_monthly_pay2"]))
    viewing_text = _row_value(row, FIELD_ALIASES["viewing_text"])
    return InventoryListingEvidence(
        evidence_id=make_evidence_id(context, listing_id),
        decision_id=context.decision_id,
        listing_id=listing_id,
        source_kind=context.source_kind,
        snapshot_id="",
        source_hash=context.source_hash,
        schema_version=context.schema_version,
        area=_row_value(row, FIELD_ALIASES["area"]),
        community=community,
        room_no=str(room_no),
        layout_desc=_row_value(row, FIELD_ALIASES["layout_desc"]),
        layout_type=_row_value(row, FIELD_ALIASES["layout_type"]),
        rent_pay1=rent_pay1,
        rent_pay2=rent_pay2,
        utility_summary=build_utility_summary(_row_value(row, FIELD_ALIASES["remark"])),
        availability_summary=build_availability_summary(viewing_text, _row_value(row, FIELD_ALIASES["availability"])),
        has_image=parse_media_bool(row, IMAGE_FIELD_ALIASES),
        has_video=parse_media_bool(row, VIDEO_FIELD_ALIASES),
        fetched_at=now_utc_iso(),
    )


def _snapshot_listing_to_evidence(
    listing: InventoryListing,
    context: InventoryReadContext,
) -> InventoryListingEvidence:
    return InventoryListingEvidence(
        evidence_id=make_evidence_id(context, listing.listing_id),
        decision_id=context.decision_id,
        listing_id=listing.listing_id,
        source_kind=context.source_kind,
        snapshot_id=context.snapshot_id,
        source_hash=context.source_hash,
        schema_version=context.schema_version,
        area=listing.area,
        community=listing.community,
        room_no=str(listing.room_no),
        layout_desc=listing.layout_desc,
        layout_type=listing.layout_type,
        rent_pay1=listing.rent_monthly_pay1,
        rent_pay2=listing.rent_monthly_pay2,
        utility_summary=dict(listing.utility_summary),
        availability_summary=dict(listing.availability_summary),
        has_image=listing.has_image,
        has_video=listing.has_video,
        fetched_at=now_utc_iso(),
    )


def _score_snapshot_listings(
    snapshot: InventorySnapshot,
    query_text: str,
) -> list[tuple[int, int, InventoryListing]]:
    row_items: list[tuple[dict[str, Any], int, InventoryListing]] = []
    for index, listing in enumerate(snapshot.listings):
        row_items.append((_listing_as_query_row(listing), index, listing))
    by_row_id = {id(row): (index, listing) for row, index, listing in row_items}
    rows = _search_rows_with_legacy_semantics(
        [row for row, _index, _listing in row_items],
        query_text,
        limit=len(row_items),
    )
    result: list[tuple[int, int, InventoryListing]] = []
    for rank, row in enumerate(rows):
        index, listing = by_row_id[id(row)]
        result.append((len(rows) - rank, index, listing))
    return result


def _search_rows_with_legacy_semantics(
    rows: list[dict[str, Any]],
    query_text: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    scorer = InventoryService()
    text = scorer._normalize_query(query_text)
    parsed_query = parse_inventory_query(text)
    room_refs = list(parsed_query.room_refs)
    records = list(rows)
    if room_refs:
        records = [row for row in records if scorer._row_matches_any_room_ref(row, room_refs)]
        if not records:
            return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in records:
        merged = " ".join(str(value).lower() for value in row.values())
        score = scorer._score_row(text, merged, row)
        if score > 0 or not text.strip():
            scored.append((score, row))
    exact_scored = scorer._exact_community_scored_rows(text, scored)
    if exact_scored:
        scored = exact_scored
    area_scored = scorer._area_scored_rows(text, scored)
    if area_scored:
        scored = area_scored
    if parsed_query.has_hard_constraints:
        scored = filter_scored_by_hard_constraints(scored, parsed_query)
        if not scored:
            return []
    strict_price = None if room_refs or parsed_query.price_range else scorer._requested_strict_price(text)
    if strict_price is not None:
        scored = [
            (score + 10, row)
            for score, row in scored
            if strict_price in scorer._row_prices(row)
        ]
        if not scored:
            return []
    scored = scorer._filter_scored_rows(scored, text=text)
    return [row for _score, row in scored[:limit]]


def _mentioned_snapshot_communities(snapshot: InventorySnapshot, normalized_query: str) -> set[str]:
    mentioned = {
        normalize_search_text(listing.community).lower()
        for listing in snapshot.listings
        if listing.community and normalize_search_text(listing.community).lower() in normalized_query
    }
    return {item for item in mentioned if item}


def _snapshot_listing_score(
    listing: InventoryListing,
    normalized_query: str,
    anchor_terms: tuple[str, ...],
    area_alias_hits: list[str],
) -> int:
    score = 0
    area = normalize_search_text(listing.area).lower()
    community = normalize_search_text(listing.community).lower()
    room_no = normalize_search_text(listing.room_no).lower()
    utility = str(listing.utility_summary.get("summary") or listing.remark or "")
    rent_text = " ".join(
        str(price)
        for price in (listing.rent_monthly_pay1, listing.rent_monthly_pay2)
        if isinstance(price, int)
    )
    media_text = " ".join(
        item
        for item, enabled in (("图片", listing.has_image), ("视频", listing.has_video))
        if enabled
    )
    haystack = normalize_search_text(
        " ".join(
            [
                listing.area,
                listing.community,
                listing.room_no,
                listing.layout_desc,
                listing.layout_type,
                utility,
                rent_text,
                media_text,
            ]
        )
    ).lower()
    if room_no and room_no in normalized_query:
        score += 1000
    for canonical_area in area_alias_hits:
        if canonical_area and canonical_area in area:
            score += 250
    for term in anchor_terms:
        normalized_term = normalize_search_text(term).lower()
        if not normalized_term:
            continue
        if normalized_term in community:
            score += 180
        elif normalized_term in area:
            score += 140
        elif normalized_term in haystack:
            score += 60
        else:
            score += fuzzy_contains_score(term, listing.community)
    for token in re.findall(r"[a-zA-Z0-9]+|[一-鿿]{2,}", normalized_query):
        if token in haystack:
            score += 10 + len(token)
        for gram in _char_grams(token):
            if gram in haystack:
                score += len(gram)
    return score


def _listing_as_query_row(listing: InventoryListing) -> dict[str, Any]:
    return {
        "区域": listing.area,
        "小区": listing.community,
        "房号": listing.room_no,
        "户型": listing.layout_desc,
        "户型分类": listing.layout_type,
        "押一付一": "" if listing.rent_monthly_pay1 is None else str(listing.rent_monthly_pay1),
        "押二付一": "" if listing.rent_monthly_pay2 is None else str(listing.rent_monthly_pay2),
        "备注": listing.utility_summary.get("summary") or listing.remark,
        "图片": "有" if listing.has_image else "",
        "视频": "有" if listing.has_video else "",
    }


def _area_alias_hits(normalized_query: str) -> list[str]:
    result: list[str] = []
    for item in area_alias_index_entries():
        alias = normalize_search_text(str(item.get("alias") or item.get("normalized_alias") or "")).lower()
        canonical = normalize_search_text(str(item.get("canonical_area") or item.get("canonical") or "")).lower()
        if alias and alias in normalized_query and canonical:
            result.append(canonical)
    return list(dict.fromkeys(result))


def _char_grams(text: str) -> list[str]:
    grams: list[str] = []
    for size in (4, 3, 2):
        grams.extend(text[index : index + size] for index in range(len(text) - size + 1))
    return list(dict.fromkeys(grams))


def _query_text(query_state: Any) -> str:
    if isinstance(query_state, str):
        return query_state
    if not isinstance(query_state, dict):
        return str(query_state or "")
    parts: list[str] = []
    for key in ("query", "effective_query", "text", "content", "area", "community", "layout", "budget_label"):
        value = query_state.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    for key in ("communities", "room_refs"):
        value = query_state.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if str(item or "").strip())
    budget_range = query_state.get("budget_range") or query_state.get("price_range")
    if isinstance(budget_range, (list, tuple)) and len(budget_range) == 2:
        parts.append(f"{budget_range[0]}-{budget_range[1]}")
    return " ".join(parts).strip()


def _legacy_cache_meta(inventory_service: Any) -> dict[str, Any]:
    meta = getattr(inventory_service, "cache_meta", {}) or {}
    if callable(meta):
        meta = meta()
    return dict(meta or {})


def _row_value(row: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value is not None and str(value).strip():
            return str(value).replace("\ufeff", "").strip()
    return ""


def _strip_sensitive_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if key_text in {"__inventory_meta"}:
                result[key_text] = _strip_sensitive_payload(item)
                continue
            if key_text in {"viewing_summary", "availability_summary"}:
                result[key_text] = _strip_sensitive_payload(item)
                continue
            if any(marker in lowered for marker in ("password", "secret", "token", "phone", "mobile", "private")):
                continue
            if key_text in {"viewing", "viewing_text", "看房方式密码", "看房密码", "密码"}:
                continue
            result[key_text] = _strip_sensitive_payload(item)
        return sanitize_for_log(result)
    if isinstance(value, list):
        return [_strip_sensitive_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_sensitive_payload(item) for item in value]
    return sanitize_for_log(value)


def _configured_snapshot_reader() -> SnapshotReader:
    max_age = int(getattr(settings, "inventory_snapshot_max_age_seconds", 0) or 0)
    return SnapshotReader(
        settings.inventory_snapshot_root,
        max_age_seconds=max_age if max_age > 0 else None,
    )


def _assert_snapshot_rewrite_index_headers(index: dict[str, Any], context: InventoryReadContext) -> None:
    snapshot_id = str(index.get("snapshot_id") or "")
    source_hash = str(index.get("source_hash") or "")
    source = str(index.get("source") or "")
    if source != "inventory_snapshot":
        raise InventoryReadError(
            REASON_CONTEXT_SNAPSHOT_MISMATCH,
            "snapshot rewrite index must declare inventory_snapshot source headers",
            details={"source": source},
        )
    if snapshot_id != context.snapshot_id:
        raise InventoryReadError(
            REASON_CONTEXT_SNAPSHOT_MISMATCH,
            "snapshot rewrite index snapshot_id does not match context",
            details={"index_snapshot_id": snapshot_id, "context_snapshot_id": context.snapshot_id},
        )
    if source_hash != context.source_hash:
        raise InventoryReadError(
            REASON_CONTEXT_SNAPSHOT_MISMATCH,
            "snapshot rewrite index source_hash does not match context",
            details={"index_source_hash": source_hash, "context_source_hash": context.source_hash},
        )
