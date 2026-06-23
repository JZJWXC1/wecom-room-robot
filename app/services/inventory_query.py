from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text


ROOM_TYPE_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "一室一厅",
        (
            "一室一厅",
            "1室1厅",
            "一房一厅",
            "一室带厅",
            "1室带厅",
            "一房带厅",
            "带厅的一室",
            "带厅一室",
            "一室要带厅",
            "一室有厅",
        ),
        ("一室一厅", "1室1厅", "一房一厅"),
        "一室",
    ),
    ("两室一厅", ("两室一厅", "二室一厅", "2室1厅", "两房一厅", "二房一厅"), ("两室一厅", "二室一厅", "2室1厅", "两房一厅", "二房一厅"), "两室"),
    ("两室两厅", ("两室两厅", "二室两厅", "2室2厅", "两房两厅", "二房两厅"), ("两室两厅", "二室两厅", "2室2厅", "两房两厅", "二房两厅"), "两室"),
    ("三室一厅", ("三室一厅", "3室1厅", "三房一厅"), ("三室一厅", "3室1厅", "三房一厅"), "三室"),
    ("三室两厅", ("三室两厅", "3室2厅", "三房两厅"), ("三室两厅", "3室2厅", "三房两厅"), "三室"),
    ("四室两厅", ("四室两厅", "4室2厅", "四房两厅"), ("四室两厅", "4室2厅", "四房两厅"), "四室"),
    ("一室", ("一室", "1室", "一房"), ("一室", "1室", "一房"), "一室"),
    ("两室", ("两室", "二室", "2室", "两房", "二房", "2房"), ("两室", "二室", "2室", "两房", "二房", "2房"), "两室"),
    ("三室", ("三室", "3室", "三房"), ("三室", "3室", "三房"), "三室"),
    ("四室", ("四室", "4室", "四房"), ("四室", "4室", "四房"), "四室"),
    ("单间", ("单间", "开间"), ("单间", "开间", "一室", "1室", "一房"), "一室"),
)

FEATURE_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("燃气", ("燃气", "带燃气", "煤气", "天然气"), ("燃气", "煤气", "天然气")),
    ("阳台", ("阳台", "带阳台"), ("阳台", "带阳台")),
    (
        "独立厨卫",
        ("独厨卫", "独立厨卫", "独立厨房", "独卫", "独厨", "带厨房", "厨房"),
        ("独厨卫", "独立厨卫", "独立厨房", "独立厨", "独卫", "厨卫", "厨房", "内厨", "内卫"),
    ),
    ("朝南", ("朝南", "南向"), ("朝南", "南向")),
)

GENERIC_ANCHOR_WORDS = {
    "有没有",
    "还有没有",
    "有什么",
    "有吗",
    "还有",
    "有房",
    "房子",
    "房源",
    "房间",
    "小区",
    "区域",
    "附近",
    "周边",
    "预算",
    "左右",
    "上下",
    "以内",
    "以下",
    "价格",
    "多少钱",
    "视频",
    "照片",
    "图片",
    "原视频",
    "高清视频",
    "高清",
    "清楚",
    "清楚点",
    "发我",
    "发一下",
    "发给我",
    "给我发",
    "先发",
    "都发",
    "都发我",
    "全部",
    "全发",
    "看看",
    "看一下",
    "筛一下",
    "资料",
    "详情",
    "户型",
    "整租",
    "最好",
    "合适",
    "最合适",
    "押一付",
    "押二付",
    "押一付一",
    "押二付一",
    "推荐",
    "客户",
    "租客",
    "这边",
    "那边",
    "这里",
    "那里",
    "这些",
    "那些",
    "这几个",
    "那几个",
    "这几套",
    "那几套",
    "这两套",
    "那两套",
    "前两套",
    "前三套",
    "第一套",
    "第二套",
    "第三套",
    "第一个",
    "第二个",
    "第三个",
    "这套",
    "那套",
    "这个",
    "那个",
    "一室",
    "两室",
    "二室",
    "三室",
    "四室",
    "一房",
    "两房",
    "二房",
    "三房",
    "四房",
    "一厅",
    "两厅",
    "单间",
    "开间",
    "独卫",
    "厨卫",
    "带厅",
    "有厅",
    "要厅",
    "也算",
    "厨房",
    "带厨房",
    "独厨",
    "独立厨卫",
    "独立厨房",
    "独立",
    "燃气",
    "阳台",
    "朝南",
    "朝北",
    "水电",
    "水电费",
    "怎么收",
    "密码",
    "密码多少",
    "看房",
    "今天",
    "今天看",
    "自己看",
    "能看",
    "空出",
    "什么时候空出",
    "联系谁",
    "怎么安排",
    "怎么定",
    "定房",
    "看中",
    "最低",
    "更低",
    "啊",
    "的",
    "吗",
    "嘛",
    "呢",
    "吧",
}


