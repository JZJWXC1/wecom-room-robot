from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping
import unicodedata


AreaAliasStatus = Literal["active", "ambiguous", "obsolete"]


TARGET_HEADERS = [
    "区域",
    "小区",
    "房号",
    "户型",
    "户型分类",
    "押一付一",
    "押二付一",
    "看房方式密码",
    "备注",
]
DEFAULT_TARGET_AREA_TITLES = [
    "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "东新园 杭氧 新天地 成交全部全佣🧧",
    "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
]
DEFAULT_AREA_DRIVE_FOLDER_NAMES = {
    "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧": "拱墅万达 北部软件园 城北万象城",
    "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧": "石桥街道 华丰 石桥 永佳 半山",
    "东新园 杭氧 新天地 成交全部全佣🧧": "东新园 杭氧 新天地",
    "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧": "闸弄口 新塘 元宝塘 东站",
}
DEFAULT_AREA_LABELS = {
    "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧": "拱墅万达\n北部软件园\n城北万象城",
    "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧": "石桥街道\n华丰 石桥\n永佳 半山",
    "东新园 杭氧 新天地 成交全部全佣🧧": "东新园\n杭氧\n新天地",
    "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧": "闸弄口\n新塘\n元宝塘\n东站",
}

CANONICAL_AREA_TITLES = {
    canonical: title
    for title, canonical in DEFAULT_AREA_DRIVE_FOLDER_NAMES.items()
}


@dataclass(frozen=True)
class AreaAliasDefinition:
    alias: str
    canonical_area: str
    provenance: str
    status: AreaAliasStatus = "active"
    ambiguity: bool = False
    classification: str = ""
    normalized_alias: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias", normalize_area_alias_text(self.alias))
        object.__setattr__(self, "canonical_area", normalize_area_alias_text(self.canonical_area))
        normalized = self.normalized_alias or self.alias
        object.__setattr__(self, "normalized_alias", normalize_area_alias_text(normalized))

    def to_index_entry(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "normalized_alias": self.normalized_alias,
            "canonical": self.canonical_area,
            "canonical_area": self.canonical_area,
            "provenance": self.provenance,
            "status": self.status,
            "ambiguity": self.ambiguity,
        }


@dataclass(frozen=True)
class AreaAliasValidationResult:
    missing_valid_aliases: int
    unresolved_aliases: int
    active_alias_conflicts: int
    unknown_canonical_areas: int
    ambiguous_direct_mappings: int

    @property
    def ok(self) -> bool:
        return not any(self.to_dict().values())

    def to_dict(self) -> dict[str, int]:
        return {
            "missing_valid_aliases": self.missing_valid_aliases,
            "unresolved_aliases": self.unresolved_aliases,
            "active_alias_conflicts": self.active_alias_conflicts,
            "unknown_canonical_areas": self.unknown_canonical_areas,
            "ambiguous_direct_mappings": self.ambiguous_direct_mappings,
        }


def normalize_area_alias_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").replace("\ufeff", "")).strip()
    return re.sub(r"\s+", " ", text)


def canonical_area_title(canonical_area: str) -> str:
    canonical = normalize_area_alias_text(canonical_area)
    return CANONICAL_AREA_TITLES.get(canonical, canonical)


