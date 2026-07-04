from __future__ import annotations

import re
from pathlib import Path

from app.services.region_inventory_constants import (
    DEFAULT_AREA_DRIVE_FOLDER_NAMES,
    MEDIA_WRAPPER_FOLDER_NAMES,
    NOTE_ALIASES,
    NOT_RENTING_WORDS,
    ROOM_REFERENCE_RE,
    STATUS_ALIASES,
)


def pick_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = str(row.get(alias) or "").strip()
        if value:
            return value
    return ""


def is_not_renting(row: dict[str, str]) -> bool:
    status = pick_value(row, STATUS_ALIASES)
    return any(word in status for word in NOT_RENTING_WORDS)


def room_reference_from_row(row: dict[str, str], community: str = "", room_no: str = "") -> tuple[str, str]:
    texts = [pick_value(row, NOTE_ALIASES), *[str(value) for value in row.values()]]
    for text in texts:
        match = ROOM_REFERENCE_RE.search(text)
        if match:
            return community or match.group(1).strip(), room_no or normalize_room_no(match.group(2))
    return community, room_no


def normalize_room_no(value: str) -> str:
    return value.replace("－", "-").replace("—", "-").strip()


def normalize_key(community: str, room_no: str) -> str:
    return normalize_text(f"{community}{normalize_room_no(room_no)}")


def normalize_text(value: str) -> str:
    return re.sub(r"[\s\\/_\-－—:：，,。.!！?？]+", "", str(value or "")).lower()


def folder_match_key(value: str) -> str:
    cleaned = str(value or "").casefold()
    cleaned = cleaned.translate(
        str.maketrans(
            {
                "\u00a0": "",
                "\u1680": "",
                "\u180e": "",
                "\u2000": "",
                "\u2001": "",
                "\u2002": "",
                "\u2003": "",
                "\u2004": "",
                "\u2005": "",
                "\u2006": "",
                "\u2007": "",
                "\u2008": "",
                "\u2009": "",
                "\u200a": "",
                "\u200b": "",
                "\u202f": "",
                "\u205f": "",
                "\u3000": "",
                "\ufeff": "",
                "－": "-",
                "—": "-",
                "–": "-",
                "_": "-",
                "/": "-",
                "\\": "-",
            }
        )
    )
    return re.sub(r"[\s\-:：，,。.!！?？]+", "", cleaned)


def is_media_wrapper_folder(name: str) -> bool:
    key = folder_match_key(name)
    return bool(key) and any(key == folder_match_key(wrapper) for wrapper in MEDIA_WRAPPER_FOLDER_NAMES)


def media_file_match_key(value: str) -> str:
    path = Path(safe_name(value))
    stem = folder_match_key(path.stem)
    suffix = path.suffix.casefold()
    return f"{stem}{suffix}"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "unnamed"

def drive_area_folder_name(area_title: str) -> str:
    mapped = DEFAULT_AREA_DRIVE_FOLDER_NAMES.get(area_title)
    if mapped:
        return mapped
    cleaned = re.sub(r"除特价成交全部全佣.*$", "", area_title).strip()
    cleaned = re.sub(r"成交全部全佣.*$", "", cleaned).strip()
    return cleaned or area_title