@dataclass(frozen=True)
class InventoryQuery:
    text: str
    normalized_text: str
    room_refs: tuple[str, ...] = ()
    price_range: tuple[int, int] | None = None
    room_type_aliases: tuple[tuple[str, ...], ...] = ()
    room_type_labels: tuple[str, ...] = ()
    feature_aliases: tuple[tuple[str, ...], ...] = ()
    feature_labels: tuple[str, ...] = ()
    anchor_terms: tuple[str, ...] = ()

    @property
    def has_hard_constraints(self) -> bool:
        return bool(self.room_refs or self.price_range or self.room_type_aliases or self.feature_aliases)

    @property
    def has_specific_anchor(self) -> bool:
        return bool(self.anchor_terms or self.room_refs)


def parse_inventory_query(text: str) -> InventoryQuery:
    normalized_text = _normalize_query_text(text)
    room_refs = tuple(_room_number_references(normalized_text))
    price_range = _requested_price_range(normalized_text)
    room_type_aliases, room_type_labels = _requested_room_types(normalized_text)
    feature_aliases, feature_labels = _requested_features(normalized_text)
    anchor_terms = tuple(_anchor_terms(normalized_text, room_refs, room_type_labels))
    return InventoryQuery(
        text=text,
        normalized_text=normalized_text,
        room_refs=room_refs,
        price_range=price_range,
        room_type_aliases=tuple(room_type_aliases),
        room_type_labels=tuple(room_type_labels),
        feature_aliases=tuple(feature_aliases),
        feature_labels=tuple(feature_labels),
        anchor_terms=anchor_terms,
    )


def row_matches_hard_constraints(row: dict[str, Any], query: InventoryQuery) -> bool:
    if query.room_refs and not _row_matches_any_room_ref(row, list(query.room_refs)):
        return False
    if query.room_type_aliases and not row_matches_room_type(row, query):
        return False
    if query.feature_aliases and not row_matches_feature_constraints(row, query):
        return False
    if query.price_range and not row_matches_price_range(row, query.price_range):
        return False
    return True


def filter_rows_by_hard_constraints(rows: list[dict[str, Any]], query: InventoryQuery) -> list[dict[str, Any]]:
    if not query.has_hard_constraints:
        return rows
    return [row for row in rows if row_matches_hard_constraints(row, query)]


def filter_scored_by_hard_constraints(
    scored: list[tuple[int, dict[str, Any]]],
    query: InventoryQuery,
) -> list[tuple[int, dict[str, Any]]]:
    if not query.has_hard_constraints:
        return scored
    return [(score, row) for score, row in scored if row_matches_hard_constraints(row, query)]


def row_matches_room_type(row: dict[str, Any], query: InventoryQuery) -> bool:
    row_type = " ".join(
        str(row.get(key, "")).strip()
        for key in ("户型", "户型分类")
        if str(row.get(key, "")).strip()
    )
    if not row_type:
        return False
    for index, aliases in enumerate(query.room_type_aliases):
        label = query.room_type_labels[index] if index < len(query.room_type_labels) else ""
        if label == "单间":
            if _row_matches_single_room(row_type):
                return True
            continue
        if any(alias in row_type for alias in aliases):
            return True
    return False


