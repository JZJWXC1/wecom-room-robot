import json
import mimetypes
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.services import kf_context_memory
from app.services.fuzzy_match import COMMUNITY_DISPLAY_ALIASES
from app.services.wx_crypto import WeComCrypto


CUSTOMER_ORIGINS = {"3", 3, "customer", "external_user", "external_contact"}
KF_MESSAGE_EVENT = "kf_msg_or_event"
KF_ENTER_SESSION_EVENT = "enter_session"
SEND_MSG_COUNT_LIMIT_ERRCODE = 95001


class WeComKfSendLimitError(RuntimeError):
    pass


class WeComKfStateStore:
    def __init__(self, path: Path | None = None, max_msgids: int = 1000) -> None:
        self.path = path or settings.wecom_kf_state_path
        self.max_msgids = max_msgids

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"cursor": "", "processed_msgids": [], "welcome_sent_at": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"cursor": "", "processed_msgids": [], "welcome_sent_at": {}}
        welcome_sent_at: dict[str, float] = {}
        for key, value in (data.get("welcome_sent_at") or {}).items():
            if not key:
                continue
            try:
                welcome_sent_at[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return {
            "cursor": str(data.get("cursor", "")),
            "processed_msgids": list(data.get("processed_msgids") or []),
            "welcome_sent_at": welcome_sent_at,
        }

    def save_cursor(self, cursor: str) -> None:
        state = self.load()
        state["cursor"] = cursor
        self._write(state)

    def is_processed(self, msgid: str) -> bool:
        if not msgid:
            return False
        return msgid in set(self.load().get("processed_msgids") or [])

    def mark_processed(self, msgid: str) -> None:
        if not msgid:
            return
        state = self.load()
        msgids = [item for item in state.get("processed_msgids", []) if item != msgid]
        msgids.append(msgid)
        state["processed_msgids"] = msgids[-self.max_msgids :]
        self._write(state)

    def last_welcome_sent_at(self, conversation_key: str) -> float:
        if not conversation_key:
            return 0.0
        return float(self.load().get("welcome_sent_at", {}).get(conversation_key) or 0.0)

    def mark_welcome_sent(self, conversation_key: str, sent_at: float | None = None) -> None:
        if not conversation_key:
            return
        state = self.load()
        welcome_sent_at = dict(state.get("welcome_sent_at") or {})
        welcome_sent_at[conversation_key] = float(sent_at or time.time())
        state["welcome_sent_at"] = dict(
            sorted(
                welcome_sent_at.items(),
                key=lambda item: float(item[1] or 0),
            )[-self.max_msgids :]
        )
        self._write(state)

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


class WeComKfContextStore:
    def __init__(self, path: Path | None = None, max_contexts: int = 200) -> None:
        self.path = path or settings.wecom_kf_context_path
        self.max_contexts = max_contexts

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): self._normalize_context(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def get(self, key: str) -> dict[str, Any] | None:
        return self.load().get(key)

    def save(self, key: str, context: dict[str, Any]) -> None:
        contexts = self.load()
        contexts[key] = self._normalize_context(context)
        items = sorted(
            contexts.items(),
            key=lambda item: float(item[1].get("updated_at", 0)),
        )[-self.max_contexts :]
        self._write(dict(items))

    def delete(self, key: str) -> None:
        contexts = self.load()
        if key not in contexts:
            return
        contexts.pop(key, None)
        self._write(contexts)

    def _normalize_context(self, context: dict[str, Any]) -> dict[str, Any]:
        recent_messages = []
        for item in context.get("recent_messages") or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if not role or not content:
                continue
            recent_messages.append(
                {
                    "role": role,
                    "content": content,
                    "created_at": float(item.get("created_at") or time.time()),
                }
            )
        normalized = {
            "image_paths": [str(item) for item in context.get("image_paths") or [] if item],
            "video_paths": [str(item) for item in context.get("video_paths") or [] if item],
            "video_urls": [str(item) for item in context.get("video_urls") or [] if item],
            "recent_messages": recent_messages[-10:],
            "updated_at": float(context.get("updated_at") or time.time()),
        }
        last_candidate_set = kf_context_memory.normalize_last_candidate_set(
            context.get("last_candidate_set")
        )
        if last_candidate_set:
            normalized["last_candidate_set"] = last_candidate_set
        confirmed = self._normalize_confirmed_room(context.get("confirmed_room"))
        if confirmed:
            normalized["confirmed_room"] = confirmed
        pending_reference = self._normalize_reference_confirmation(
            context.get("pending_reference_confirmation")
        )
        if pending_reference:
            normalized["pending_reference_confirmation"] = pending_reference
        last_understanding = self._normalize_last_message_understanding(
            context.get("last_message_understanding")
        )
        if last_understanding:
            normalized["last_message_understanding"] = last_understanding
        active_binding = self._normalize_active_context_binding(
            context.get("active_context_binding")
        )
        if active_binding:
            normalized["active_context_binding"] = active_binding
        active_query_state = kf_context_memory.normalize_active_query_state(
            context.get("active_query_state")
        )
        if active_query_state:
            normalized["active_query_state"] = active_query_state
        pending_video_sends = self._normalize_pending_video_sends(
            context.get("pending_video_sends")
        )
        if pending_video_sends:
            normalized["pending_video_sends"] = pending_video_sends
        structured_memory = kf_context_memory.normalize_structured_memory(
            context.get("structured_memory")
        )
        if (
            structured_memory.get("raw_dialog_context")
            or structured_memory.get("turn_records")
            or structured_memory.get("current_turn_id")
        ):
            normalized["structured_memory"] = structured_memory
        return normalized

    def _normalize_confirmed_room(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        row = value.get("row")
        if not isinstance(row, dict):
            return {}
        label = str(value.get("label") or "").strip()
        return {
            "row": row,
            "label": label,
            "intent": str(value.get("intent") or "details"),
            "created_at": float(value.get("created_at") or time.time()),
            "inventory_cache_meta": dict(value.get("inventory_cache_meta") or {}),
        }

    def _normalize_reference_confirmation(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        status = str(value.get("status") or "").strip()
        kind = str(value.get("kind") or "").strip()
        raw_text = str(value.get("raw_text") or "").strip()
        if not status or not kind or not raw_text:
            return {}
        return {
            "status": status,
            "kind": kind,
            "raw_text": raw_text,
            "original_query": str(value.get("original_query") or "").strip(),
            "suggested_text": str(value.get("suggested_text") or "").strip(),
            "rewritten_query": str(value.get("rewritten_query") or "").strip(),
            "options": [str(item).strip() for item in value.get("options") or [] if str(item).strip()][:5],
            "confidence": str(value.get("confidence") or "medium"),
            "reason": str(value.get("reason") or ""),
            "created_at": float(value.get("created_at") or time.time()),
        }

    def _normalize_last_message_understanding(self, value: Any) -> dict[str, Any]:
        return kf_context_memory.normalize_last_message_understanding(value)

    def _normalize_active_context_binding(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        content = str(value.get("content") or "").strip()
        rows = [row for row in value.get("rows") or [] if isinstance(row, dict)]
        if not content and not rows:
            return {}
        return {
            "content": content,
            "selected_indices": [
                int(item)
                for item in value.get("selected_indices") or []
                if isinstance(item, int)
            ][:10],
            "rows": rows[:10],
            "created_at": float(value.get("created_at") or time.time()),
        }

    def _normalize_pending_video_sends(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        paths = [str(item) for item in value.get("paths") or [] if item]
        labels = [str(item).strip() for item in value.get("labels") or [] if str(item).strip()]
        if not paths and not labels:
            return {}
        return {
            "paths": paths[:10],
            "labels": labels[:10],
            "reason": str(value.get("reason") or "send_pending"),
            "created_at": float(value.get("created_at") or time.time()),
            "attempts": int(value.get("attempts") or 0),
            "requested_count": int(value.get("requested_count") or len(paths) or len(labels)),
            "sent_count": int(value.get("sent_count") or 0),
        }

    def _write(self, contexts: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(contexts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


class WeComKfClient:
    def __init__(self, state_store: WeComKfStateStore | None = None) -> None:
        self._token: str = ""
        self._token_expire_at: float = 0
        self.state_store = state_store or WeComKfStateStore()
        self.last_next_cursor: str = ""

    @property
    def crypto(self) -> WeComCrypto:
        return WeComCrypto(
            settings.wecom_kf_token or settings.wecom_token,
            settings.wecom_kf_aes_key or settings.wecom_aes_key,
            settings.wecom_corp_id,
        )

    def verify_url(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> str:
        self.crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
        return self.crypto.decrypt(echostr)

    def parse_callback_event(
        self, body: str, msg_signature: str, timestamp: str, nonce: str
    ) -> dict[str, str]:
        encrypted = self.crypto.extract_encrypt(body)
        self.crypto.verify_signature(msg_signature, timestamp, nonce, encrypted)
        xml_text = self.crypto.decrypt(encrypted)
        root = ET.fromstring(xml_text)
        return {child.tag: child.text or "" for child in root}

    async def sync_messages(self, open_kfid: str, token: str) -> list[dict[str, Any]]:
        state = self.state_store.load()
        cursor = str(state.get("cursor", ""))
        messages, next_cursor = await self._sync_message_pages(open_kfid, token, cursor)
        if not messages and token:
            messages, next_cursor = await self._sync_message_pages(open_kfid, "", cursor)
        self.last_next_cursor = next_cursor
        if next_cursor:
            self.state_store.save_cursor(next_cursor)

        return [
            message
            for message in messages
            if not self.state_store.is_processed(str(message.get("msgid", "")))
        ]

    async def _sync_message_pages(
        self,
        open_kfid: str,
        token: str,
        cursor: str,
    ) -> tuple[list[dict[str, Any]], str]:
        access_token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"
        messages: list[dict[str, Any]] = []
        next_cursor = cursor

        async with httpx.AsyncClient(timeout=40) as client:
            for _ in range(max(settings.wecom_kf_sync_max_pages, 1)):
                payload: dict[str, Any] = {
                    "token": token,
                    "open_kfid": open_kfid,
                    "limit": settings.wecom_kf_sync_limit,
                    "voice_format": 0,
                }
                if cursor:
                    payload["cursor"] = cursor

                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"微信客服消息拉取失败：{data}")

                messages.extend(data.get("msg_list") or [])
                next_cursor = str(data.get("next_cursor") or next_cursor)
                cursor = next_cursor
                if not data.get("has_more"):
                    break

        return messages, next_cursor

    async def send_text(
        self,
        open_kfid: str,
        external_userid: str,
        content: str,
    ) -> dict[str, Any]:
        access_token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}"
        payload = self.build_text_payload(open_kfid, external_userid, content)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode") != 0:
            if data.get("errcode") == SEND_MSG_COUNT_LIMIT_ERRCODE:
                raise WeComKfSendLimitError(f"微信客服消息发送次数受限：{data}")
            raise RuntimeError(f"微信客服消息发送失败：{data}")
        return data

    async def send_welcome_text_on_event(
        self,
        welcome_code: str,
        content: str,
    ) -> dict[str, Any]:
        access_token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg_on_event?access_token={access_token}"
        payload = self.build_event_text_payload(welcome_code, content)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode") != 0:
            if data.get("errcode") == SEND_MSG_COUNT_LIMIT_ERRCODE:
                raise WeComKfSendLimitError(f"微信客服欢迎语发送次数受限：{data}")
            raise RuntimeError(f"微信客服欢迎语发送失败：{data}")
        return data

    async def upload_media(self, path: Path, media_type: str = "image") -> str:
        access_token = await self._get_access_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
        params = {"access_token": access_token, "type": media_type}
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as file:
            files = {"media": (path.name, file, content_type)}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, params=params, files=files)
                response.raise_for_status()
                data = response.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"企业微信临时素材上传失败：{data}")
        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise RuntimeError(f"企业微信临时素材上传未返回 media_id：{data}")
        return media_id

    async def send_image(
        self,
        open_kfid: str,
        external_userid: str,
        image_path: Path,
    ) -> dict[str, Any]:
        media_id = await self.upload_media(image_path, "image")
        access_token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}"
        payload = self.build_image_payload(open_kfid, external_userid, media_id)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode") != 0:
            if data.get("errcode") == SEND_MSG_COUNT_LIMIT_ERRCODE:
                raise WeComKfSendLimitError(f"微信客服图片发送次数受限：{data}")
            raise RuntimeError(f"微信客服图片发送失败：{data}")
        return data

    async def send_video(
        self,
        open_kfid: str,
        external_userid: str,
        video_path: Path,
    ) -> dict[str, Any]:
        media_id = await self.upload_media(video_path, "video")
        return await self.send_video_media(open_kfid, external_userid, media_id)

    async def send_video_media(
        self,
        open_kfid: str,
        external_userid: str,
        media_id: str,
    ) -> dict[str, Any]:
        access_token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}"
        payload = self.build_video_payload(open_kfid, external_userid, media_id)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode") != 0:
            if data.get("errcode") == SEND_MSG_COUNT_LIMIT_ERRCODE:
                raise WeComKfSendLimitError(f"微信客服视频发送次数受限：{data}")
            raise RuntimeError(f"微信客服视频发送失败：{data}")
        return data

    def build_text_payload(
        self,
        open_kfid: str,
        external_userid: str,
        content: str,
    ) -> dict[str, Any]:
        normalized_content = self._normalize_outgoing_text(content)
        return {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": normalized_content},
        }

    def build_event_text_payload(
        self,
        welcome_code: str,
        content: str,
    ) -> dict[str, Any]:
        return {
            "code": welcome_code,
            "msgtype": "text",
            "text": {"content": self._normalize_outgoing_text(content)},
        }

    def _normalize_outgoing_text(self, content: str) -> str:
        text = str(content or "")
        for wrong, right in sorted(COMMUNITY_DISPLAY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(wrong, right)
        return text

    def build_image_payload(
        self,
        open_kfid: str,
        external_userid: str,
        media_id: str,
    ) -> dict[str, Any]:
        return {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "image",
            "image": {"media_id": media_id},
        }

    def build_video_payload(
        self,
        open_kfid: str,
        external_userid: str,
        media_id: str,
    ) -> dict[str, Any]:
        return {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "video",
            "video": {"media_id": media_id},
        }

    async def _get_access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at:
            return self._token
        secret = settings.wecom_kf_secret or settings.wecom_secret
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {"corpid": settings.wecom_corp_id, "corpsecret": secret}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企业微信 access_token 获取失败：{data}")
        self._token = data["access_token"]
        self._token_expire_at = time.time() + int(data.get("expires_in", 7200)) - 300
        return self._token


def is_kf_message_event(payload: dict[str, str]) -> bool:
    return payload.get("Event") == KF_MESSAGE_EVENT and bool(payload.get("Token"))


def kf_message_event_payload(message: dict[str, Any]) -> dict[str, Any]:
    event = message.get("event")
    if isinstance(event, dict):
        return event
    if isinstance(event, str):
        return {"event_type": event}
    return {}


def is_kf_enter_session_event(message: dict[str, Any]) -> bool:
    if message.get("msgtype") != "event":
        return False
    if extract_kf_welcome_code(message):
        return True
    event = kf_message_event_payload(message)
    event_type = str(
        event.get("event_type")
        or event.get("event")
        or message.get("event_type")
        or message.get("event")
        or ""
    )
    return event_type == KF_ENTER_SESSION_EVENT


def extract_kf_welcome_code(message: dict[str, Any]) -> str:
    event = kf_message_event_payload(message)
    return str(
        event.get("welcome_code")
        or event.get("WelcomeCode")
        or message.get("welcome_code")
        or message.get("WelcomeCode")
        or ""
    ).strip()


def extract_kf_open_kfid(message: dict[str, Any]) -> str:
    event = kf_message_event_payload(message)
    return str(
        message.get("open_kfid")
        or message.get("OpenKfId")
        or event.get("open_kfid")
        or event.get("OpenKfId")
        or ""
    ).strip()


def extract_kf_external_userid(message: dict[str, Any]) -> str:
    event = kf_message_event_payload(message)
    return str(
        message.get("external_userid")
        or message.get("ExternalUserID")
        or message.get("ExternalUserId")
        or event.get("external_userid")
        or event.get("ExternalUserID")
        or event.get("ExternalUserId")
        or ""
    ).strip()


def kf_callback_payload_event_message(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = str(
        payload.get("Event")
        or payload.get("event")
        or payload.get("event_type")
        or payload.get("EventType")
        or ""
    ).strip()
    welcome_code = str(payload.get("WelcomeCode") or payload.get("welcome_code") or "").strip()
    msgtype = str(payload.get("MsgType") or payload.get("msgtype") or "").strip()
    if not msgtype and (event_type or welcome_code):
        msgtype = "event"
    if msgtype.lower() != "event":
        return {}
    open_kfid = str(payload.get("OpenKfId") or payload.get("open_kfid") or "").strip()
    external_userid = str(
        payload.get("ExternalUserID")
        or payload.get("ExternalUserId")
        or payload.get("external_userid")
        or ""
    ).strip()
    return {
        "msgtype": "event",
        "open_kfid": open_kfid,
        "external_userid": external_userid,
        "event": {
            "event_type": event_type,
            "welcome_code": welcome_code,
            "open_kfid": open_kfid,
            "external_userid": external_userid,
        },
    }


def extract_kf_text(message: dict[str, Any]) -> str:
    if message.get("msgtype") != "text":
        return ""
    text = message.get("text") or {}
    return str(text.get("content") or "").strip()


def should_auto_reply_kf_message(message: dict[str, Any]) -> bool:
    if not message.get("open_kfid") or not message.get("external_userid"):
        return False
    if message.get("origin") not in CUSTOMER_ORIGINS:
        return False
    return bool(extract_kf_text(message))
