from __future__ import annotations

import asyncio

from app.config import settings
from app.services.inventory import InventoryService
from app.services.inventory_query import parse_inventory_query


QUERIES = [
    "万达有什么2000以下的一室",
    "拱墅万达 2000以下 一室",
    "荣润府有没有押一付一的？预算1600到1800。",
    "棠润府 1600 1800 押一付一",
    "石桥附近5000左右有两室吗？最好整租。",
    "石桥 5000 两室 整租",
]


def value(row: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return ""


async def main() -> None:
    svc = InventoryService()
    rows = await svc.all_rows(limit=1000)
    print("source=", settings.inventory_source, "rows=", len(rows), "last_error=", svc.last_error)
    print("columns_sample=", list(rows[0].keys()) if rows else [])
    for query in QUERIES:
        parsed = parse_inventory_query(query)
        print(
            "\nPARSED:",
            query,
            {
                "room_refs": parsed.room_refs,
                "price_range": parsed.price_range,
                "room_type_labels": parsed.room_type_labels,
                "anchor_terms": parsed.anchor_terms,
                "normalized_text": parsed.normalized_text,
            },
        )
        found = await svc.search(query, limit=8)
        print("\nQUERY:", query, "count=", len(found))
        for row in found[:8]:
            print(
                " -",
                value(row, "区域"),
                value(row, "小区"),
                value(row, "房号"),
                value(row, "户型分类", "户型"),
                value(row, "押一付一", "押一付"),
                value(row, "押二付一", "押二付"),
                value(row, "备注"),
            )


if __name__ == "__main__":
    asyncio.run(main())