def row_matches_feature_constraints(row: dict[str, Any], query: InventoryQuery) -> bool:
    row_text = _normalize_query_text(
        " ".join(
            str(value).strip()
            for key, value in row.items()
            if key not in {"区域", "商圈", "板块", "位置", "小区", "社区", "楼盘", "房号", "房间号"}
            and str(value).strip()
        )
    )
    return all(any(_normalize_query_text(alias) in row_text for alias in aliases) for aliases in query.feature_aliases)


def _row_matches_single_room(row_type: str) -> bool:
    text = _normalize_query_text(row_type)
    if any(alias in text for alias in ("单间", "开间")):
        return True
    if any(alias in text for alias in ("一室一厅", "1室1厅", "一房一厅", "带厅")):
        return False
    if any(alias in text for alias in ("两室", "二室", "2室", "三室", "3室", "四室", "4室")):
        return False
    return any(alias in text for alias in ("一室", "1室", "一房"))


def row_matches_price_range(row: dict[str, Any], price_range: tuple[int, int]) -> bool:
    low, high = price_range
    return any(low <= price <= high for price in row_prices(row))


def row_prices(row: dict[str, Any]) -> list[int]:
    prices: list[int] = []
    for key, value in row.items():
        key_text = str(key)
        if not any(marker in key_text for marker in ("押", "价", "租金")):
            continue
        for match in re.findall(r"\d{3,5}", str(value)):
            prices.append(int(match))
    return prices


def row_matches_query_anchor(row: dict[str, Any], query: InventoryQuery) -> bool:
    if not query.anchor_terms:
        return True
    anchor_text = normalize_search_text(
        " ".join(
            str(row.get(key, "")).strip()
            for key in ("区域", "商圈", "板块", "位置", "小区", "社区", "楼盘", "房号", "房间号")
            if str(row.get(key, "")).strip()
        )
    )
    community = str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip()
    for term in query.anchor_terms:
        normalized_term = normalize_search_text(term)
        if normalized_term and normalized_term in anchor_text:
            return True
        if community and fuzzy_contains_score(term, community) >= 20:
            return True
    return False


def has_new_anchor_outside_rows(text: str, rows: list[dict[str, Any]]) -> bool:
    query = parse_inventory_query(text)
    if not query.anchor_terms:
        return False
    return not any(row_matches_query_anchor(row, query) for row in rows)


def _normalize_query_text(text: str) -> str:
    value = (text or "").lower().strip()
    value = value.replace("－", "-").replace("—", "-")
    return re.sub(r"\s+", "", value)


def _room_number_references(text: str) -> list[str]:
    refs = re.findall(r"[a-zA-Z]?\d+(?:[-－—]\d+)+(?:[-－—][a-zA-Z])?(?:[a-zA-Z])?", text)
    for building, room in re.findall(r"(\d+)\s*[幢栋]\s*(\d{2,4}[a-zA-Z]?)", text):
        refs.append(f"{building}-{room}")
    for building, room in re.findall(r"(\d+)\s*号楼\s*(\d{2,4}[a-zA-Z]?)", text):
        refs.append(f"{building}-{room}")
    if not refs and not any(marker in text for marker in ("预算", "左右", "以内", "以下", "那套", "哪套", "几号", "推荐", "价格", "押")):
        for match in re.finditer(r"(?<!\d)(\d{1,2})([2-9]\d{2}[a-zA-Z]?)(?!\d)", text):
            building, room = match.group(1), match.group(2)
            next_char = text[match.end() : match.end() + 1]
            if room.endswith("00") or next_char == "的":
                continue
            refs.append(f"{building}-{room}")
    return list(
        dict.fromkeys(
            _normalize_room_no(ref)
            for ref in refs
            if ref and not _looks_like_price_range_ref(ref)
        )
    )


def _looks_like_price_range_ref(ref: str) -> bool:
    parts = re.split(r"[-－—]", str(ref or "").strip())
    if len(parts) != 2:
        return False
    left, right = parts
    if not (left.isdigit() and right.isdigit()):
        return False
    if left == "0" and len(right) >= 3:
        return True
    return len(left) >= 3 and len(right) >= 3


def _requested_price_range(text: str) -> tuple[int, int] | None:
    range_match = re.search(r"(\d{3,5})\s*(?:到|至|-|~|～)\s*(\d{3,5})", text)
    if range_match:
        low, high = sorted((int(range_match.group(1)), int(range_match.group(2))))
        return (low, high)
    if not any(marker in text for marker in ("预算", "以内", "左右", "到", "至", "以下", "上下")):
        return None
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


