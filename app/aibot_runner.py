import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any

from wecom_aibot_sdk import DefaultLogger, WSClient, generate_req_id

from app.config import settings
from app.models import IncomingMessage
from app.services.inventory import InventoryService
from app.services.llm import ReplyGenerator
from app.services.media_store import MediaStore

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("wecom-aibot")

inventory = InventoryService()
media_store = MediaStore()
reply_generator = ReplyGenerator()
conversation_memory: dict[str, dict[str, Any]] = {}
CONTEXT_TTL_SECONDS = 30 * 60


def _text_from_frame(frame: dict[str, Any]) -> str:
    body = frame.get("body", {})
    if "text" in body:
        return body.get("text", {}).get("content", "")
    if "voice" in body:
        return body.get("voice", {}).get("text", "")
    if "mixed" in body:
        items = body.get("mixed", {}).get("items", [])
        texts = [
            item.get("text", {}).get("content", "")
            for item in items
            if item.get("msgtype") == "text"
        ]
        return "\n".join(text for text in texts if text)
    return ""


def _log_event(name: str, payload: Any = None) -> None:
    if payload is None:
        print(f"[wecom-aibot] {name}", flush=True)
        return
    try:
        compact = json.dumps(payload, ensure_ascii=False)[:1200]
    except TypeError:
        compact = str(payload)[:1200]
    print(f"[wecom-aibot] {name}: {compact}", flush=True)


async def _reply_text(ws_client: WSClient, frame: dict[str, Any], stream_id: str, content: str, finish: bool) -> None:
    _log_event("回复文本", content[:800])
    await ws_client.reply_stream(frame, stream_id, content, finish)


def _wants_inventory_image(text: str) -> bool:
    return any(keyword in text for keyword in ("房源表", "表格", "截图", "图片", "照片"))


def _wants_video(text: str) -> bool:
    return any(keyword in text for keyword in ("视频", "实拍", "看房视频", "内部视频", "房间视频"))


def _asks_price_or_detail(text: str) -> bool:
    return any(keyword in text for keyword in ("租金", "价格", "多少钱", "多少", "押一付", "押二付", "房租"))


def _asks_password(text: str) -> bool:
    return any(keyword in text for keyword in ("密码", "门锁", "开门"))


def _looks_like_followup(text: str) -> bool:
    return any(keyword in text for keyword in ("这两个", "这几套", "这套", "刚才", "你发", "上面", "前面", "它们", "这个"))


def _conversation_key(frame: dict[str, Any]) -> str:
    body = frame.get("body", {})
    sender = body.get("from", {}).get("userid", "")
    return str(body.get("chatid") or frame.get("chatid") or sender or "default")


def _remember_context(frame: dict[str, Any], rooms: list[dict[str, Any]], videos: list[Path] | None = None) -> None:
    if not rooms and not videos:
        return
    conversation_memory[_conversation_key(frame)] = {
        "rooms": rooms,
        "videos": [str(path) for path in (videos or [])],
        "updated_at": time.time(),
    }


def _recent_context(frame: dict[str, Any]) -> dict[str, Any] | None:
    context = conversation_memory.get(_conversation_key(frame))
    if not context:
        return None
    if time.time() - float(context.get("updated_at", 0)) > CONTEXT_TTL_SECONDS:
        conversation_memory.pop(_conversation_key(frame), None)
        return None
    return context


def _text_for_video_count(count: int) -> str:
    if count <= 0:
        return "我这边没匹配到对应视频，你把小区名或房号发我，我再找。"
    if count == 1:
        return "找到了，我把视频发你。"
    return f"找到了 {count} 条视频，我都发你。"


def _parse_price(value: str) -> int | None:
    digits = "".join(char for char in value if char.isdigit())
    if not digits:
        return None
    return int(digits)


def _text_for_room_prices(rooms: list[dict[str, Any]], lowest_only: bool = False) -> str:
    if not rooms:
        return ""
    lines = []
    for row in rooms[:6]:
        community = str(row.get("小区", "")).strip()
        room_no = str(row.get("房号", "")).strip()
        layout = str(row.get("户型", "")).strip()
        pay_one = str(row.get("押一付", "")).strip()
        pay_two = str(row.get("押二付", "")).strip()
        parts = [part for part in (community, room_no, layout) if part]
        if lowest_only:
            price_options = [
                ("押一付", pay_one, _parse_price(pay_one)),
                ("押二付", pay_two, _parse_price(pay_two)),
            ]
            valid_prices = [item for item in price_options if item[2] is not None]
            if valid_prices:
                label, value, _ = min(valid_prices, key=lambda item: item[2] or 0)
                price = f"最低{value}（{label}）"
            else:
                price = ""
        else:
            price = " / ".join(part for part in (f"押一付{pay_one}" if pay_one else "", f"押二付{pay_two}" if pay_two else "") if part)
        if price:
            parts.append(price)
        if parts:
            lines.append("，".join(parts))
    return "\n".join(lines)


