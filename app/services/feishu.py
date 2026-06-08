from pathlib import Path
import asyncio
from typing import Any

import httpx
import pandas as pd

from app.config import settings


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_SHEET_SYNC_COLUMNS = 50
MAX_SHEET_SYNC_ROWS = 1000
INVALID_ACCESS_TOKEN_CODES = {99991663}


def _is_invalid_access_token_response(data: dict[str, Any]) -> bool:
    code = data.get("code")
    if code in INVALID_ACCESS_TOKEN_CODES:
        return True
    message = str(data.get("msg") or data.get("message") or "").lower()
    return "invalid access token" in message


class FeishuClient:
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
                    raise RuntimeError(f"Feishu API failed: {data}")
                if data.get("code", 0) != 0:
                    raise RuntimeError(f"Feishu API failed: {data}")
                return data
        raise RuntimeError("Feishu API failed after token refresh retry")

    async def list_bitable_records(
        self,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        view_id: str | None = None,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        app_token = app_token or settings.feishu_bitable_app_token
        table_id = table_id or settings.feishu_bitable_table_id
        view_id = view_id if view_id is not None else settings.feishu_bitable_view_id
        if not app_token or not table_id:
            raise ValueError("Feishu bitable app token and table id are required")

        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            body: dict[str, Any] = {}
            if view_id:
                body["view_id"] = view_id
            data = await self._request_json(
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                params=params,
                json=body,
            )
            payload = data.get("data") or {}
            records.extend(payload.get("items") or payload.get("records") or [])
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return records

    async def read_bitable_dataframe(self) -> pd.DataFrame:
        records = await self.list_bitable_records()
        rows = [self._record_to_row(record) for record in records]
        return pd.DataFrame(rows)

    async def list_spreadsheet_sheets(
        self,
        *,
        spreadsheet_token: str | None = None,
    ) -> list[dict[str, Any]]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        data = await self._request_json(
            "GET",
            f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        )
        return list((data.get("data") or {}).get("sheets") or [])

    async def read_spreadsheet_values(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        grid = sheet.get("grid_properties") or {}
        row_count = max(1, min(int(grid.get("row_count") or 200), MAX_SHEET_SYNC_ROWS))
        column_count = max(1, min(int(grid.get("column_count") or 20), MAX_SHEET_SYNC_COLUMNS))
        end_column = self._column_letter(column_count)
        range_name = f"{selected_sheet_id}!A1:{end_column}{row_count}"
        data = await self._request_json(
            "GET",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_name}",
        )
        value_range = (data.get("data") or {}).get("valueRange") or {}
        values = value_range.get("values") or []
        return {
            "sheet_id": selected_sheet_id,
            "title": sheet.get("title") or "",
            "range": value_range.get("range") or range_name,
            "revision": value_range.get("revision") or (data.get("data") or {}).get("revision"),
            "values": [
                [self._format_field_value(cell) for cell in row]
                for row in values
            ],
        }

    def _select_sheet(
        self,
        sheets: list[dict[str, Any]],
        *,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        visible_sheets = [sheet for sheet in sheets if not sheet.get("hidden")]
        if sheet_id:
            for sheet in visible_sheets or sheets:
                if str(sheet.get("sheet_id") or "") == sheet_id:
                    return sheet
            raise ValueError(f"Feishu sheet id not found: {sheet_id}")
        if visible_sheets:
            return sorted(visible_sheets, key=lambda item: int(item.get("index") or 0))[0]
        if sheets:
            return sorted(sheets, key=lambda item: int(item.get("index") or 0))[0]
        raise RuntimeError("Feishu spreadsheet has no sheets")

    def _column_letter(self, column_number: int) -> str:
        letters = ""
        while column_number > 0:
            column_number, remainder = divmod(column_number - 1, 26)
            letters = chr(65 + remainder) + letters
        return letters or "A"

    def _record_to_row(self, record: dict[str, Any]) -> dict[str, str]:
        fields = record.get("fields") or {}
        return {
            str(key).strip(): self._format_field_value(value)
            for key, value in fields.items()
            if str(key).strip()
        }

    def _format_field_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value).strip()
        if isinstance(value, list):
            return " ".join(
                item for item in (self._format_field_value(item) for item in value) if item
            ).strip()
        if isinstance(value, dict):
            preferred_keys = (
                "text",
                "name",
                "link",
                "url",
                "email",
                "phone",
                "en_us",
                "zh_cn",
            )
            values = [
                self._format_field_value(value[key])
                for key in preferred_keys
                if key in value
            ]
            if values:
                return " ".join(item for item in values if item).strip()
            return " ".join(
                item for item in (self._format_field_value(item) for item in value.values()) if item
            ).strip()
        return str(value).strip()

    async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "folder_token": folder_token,
                "page_size": 200,
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._request_json("GET", "/drive/v1/files", params=params)
            payload = data.get("data") or {}
            files.extend(payload.get("files") or payload.get("items") or [])
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("next_page_token") or payload.get("page_token") or "")
            if not page_token:
                break
        return files

    async def download_file(self, file_token: str, target_path: Path) -> Path:
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=120,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            await self._stream_download(
                client,
                f"/drive/v1/files/{file_token}/download",
                target_path,
                headers,
            )
        return target_path

    async def export_sheet_xlsx(
        self,
        *,
        sheet_token: str | None = None,
        target_path: Path,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
    ) -> Path:
        sheet_token = sheet_token or settings.feishu_inventory_sheet_token
        if not sheet_token:
            raise ValueError("Feishu inventory sheet token is required")

        ticket = await self.create_export_task(
            file_token=sheet_token,
            file_type="sheet",
            file_extension="xlsx",
        )
        file_token = await self.wait_export_task(
            ticket=ticket,
            source_token=sheet_token,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        return await self.download_exported_file(file_token, target_path)

    async def export_sheet_csv(
        self,
        *,
        sheet_token: str | None = None,
        target_path: Path,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
    ) -> Path:
        sheet_token = sheet_token or settings.feishu_inventory_sheet_token
        if not sheet_token:
            raise ValueError("Feishu inventory sheet token is required")

        ticket = await self.create_export_task(
            file_token=sheet_token,
            file_type="sheet",
            file_extension="csv",
        )
        file_token = await self.wait_export_task(
            ticket=ticket,
            source_token=sheet_token,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        return await self.download_exported_file(file_token, target_path)

    async def create_export_task(
        self,
        *,
        file_token: str,
        file_type: str,
        file_extension: str,
    ) -> str:
        data = await self._request_json(
            "POST",
            "/drive/v1/export_tasks",
            json={
                "token": file_token,
                "type": file_type,
                "file_extension": file_extension,
            },
        )
        ticket = str((data.get("data") or {}).get("ticket") or "")
        if not ticket:
            raise RuntimeError(f"Feishu export task ticket is empty: {data}")
        return ticket

    async def wait_export_task(
        self,
        *,
        ticket: str,
        source_token: str,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_response: dict[str, Any] = {}
        while True:
            data = await self._request_json(
                "GET",
                f"/drive/v1/export_tasks/{ticket}",
                params={"token": source_token},
            )
            last_response = data
            payload = data.get("data") or {}
            result = payload.get("result") or payload
            job_status = str(
                result.get("job_status")
                or result.get("status")
                or payload.get("job_status")
                or ""
            ).lower()
            file_token = str(
                result.get("file_token")
                or result.get("token")
                or payload.get("file_token")
                or ""
            )
            if file_token and job_status not in {"failed", "fail", "error"}:
                return file_token
            if job_status in {"failed", "fail", "error"}:
                raise RuntimeError(f"Feishu export task failed: {data}")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Feishu export task timed out: {last_response}")
            await asyncio.sleep(poll_interval_seconds)

    async def download_exported_file(self, file_token: str, target_path: Path) -> Path:
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=120,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            await self._stream_download(
                client,
                f"/drive/v1/export_tasks/file/{file_token}/download",
                target_path,
                headers,
            )
        return target_path

    async def download_attachment(
        self,
        *,
        file_token: str,
        target_path: Path,
        download_url: str = "",
    ) -> Path:
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=120,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            if download_url:
                await self._stream_download(client, download_url, target_path, headers)
            else:
                downloaded = await self._stream_download(
                    client,
                    f"/drive/v1/medias/{file_token}/download",
                    target_path,
                    headers,
                    ignored_statuses={404},
                )
                if not downloaded:
                    await self._stream_download(
                        client,
                        f"/drive/v1/files/{file_token}/download",
                        target_path,
                        headers,
                    )
        return target_path

    async def _stream_download(
        self,
        client: httpx.AsyncClient,
        url: str,
        target_path: Path,
        headers: dict[str, str],
        ignored_statuses: set[int] | None = None,
    ) -> bool:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f"{target_path.name}.part")
        async with client.stream("GET", url, headers=headers) as response:
            if ignored_statuses and response.status_code in ignored_statuses:
                return False
            response.raise_for_status()
            with temp_path.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        output.write(chunk)
        temp_path.replace(target_path)
        return True

    async def sync_bitable_media(
        self,
        *,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        for record in await self.list_bitable_records():
            row = self._record_to_row(record)
            room_parts = [
                row.get("小区") or row.get("社区") or row.get("楼盘") or "",
                row.get("房号") or row.get("房间号") or row.get("编号") or "",
            ]
            folder_name = "-".join(part for part in room_parts if part).strip()
            if not folder_name:
                folder_name = str(record.get("record_id") or "unnamed")
            for attachment in self._extract_attachments(record):
                name = str(attachment.get("name") or attachment.get("file_name") or "").strip()
                file_token = str(
                    attachment.get("file_token")
                    or attachment.get("token")
                    or attachment.get("fileKey")
                    or ""
                )
                download_url = str(
                    attachment.get("url")
                    or attachment.get("tmp_url")
                    or attachment.get("download_url")
                    or ""
                )
                if not name or not file_token:
                    skipped.append(name or file_token or "unnamed")
                    continue
                suffix = Path(name).suffix.lower()
                if suffix in VIDEO_EXTENSIONS:
                    media_dir = "video"
                elif suffix in IMAGE_EXTENSIONS:
                    media_dir = "images"
                else:
                    skipped.append(name)
                    continue
                target_path = target_root.joinpath(
                    media_dir,
                    self._safe_path_part(folder_name),
                    self._safe_path_part(name),
                )
                if self._has_downloaded_file(target_path):
                    skipped.append(str(target_path))
                    continue
                await self.download_attachment(
                    file_token=file_token,
                    download_url=download_url,
                    target_path=target_path,
                )
                downloaded.append(str(target_path))
        return {"downloaded": downloaded, "skipped": skipped}

    async def sync_all_media(
        self,
        *,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        sources: dict[str, Any] = {}

        if settings.feishu_bitable_app_token and settings.feishu_bitable_table_id:
            bitable_result = await self.sync_bitable_media(target_root=target_root)
            downloaded.extend(bitable_result["downloaded"])
            skipped.extend(bitable_result["skipped"])
            sources["bitable"] = bitable_result

        if settings.feishu_drive_root_folder_token:
            drive_result = await self.sync_drive_media(target_root=target_root)
            downloaded.extend(drive_result["downloaded"])
            skipped.extend(drive_result["skipped"])
            sources["drive"] = drive_result

        return {"downloaded": downloaded, "skipped": skipped, "sources": sources}

    async def sync_drive_media(
        self,
        *,
        folder_token: str | None = None,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        folder_token = folder_token or settings.feishu_drive_root_folder_token
        if not folder_token:
            raise ValueError("Feishu drive root folder token is required")
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        await self._sync_folder(folder_token, target_root, [], downloaded, skipped)
        return {"downloaded": downloaded, "skipped": skipped}

    async def _sync_folder(
        self,
        folder_token: str,
        target_root: Path,
        folder_parts: list[str],
        downloaded: list[str],
        skipped: list[str],
    ) -> None:
        for item in await self.list_folder_files(folder_token):
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            item_type = str(item.get("type") or "").lower()
            token = str(item.get("token") or item.get("file_token") or "")
            if item_type == "folder":
                if token:
                    await self._sync_folder(
                        token,
                        target_root,
                        folder_parts + [self._safe_path_part(name)],
                        downloaded,
                        skipped,
                    )
                continue

            suffix = Path(name).suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                media_dir = "video"
            elif suffix in IMAGE_EXTENSIONS:
                media_dir = "images"
            else:
                skipped.append(name)
                continue
            if not token:
                skipped.append(name)
                continue
            relative_parts = [media_dir, *folder_parts, self._safe_path_part(name)]
            target_path = target_root.joinpath(*relative_parts)
            if self._has_downloaded_file(target_path):
                skipped.append(str(target_path))
                continue
            await self.download_file(token, target_path)
            downloaded.append(str(target_path))

    def _has_downloaded_file(self, path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    def _extract_attachments(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for value in (record.get("fields") or {}).values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and (
                        item.get("file_token") or item.get("token") or item.get("fileKey")
                    ):
                        attachments.append(item)
            elif isinstance(value, dict) and (
                value.get("file_token") or value.get("token") or value.get("fileKey")
            ):
                attachments.append(value)
        return attachments

    def _safe_path_part(self, value: str) -> str:
        cleaned = value.replace("\\", "_").replace("/", "_").strip()
        return cleaned or "unnamed"
