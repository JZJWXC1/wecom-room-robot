from io import BytesIO, StringIO
import base64
import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI
import pandas as pd

from app.config import settings
from app.services.config_check import is_missing_or_placeholder
from app.services.feishu import FeishuClient
from app.services.fuzzy_match import canonical_community_display, fuzzy_contains_score
from app.services.inventory_legacy_parser import spreadsheet_values_to_inventory_rows
from app.services.inventory_query import (
    filter_scored_by_hard_constraints,
    parse_inventory_query,
    row_prices,
    _strip_negated_anchor_phrases,
)


GENERIC_QUERY_WORDS = (
    "看房视频",
    "房间视频",
    "内部视频",
    "视频",
    "房源",
    "房子",
    "房间",
    "还有什么",
    "有什么",
    "还有",
    "哪些",
    "哪个",
    "附近",
    "类似价格",
    "类似",
    "同价位",
    "差不多",
    "什么",
    "发一下",
    "发我",
    "发",
    "看一下",
    "看看",
    "在租",
    "有吗",
    "有没有",
    "现在",
    "目前",
    "的",
    "呢",
    "吗",
)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


class InventoryService:
    def __init__(self, *, client: FeishuClient | None = None) -> None:
        self._client = client
        self._cache: pd.DataFrame | None = None
        self._cache_file_marker: tuple[int, int] | None = None
        self._image_text: str = ""
        self._last_error: str = ""
        self._cache_meta: dict[str, Any] = {}

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def cache_meta(self) -> dict[str, Any]:
        if self._cache_meta:
            return dict(self._cache_meta)
        self._cache_meta = self._read_cache_meta()
        return dict(self._cache_meta)

    async def refresh(self) -> pd.DataFrame:
        if settings.inventory_source == "local_image":
            self._image_text = await self._read_image_inventory_text()
            frame = pd.DataFrame()
            self._cache = frame
            self._last_error = ""
            self._cache_meta = self._build_cache_meta(
                source="local_image",
                status="success",
                row_count=0,
                source_detail=str(settings.inventory_image_glob or settings.inventory_image_path),
            )
            self._write_cache_meta(self._cache_meta)
            return frame
        if settings.inventory_source == "local_cache":
            frame = self._read_cache()
            self._cache = frame
            self._last_error = ""
            self._cache_meta = self._build_cache_meta(
                source="local_cache",
                status="success",
                row_count=len(frame),
                source_detail=str(settings.inventory_cache_path),
            )
            self._write_cache_meta(self._cache_meta)
            return frame
        if settings.inventory_source == "feishu_bitable":
            try:
                if settings.feishu_inventory_sheet_token:
                    frame = await self._read_feishu_inventory_sheet()
                else:
                    frame = await FeishuClient().read_bitable_dataframe()
                frame = self._normalize(frame)
                self._save_cache(frame)
                self._cache_meta = self._build_cache_meta(
                    source="feishu_bitable",
                    status="success",
                    row_count=len(frame),
                    source_detail=self._feishu_source_detail(),
                )
                self._write_cache_meta(self._cache_meta)
                self._cache = frame
                self._last_error = ""
                return frame
            except Exception as exc:
                self._last_error = str(exc)
                frame = self._read_cache()
                self._cache_meta = self._build_cache_meta(
                    source="feishu_bitable",
                    status="fallback_cache",
                    row_count=len(frame),
                    source_detail=self._feishu_source_detail(),
                    error=str(exc),
                )
                self._write_cache_meta(self._cache_meta)
                self._cache = frame
                return frame
        try:
            frame = await self._read_public_document(settings.kdocs_public_url)
            frame = self._normalize(frame)
            self._save_cache(frame)
            self._cache_meta = self._build_cache_meta(
                source="public_document",
                status="success",
                row_count=len(frame),
                source_detail=settings.kdocs_public_url,
            )
            self._write_cache_meta(self._cache_meta)
            self._cache = frame
            self._last_error = ""
            return frame
        except Exception as exc:
            self._last_error = str(exc)
            frame = self._read_cache()
            self._cache_meta = self._build_cache_meta(
                source="public_document",
                status="fallback_cache",
                row_count=len(frame),
                source_detail=settings.kdocs_public_url,
                error=str(exc),
            )
            self._write_cache_meta(self._cache_meta)
            self._cache = frame
            return frame

    async def all_rows(
        self,
        *,
        limit: int = 500,
        refresh_if_needed: bool = True,
    ) -> list[dict[str, Any]]:
        if settings.inventory_source == "local_image":
            if not self._image_text and refresh_if_needed:
                await self.refresh()
            rows = self._parse_image_rows(self._image_text)
            return [self._with_inventory_meta(row) for row in rows[:limit]]
        self._reload_cache_if_file_changed()
        frame = self._cache
        if frame is None:
            frame = await self.refresh() if refresh_if_needed else self._read_cache()
        if frame.empty:
            return []
        records = frame.fillna("").to_dict(orient="records")
        return [self._with_inventory_meta(row) for row in records[:limit]]

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if settings.inventory_source == "local_image":
            if not self._image_text:
                await self.refresh()
            rows = self._parse_image_rows(self._image_text)
            text = self._normalize_query(query)
            scoring_text = _strip_negated_anchor_phrases(text)
            parsed_query = parse_inventory_query(text)
            room_refs = list(parsed_query.room_refs)
            if room_refs:
                rows = [row for row in rows if self._row_matches_any_room_ref(row, room_refs)]
                if not rows:
                    return []
            scored: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                merged = " ".join(str(value).lower() for value in row.values())
                score = self._score_row(scoring_text, merged, row)
                if score > 0 or not text.strip():
                    scored.append((score, row))
            if parsed_query.has_hard_constraints:
                scored = filter_scored_by_hard_constraints(scored, parsed_query)
                if not scored:
                    return []
            scored = self._filter_scored_rows(scored, text=scoring_text)
            return [self._with_inventory_meta(row) for _, row in scored[:limit]]
        self._reload_cache_if_file_changed()
        frame = self._cache if self._cache is not None else await self.refresh()
        if frame.empty:
            return []
        text = self._normalize_query(query)
        scoring_text = _strip_negated_anchor_phrases(text)
        parsed_query = parse_inventory_query(text)
        room_refs = list(parsed_query.room_refs)
        records = frame.fillna("").to_dict(orient="records")
        if room_refs:
            records = [row for row in records if self._row_matches_any_room_ref(row, room_refs)]
            if not records:
                return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in records:
            merged = " ".join(str(value).lower() for value in row.values())
            score = self._score_row(scoring_text, merged, row)
            if score > 0 or not text.strip():
                scored.append((score, row))
        exact_scored = self._exact_community_scored_rows(scoring_text, scored)
        if exact_scored:
            scored = exact_scored
        area_scored = self._area_scored_rows(scoring_text, scored)
        if area_scored:
            scored = area_scored
        if parsed_query.has_hard_constraints:
            scored = filter_scored_by_hard_constraints(scored, parsed_query)
            if not scored:
                return []
        strict_price = None if room_refs or parsed_query.price_range else self._requested_strict_price(scoring_text)
        if strict_price is not None:
            scored = [
                (score + 10, row)
                for score, row in scored
                if strict_price in self._row_prices(row)
            ]
            if not scored:
                return []
        scored = self._filter_scored_rows(scored, text=scoring_text)
        return [self._with_inventory_meta(row) for _, row in scored[:limit]]

    def _score_row(self, text: str, merged: str, row: dict[str, Any]) -> int:
        score = 0
        search_text = self._strip_generic_query_words(text)
        for token in re.findall(r"[a-zA-Z0-9]+|[一-鿿]{2,}", search_text):
            if token.lower() in merged:
                score += 10 + len(token)
            for gram in self._char_grams(token.lower()):
                if gram in merged:
                    score += len(gram)
            community = self._community_name(row)
            if community:
                score += fuzzy_contains_score(token, community)
        for value in row.values():
            value_text = str(value).strip().lower()
            if len(value_text) >= 2 and value_text in search_text:
                score += 3
        return score

    def _room_number_references(self, text: str) -> list[str]:
        refs = re.findall(r"\d+(?:[-－—][a-zA-Z0-9]+)+", text)
        for building, room in re.findall(r"(\d+)\s*[幢栋]\s*(\d{2,4}[a-zA-Z]?)", text):
            refs.append(f"{building}-{room}")
        for building, room in re.findall(r"(\d+)\s*号楼\s*(\d{2,4}[a-zA-Z]?)", text):
            refs.append(f"{building}-{room}")
        if not any(marker in text for marker in ("预算", "左右", "以内", "以下", "那套", "哪套", "几号", "推荐", "价格", "押")):
            for match in re.finditer(r"(?<!\d)(\d{1,2})([2-9]\d{2}[a-zA-Z]?)(?!\d)", text):
                building, room = match.group(1), match.group(2)
                next_char = text[match.end() : match.end() + 1]
                if room.endswith("00") or next_char == "的":
                    continue
                refs.append(f"{building}-{room}")
        return list(dict.fromkeys(self._normalize_room_no(ref) for ref in refs if ref))

    def _row_matches_any_room_ref(self, row: dict[str, Any], room_refs: list[str]) -> bool:
        room_no = self._normalize_room_no(str(row.get("房号", "")).strip())
        compact_room_no = self._compact_room_no(room_no)
        community = self._normalize_query(str(row.get("小区", "")).strip())
        compact_refs = {self._compact_room_no(ref) for ref in room_refs}
        if room_no and (room_no in room_refs or compact_room_no in compact_refs):
            return True
        for compact_ref in compact_refs:
            if compact_room_no and compact_ref.endswith(compact_room_no):
                prefix = compact_ref[: -len(compact_room_no)]
                if prefix and prefix in community:
                    return True
        return False

    def _normalize_room_no(self, value: str) -> str:
        value = value.lower().strip()
        value = value.replace("－", "-").replace("—", "-")
        value = re.sub(r"\s+", "", value)
        return value

    def _compact_room_no(self, value: str) -> str:
        return re.sub(r"[-－—\s]+", "", value.lower().strip())

    def _community_name(self, row: dict[str, Any]) -> str:
        for key in ("小区", "社区", "楼盘"):
            value = str(row.get(key, "")).strip()
            if value:
                return value
        return ""

    def _area_text(self, row: dict[str, Any]) -> str:
        for key in ("区域", "商圈", "板块", "位置"):
            value = str(row.get(key, "")).strip().lower()
            if value:
                return value
        return ""

    def _filter_scored_rows(
        self,
        scored: list[tuple[int, dict[str, Any]]],
        *,
        text: str = "",
    ) -> list[tuple[int, dict[str, Any]]]:
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score = scored[0][0]
        if best_score <= 0:
            return self._prefer_low_price_rows(scored, text)
        min_score = max(6, int(best_score * 0.6))
        filtered = [item for item in scored if item[0] >= min_score]
        return self._prefer_low_price_rows(filtered, text)

    def _prefer_low_price_rows(
        self,
        scored: list[tuple[int, dict[str, Any]]],
        text: str,
    ) -> list[tuple[int, dict[str, Any]]]:
        if not scored or not self._requests_low_price(text):
            return scored
        priced = [
            (score, row, lowest)
            for score, row in scored
            if (lowest := self._lowest_row_price(row)) is not None
        ]
        if not priced:
            return scored
        cheapest = min(lowest for _score, _row, lowest in priced)
        soft_ceiling = max(cheapest + 1500, int(cheapest * 1.35))
        affordable = [
            (score, row, lowest)
            for score, row, lowest in priced
            if lowest <= soft_ceiling
        ] or priced
        affordable.sort(key=lambda item: (item[2], -item[0]))
        return [(score, row) for score, row, _lowest in affordable]

    def _requests_low_price(self, text: str) -> bool:
        normalized = self._normalize_query(text)
        return any(
            marker in normalized
            for marker in (
                "低价",
                "便宜",
                "便宜点",
                "便宜的",
                "低预算",
                "预算低",
                "实惠",
                "性价比",
            )
        )

    def _lowest_row_price(self, row: dict[str, Any]) -> int | None:
        prices = self._row_prices(row)
        return min(prices) if prices else None

    def _exact_community_scored_rows(
        self,
        text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        search_text = self._strip_generic_query_words(text)
        if not search_text.strip():
            return []
        preferred_community = self._preferred_exact_community(text, scored)
        exact: list[tuple[int, dict[str, Any]]] = []
        for score, row in scored:
            community = self._community_name(row).lower()
            if preferred_community and community != preferred_community:
                continue
            if len(community) >= 2 and community in search_text:
                exact.append((score, row))
        return exact

    def _preferred_exact_community(
        self,
        text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> str:
        communities = _dedupe(
            [
                self._community_name(row).lower()
                for _score, row in scored
                if self._community_name(row).strip()
            ]
        )
        mentioned = [
            (community, text.rfind(community))
            for community in communities
            if len(community) >= 2 and community in text
        ]
        if len(mentioned) <= 1:
            return ""
        mentioned.sort(key=lambda item: item[1], reverse=True)
        return mentioned[0][0]

    def _area_scored_rows(
        self,
        text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        search_text = self._strip_generic_query_words(text).lower()
        if not search_text.strip():
            return []
        area_tokens = self._mentioned_area_tokens(search_text, scored)
        if not area_tokens:
            return []
        return [
            (score + 40, row)
            for score, row in scored
            if any(token in self._area_text(row) for token in area_tokens)
        ]

    def _mentioned_area_tokens(
        self,
        search_text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> list[str]:
        ignored = {
            "区域",
            "附近",
            "周边",
            "预算",
            "以内",
            "左右",
            "客户",
            "整租",
            "推荐",
            "视频",
            "一室",
            "两室",
            "三室",
            "四室",
            "一厅",
            "两厅",
            "独卫",
            "厨卫",
            "独立",
            "燃气",
            "阳台",
        }
        tokens: list[str] = []
        for _score, row in scored:
            area = self._area_text(row)
            for area_token in re.findall(r"[一-鿿A-Za-z0-9]+", area):
                if len(area_token) < 2:
                    continue
                candidates = [area_token]
                for size in range(min(4, len(area_token)), 1, -1):
                    candidates.extend(
                        area_token[index : index + size]
                        for index in range(len(area_token) - size + 1)
                    )
                for candidate in candidates:
                    if candidate in ignored or len(candidate) < 2:
                        continue
                    if candidate in search_text:
                        tokens.append(candidate)
        return _dedupe(tokens)

    def _requested_room_type_aliases(self, text: str) -> list[tuple[str, ...]]:
        room_type_groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
            (("一室一厅", "1室1厅", "一房一厅"), ("一室一厅", "1室1厅", "一房一厅")),
            (("两室一厅", "二室一厅", "2室1厅", "两房一厅", "二房一厅"), ("两室一厅", "二室一厅", "2室1厅", "两房一厅", "二房一厅")),
            (("两室两厅", "二室两厅", "2室2厅", "两房两厅", "二房两厅"), ("两室两厅", "二室两厅", "2室2厅", "两房两厅", "二房两厅")),
            (("三室一厅", "3室1厅", "三房一厅"), ("三室一厅", "3室1厅", "三房一厅")),
            (("三室两厅", "3室2厅", "三房两厅"), ("三室两厅", "3室2厅", "三房两厅")),
            (("四室两厅", "4室2厅", "四房两厅"), ("四室两厅", "4室2厅", "四房两厅")),
            (("一室", "1室", "一房"), ("一室", "1室", "一房")),
            (("两室", "二室", "2室", "两房", "二房", "2房"), ("两室", "二室", "2室", "两房", "二房", "2房")),
            (("三室", "3室", "三房"), ("三室", "3室", "三房")),
            (("四室", "4室", "四房"), ("四室", "4室", "四房")),
            (("单间",), ("单间",)),
        ]
        requested: list[tuple[str, ...]] = []
        for query_aliases, row_aliases in room_type_groups:
            if any(alias in text for alias in query_aliases):
                requested.append(row_aliases)
        return requested

    def _room_type_scored_rows(
        self,
        text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]] | None:
        requested_types = self._requested_room_type_aliases(text)
        if not requested_types:
            return None
        filtered: list[tuple[int, dict[str, Any]]] = []
        for score, row in scored:
            row_type = " ".join(
                str(row.get(key, "")).strip()
                for key in ("户型", "户型分类")
                if str(row.get(key, "")).strip()
            )
            if any(any(alias in row_type for alias in aliases) for aliases in requested_types):
                filtered.append((score + 20, row))
        return filtered

    def _price_scored_rows(
        self,
        text: str,
        scored: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        price_range = self._requested_price_range(text)
        if not price_range:
            return []
        low, high = price_range
        filtered = [
            (score + 10, row)
            for score, row in scored
            if any(low <= price <= high for price in self._row_prices(row))
        ]
        return filtered

    def _requested_price_range(self, text: str) -> tuple[int, int] | None:
        if not any(marker in text for marker in ("预算", "以内", "左右", "到", "至", "以下", "上下")):
            return None
        range_match = re.search(r"(\d{3,5})\s*(?:到|至|-|~|～)\s*(\d{3,5})", text)
        if range_match:
            low, high = sorted((int(range_match.group(1)), int(range_match.group(2))))
            return (low, high)
        amount_match = re.search(r"(\d{3,5})\s*(?:以内|以下)", text)
        if amount_match:
            return (0, int(amount_match.group(1)))
        amount_match = re.search(r"(\d{3,5})\s*(?:左右|上下)", text)
        if amount_match:
            amount = int(amount_match.group(1))
            return (max(0, amount - 500), amount + 500)
        if "预算" in text:
            amount_match = re.search(r"预算\D{0,6}(\d{3,5})", text)
            if amount_match:
                amount = int(amount_match.group(1))
                return (max(0, amount - 500), amount + 500)
        return None

    def _requested_strict_price(self, text: str) -> int | None:
        if any(
            marker in text
            for marker in (
                "预算",
                "左右",
                "以内",
                "以下",
                "上下",
                "附近",
                "区域",
                "推荐",
                "服务费",
                "手续费",
                "免押",
                "芝麻",
                "合同",
            )
        ):
            return None
        if re.search(r"\d{3,5}\s*(?:到|至|-|~|～)\s*\d{3,5}", text):
            return None
        if not any(marker in text for marker in ("那套", "哪套", "哪个", "哪些", "几号", "还在", "还有", "有吗", "有没有", "是不是")):
            return None
        match = re.search(r"(?<![-\d])([1-9]\d{2,4})(?![-\dA-Za-z])", text)
        return int(match.group(1)) if match else None

    def _row_prices(self, row: dict[str, Any]) -> list[int]:
        return row_prices(row)

    def format_rows(self, rows: list[dict[str, Any]], limit: int = 8) -> str:
        if not rows:
            return ""
        lines: list[str] = []
        for index, row in enumerate(rows[:limit], start=1):
            compact = "；".join(
                f"{key}:{value}"
                for key, value in self._display_row_items(row)
                if not str(key).startswith("__") and str(value).strip()
            )
            lines.append(f"{index}. {compact}")
        return "\n".join(lines)

    def _display_row_items(self, row: dict[str, Any]) -> list[tuple[str, Any]]:
        items: list[tuple[str, Any]] = []
        for key, value in row.items():
            if str(key) in {"小区", "社区", "楼盘"}:
                items.append((key, canonical_community_display(str(value))))
            else:
                items.append((key, value))
        return items

    async def snapshot(self, limit: int = 40) -> str:
        if settings.inventory_source == "local_image":
            if not self._image_text:
                await self.refresh()
            return self._image_text or (
                "\u5f53\u524d\u4f7f\u7528\u672c\u5730\u623f\u6e90\u8868\u56fe\u7247\uff1a"
                f"{settings.inventory_image_path}\u3002"
                "\u5c1a\u672a\u63d0\u53d6\u6210\u7ed3\u6784\u5316\u5e93\u5b58\u6570\u636e\u3002"
            )
        self._reload_cache_if_file_changed()
        frame = self._cache if self._cache is not None else await self.refresh()
        if frame.empty:
            return "\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u623f\u6e90\u5e93\u5b58\u6570\u636e\u3002"
        records = frame.fillna("").head(limit).to_dict(orient="records")
        lines: list[str] = []
        for index, row in enumerate(records, start=1):
            compact = "\uff1b".join(
                f"{key}:{value}"
                for key, value in self._display_row_items(row)
                if not str(key).startswith("__") and str(value).strip()
            )
            lines.append(f"{index}. {compact}")
        return "\n".join(lines)

    async def _read_image_inventory_text(self) -> str:
        image_paths = self._image_paths()
        cache_path = settings.inventory_image_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        image_signature = "|".join(f"{path.name}:{path.stat().st_mtime_ns}" for path in image_paths)
        cache_marker = f"<!-- model:{settings.dashscope_vision_model}; images:{image_signature} -->"

        if cache_path.exists() and image_paths:
            cache_text = cache_path.read_text(encoding="utf-8").strip()
            latest_image_mtime = max(path.stat().st_mtime for path in image_paths)
            if (
                cache_text
                and cache_text.startswith(cache_marker)
                and cache_path.stat().st_mtime >= latest_image_mtime
            ):
                return cache_text.replace(cache_marker, "", 1).strip()

        if not image_paths:
            return ""

        provider = settings.llm_provider_for("vision")
        if is_missing_or_placeholder(settings.llm_api_key_for(provider)):
            if cache_path.exists():
                return cache_path.read_text(encoding="utf-8").strip()
            return ""

        client = AsyncOpenAI(
            api_key=settings.llm_api_key_for(provider),
            base_url=settings.llm_base_url_for(provider),
        )
        prompt = (
            "你是专业的房源表格 OCR 和结构化转写助手。"
            "下面有多张连续的房源表截图，请按图片顺序逐行识别。"
            "请严格保留原始表格的列关系，不要总结、不要推理、不要改写。"
            "输出要求："
            "1. 用 Markdown 表格输出。"
            "2. 表头固定为：区域、小区、房号、户型、户型分类、押一付、押二付、密码、备注。"
            "3. 遇到黄色区域标题行，要把区域名称补到后续房源行的“区域”列，直到下一个区域标题。"
            "4. 合并单元格的小区名称要补齐到后续空白行。"
            "5. 每一条房源都要保留，不要只挑部分房源。"
            "6. 不确定的单元格写“看不清”，禁止猜测。"
            "7. 不要把押一付、押二付、密码、备注互相串列。"
            "8. 不要输出解释，只输出识别后的 Markdown 表格。"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            data_url = f"data:{mime_type};base64,{image_data}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        response = await client.chat.completions.create(
            model=settings.llm_model_for("vision"),
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
        if text:
            cache_path.write_text(f"{cache_marker}\n{text}", encoding="utf-8")
        return text

    def _image_paths(self) -> list:
        paths = sorted(settings.room_database_path.parent.glob(settings.inventory_image_glob))
        if not paths and settings.inventory_image_path.exists():
            paths = [settings.inventory_image_path]
        return [path for path in paths if path.is_file()]

    def _parse_image_rows(self, text: str) -> list[dict[str, str]]:
        headers = ["区域", "小区", "房号", "户型", "户型分类", "押一付", "押二付", "密码", "备注"]
        rows: list[dict[str, str]] = []
        current_area = ""
        current_community = ""

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line:
                continue
            cells = [self._clean_markdown_cell(cell) for cell in line.strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            cells = cells[: len(headers)]
            if cells == headers or cells[0] == "区域":
                continue

            row = dict(zip(headers, cells))
            if row["区域"]:
                current_area = row["区域"]
            if row["小区"]:
                current_community = row["小区"]
            if not row["房号"]:
                continue

            row["区域"] = row["区域"] or current_area
            row["小区"] = row["小区"] or current_community
            rows.append(row)
        return rows

    def _clean_markdown_cell(self, value: str) -> str:
        value = value.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
        value = re.sub(r"[*`]+", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _normalize_query(self, query: str) -> str:
        replacements = {
            "星期": "星桥",
            "星琪": "星桥",
            "星乔": "星桥",
            "小阳坝": "小洋坝",
            "小洋吧": "小洋坝",
            "小羊坝": "小洋坝",
            "小杨坝": "小洋坝",
            "堂润府": "棠润府",
            "棠闰府": "棠润府",
            "棠润肤": "棠润府",
            "杨了府": "杨乐府",
            "杨乐俯": "杨乐府",
            "扬乐府": "杨乐府",
            "杨乐付": "杨乐府",
            "兴业杨加府": "兴业杨家府",
            "兴业杨家俯": "兴业杨家府",
            "兴业杨佳府": "兴业杨家府",
        }
        text = query.lower()
        text = re.sub(r"(\d+)\s*[幢栋]\s*(\d{2,4}[a-zA-Z]?)", r"\1-\2", text)
        text = re.sub(r"(\d+)\s*号楼\s*(\d{2,4}[a-zA-Z]?)", r"\1-\2", text)
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text

    def _strip_generic_query_words(self, text: str) -> str:
        cleaned = text
        for word in GENERIC_QUERY_WORDS:
            cleaned = cleaned.replace(word, " ")
        return cleaned

    def _char_grams(self, text: str) -> list[str]:
        grams: list[str] = []
        for size in (4, 3, 2):
            grams.extend(text[index : index + size] for index in range(len(text) - size + 1))
        return list(dict.fromkeys(grams))

    async def _read_public_document(self, url: str) -> pd.DataFrame:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            ),
            "Accept": "text/csv,application/vnd.ms-excel,*/*",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            content = response.content
            final_host = urlparse(str(response.url)).hostname or ""

        if final_host.endswith("account.kdocs.cn"):
            raise ValueError("KDocs public CSV redirected to login page")

        lowered_url = url.lower()
        if "f=csv" in lowered_url or lowered_url.endswith(".csv") or "text/csv" in content_type:
            return pd.read_csv(BytesIO(content))
        if lowered_url.endswith((".xlsx", ".xls")) or "spreadsheet" in content_type:
            return pd.read_excel(BytesIO(content))

        html = content.decode("utf-8", errors="ignore")
        tables = pd.read_html(StringIO(html))
        if not tables:
            raise ValueError("No table found in public KDocs page")
        return tables[0]

    async def _read_feishu_inventory_sheet(self) -> pd.DataFrame:
        spreadsheet_token = settings.feishu_inventory_sheet_token
        if settings.feishu_inventory_drive_folder_token:
            spreadsheet_token = await self._discover_inventory_sheet_token_from_drive_folder(
                settings.feishu_inventory_drive_folder_token
            )
        data = await self._feishu_client().read_spreadsheet_values(
            spreadsheet_token=spreadsheet_token
        )
        return self._spreadsheet_values_to_frame(data.get("values") or [])

    def _feishu_client(self) -> FeishuClient:
        return self._client or FeishuClient()

    async def _discover_inventory_sheet_token_from_drive_folder(self, folder_token: str) -> str:
        files = await self._feishu_client().list_folder_files(folder_token)
        sheet_items = [
            item for item in files
            if str(item.get("type") or "").lower() in {"sheet", "spreadsheet"}
        ]
        if not sheet_items:
            raise RuntimeError("Feishu inventory drive folder has no sheet files")
        preferred = [
            item for item in sheet_items
            if any(word in str(item.get("name") or item.get("title") or "") for word in ("房源", "待租"))
        ] or sheet_items
        preferred.sort(
            key=lambda item: int(str(item.get("modified_time") or item.get("created_time") or "0") or 0),
            reverse=True,
        )
        token = str(preferred[0].get("token") or preferred[0].get("file_token") or "").strip()
        if not token:
            raise RuntimeError(f"Feishu inventory sheet token is empty: {preferred[0]}")
        return token

    def _spreadsheet_values_to_frame(self, values: list[list[Any]]) -> pd.DataFrame:
        return pd.DataFrame(spreadsheet_values_to_inventory_rows(values))

    def _normalize(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.dropna(how="all").copy()
        frame.columns = [str(column).strip() for column in frame.columns]
        column_aliases = {
            "户型": "户型描述",
            "描述": "户型描述",
            "押一付": "押一付一",
            "押二付": "押二付一",
            "密码": "看房方式密码",
            "看房密码": "看房方式密码",
            "看房方式/密码": "看房方式密码",
            "看房方式": "看房方式密码",
        }
        frame = frame.rename(
            columns={column: column_aliases.get(column, column) for column in frame.columns}
        )
        frame = frame.loc[:, [column for column in frame.columns if column]]
        return frame

    def _save_cache(self, frame: pd.DataFrame) -> None:
        path = settings.inventory_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        self._cache_file_marker = self._cache_marker()

    def _read_cache(self) -> pd.DataFrame:
        path = settings.inventory_cache_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame().to_csv(path, index=False, encoding="utf-8-sig")
            self._cache_file_marker = self._cache_marker()
            return pd.DataFrame()
        if path.stat().st_size == 0:
            self._cache_file_marker = self._cache_marker()
            return pd.DataFrame()
        try:
            frame = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            frame = pd.DataFrame()
        self._cache_file_marker = self._cache_marker()
        return frame

    def _cache_marker(self) -> tuple[int, int] | None:
        path = settings.inventory_cache_path
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _reload_cache_if_file_changed(self) -> None:
        if settings.inventory_source == "local_image" or self._cache is None:
            return
        if self._cache_file_marker is None:
            return
        marker = self._cache_marker()
        if marker is None or marker == self._cache_file_marker:
            return
        self._cache = self._read_cache()
        self._cache_meta = self._read_cache_meta()
        if not self._cache_meta:
            self._cache_meta = self._build_cache_meta(
                source=settings.inventory_source,
                status="success",
                row_count=len(self._cache),
                source_detail=str(settings.inventory_cache_path),
            )
        self._last_error = ""

    def _with_inventory_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["__inventory_meta"] = self.cache_meta
        return result

    def _feishu_source_detail(self) -> str:
        if settings.feishu_inventory_drive_folder_token:
            return f"drive_folder:{settings.feishu_inventory_drive_folder_token}"
        if settings.feishu_inventory_sheet_token:
            return f"spreadsheet:{settings.feishu_inventory_sheet_token}"
        if settings.feishu_bitable_app_token or settings.feishu_bitable_table_id:
            return f"bitable:{settings.feishu_bitable_app_token}/{settings.feishu_bitable_table_id}"
        return "feishu_bitable"

    def _build_cache_meta(
        self,
        *,
        source: str,
        status: str,
        row_count: int,
        source_detail: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        path = settings.inventory_cache_path
        now = time.time()
        path_text = str(path.resolve()) if path.exists() else str(path)
        cache_hash = self._file_sha256(path) if path.exists() and path.is_file() else ""
        mtime = path.stat().st_mtime if path.exists() else 0.0
        return {
            "source": source,
            "source_detail": source_detail,
            "status": status,
            "cache_path": path_text,
            "cache_mtime": mtime,
            "cache_mtime_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "",
            "synced_at": now,
            "synced_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "row_count": int(row_count),
            "hash": cache_hash,
            "max_age_seconds": int(getattr(settings, "inventory_cache_max_age_seconds", 300)),
            "error": str(error or "")[:500],
        }

    def _read_cache_meta(self) -> dict[str, Any]:
        path = settings.inventory_cache_meta_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        row_count = len(self._cache) if self._cache is not None else 0
        return self._build_cache_meta(
            source=str(settings.inventory_source),
            status="unknown",
            row_count=row_count,
            source_detail=str(settings.inventory_cache_path),
        )

    def _write_cache_meta(self, meta: dict[str, Any]) -> None:
        path = settings.inventory_cache_meta_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