def _text_for_room_passwords(rooms: list[dict[str, Any]]) -> str:
    if not rooms:
        return ""
    lines = []
    for row in rooms[:6]:
        community = str(row.get("小区", "")).strip()
        room_no = str(row.get("房号", "")).strip()
        password = str(row.get("密码", "")).strip()
        note = str(row.get("备注", "")).strip()
        if not password:
            continue
        parts = [part for part in (community, room_no, f"密码{password}") if part]
        if note and any(keyword in password for keyword in ("看房", "联系", "动态", "空出", "没锁")):
            parts.append(note)
        if parts:
            lines.append("，".join(parts))
    return "\n".join(lines)


def _query_from_rooms(rooms: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rooms[:6]:
        community = str(row.get("小区", "")).strip()
        room_no = str(row.get("房号", "")).strip()
        if community:
            parts.append(community)
        if room_no:
            parts.append(room_no)
    return " ".join(parts)


def _text_for_missing_video(rooms: list[dict[str, Any]]) -> str:
    if not rooms:
        return "我这边没匹配到对应视频，你把小区名或房号发我，我再找。"
    lines = ["这边查到房源了，但素材库里还没放对应视频。"]
    for row in rooms[:3]:
        community = str(row.get("小区", "")).strip()
        room_no = str(row.get("房号", "")).strip()
        layout = str(row.get("户型", "")).strip()
        price = str(row.get("押一付", "")).strip()
        note = str(row.get("密码", "")).strip()
        parts = [part for part in (community, room_no, layout, f"押一付{price}" if price else "", note) if part]
        if parts:
            lines.append("，".join(parts))
    return "\n".join(lines)


async def _build_reply(frame: dict[str, Any], msg_type: str) -> tuple[str, list[Path]]:
    text = _text_from_frame(frame)
    message = IncomingMessage(
        source="wecom_aibot",
        user_id=str(frame.get("chatid") or frame.get("from_userid") or ""),
        msg_type=msg_type,
        content=text,
        raw=frame,
    )
    rooms = await inventory.search(text)
    snapshot = inventory.format_rows(rooms) or await inventory.snapshot()
    media = media_store.list_for_rooms(rooms)
    images, videos = media_store.public_urls(media)
    matched_video_paths = media_store.list_room_database_videos(text)
    video_refs = videos + media_store.describe_paths(matched_video_paths)
    reply = await reply_generator.generate(message, snapshot, images, video_refs)
    return reply.text, matched_video_paths


async def _reply_inventory_image(ws_client: WSClient, frame: dict[str, Any]) -> None:
    image_paths = sorted(settings.room_database_path.parent.glob(settings.inventory_image_glob))
    if not image_paths and settings.inventory_image_path.exists():
        image_paths = [settings.inventory_image_path]
    for image_path in image_paths[:5]:
        if not image_path.exists():
            continue
        try:
            result = await ws_client.upload_media(
                image_path.read_bytes(),
                type="image",
                filename=image_path.name,
            )
            await ws_client.reply_media(frame, "image", result["media_id"])
        except Exception:
            logger.exception("房源表图片发送失败")


async def _reply_videos(
    ws_client: WSClient,
    frame: dict[str, Any],
    video_paths: list[Path],
    limit: int = 3,
) -> int:
    sent_count = 0
    for path in video_paths[:limit]:
        if not path.exists():
            continue
        try:
            _log_event("准备发送匹配视频", str(path))
            result = await ws_client.upload_media(
                path.read_bytes(),
                type="video",
                filename=path.name,
            )
            await ws_client.reply_media(frame, "video", result["media_id"])
            sent_count += 1
        except Exception:
            logger.exception("房源视频发送失败: %s", path)
    return sent_count


async def _handle_message(ws_client: WSClient, frame: dict[str, Any], msg_type: str) -> None:
    stream_id = generate_req_id("room")
    try:
        _log_event(f"收到消息 {msg_type}", frame)
        body_text = _text_from_frame(frame)
        context = _recent_context(frame)
        if context and _asks_password(body_text):
            direct_rooms = await inventory.search(body_text, limit=3)
            if _looks_like_followup(body_text) or not direct_rooms:
                rooms = list(context.get("rooms") or [])
                password_text = _text_for_room_passwords(rooms)
                if password_text:
                    await _reply_text(ws_client, frame, stream_id, password_text, True)
                    return

        if context and _asks_price_or_detail(body_text):
            direct_rooms = await inventory.search(body_text, limit=3)
            if _looks_like_followup(body_text) or not direct_rooms:
                rooms = list(context.get("rooms") or [])
                price_text = _text_for_room_prices(rooms, lowest_only="最低" in body_text)
                if price_text:
                    await _reply_text(ws_client, frame, stream_id, price_text, True)
                    return

        if settings.inventory_source == "local_image" and _wants_inventory_image(body_text) and not _wants_video(body_text):
            await _reply_text(ws_client, frame, stream_id, "房源表我发你，两张图都在下面。", True)
            await _reply_inventory_image(ws_client, frame)
            return

        if _wants_video(body_text):
            matched_video_paths = media_store.list_room_database_videos(body_text)
            matched_rooms = await inventory.search(body_text, limit=6)
            if not matched_video_paths and context:
                context_rooms = list(context.get("rooms") or [])
                context_query = _query_from_rooms(context_rooms)
                if context_query:
                    matched_video_paths = media_store.list_room_database_videos(context_query)
                    if matched_video_paths and not matched_rooms:
                        matched_rooms = context_rooms
            await _reply_text(
                ws_client,
                frame,
                stream_id,
                _text_for_video_count(len(matched_video_paths))
                if matched_video_paths
                else _text_for_missing_video(matched_rooms),
                True,
            )
            if matched_video_paths:
                await _reply_videos(ws_client, frame, matched_video_paths)
                _remember_context(frame, matched_rooms, matched_video_paths)
            else:
                _log_event("没有匹配到可发送视频", body_text)
                _remember_context(frame, matched_rooms)
            return

        await _reply_text(ws_client, frame, stream_id, "我查一下房源。", False)
        text, matched_video_paths = await _build_reply(frame, msg_type)
        content = text or settings.default_fallback_reply
        await _reply_text(ws_client, frame, stream_id, content, True)
        matched_rooms = await inventory.search(body_text, limit=6)
        _remember_context(frame, matched_rooms, matched_video_paths)
        if settings.inventory_source == "local_image" and _wants_inventory_image(body_text):
            await _reply_inventory_image(ws_client, frame)
        if _wants_video(body_text):
            sent_count = await _reply_videos(ws_client, frame, matched_video_paths)
            if sent_count == 0:
                _log_event("没有匹配到可发送视频", body_text)
    except Exception:
        logger.exception("长连接消息处理失败")
        await ws_client.reply_stream(
            frame,
            stream_id,
            settings.default_fallback_reply,
            True,
        )


async def main() -> None:
    if not settings.wecom_aibot_bot_id or not settings.wecom_aibot_secret:
        raise RuntimeError("缺少 WECOM_AIBOT_BOT_ID 或 WECOM_AIBOT_SECRET")

    settings.room_database_path.mkdir(parents=True, exist_ok=True)

    ws_client = WSClient(
        bot_id=settings.wecom_aibot_bot_id,
        secret=settings.wecom_aibot_secret,
        logger=DefaultLogger("RoomAiBot"),
    )

    ws_client.on("connected", lambda: _log_event("WebSocket 已连接"))
    ws_client.on("authenticated", lambda: _log_event("企业微信智能机器人长连接认证成功"))
    ws_client.on("disconnected", lambda reason: _log_event("WebSocket 已断开", reason))
    ws_client.on("reconnecting", lambda attempt: _log_event("WebSocket 正在重连", attempt))
    ws_client.on("error", lambda error: _log_event("WebSocket 错误", repr(error)))
    ws_client.on("message", lambda frame: _log_event("收到原始消息", frame))
    ws_client.on("message.text", lambda frame: _handle_message(ws_client, frame, "text"))
    ws_client.on("message.voice", lambda frame: _handle_message(ws_client, frame, "voice"))
    ws_client.on("message.mixed", lambda frame: _handle_message(ws_client, frame, "mixed"))
    ws_client.on("message.image", lambda frame: _handle_message(ws_client, frame, "image"))
    ws_client.on("message.file", lambda frame: _handle_message(ws_client, frame, "file"))

    async def on_enter(frame: dict[str, Any]) -> None:
        await ws_client.reply_welcome(
            frame,
            {
                "msgtype": "text",
                "text": {
                    "content": "您好，我是房源咨询助手。您可以直接问价格、房型、库存，或让我发送房源表和视频。"
                },
            },
        )

    ws_client.on("event.enter_chat", on_enter)

    _log_event("准备建立企业微信智能机器人长连接")
    await ws_client.connect()
    _log_event("长连接 connect 调用完成")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
