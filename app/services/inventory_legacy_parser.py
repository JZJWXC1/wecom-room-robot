from __future__ import annotations

from typing import Any


HEADER_ALIASES = {
    "户型": "户型描述",
    "描述": "户型描述",
    "押一付": "押一付一",
    "押二付": "押二付一",
    "密码": "看房方式密码",
    "看房密码": "看房方式密码",
    "看房方式/密码": "看房方式密码",
    "看房方式": "看房方式密码",
    "房间号": "房号",
    "编号": "房号",
    "社区": "小区",
    "楼盘": "小区",
    "图片状态": "图片",
    "视频状态": "视频",
}

TARGET_HEADERS = {
    "区域",
    "小区",
    "房号",
    "户型描述",
    "户型分类",
    "押一付一",
    "押二付一",
    "看房方式密码",
    "备注",
    "租期",
    "图片",
    "视频",
    "房源图片",
    "房源视频",
    "图片数量",
    "视频数量",
    "has_image",
    "has_video",
}


def spreadsheet_values_to_inventory_rows(values: list[list[Any]]) -> list[dict[str, str]]:
    header_index = -1
    headers: list[str] = []
    for index, row in enumerate(values):
        normalized = [
            HEADER_ALIASES.get(str(cell).strip(), str(cell).strip())
            for cell in row
        ]
        if "小区" in normalized and "房号" in normalized:
            header_index = index
            headers = normalized
            break
    if header_index < 0:
        return []

    rows: list[dict[str, str]] = []
    current_area = ""
    current_community = ""
    for raw_row in values[header_index + 1 :]:
        padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
        row = {
            header: str(value).replace("\ufeff", "").strip()
            for header, value in zip(headers, padded)
            if header in TARGET_HEADERS
        }
        if not any(row.values()):
            continue
        room_no = row.get("房号", "")
        community = row.get("小区", "")
        area = row.get("区域", "")
        if area and not community and not room_no:
            current_area = area
            current_community = ""
            continue
        if area:
            if area != current_area:
                current_community = ""
            current_area = area
        if community:
            current_community = community
        if not room_no:
            continue
        row["区域"] = row.get("区域") or current_area
        row["小区"] = row.get("小区") or current_community
        rows.append(row)
    return rows
