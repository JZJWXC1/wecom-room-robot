import logging
import asyncio
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models import IncomingMessage
from app.services.config_check import get_config_status
from app.services.inventory import InventoryService
from app.services.inventory_image_sync import InventoryImageSyncer
from app.services.llm import ReplyGenerator
from app.services.media_store import GENERIC_MEDIA_WORDS, MediaStore
from app.services.video_transcoder import needs_wecom_video_transcode, prepare_wecom_video
from app.services.feishu import FeishuClient
from app.services.wecom import WeComClient
from app.services.wecom_kf import (
    WeComKfClient,
    WeComKfContextStore,
    WeComKfSendLimitError,
    extract_kf_text,
    is_kf_message_event,
    should_auto_reply_kf_message,
)

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("room-robot")

app = FastAPI(title="企业微信房源自动回复机器人")
inventory = InventoryService()
inventory_image_syncer = InventoryImageSyncer()
media_store = MediaStore()
reply_generator = ReplyGenerator()
wecom = WeComClient()
wecom_kf = WeComKfClient()
wecom_kf_context_store = WeComKfContextStore()
wecom_kf_idle_sequences: dict[str, int] = {}
wecom_kf_conversation_memory: dict[str, dict[str, Any]] = {}
wecom_kf_sync_lock = asyncio.Lock()

VIDEO_KEYWORDS = ("\u89c6\u9891", "\u5b9e\u62cd", "\u770b\u623f\u89c6\u9891", "\u5185\u90e8\u89c6\u9891", "\u623f\u95f4\u89c6\u9891", "\u7b14\u8bb0")
INVENTORY_IMAGE_KEYWORDS = ("\u623f\u6e90\u8868", "\u8868\u683c", "\u622a\u56fe", "\u56fe\u7247", "\u7167\u7247")
INVENTORY_TABLE_SHORT_EXCLUSIONS = (
    "发表",
    "表达",
    "表情",
    "表面",
    "手表",
    "钟表",
    "电表",
    "水表",
    "表演",
    "表哥",
    "表姐",
    "表弟",
    "表妹",
)
INVENTORY_TABLE_SHORT_PATTERNS = (
    r"表(?:发|给|看|来|传|截|拍)(?:我|一下|下|个|一份|张|份|吗|吧|哈|呗)?",
    r"发(?:我|一下|下|个|一份|张|份|最新)(?:房源)?表",
    r"(?:给|传|来|看)(?:我|一下|下|个|一份|张|份|最新)?(?:房源)?表",
    r"(?:房源|租房|空房|在租|最新)表(?:格)?",
)
ROOM_IMAGE_KEYWORDS = ("\u5b9e\u62cd\u56fe", "\u623f\u95f4\u56fe", "\u56fe\u7247", "\u7167\u7247")
DISSATISFACTION_KEYWORDS = (
    "\u4e0d\u6ee1\u610f",
    "\u4e0d\u5bf9",
    "\u4e0d\u662f",
    "\u4e0d\u884c",
    "\u6ca1\u6709\u76f4\u63a5",
    "\u6ca1\u6709\u53d1",
    "\u6ca1\u53d1",
    "\u6ca1\u7ed9",
    "\u660e\u660e\u6709",
    "\u600e\u4e48\u8fd8\u95ee",
    "\u8fd8\u95ee",
    "\u54ea\u4e2a\u5c0f\u533a",
    "\u6587\u4ef6\u5939",
    "\u623f\u95f4\u53f7",
    "\u4e0d\u5c31\u662f",
)
SATISFACTION_KEYWORDS = ("\u6ee1\u610f", "\u8fd8\u53ef\u4ee5", "\u53ef\u4ee5\u4e86", "\u53ef\u4ee5\u7684", "\u884c\u4e86", "\u597d\u7684", "\u597d")
KF_VIDEO_SEND_LIMIT = 3
KF_INVENTORY_IMAGE_SEND_LIMIT = 5
KF_CONTEXT_TTL_SECONDS = 30 * 60
CONTRACT_CONTACT_NUMBERS = ("18758141785", "13282125992", "19941091943")
CONTRACT_CONTACT_KEYWORDS = (
    "签合同",
    "合同",
    "订房",
    "定房",
    "怎么订",
    "交定金",
    "定金",
    "订金",
    "办手续",
)
GREETING_ONLY_TEXTS = (
    "你好",
    "您好",
    "你好呀",
    "您好呀",
    "哈喽",
    "hello",
    "hi",
    "在吗",
    "有人吗",
)
DEPOSIT_WAIVER_KEYWORDS = (
    "免押",
    "免押金",
    "无忧住",
    "芝麻信用",
    "芝麻分",
    "信用免押",
    "免押服务费",
)
MEDIA_FOLLOWUP_FILLER_WORDS = (
    *GENERIC_MEDIA_WORDS,
    "这个",
    "这两个",
    "这几个",
    "这个房子",
    "这个房间",
    "这个房源",
    "这套",
    "这两套",
    "这几套",
    "那套",
    "那两套",
    "那几套",
    "它",
    "发",
    "给我",
    "一下",
    "一下吧",
    "下吧",
    "吧",
    "两套",
    "几套",
)
MEDIA_SEND_FOLLOWUP_WORDS = (
    "发我",
    "发我吧",
    "发给我",
    "发一下",
    "发来",
    "直接发",
    "马上发",
    "给我发",
    "要",
)
MEDIA_SEARCH_ALIASES = (
    ("小样坝家园", "小洋坝家园"),
    ("小样坝", "小洋坝"),
    ("小样吧家园", "小洋坝家园"),
    ("小样吧", "小洋坝"),
)
ROOM_DETAIL_MARKERS = (
    "小区",
    "房号",
    "户型",
    "价格",
    "押一",
    "押二",
    "密码",
    "随时可看",
)