AREA_ALIAS_DEFINITIONS: tuple[AreaAliasDefinition, ...] = (
    AreaAliasDefinition("万达", "拱墅万达 北部软件园 城北万象城", "business_shorthand"),
    AreaAliasDefinition("拱墅万达", "拱墅万达 北部软件园 城北万象城", "canonical_component"),
    AreaAliasDefinition("北部软件园", "拱墅万达 北部软件园 城北万象城", "canonical_component"),
    AreaAliasDefinition("城北万象城", "拱墅万达 北部软件园 城北万象城", "canonical_component"),
    AreaAliasDefinition("拱墅万达 北部软件园 城北万象城", "拱墅万达 北部软件园 城北万象城", "canonical_area"),
    AreaAliasDefinition("万达区域", "拱墅万达 北部软件园 城北万象城", "business_shorthand"),
    AreaAliasDefinition("华丰", "石桥街道 华丰 石桥 永佳 半山", "canonical_component"),
    AreaAliasDefinition("石桥", "石桥街道 华丰 石桥 永佳 半山", "canonical_component"),
    AreaAliasDefinition("石桥街道", "石桥街道 华丰 石桥 永佳 半山", "canonical_component"),
    AreaAliasDefinition("永佳", "石桥街道 华丰 石桥 永佳 半山", "canonical_component"),
    AreaAliasDefinition("半山", "石桥街道 华丰 石桥 永佳 半山", "canonical_component"),
    AreaAliasDefinition("杨家", "石桥街道 华丰 石桥 永佳 半山", "business_shorthand", status="ambiguous", ambiguity=True),
    AreaAliasDefinition("华丰 石桥 永佳 杨家", "石桥街道 华丰 石桥 永佳 半山", "business_shorthand"),
    AreaAliasDefinition("石桥街道 华丰 石桥 永佳 半山", "石桥街道 华丰 石桥 永佳 半山", "canonical_area"),
    AreaAliasDefinition("东新", "东新园 杭氧 新天地", "business_shorthand", classification="valid_missing"),
    AreaAliasDefinition("东新园", "东新园 杭氧 新天地", "canonical_component"),
    AreaAliasDefinition("杭氧", "东新园 杭氧 新天地", "canonical_component"),
    AreaAliasDefinition("新天地", "东新园 杭氧 新天地", "canonical_component"),
    AreaAliasDefinition("鑫天地", "东新园 杭氧 新天地", "legacy_typo_alias"),
    AreaAliasDefinition("新填地", "东新园 杭氧 新天地", "legacy_typo_alias", classification="valid_missing"),
    AreaAliasDefinition("东新 杭氧 新天地", "东新园 杭氧 新天地", "canonical_area_variant"),
    AreaAliasDefinition("东新园 杭氧 新天地", "东新园 杭氧 新天地", "canonical_area"),
    AreaAliasDefinition("东站", "闸弄口 新塘 元宝塘 东站", "canonical_component"),
    AreaAliasDefinition("闸弄口", "闸弄口 新塘 元宝塘 东站", "canonical_component"),
    AreaAliasDefinition("新塘", "闸弄口 新塘 元宝塘 东站", "canonical_component"),
    AreaAliasDefinition("元宝塘", "闸弄口 新塘 元宝塘 东站", "canonical_component"),
    AreaAliasDefinition("东站 闸弄口 新塘 元宝塘", "闸弄口 新塘 元宝塘 东站", "canonical_area_variant"),
    AreaAliasDefinition("闸弄口 新塘 元宝塘 东站", "闸弄口 新塘 元宝塘 东站", "canonical_area"),
)

REQUIRED_ACTIVE_AREA_ALIASES = {
    "新填地": "东新园 杭氧 新天地",
    "东新": "东新园 杭氧 新天地",
}


def active_area_alias_definitions(
    definitions: tuple[AreaAliasDefinition, ...] | list[AreaAliasDefinition] = AREA_ALIAS_DEFINITIONS,
) -> tuple[AreaAliasDefinition, ...]:
    return tuple(item for item in definitions if item.status == "active" and not item.ambiguity)


