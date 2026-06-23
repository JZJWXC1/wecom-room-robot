from typing import Any

import httpx

from app.config import settings


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_SHEET_SYNC_COLUMNS = 50
MAX_SHEET_SYNC_ROWS = 1000
INVALID_ACCESS_TOKEN_CODES = {99991663}
DELETED_NOTE_ERROR_CODE = 1002


class FeishuApiError(RuntimeError):
    def __init__(self, data: dict[str, Any], *, status_code: int | None = None) -> None:
        self.data = data
        self.status_code = status_code
        self.code = data.get("code")
        self.message = str(data.get("msg") or data.get("message") or "")
        super().__init__(f"Feishu API failed: {data}")


def _is_invalid_access_token_response(data: dict[str, Any]) -> bool:
    code = data.get("code")
    if code in INVALID_ACCESS_TOKEN_CODES:
        return True
    message = str(data.get("msg") or data.get("message") or "").lower()
    return "invalid access token" in message


def is_deleted_note_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    if "note has been deleted" not in text:
        return False
    if isinstance(exc, FeishuApiError):
        return str(exc.code) == str(DELETED_NOTE_ERROR_CODE)
    return (
        "'code': 1002" in text
        or '"code": 1002' in text
        or "code=1002" in text
    )


class FeishuAuthMixin:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._tenant_access_token = ""
        self._transport = transport

    async def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        payload = {
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret,
        }
        data = await self._request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json=payload,
            auth=False,
        )
        token = str(
            data.get("tenant_access_token")
            or data.get("data", {}).get("tenant_access_token")
            or ""
        )
        if not token:
            raise RuntimeError("Feishu tenant_access_token is empty")
        self._tenant_access_token = token
        return token

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        base_headers = dict(kwargs.pop("headers", {}))
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=60,
            transport=self._transport,
        ) as client:
            for attempt in range(2):
                headers = dict(base_headers)
                if auth:
                    token = await self._get_tenant_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                response = await client.request(method, path, headers=headers, **kwargs)
                try:
                    data = response.json()
                except ValueError:
                    response.raise_for_status()
                    raise
                if auth and attempt == 0 and _is_invalid_access_token_response(data):
                    self._tenant_access_token = ""
                    continue
                if response.status_code >= 400:
                    raise FeishuApiError(data, status_code=response.status_code)
                if data.get("code", 0) != 0:
                    raise FeishuApiError(data, status_code=response.status_code)
                return data
        raise RuntimeError("Feishu API failed after token refresh retry")