def _requested_room_types(text: str) -> tuple[list[tuple[str, ...]], list[str]]:
    exact_aliases: list[tuple[str, ...]] = []
    exact_labels: list[str] = []
    exact_families: set[str] = set()
    broad_aliases: list[tuple[str, ...]] = []
    broad_labels: list[str] = []
    has_one_room_context = any(alias in text for alias in ("一室", "1室", "一房"))
    has_living_room_request = any(alias in text for alias in ("带厅", "有厅", "要厅"))
    for label, query_aliases, row_aliases, family in ROOM_TYPE_GROUPS:
        if label == "一室一厅" and has_living_room_request and has_one_room_context:
            exact_aliases.append(row_aliases)
            exact_labels.append(label)
            exact_families.add(family)
            continue
        if not any(alias in text for alias in query_aliases):
            continue
        is_broad = label in {"一室", "两室", "三室", "四室", "单间"}
        if is_broad:
            if label in exact_families:
                continue
            broad_aliases.append(row_aliases)
            broad_labels.append(label)
        else:
            exact_aliases.append(row_aliases)
            exact_labels.append(label)
            exact_families.add(family)
            broad_aliases = [
                aliases
                for aliases, broad_label in zip(broad_aliases, broad_labels)
                if broad_label != family
            ]
            broad_labels = [broad_label for broad_label in broad_labels if broad_label != family]
    aliases = exact_aliases + broad_aliases
    labels = exact_labels + broad_labels
    return _dedupe_tuple_list(aliases), list(dict.fromkeys(labels))


def _requested_features(text: str) -> tuple[list[tuple[str, ...]], list[str]]:
    aliases: list[tuple[str, ...]] = []
    labels: list[str] = []
    for label, query_aliases, row_aliases in FEATURE_GROUPS:
        if any(alias in text for alias in query_aliases):
            aliases.append(row_aliases)
            labels.append(label)
    return _dedupe_tuple_list(aliases), list(dict.fromkeys(labels))


def _anchor_terms(text: str, room_refs: tuple[str, ...], room_type_labels: tuple[str, ...]) -> list[str]:
    cleaned = text
    for ref in room_refs:
        cleaned = cleaned.replace(ref, "")
        cleaned = cleaned.replace(ref.replace("-", ""), "")
    cleaned = re.sub(r"\d{3,5}", "", cleaned)
    for label in room_type_labels:
        cleaned = cleaned.replace(label, "")
    for word in sorted(GENERIC_ANCHOR_WORDS, key=len, reverse=True):
        cleaned = cleaned.replace(word, "")
    terms = re.findall(r"[a-zA-Z]{2,}|[一-鿿]{2,}", cleaned)
    return list(dict.fromkeys(term for term in terms if term not in GENERIC_ANCHOR_WORDS))


def _row_matches_any_room_ref(row: dict[str, Any], room_refs: list[str]) -> bool:
    room_no = _normalize_room_no(str(row.get("房号") or row.get("房间号") or row.get("room_id") or row.get("RoomID") or row.get("编号") or "").strip())
    compact_room_no = _compact_room_no(room_no)
    community = normalize_search_text(str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip()).lower()
    compact_refs = {_compact_room_no(ref) for ref in room_refs}
    if room_no and (room_no in room_refs or compact_room_no in compact_refs):
        return True
    for compact_ref in compact_refs:
        if compact_room_no and compact_ref.endswith(compact_room_no):
            prefix = compact_ref[: -len(compact_room_no)]
            if prefix and prefix in community:
                return True
    return False


def _normalize_room_no(value: str) -> str:
    value = value.lower().strip()
    value = value.replace("－", "-").replace("—", "-")
    return re.sub(r"\s+", "", value)


def _compact_room_no(value: str) -> str:
    return re.sub(r"[-－—\s]+", "", value.lower().strip())


def _dedupe_tuple_list(items: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    seen: set[tuple[str, ...]] = set()
    result: list[tuple[str, ...]] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
