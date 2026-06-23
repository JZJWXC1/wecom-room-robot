from __future__ import annotations

import re


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
DEFAULT_AREA_TITLE_ALIASES = {
    "万达": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "拱墅万达": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "北部软件园": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "城北万象城": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "拱墅万达 北部软件园 城北万象城": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "万达区域": "拱墅万达 北部软件园 城北万象城 成交全部全佣🧧",
    "华丰": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "石桥": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "石桥街道": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "永佳": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "半山": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "杨家": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "华丰 石桥 永佳 杨家": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "石桥街道 华丰 石桥 永佳 半山": "石桥街道 华丰 石桥 永佳 半山 成交全部全佣🧧",
    "东新": "东新园 杭氧 新天地 成交全部全佣🧧",
    "东新园": "东新园 杭氧 新天地 成交全部全佣🧧",
    "杭氧": "东新园 杭氧 新天地 成交全部全佣🧧",
    "新天地": "东新园 杭氧 新天地 成交全部全佣🧧",
    "东新 杭氧 新天地": "东新园 杭氧 新天地 成交全部全佣🧧",
    "东新园 杭氧 新天地": "东新园 杭氧 新天地 成交全部全佣🧧",
    "东站": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
    "闸弄口": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
    "新塘": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
    "元宝塘": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
    "东站 闸弄口 新塘 元宝塘": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
    "闸弄口 新塘 元宝塘 东站": "闸弄口 新塘 元宝塘 东站除特价成交全部全佣🧧",
}

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
