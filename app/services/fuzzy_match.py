from __future__ import annotations

import re


COMMUNITY_DISPLAY_ALIASES = {
    "华丰新苑": "华丰欣苑",
    "华丰欣园": "华丰欣苑",
    "合塘悦府": "合嵣悦府",
    "合峙悦府": "合嵣悦府",
    "合幢悦府": "合嵣悦府",
    "棠闰府": "棠润府",
    "堂润府": "棠润府",
    "棠润肤": "棠润府",
    "杨了府": "杨乐府",
    "杨乐俯": "杨乐府",
    "扬乐府": "杨乐府",
    "兴业杨加府": "兴业杨家府",
    "兴业杨家俯": "兴业杨家府",
    "兴业杨佳府": "兴业杨家府",
    "杨家兴雅苑": "杨家新雅苑",
    "石桥名苑": "石桥铭苑",
    "石桥明苑": "石桥铭苑",
    "永佳新园": "永佳新苑",
    "永住新苑": "永佳新苑",
    "范骏悦邸": "范珺悦邸",
    "范俊悦邸": "范珺悦邸",
    "范君悦邸": "范珺悦邸",
    "高塘运都": "皋塘运都",
    "皋塘云都": "皋塘运都",
    "孔家带和府": "孔家埭和府",
    "孔家埭合府": "孔家埭和府",
    "爱颐湾": "瑷颐湾",
    "瑷颐弯": "瑷颐湾",
    "中融城花园": "中融城市花园",
    "中融城市华园": "中融城市花园",
    "西文北院": "西文北苑",
    "西纹北苑": "西文北苑",
}


def canonical_community_display(text: str) -> str:
    cleaned = str(text or "").strip()
    return COMMUNITY_DISPLAY_ALIASES.get(cleaned, cleaned)


def normalize_search_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]", "", text).lower()


def fuzzy_contains_score(query: str, target: str) -> int:
    query_norm = normalize_search_text(query)
    target_norm = normalize_search_text(target)
    if len(query_norm) < 2 or not target_norm:
        return 0
    if query_norm in target_norm:
        return 0
    if not re.search(r"[一-鿿]", query_norm):
        return 0
    if not re.search(r"[一-鿿]", target_norm):
        return 0

    # 小区名模糊匹配只允许“同长度、一个错别字”的情况。
    # 三字小区首字不同风险很高，例如“荣润府”不能自动猜成“棠润府”；
    # 明确常见别名仍然走 COMMUNITY_DISPLAY_ALIASES 精准归一。
    if len(query_norm) <= 3 and len(target_norm) <= 3 and query_norm[:1] != target_norm[:1]:
        return 0
    best = _single_typo_substring_score(query_norm, target_norm)
    if best:
        return best
    return _single_typo_substring_score(target_norm, query_norm)


def _single_typo_substring_score(source: str, target: str) -> int:
    if len(target) < 2 or len(source) < len(target):
        return 0
    best = 0
    size = len(target)
    for index in range(len(source) - size + 1):
        candidate = source[index : index + size]
        previous_char = source[index - 1 : index]
        next_char = source[index + size : index + size + 1]
        if (previous_char and previous_char == candidate[0]) or (next_char and next_char == candidate[-1]):
            continue
        distance = _bounded_levenshtein(candidate, target, 1)
        if distance != 1:
            continue
        common_count = len(set(candidate) & set(target))
        if common_count < 2:
            continue
        ratio = 1 - distance / max(len(candidate), len(target))
        best = max(best, int(36 * ratio) + len(target) * 2)
    return best


def _bounded_levenshtein(left: str, right: str, max_distance: int) -> int | None:
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
