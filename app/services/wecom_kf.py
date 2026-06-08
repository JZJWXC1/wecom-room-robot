import json
import mimetypes
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.services.wx_crypto import WeComCrypto


CUSTOMER_ORIGINS = {"3", 3, "customer", "external_user", "external_contact"}
KF_MESSAGE_EVENT = "kf_msg_or_event"
SEND_MSG_COUNT_LIMIT_ERRCODE = 95001


class WeComKfSendLimitError(RuntimeError):
    pass


class WeComKfStateStore:
    def __init__(self, path: Path | None = None, max_msgids: int = 1000) -> None:
        self.path = path or settings.wecom_kf_state_path
        self.max_msgids = max_msgids

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"cursor": "", "processed_msgids": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"cursor": "", "processed_msgids": []}
        return {
            "cursor": str(data.get("cursor", "")),
            "processed_msgids": list(data.get("processed_msgids") or []),
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
        send_limited = context.get("send_limited")
        if isinstance(send_limited, dict):
            video_urls = [
                str(item)
                for item in send_limited.get("video_urls") or []
                if item
            ]
            normalized["send_limited"] = {
                "triggered_at": float(send_limited.get("triggered_at") or time.time()),
                "summary": str(send_limited.get("summary") or "")[:500],
                "video_urls": video_urls[:20],
            }
        return normalized

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
        return {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": content},
        }

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