def area_alias_index_entries(
    definitions: tuple[AreaAliasDefinition, ...] | list[AreaAliasDefinition] = AREA_ALIAS_DEFINITIONS,
    *,
    extra_aliases: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    items = list(active_area_alias_definitions(definitions))
    if extra_aliases:
        known = {(item.normalized_alias, item.canonical_area) for item in items}
        for alias, canonical in extra_aliases.items():
            definition = AreaAliasDefinition(alias, canonical, "runtime_override")
            if (definition.normalized_alias, definition.canonical_area) not in known:
                items.append(definition)
    entries = [item.to_index_entry() for item in items]
    return sorted(entries, key=lambda item: (str(item["normalized_alias"]), str(item["alias"])))


def active_area_title_alias_map(
    definitions: tuple[AreaAliasDefinition, ...] | list[AreaAliasDefinition] = AREA_ALIAS_DEFINITIONS,
) -> dict[str, str]:
    result = {
        item.alias: canonical_area_title(item.canonical_area)
        for item in active_area_alias_definitions(definitions)
    }
    for title in DEFAULT_TARGET_AREA_TITLES:
        result[title] = title
    return result


def validate_area_alias_definitions(
    definitions: tuple[AreaAliasDefinition, ...] | list[AreaAliasDefinition] = AREA_ALIAS_DEFINITIONS,
    *,
    required_aliases: Mapping[str, str] = REQUIRED_ACTIVE_AREA_ALIASES,
    known_canonical_areas: set[str] | None = None,
) -> AreaAliasValidationResult:
    known = known_canonical_areas or set(CANONICAL_AREA_TITLES)
    active = active_area_alias_definitions(definitions)
    active_by_normalized: dict[str, set[str]] = {}
    for item in active:
        active_by_normalized.setdefault(item.normalized_alias, set()).add(item.canonical_area)

    missing_valid_aliases = 0
    for alias, canonical in required_aliases.items():
        normalized = normalize_area_alias_text(alias)
        canonical_normalized = normalize_area_alias_text(canonical)
        if canonical_normalized not in active_by_normalized.get(normalized, set()):
            missing_valid_aliases += 1

    unresolved_aliases = sum(
        1
        for item in definitions
        if item.status == "active" and (not item.normalized_alias or not item.canonical_area or item.ambiguity)
    )
    active_alias_conflicts = sum(1 for targets in active_by_normalized.values() if len(targets) > 1)
    unknown_canonical_areas = sum(1 for item in active if item.canonical_area not in known)
    ambiguous_direct_mappings = sum(1 for item in definitions if item.status == "ambiguous" and not item.ambiguity)
    return AreaAliasValidationResult(
        missing_valid_aliases=missing_valid_aliases,
        unresolved_aliases=unresolved_aliases,
        active_alias_conflicts=active_alias_conflicts,
        unknown_canonical_areas=unknown_canonical_areas,
        ambiguous_direct_mappings=ambiguous_direct_mappings,
    )


DEFAULT_AREA_TITLE_ALIASES = active_area_title_alias_map()

AREA_ALIASES = ("区域", "片区", "区域名", "区域名称", "目标区域", "同步区域")
COMMUNITY_ALIASES = ("小区", "小区名称", "社区", "楼盘", "楼盘名称", "项目", "小区名")
ROOM_ALIASES = ("房号", "房间号", "房源编号", "编号", "门牌", "房源")
LAYOUT_ALIASES = ("户型", "户型描述", "房型", "格局")
LAYOUT_CLASS_ALIASES = ("户型分类", "房型分类", "分类")
RENT_ONE_ALIASES = ("押一付一", "押一付一月租金", "押一付", "月租", "租金", "价格")
RENT_TWO_ALIASES = ("押二付一", "押二付一 月租金", "押二付一月租金", "押二付")
PASSWORD_ALIASES = ("看房方式密码", "看房方式", "看房密码", "密码", "门锁密码")
REMARK_ALIASES = ("备注", "说明", "房源说明", "描述")
NOTE_ALIASES = ("房源笔记", "笔记", "素材", "图片视频", "视频图片笔记")
STATUS_ALIASES = ("状态", "房态", "出租状态", "是否在租")
NOT_RENTING_WORDS = ("已租", "下架", "停租", "删除", "不可租", "失效", "不在租")
ROOM_REFERENCE_RE = re.compile(r"([一-鿿A-Za-z]+)\s*(\d+(?:[-－—]\d+)+(?:[-－—]?[A-Za-z0-9]+)?)")
WHOLE_RENT_RE = re.compile(r"[\(（]\s*整\s*[\)）]|整租|整套|整间")
LEADING_WHOLE_RENT_RE = re.compile(r"^\s*[\(（]\s*整\s*[\)）]\s*")
NORMAL_ROOM_BACK_COLOR = "#E7E6E6"
DATA_FONT_COLOR = "#000000"
AREA_LABEL_BACK_COLOR = "#FFF2CC"
AREA_LABEL_FONT_COLOR = "#FF0000"
SECTION_TITLE_BACK_COLOR = "#FFD966"
BORDER_COLOR = "#808080"
DEFAULT_FONT = {"bold": False, "italic": False, "fontSize": "16pt/1.5", "clean": False}
RICH_TEXT_FONT_SIZE = 16
DATA_ROW_HEIGHT_PX = 72
SECTION_TITLE_ROW_HEIGHT_PX = 48
DRIVE_UPLOAD_SAFE_VIDEO_BYTES = 19 * 1024 * 1024
DATA_CELL_STYLE = {
    "font": DEFAULT_FONT,
    "textDecoration": 0,
    "formatter": "",
    "hAlign": 1,
    "vAlign": 1,
    "foreColor": DATA_FONT_COLOR,
    "backColor": NORMAL_ROOM_BACK_COLOR,
    "borderType": "FULL_BORDER",
    "borderColor": BORDER_COLOR,
    "clean": False,
}
AREA_LABEL_STYLE = {
    "font": {**DEFAULT_FONT, "bold": True},
    "textDecoration": 0,
    "formatter": "",
    "hAlign": 1,
    "vAlign": 1,
    "foreColor": AREA_LABEL_FONT_COLOR,
    "backColor": AREA_LABEL_BACK_COLOR,
    "borderType": "FULL_BORDER",
    "borderColor": BORDER_COLOR,
    "clean": False,
}
SECTION_TITLE_STYLE = {
    "font": {**DEFAULT_FONT, "bold": True},
    "textDecoration": 0,
    "formatter": "",
    "hAlign": 1,
    "vAlign": 1,
    "foreColor": AREA_LABEL_FONT_COLOR,
    "backColor": SECTION_TITLE_BACK_COLOR,
    "borderType": "FULL_BORDER",
    "borderColor": BORDER_COLOR,
    "clean": False,
}