settings.media_root.mkdir(parents=True, exist_ok=True)
settings.room_database_path.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory="media"), name="media")
app.mount("/room-database", StaticFiles(directory=settings.room_database_path), name="room_database")


def _wants_video(text: str) -> bool:
    return any(keyword in text for keyword in VIDEO_KEYWORDS)


def _wants_context_video(open_kfid: str, external_userid: str, content: str) -> bool:
    if not _is_generic_media_followup(content):
        return False
    if not any(word in content for word in MEDIA_SEND_FOLLOWUP_WORDS):
        return False
    context = _recent_kf_media_context(open_kfid, external_userid) or {}
    recent_messages = list(context.get("recent_messages") or [])[-10:]
    if context.get("video_paths") or context.get("video_urls"):
        return True
    for item in reversed(recent_messages):
        role = str(item.get("role") or "").strip()
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        if role == "客户" and text == content:
            continue
        if _wants_video(text):
            return True
        if role == "客服" and _looks_like_room_detail(text) and "视频" in text:
            return True
    return False


def _wants_inventory_image(text: str) -> bool:
    if any(keyword in text for keyword in INVENTORY_IMAGE_KEYWORDS):
        return True
    normalized = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()【】\[\]]+", "", text.strip())
    if not normalized or any(word in normalized for word in INVENTORY_TABLE_SHORT_EXCLUSIONS):
        return False
    return any(re.search(pattern, normalized) for pattern in INVENTORY_TABLE_SHORT_PATTERNS)


def _wants_room_image(text: str) -> bool:
    return any(keyword in text for keyword in ROOM_IMAGE_KEYWORDS)


