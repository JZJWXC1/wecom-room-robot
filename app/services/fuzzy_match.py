from __future__ import annotations

import re


def normalize_search_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text).lower()


def fuzzy_contains_score(query: str, target: str) -> int:
    query_norm = normalize_search_text(query)
    target_norm = normalize_search_text(target)
    if len(query_norm) < 2 or not target_norm:
        return 0
    if query_norm in target_norm:
        return 0
    if not re.search(r"[\u4e00-\u9fff]", query_norm):
        return 0

    best = 0
    min_size = max(2, len(query_norm) - 1)
    max_size = min(len(target_norm), len(query_norm) + 1)
    for size in range(min_size, max_size + 1):
        for index in range(len(target_norm) - size + 1):
            candidate = target_norm[index : index + size]
            distance = _bounded_levenshtein(query_norm, candidate, _max_distance(query_norm))
            if distance is None:
                continue
            common_count = len(set(query_norm) & set(candidate))
            if len(query_norm) <= 2 and common_count < 1:
                continue
            if len(query_norm) >= 3 and common_count < max(2, len(set(query_norm)) - 1):
                continue
            ratio = 1 - distance / max(len(query_norm), len(candidate))
            best = max(best, int(36 * ratio) + len(query_norm) * 2)
    return best


def _max_distance(text: str) -> int:
    return 1 if len(text) <= 5 else 2


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
