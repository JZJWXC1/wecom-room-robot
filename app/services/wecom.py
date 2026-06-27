import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.models import IncomingMessage
from app.services.kf_contracts import redact_sensitive_text, redact_sensitive_value
from app.services.wx_crypto import WeComCrypto


def _raise_for_status_sanitized(response: httpx.Response, label: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"{label} HTTP 失败：{redact_sensitive_text(str(exc))}") from None


def _safe_api_error(prefix: str, data: dict[str, Any]) -> RuntimeError:
    return RuntimeError(f"{prefix}：{redact_sensitive_value(data)}")


class WeComClient:
    def __init__(self) -> None:
        self._token: str = ""
        self._token_expire_at: float = 0

    @property
    def crypto(self) -> WeComCrypto:
        return WeComCrypto(
            settings.wecom_token,
            settings.wecom_aes_key,
            settings.wecom_corp_id,
        )

    def verify_url(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> str:
        self.crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
        return self.crypto.decrypt(echostr)

    def parse_callback(
        self, body: str, msg_signature: str, timestamp: str, nonce: str
    ) -> IncomingMessage:
        encrypted = self.crypto.extract_encrypt(body)
        self.crypto.verify_signature(msg_signature, timestamp, nonce, encrypted)
        xml_text = self.crypto.decrypt(encrypted)
        root = ET.fromstring(xml_text)
        payload = {child.tag: child.text or "" for child in root}
        return IncomingMessage(
            source="wecom",
            user_id=payload.get("FromUserName", ""),
            msg_type=payload.get("MsgType", ""),
            content=payload.get("Content", ""),
            media_id=payload.get("MediaId", ""),
            raw=payload,
        )

    async def send_text(self, user_id: str, content: str) -> dict[str, Any]:
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(settings.wecom_agent_id),
            "text": {"content": content},
            "safe": 0,
        }
        return await self._post_message(payload)

    async def send_image(self, user_id: str, media_id: str) -> dict[str, Any]:
        payload = {
            "touser": user_id,
            "msgtype": "image",
            "agentid": int(settings.wecom_agent_id),
            "image": {"media_id": media_id},
            "safe": 0,
        }
        return await self._post_message(payload)

    async def upload_temporary_media(self, media_type: str, path: Path) -> str:
        token = await self._get_access_token()
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
            f"?access_token={token}&type={media_type}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            with path.open("rb") as file_obj:
                response = await client.post(url, files={"media": file_obj})
            _raise_for_status_sanitized(response, "企业微信素材上传")
            data = response.json()
        if data.get("errcode") != 0:
            raise _safe_api_error("企业微信素材上传失败", data)
        return data["media_id"]

    async def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            _raise_for_status_sanitized(response, "企业微信消息发送")
            data = response.json()
        if data.get("errcode") != 0:
            raise _safe_api_error("企业微信消息发送失败", data)
        return data

    async def _get_access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at:
            return self._token
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {"corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params)
            _raise_for_status_sanitized(response, "企业微信 access_token 获取")
            data = response.json()
        if data.get("errcode") != 0:
            raise _safe_api_error("企业微信 access_token 获取失败", data)
        self._token = data["access_token"]
        self._token_expire_at = time.time() + int(data.get("expires_in", 7200)) - 300
        return self._token