def _wants_contract_contact(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if any(keyword in stripped for keyword in CONTRACT_CONTACT_KEYWORDS):
        return True
    return "联系谁" in stripped and any(
        keyword in stripped for keyword in ("签", "合同", "订", "定金", "订金")
    )


def _contract_contact_reply() -> str:
    return (
        "签合同、订房、交定金请直接联系房源表上的这三个号码：\n"
        + "\n".join(CONTRACT_CONTACT_NUMBERS)
        + "\n\n你把想订的小区和房号发给他们就行。"
    )


def _is_greeting_only(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()【】\[\]～~]+", "", text.strip()).lower()
    return normalized in {item.lower() for item in GREETING_ONLY_TEXTS}


def _greeting_reply() -> str:
    return "你好呀，我在的。你可以直接发小区、房号、预算，或者问我价格、空房、视频、房源表，我帮你查。"


def _wants_deposit_waiver(text: str) -> bool:
    return any(keyword in text for keyword in DEPOSIT_WAIVER_KEYWORDS)


def _deposit_waiver_reply(text: str) -> str:
    if any(keyword in text for keyword in ("能做", "能不能", "可以做", "可不可以", "能用", "可以用")):
        return (
            "可以先看自己有没有免押资格，主要条件是：\n"
            "1. 芝麻分大于等于 550 分。\n"
            "2. 合同周期 3-12 个月。\n"
            "3. 必须签电子合同。\n"
            "4. 合同起始时间要在当天及之后。\n"
            "5. 芝麻信用不能有到期未守约记录，比如充电宝没还。\n"
            "6. 建行惠市宝收款卡对应的房源不能用免押。\n"
            "7. 目前仅新签合同支持免押。\n\n"
            "自查方式：打开支付宝 - 我的 - 芝麻信用 - 我的 - 信用额度 - "
            "租房板块申请额度。有额度的话，基本就可以走免押租房。"
        )
    if "服务费" in text or "费用" in text or "多少钱" in text:
        return (
            "免押服务费是支付宝收取的：\n"
            "合同 3 个月：免押金额 5.5%\n"
            "合同 3-6 个月：免押金额 7%\n"
            "合同 6-12 个月：免押金额 8%"
        )
    return (
        "免押是支付宝芝麻信用的“无忧住”服务，不是免费服务。\n"
        "符合支付宝芝麻信用风控标准的租客，可以不直接支付押金，但需要支付押金金额 "
        "5.5%-8% 的免押服务费，这个费用由支付宝收取。\n\n"
        "基本条件：\n"
        "1. 芝麻分大于等于 550 分。\n"
        "2. 合同周期 3-12 个月。\n"
        "3. 必须签电子合同。\n"
        "4. 合同起始时间要在当天及之后。\n"
        "5. 芝麻信用有到期未守约记录的不能用，比如充电宝没还。\n"
        "6. 建行惠市宝收款卡对应的房源不能用免押。\n"
        "7. 目前仅新签合同支持免押；已买过的租客要先确认原免押合同是否作废。\n\n"
        "免押服务费：\n"
        "合同 3 个月：免押金额 5.5%\n"
        "合同 3-6 个月：免押金额 7%\n"
        "合同 6-12 个月：免押金额 8%\n\n"
        "你可以自己先查资格：打开支付宝 - 我的 - 芝麻信用 - 我的 - "
        "信用额度 - 租房板块申请额度。有额度的话，基本就可以走免押租房。"
    )


def _is_availability_question(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    question_markers = ("?", "？", "吗", "嘛", "呢")
    availability_markers = (
        "有没有",
        "还有没有",
        "还有吗",
        "有房",
        "有房子",
        "有房间",
        "有带",
        "带阳台",
        "能短租",
        "可以短租",
    )
    return any(item in stripped for item in availability_markers) and (
        any(item in stripped for item in question_markers)
        or "有没有" in stripped
        or "还有没有" in stripped
    )


def _is_dissatisfied_or_correction(text: str) -> bool:
    if _is_availability_question(text) and "不满意" not in text:
        return False
    return any(keyword in text for keyword in DISSATISFACTION_KEYWORDS)


def _is_satisfied_feedback(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _is_greeting_only(stripped):
        return False
    if any(mark in stripped for mark in ("?", "？")):
        return False
    if _is_dissatisfied_or_correction(stripped):
        return False
    return any(keyword in stripped for keyword in SATISFACTION_KEYWORDS)


def _polish_kf_reply_text(customer_text: str, reply_text: str) -> str:
    polished = reply_text.strip()
    replacements = (
        ("详细笔记", "房间详细信息"),
        ("笔记图", "房间图片"),
        ("笔记", "房间详细信息"),
    )
    for source, target in replacements:
        polished = polished.replace(source, target)
    if _needs_viewing_contact_guidance(customer_text, polished):
        polished = polished.rstrip()
        if polished.endswith(("。", "！", "？", "?", "!")):
            polished = polished + "\n\n" + _viewing_contact_guidance()
        else:
            polished = polished + "。\n\n" + _viewing_contact_guidance()
    return polished


def _needs_viewing_contact_guidance(customer_text: str, reply_text: str) -> bool:
    if any(number in reply_text for number in CONTRACT_CONTACT_NUMBERS):
        return False
    asks_viewing = any(
        keyword in customer_text
        for keyword in ("看房", "能看", "现在看", "预约", "联系一下", "帮我联系", "安排看")
    )
    not_vacant = any(
        keyword in reply_text
        for keyword in ("暂时看不了", "还没空出", "没空出", "未空出", "才空出", "等空出", "空出来")
    )
    return asks_viewing and not_vacant


def _viewing_contact_guidance() -> str:
    return "这类还没空出的房子，看房需要提前预约。你可以直接联系：\n" + "\n".join(
        CONTRACT_CONTACT_NUMBERS
    )


def _room_database_public_urls(paths: list[Path]) -> list[str]:
    base = settings.public_base_url.rstrip("/")
    root = settings.room_database_path.resolve()
    urls: list[str] = []
    for path in paths:
        try:
            relative_path = path.resolve().relative_to(root)
        except ValueError:
            continue
        encoded_path = quote(relative_path.as_posix(), safe="/")
        urls.append(f"{base}/room-database/{encoded_path}")
    return urls


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _inventory_image_paths() -> list[Path]:
    paths = sorted(settings.room_database_path.parent.glob(settings.inventory_image_glob))
    if not paths and settings.inventory_image_path.exists():
        paths = [settings.inventory_image_path]
    return [path for path in paths if path.exists()][:KF_INVENTORY_IMAGE_SEND_LIMIT]


async def _refresh_inventory_images_if_needed() -> None:
    try:
        await inventory_image_syncer.refresh_if_changed()
    except Exception:
        logger.exception("Feishu inventory sheet image refresh failed")


async def _send_kf_inventory_images(
    open_kfid: str,
    external_userid: str,
    image_paths: list[Path],
) -> bool:
    failed_paths: list[Path] = []
    for image_path in image_paths:
        try:
            await wecom_kf.send_image(open_kfid, external_userid, image_path)
        except WeComKfSendLimitError:
            logger.warning("微信客服发送次数已达上限，停止继续发送房源表图片")
            return False
        except Exception:
            failed_paths.append(image_path)
            logger.exception("微信客服房源表图片发送失败: %s", image_path)

    if failed_paths:
        image_urls = _room_database_public_urls(failed_paths)
        if image_urls:
            try:
                await wecom_kf.send_text(
                    open_kfid,
                    external_userid,
                    "图片直发失败，我先把可打开的房源表链接发你：",
                )
                for image_url in image_urls:
                    await wecom_kf.send_text(open_kfid, external_userid, image_url)
            except WeComKfSendLimitError:
                logger.warning("微信客服发送次数已达上限，停止发送房源表兜底链接")
                return False
    return True


async def _send_kf_room_database_images(
    open_kfid: str,
    external_userid: str,
    image_paths: list[Path],
) -> bool:
    return await _send_kf_inventory_images(open_kfid, external_userid, image_paths)


async def _send_kf_room_database_videos(
    open_kfid: str,
    external_userid: str,
    video_paths: list[Path],
) -> bool:
    failed_paths: list[Path] = []
    for video_path in video_paths:
        try:
            await wecom_kf.send_text(
                open_kfid,
                external_userid,
                f"这是{_room_video_label(video_path)}的视频。",
            )
            send_path = (
                prepare_wecom_video(video_path)
                if needs_wecom_video_transcode(video_path)
                else video_path
            )
            try:
                await wecom_kf.send_video(open_kfid, external_userid, send_path)
            except WeComKfSendLimitError:
                raise
            except Exception:
                if send_path != video_path:
                    raise
                retry_path = prepare_wecom_video(video_path, force=True)
                await wecom_kf.send_video(open_kfid, external_userid, retry_path)
        except WeComKfSendLimitError:
            logger.warning("微信客服发送次数已达上限，停止继续发送房源视频")
            return False
        except Exception:
            failed_paths.append(video_path)
            logger.exception("微信客服房源视频发送失败: %s", video_path)

    if failed_paths:
        video_urls = _room_database_public_urls(failed_paths)
        if video_urls:
            try:
                await wecom_kf.send_text(
                    open_kfid,
                    external_userid,
                    "视频直发失败，我先把可打开的视频链接发你：\n" + "\n".join(video_urls),
                )
            except WeComKfSendLimitError:
                logger.warning("微信客服发送次数已达上限，停止发送视频兜底链接")
                return False
    return True


def _room_video_label(video_path: Path) -> str:
    label = video_path.parent.name.strip() or video_path.stem.strip()
    return re.sub(r"\s+", " ", label)


def _kf_conversation_key(open_kfid: str, external_userid: str) -> str:
    return f"{open_kfid}:{external_userid}"


def _normalize_kf_media_context(context: dict[str, Any]) -> dict[str, Any]:
    recent_messages: list[dict[str, Any]] = []
    for item in context.get("recent_messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            recent_messages.append(
                {
                    "role": role,
                    "content": content,
                    "created_at": float(item.get("created_at") or time.time()),
                }
            )
    return {
        "image_paths": [Path(item) for item in context.get("image_paths") or []],
        "video_paths": [Path(item) for item in context.get("video_paths") or []],
        "video_urls": list(context.get("video_urls") or []),
        "recent_messages": recent_messages[-10:],
        "updated_at": float(context.get("updated_at") or time.time()),
    }


def _remember_kf_media_context(
    open_kfid: str,
    external_userid: str,
    *,
    image_paths: list[Path] | None = None,
    video_paths: list[Path] | None = None,
    video_urls: list[str] | None = None,
) -> None:
    key = _kf_conversation_key(open_kfid, external_userid)
    context = _recent_kf_media_context(open_kfid, external_userid) or {}
    context = {
        **context,
        "image_paths": image_paths if image_paths is not None else context.get("image_paths", []),
        "video_paths": video_paths if video_paths is not None else context.get("video_paths", []),
        "video_urls": video_urls if video_urls is not None else context.get("video_urls", []),
        "recent_messages": list(context.get("recent_messages") or [])[-10:],
        "updated_at": time.time(),
    }
    wecom_kf_conversation_memory[key] = context
    try:
        wecom_kf_context_store.save(key, context)
    except Exception:
        logger.exception("WeCom KF context save failed")


def _recent_kf_media_context(
    open_kfid: str,
    external_userid: str,
) -> dict[str, Any] | None:
    key = _kf_conversation_key(open_kfid, external_userid)
    context = wecom_kf_conversation_memory.get(key)
    if not context:
        try:
            context = wecom_kf_context_store.get(key)
        except Exception:
            logger.exception("WeCom KF context load failed")
            return None
        if not context:
            return None
        context = _normalize_kf_media_context(context)
        wecom_kf_conversation_memory[key] = context
    if time.time() - float(context.get("updated_at", 0)) > KF_CONTEXT_TTL_SECONDS:
        wecom_kf_conversation_memory.pop(key, None)
        try:
            wecom_kf_context_store.delete(key)
        except Exception:
            logger.exception("WeCom KF expired context cleanup failed")
        return None
    return context


def _save_kf_context(open_kfid: str, external_userid: str, context: dict[str, Any]) -> None:
    key = _kf_conversation_key(open_kfid, external_userid)
    context["updated_at"] = time.time()
    wecom_kf_conversation_memory[key] = context
    try:
        wecom_kf_context_store.save(key, context)
    except Exception:
        logger.exception("WeCom KF context save failed")


def _append_kf_dialog_message(
    open_kfid: str,
    external_userid: str,
    role: str,
    content: str,
) -> None:
    content = content.strip()
    if not content:
        return
    context = _recent_kf_media_context(open_kfid, external_userid) or {
        "image_paths": [],
        "video_paths": [],
        "video_urls": [],
        "recent_messages": [],
    }
    recent_messages = list(context.get("recent_messages") or [])
    recent_messages.append(
        {
            "role": role,
            "content": content[:1000],
            "created_at": time.time(),
        }
    )
    context["recent_messages"] = recent_messages[-10:]
    _save_kf_context(open_kfid, external_userid, context)


def _format_kf_dialog_context(open_kfid: str, external_userid: str) -> str:
    context = _recent_kf_media_context(open_kfid, external_userid) or {}
    lines = []
    for item in list(context.get("recent_messages") or [])[-10:]:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _kf_media_search_text(open_kfid: str, external_userid: str, content: str) -> str:
    search_texts = _kf_media_search_texts(open_kfid, external_userid, content)
    if search_texts:
        return search_texts[0]
    return _normalize_media_search_aliases(content.strip())


def _kf_media_search_texts(open_kfid: str, external_userid: str, content: str) -> list[str]:
    context = _recent_kf_media_context(open_kfid, external_userid) or {}
    if not _is_generic_media_followup(content):
        return [_normalize_media_search_aliases(content.strip())]

    recent_messages = list(context.get("recent_messages") or [])[-10:]
    contexts = _latest_room_detail_contexts(
        recent_messages,
        content,
        limit=_media_followup_requested_count(content),
    )
    if contexts:
        return [
            _normalize_media_search_aliases("\n".join([selected_context, content]).strip())
            for selected_context in contexts
        ]
    return [_normalize_media_search_aliases(content.strip())]


def _normalize_media_search_aliases(text: str) -> str:
    normalized = text
    for source, target in MEDIA_SEARCH_ALIASES:
        normalized = normalized.replace(source, target)
    return normalized


def _is_generic_media_followup(content: str) -> bool:
    cleaned = content.strip()
    for word in MEDIA_FOLLOWUP_FILLER_WORDS:
        cleaned = cleaned.replace(word, " ")
    return not re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", cleaned)


def _media_followup_requested_count(content: str) -> int:
    if any(word in content for word in ("这两套", "那两套", "两个", "两套", "两条")):
        return 2
    if any(word in content for word in ("这几套", "那几套", "几个", "几套", "几条")):
        return KF_VIDEO_SEND_LIMIT
    return 1


def _latest_room_detail_context(
    recent_messages: list[dict[str, Any]],
    current_content: str,
) -> str:
    contexts = _latest_room_detail_contexts(recent_messages, current_content, limit=1)
    return contexts[0] if contexts else ""


def _latest_room_detail_contexts(
    recent_messages: list[dict[str, Any]],
    current_content: str,
    *,
    limit: int,
) -> list[str]:
    skipped_current = False
    contexts: list[str] = []
    fallback_customer_texts: list[str] = []
    for item in reversed(recent_messages):
        role = str(item.get("role") or "").strip()
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        if role == "客户" and text == current_content and not skipped_current:
            skipped_current = True
            continue
        if role == "客服" and _looks_like_room_detail(text):
            contexts.append(text)
            if len(contexts) >= limit:
                break
        if role == "客户" and not _is_generic_media_followup(text) and not _is_satisfied_feedback(text):
            fallback_customer_texts.append(text)
    if contexts:
        return list(reversed(contexts))
    return list(reversed(fallback_customer_texts[:limit]))


def _collect_kf_room_database_video_paths(
    open_kfid: str,
    external_userid: str,
    content: str,
) -> list[Path]:
    search_texts = _kf_media_search_texts(open_kfid, external_userid, content)
    requested_count = _media_followup_requested_count(content)
    if len(search_texts) > 1:
        limit = min(KF_VIDEO_SEND_LIMIT, max(requested_count, len(search_texts)))
        paths: list[Path] = []
        seen: set[str] = set()
        for search_text in search_texts:
            for path in media_store.list_room_database_videos(search_text, limit=1):
                key = str(path)
                if key not in seen:
                    paths.append(path)
                    seen.add(key)
                if len(paths) >= limit:
                    return paths
        return paths

    search_text = search_texts[0] if search_texts else content
    video_limit = 1 if _is_specific_room_video_query(search_text) else KF_VIDEO_SEND_LIMIT
    if requested_count > 1:
        video_limit = min(KF_VIDEO_SEND_LIMIT, requested_count)
    return media_store.list_room_database_videos(search_text, limit=video_limit)


def _looks_like_room_detail(text: str) -> bool:
    if any(marker in text for marker in ROOM_DETAIL_MARKERS):
        return True
    return bool(re.search(r"\d+[-栋幢区]\d+", text))


def _is_specific_room_video_query(text: str) -> bool:
    if not _wants_video(text):
        return False
    if any(marker in text for marker in ("房号", "小区：", "户型：")):
        return True
    return bool(re.search(r"\d+[-栋幢区]\d+(?:[-栋幢区]\d+)*(?:[A-Za-z])?", text))


def _next_kf_idle_sequence(open_kfid: str, external_userid: str) -> int:
    key = _kf_conversation_key(open_kfid, external_userid)
    sequence = wecom_kf_idle_sequences.get(key, 0) + 1
    wecom_kf_idle_sequences[key] = sequence
    return sequence


def _schedule_kf_satisfaction_prompt(
    open_kfid: str,
    external_userid: str,
    sequence: int,
) -> None:
    return


async def _send_kf_satisfaction_prompt_after_idle(
    open_kfid: str,
    external_userid: str,
    sequence: int,
) -> None:
    return


async def _send_kf_video_links(
    open_kfid: str,
    external_userid: str,
    video_urls: list[str],
) -> None:
    urls = _dedupe(video_urls)
    if not urls:
        return
    await wecom_kf.send_text(
        open_kfid,
        external_userid,
        "\n".join(urls),
    )


@app.on_event("startup")
async def startup() -> None:
    if settings.feishu_inventory_sheet_sync_on_startup:
        try:
            await inventory_image_syncer.refresh_if_changed(force=True)
        except Exception:
            logger.exception("Feishu inventory sheet image sync failed on startup")
    if settings.feishu_sync_media_on_startup:
        try:
            await FeishuClient().sync_all_media()
        except Exception:
            logger.exception("Feishu media sync failed on startup")
    if settings.inventory_source != "local_image":
        await inventory.refresh()


@app.get("/health")
async def health() -> dict:
    config_status = get_config_status()
    return {
        "ok": config_status["ok"],
        "config": config_status,
        "inventory_last_error": inventory.last_error,
    }


@app.get("/admin/config/check")
async def check_config() -> dict:
    return get_config_status()


@app.post("/admin/inventory/refresh")
async def refresh_inventory() -> dict:
    frame = await inventory.refresh()
    return {"rows": len(frame), "last_error": inventory.last_error}


@app.post("/admin/feishu/sync-media")
async def sync_feishu_media() -> dict:
    return await FeishuClient().sync_all_media()


@app.post("/admin/feishu/sync-inventory-image")
async def sync_feishu_inventory_image(force: bool = False) -> dict:
    try:
        result = await inventory_image_syncer.refresh_if_changed(force=force)
    except Exception as exc:
        logger.exception("Feishu inventory sheet image sync failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


@app.get("/wecom/callback", response_class=PlainTextResponse)
async def verify_wecom_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> str:
    try:
        return wecom.verify_url(msg_signature, timestamp, nonce, echostr)
    except Exception as exc:
        logger.exception("企业微信 URL 校验失败")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/wecom/callback", response_class=PlainTextResponse)
async def receive_wecom_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> str:
    body = (await request.body()).decode("utf-8")
    try:
        message = wecom.parse_callback(body, msg_signature, timestamp, nonce)
    except Exception as exc:
        logger.exception("企业微信消息解析失败")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background_tasks.add_task(handle_message, message)
    return "success"


@app.get("/wecom/kf/callback", response_class=PlainTextResponse)
async def verify_wecom_kf_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> str:
    try:
        return wecom_kf.verify_url(msg_signature, timestamp, nonce, echostr)
    except Exception as exc:
        logger.exception("微信客服 URL 校验失败")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/wecom/kf/callback", response_class=PlainTextResponse)
async def receive_wecom_kf_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> str:
    body = (await request.body()).decode("utf-8")
    try:
        event = wecom_kf.parse_callback_event(body, msg_signature, timestamp, nonce)
    except Exception as exc:
        logger.exception("微信客服回调解析失败")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_kf_message_event(event):
        background_tasks.add_task(handle_kf_event, event)
    return "success"


async def handle_message(message: IncomingMessage) -> None:
    try:
        rooms = await inventory.search(message.content)
        snapshot = await inventory.snapshot()
        media = media_store.list_for_rooms(rooms)
        images, videos = media_store.public_urls(media)
        reply = await reply_generator.generate(message, snapshot, images, videos)
        await wecom.send_text(message.user_id, reply.text)
        # 企业微信图片/视频需要先上传临时素材。这里先用文本发链接，后续可按素材上传策略改成原生图片视频。
        for image in reply.images:
            await wecom.send_text(message.user_id, image)
        for video in reply.videos:
            await wecom.send_text(message.user_id, video)
    except Exception:
        logger.exception("自动回复处理失败")


async def handle_kf_event(event: dict[str, str]) -> None:
    open_kfid = event.get("OpenKfId", "")
    token = event.get("Token", "")
    async with wecom_kf_sync_lock:
        await _handle_kf_event_locked(open_kfid, token)


async def _handle_kf_event_locked(open_kfid: str, token: str) -> None:
    try:
        messages = await wecom_kf.sync_messages(open_kfid, token)
        all_handled = True
        for message in messages:
            msgid = str(message.get("msgid", ""))
            try:
                if should_auto_reply_kf_message(message):
                    await handle_kf_message(message)
                wecom_kf.state_store.mark_processed(msgid)
            except Exception:
                all_handled = False
                logger.exception("微信客服消息处理失败: %s", msgid)
        if all_handled and wecom_kf.last_next_cursor:
            wecom_kf.state_store.save_cursor(wecom_kf.last_next_cursor)
    except Exception:
        logger.exception("微信客服消息拉取失败")


async def handle_kf_message(kf_message: dict) -> None:
    content = extract_kf_text(kf_message)
    open_kfid = str(kf_message.get("open_kfid", ""))
    external_userid = str(kf_message.get("external_userid", ""))
    idle_sequence = _next_kf_idle_sequence(open_kfid, external_userid)
    _append_kf_dialog_message(open_kfid, external_userid, "客户", content)
    wants_video = _wants_video(content) or _wants_context_video(
        open_kfid,
        external_userid,
        content,
    )
    if _is_greeting_only(content):
        reply_text = _greeting_reply()
        await wecom_kf.send_text(open_kfid, external_userid, reply_text)
        _append_kf_dialog_message(open_kfid, external_userid, "客服", reply_text)
        return

    if _is_satisfied_feedback(content):
        reply_text = "好的，有需要随时发我。"
        await wecom_kf.send_text(open_kfid, external_userid, reply_text)
        _append_kf_dialog_message(open_kfid, external_userid, "客服", reply_text)
        return

    if _wants_contract_contact(content):
        reply_text = _contract_contact_reply()
        await wecom_kf.send_text(open_kfid, external_userid, reply_text)
        _append_kf_dialog_message(open_kfid, external_userid, "客服", reply_text)
        return

    if _wants_deposit_waiver(content):
        reply_text = _deposit_waiver_reply(content)
        await wecom_kf.send_text(open_kfid, external_userid, reply_text)
        _append_kf_dialog_message(open_kfid, external_userid, "客服", reply_text)
        return

    if _is_dissatisfied_or_correction(content):
        context = _recent_kf_media_context(open_kfid, external_userid)
        if context:
            image_paths = list(context.get("image_paths") or [])
            video_paths = list(context.get("video_paths") or [])
            video_urls = list(context.get("video_urls") or [])
            if image_paths:
                sent = await _send_kf_inventory_images(
                    open_kfid,
                    external_userid,
                    image_paths,
                )
                if sent:
                    _append_kf_dialog_message(open_kfid, external_userid, "客服", "已直接发送相关图片。")
                return
            if video_paths:
                sent = await _send_kf_room_database_videos(
                    open_kfid,
                    external_userid,
                    video_paths,
                )
                if sent:
                    _append_kf_dialog_message(open_kfid, external_userid, "客服", "已直接发送相关视频。")
                return
            if video_urls:
                await _send_kf_video_links(open_kfid, external_userid, video_urls)
                _append_kf_dialog_message(open_kfid, external_userid, "客服", "\n".join(video_urls))
                return

    if _wants_room_image(content) and not wants_video:
        search_text = _kf_media_search_text(open_kfid, external_userid, content)
        room_database_image_paths = media_store.list_room_database_images(
            search_text,
            limit=KF_INVENTORY_IMAGE_SEND_LIMIT,
        )
        if room_database_image_paths:
            _remember_kf_media_context(
                open_kfid,
                external_userid,
                image_paths=room_database_image_paths,
            )
            sent = await _send_kf_room_database_images(
                open_kfid,
                external_userid,
                room_database_image_paths,
            )
            if sent:
                _append_kf_dialog_message(open_kfid, external_userid, "客服", "已直接发送相关图片。")
            return

    if _wants_inventory_image(content) and not wants_video:
        await _refresh_inventory_images_if_needed()
        image_paths = _inventory_image_paths()
        if image_paths:
            _remember_kf_media_context(
                open_kfid,
                external_userid,
                image_paths=image_paths,
            )
            sent = await _send_kf_inventory_images(open_kfid, external_userid, image_paths)
            if sent:
                _append_kf_dialog_message(open_kfid, external_userid, "客服", "已直接发送房源表图片。")
            return

    message = IncomingMessage(
        source="wecom_kf",
        user_id=str(kf_message.get("external_userid", "")),
        msg_type=str(kf_message.get("msgtype", "")),
        content=content,
        raw=kf_message,
    )
    rooms = await inventory.search(message.content)
    snapshot = inventory.format_rows(rooms) or await inventory.snapshot()
    media = media_store.list_for_rooms(rooms)
    images, videos = media_store.public_urls(media)
    room_database_video_urls: list[str] = []
    if wants_video:
        room_database_video_paths = _collect_kf_room_database_video_paths(
            open_kfid,
            external_userid,
            content,
        )
        room_database_video_urls = _room_database_public_urls(room_database_video_paths)
        if room_database_video_paths:
            _remember_kf_media_context(
                open_kfid,
                external_userid,
                video_paths=room_database_video_paths,
                video_urls=room_database_video_urls,
            )
            sent = await _send_kf_room_database_videos(
                open_kfid,
                external_userid,
                room_database_video_paths,
            )
            if sent:
                _append_kf_dialog_message(open_kfid, external_userid, "客服", "已直接发送相关视频。")
            return
    videos = _dedupe(videos + room_database_video_urls)
    if room_database_video_urls:
        _remember_kf_media_context(
            open_kfid,
            external_userid,
            video_urls=room_database_video_urls,
        )
    conversation_context = _format_kf_dialog_context(open_kfid, external_userid)
    reply = await reply_generator.generate(
        message,
        snapshot,
        images,
        videos,
        conversation_context=conversation_context,
    )
    reply.text = _polish_kf_reply_text(content, reply.text)
    await wecom_kf.send_text(open_kfid, external_userid, reply.text)
    _append_kf_dialog_message(open_kfid, external_userid, "客服", reply.text)
    for image in reply.images:
        await wecom_kf.send_text(open_kfid, external_userid, image)
        _append_kf_dialog_message(open_kfid, external_userid, "客服", image)
    await _send_kf_video_links(open_kfid, external_userid, reply.videos + room_database_video_urls)
    if reply.videos or room_database_video_urls:
        _append_kf_dialog_message(
            open_kfid,
            external_userid,
            "客服",
            "\n".join(reply.videos + room_database_video_urls),
        )


@app.post("/debug/message")
async def debug_message(payload: dict) -> dict:
    message = IncomingMessage(
        source="debug",
        user_id=payload.get("user_id", "debug-user"),
        msg_type=payload.get("msg_type", "text"),
        content=payload.get("content", ""),
        media_id=payload.get("media_id", ""),
        raw=payload,
    )
    rooms = await inventory.search(message.content)
    snapshot = await inventory.snapshot()
    media = media_store.list_for_rooms(rooms)
    images, videos = media_store.public_urls(media)
    if _wants_inventory_image(message.content) and not _wants_video(message.content):
        images = _dedupe(images + _room_database_public_urls(_inventory_image_paths()))
    if _wants_room_image(message.content) and not _wants_video(message.content):
        images = _dedupe(
            images
            + _room_database_public_urls(
                media_store.list_room_database_images(
                    message.content,
                    limit=KF_INVENTORY_IMAGE_SEND_LIMIT,
                )
            )
        )
    if _wants_video(message.content):
        videos = _dedupe(
            videos
            + _room_database_public_urls(
                media_store.list_room_database_videos(
                    message.content,
                    limit=KF_VIDEO_SEND_LIMIT,
                )
            )
        )
    reply = await reply_generator.generate(message, snapshot, images, videos)
    return {"reply": reply, "matched_rooms": rooms, "inventory_error": inventory.last_error}
