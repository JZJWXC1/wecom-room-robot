from pathlib import Path
import asyncio
import re
from typing import Any

import httpx

from app.config import settings
from app.services.feishu_base import (
    FEISHU_BASE_URL,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    is_deleted_note_error,
)
from app.services.region_inventory_utils import is_media_wrapper_folder


class FeishuDriveMixin:
    async def create_drive_folder(self, *, parent_folder_token: str, name: str) -> dict[str, Any]:
        if not parent_folder_token:
            raise ValueError("Feishu parent folder token is required")
        if not name.strip():
            raise ValueError("Feishu folder name is required")
        data = await self._request_json(
            "POST",
            "/drive/v1/files/create_folder",
            json={"folder_token": parent_folder_token, "name": name.strip()},
        )
        return dict(data.get("data") or {})

    async def upload_drive_file(
        self,
        *,
        parent_folder_token: str,
        file_path: Path,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        if not parent_folder_token:
            raise ValueError("Feishu parent folder token is required")
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        name = (file_name or file_path.name).strip()
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        data = {
            "file_name": name,
            "parent_type": "explorer",
            "parent_node": parent_folder_token,
            "size": str(file_path.stat().st_size),
        }
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=180,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            with file_path.open("rb") as file:
                response = await client.post(
                    "/drive/v1/files/upload_all",
                    headers=headers,
                    data=data,
                    files={"file": (name, file, "application/octet-stream")},
                )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            raise RuntimeError(f"Feishu upload failed: {payload}")
        return dict(payload.get("data") or payload)

    async def upload_file_to_folder(
        self,
        source_path: Path,
        *,
        folder_token: str,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        return await self.upload_drive_file(
            parent_folder_token=folder_token,
            file_path=source_path,
            file_name=file_name,
        )

    async def delete_file(self, file_token: str, *, file_type: str = "file") -> dict[str, Any]:
        if not file_token:
            raise ValueError("Feishu file token is required")
        return await self._request_json(
            "DELETE",
            f"/drive/v1/files/{file_token}",
            params={"type": file_type or "file"},
        )

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
        missing_notes: list[dict[str, str]] = []
        try:
            records = await self.list_bitable_records()
        except Exception as exc:
            if not is_deleted_note_error(exc):
                raise
            missing_notes.append(
                {
                    "room": "未知房源",
                    "reason": "源多维表格存在已删除的房源笔记，飞书未返回具体记录",
                }
            )
            return {
                "downloaded": downloaded,
                "skipped": skipped,
                "missing_notes": missing_notes,
            }
        for record in records:
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
        return {"downloaded": downloaded, "skipped": skipped, "missing_notes": missing_notes}

    async def sync_all_media(
        self,
        *,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        missing_notes: list[dict[str, str]] = []
        sources: dict[str, Any] = {}

        if settings.feishu_bitable_app_token and settings.feishu_bitable_table_id:
            bitable_result = await self.sync_bitable_media(target_root=target_root)
            downloaded.extend(bitable_result["downloaded"])
            skipped.extend(bitable_result["skipped"])
            missing_notes.extend(bitable_result.get("missing_notes") or [])
            sources["bitable"] = bitable_result

        drive_folder_token = await self.resolve_inventory_media_folder_token()
        if drive_folder_token:
            drive_result = await self.sync_drive_media(
                folder_token=drive_folder_token,
                target_root=target_root,
            )
            downloaded.extend(drive_result["downloaded"])
            skipped.extend(drive_result["skipped"])
            sources["drive"] = drive_result

        return {
            "downloaded": downloaded,
            "skipped": skipped,
            "missing_notes": missing_notes,
            "sources": sources,
        }

    async def sync_media_for_rooms(
        self,
        rows: list[dict[str, Any]],
        *,
        media_kind: str | None = None,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        target_root = target_root or settings.room_database_path
        room_rows = [row for row in rows if self._room_label(row)]
        downloaded: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        failed: list[dict[str, str]] = []
        missing_notes: list[dict[str, str]] = []
        sources: dict[str, Any] = {}

        if not room_rows:
            return {
                "downloaded": downloaded,
                "skipped": skipped,
                "missing": missing,
                "failed": failed,
                "missing_notes": missing_notes,
                "sources": sources,
            }

        if settings.feishu_bitable_app_token and settings.feishu_bitable_table_id:
            try:
                bitable_result = await self.sync_bitable_media_for_rooms(
                    room_rows,
                    media_kind=media_kind,
                    target_root=target_root,
                )
                downloaded.extend(bitable_result["downloaded"])
                skipped.extend(bitable_result["skipped"])
                missing.extend(bitable_result.get("missing") or [])
                failed.extend(bitable_result.get("failed") or [])
                missing_notes.extend(bitable_result.get("missing_notes") or [])
                sources["bitable"] = bitable_result
            except Exception as exc:
                failed.append({"source": "bitable", "reason": str(exc)})

        drive_folder_token = await self.resolve_inventory_media_folder_token()
        if drive_folder_token:
            try:
                drive_result = await self.sync_drive_media_for_rooms(
                    room_rows,
                    media_kind=media_kind,
                    folder_token=drive_folder_token,
                    target_root=target_root,
                )
                downloaded.extend(drive_result["downloaded"])
                skipped.extend(drive_result["skipped"])
                missing.extend(drive_result.get("missing") or [])
                failed.extend(drive_result.get("failed") or [])
                sources["drive"] = drive_result
            except Exception as exc:
                failed.append({"source": "drive", "reason": str(exc)})

        return {
            "downloaded": downloaded,
            "skipped": skipped,
            "missing": list(dict.fromkeys(missing)),
            "failed": failed,
            "missing_notes": missing_notes,
            "sources": sources,
        }

    async def resolve_inventory_media_folder_token(self) -> str:
        if settings.feishu_inventory_drive_folder_token:
            files = await self.list_folder_files(settings.feishu_inventory_drive_folder_token)
            folder_items = [
                item for item in files
                if str(item.get("type") or "").lower() == "folder"
            ]
            preferred = [
                item for item in folder_items
                if "房源素材" in str(item.get("name") or item.get("title") or "")
            ]
            selected = (preferred or folder_items[:1])
            if selected:
                return str(selected[0].get("token") or selected[0].get("file_token") or "").strip()
        return str(settings.feishu_drive_root_folder_token or "").strip()

    async def sync_bitable_media_for_rooms(
        self,
        rows: list[dict[str, Any]],
        *,
        media_kind: str | None = None,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        failed: list[dict[str, str]] = []
        missing_notes: list[dict[str, str]] = []
        matched_labels: set[str] = set()

        try:
            records = await self.list_bitable_records()
        except Exception as exc:
            if not is_deleted_note_error(exc):
                raise
            missing_notes.append(
                {
                    "room": "未知房源",
                    "reason": "源多维表格存在已删除的房源笔记，飞书未返回具体记录",
                }
            )
            return {
                "downloaded": downloaded,
                "skipped": skipped,
                "missing": [self._room_label(row) for row in rows],
                "failed": failed,
                "missing_notes": missing_notes,
            }

        for record in records:
            source_row = self._record_to_row(record)
            target_row = self._matching_room_row(source_row, rows)
            if target_row is None:
                continue
            label = self._room_label(target_row)
            matched_labels.add(label)
            folder_name = self._bitable_media_folder_name(source_row, record)
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
                media_dir = self._media_dir_for_name(name, media_kind=media_kind)
                if not name or not file_token or not media_dir:
                    skipped.append(name or file_token or "unnamed")
                    continue
                target_path = target_root.joinpath(
                    media_dir,
                    self._safe_path_part(folder_name),
                    self._safe_path_part(name),
                )
                if self._has_downloaded_file(target_path):
                    skipped.append(str(target_path))
                    continue
                try:
                    await self.download_attachment(
                        file_token=file_token,
                        download_url=download_url,
                        target_path=target_path,
                    )
                    downloaded.append(str(target_path))
                except Exception as exc:
                    failed.append({"room": label, "file": name, "reason": str(exc)})

        missing = [
            self._room_label(row)
            for row in rows
            if self._room_label(row) and self._room_label(row) not in matched_labels
        ]
        return {
            "downloaded": downloaded,
            "skipped": skipped,
            "missing": missing,
            "failed": failed,
            "missing_notes": missing_notes,
        }

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

    async def sync_drive_media_for_rooms(
        self,
        rows: list[dict[str, Any]],
        *,
        media_kind: str | None = None,
        folder_token: str | None = None,
        target_root: Path | None = None,
    ) -> dict[str, Any]:
        folder_token = folder_token or settings.feishu_drive_root_folder_token
        if not folder_token:
            raise ValueError("Feishu drive root folder token is required")
        target_root = target_root or settings.room_database_path
        downloaded: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        failed: list[dict[str, str]] = []
        targets = {
            self._normalize_room_label(self._room_label(row)): row
            for row in rows
            if self._room_label(row)
        }
        matched_targets: set[str] = set()

        async def sync_matching_folder(item: dict[str, Any], folder_parts: list[str]) -> bool:
            name = str(item.get("name") or item.get("title") or "").strip()
            item_type = str(item.get("type") or "").lower()
            token = str(item.get("token") or item.get("file_token") or "")
            normalized = self._normalize_room_label(name)
            if item_type != "folder" or not token or normalized not in targets:
                return False
            try:
                await self._sync_folder(
                    token,
                    target_root,
                    folder_parts + [self._safe_path_part(name)],
                    downloaded,
                    skipped,
                    media_kind=media_kind,
                )
                matched_targets.add(normalized)
            except Exception as exc:
                failed.append({"room": self._room_label(targets[normalized]), "reason": str(exc)})
            return True

        root_items = await self.list_folder_files(folder_token)
        area_folders: list[dict[str, Any]] = []
        for item in root_items:
            if await sync_matching_folder(item, []):
                continue
            if str(item.get("type") or "").lower() == "folder":
                area_folders.append(item)

        for area in area_folders:
            area_name = str(area.get("name") or area.get("title") or "").strip()
            area_token = str(area.get("token") or area.get("file_token") or "")
            if not area_token:
                continue
            try:
                for item in await self.list_folder_files(area_token):
                    await sync_matching_folder(item, [self._safe_path_part(area_name)])
            except Exception as exc:
                failed.append({"room": area_name, "reason": str(exc)})

        missing = [
            self._room_label(row)
            for normalized, row in targets.items()
            if normalized not in matched_targets
        ]
        return {"downloaded": downloaded, "skipped": skipped, "missing": missing, "failed": failed}

    async def _sync_folder(
        self,
        folder_token: str,
        target_root: Path,
        folder_parts: list[str],
        downloaded: list[str],
        skipped: list[str],
        *,
        media_kind: str | None = None,
    ) -> None:
        for item in await self.list_folder_files(folder_token):
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            item_type = str(item.get("type") or "").lower()
            token = str(item.get("token") or item.get("file_token") or "")
            if item_type == "folder":
                if token:
                    # 纯包装目录(如"房源素材")不进入镜像路径,避免云盘整体包一层时
                    # 服务器长出 video/房源素材/<区域>/ 双层树(2026-07-01 实证)。
                    child_parts = (
                        folder_parts
                        if is_media_wrapper_folder(name)
                        else folder_parts + [self._safe_path_part(name)]
                    )
                    await self._sync_folder(
                        token,
                        target_root,
                        child_parts,
                        downloaded,
                        skipped,
                        media_kind=media_kind,
                    )
                continue

            media_dir = self._media_dir_for_name(name, media_kind=media_kind)
            if not media_dir:
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

    def _media_dir_for_name(self, name: str, *, media_kind: str | None = None) -> str:
        suffix = Path(name).suffix.lower()
        kind = str(media_kind or "").strip().lower()
        wants_video = kind in {"", "video", "videos"}
        wants_image = kind in {"", "image", "images", "photo", "photos"}
        if suffix in VIDEO_EXTENSIONS and wants_video:
            return "video"
        if suffix in IMAGE_EXTENSIONS and wants_image:
            return "images"
        return ""

    def _bitable_media_folder_name(self, row: dict[str, Any], record: dict[str, Any]) -> str:
        room_parts = [
            row.get("小区") or row.get("社区") or row.get("楼盘") or "",
            row.get("房号") or row.get("房间号") or row.get("编号") or "",
        ]
        folder_name = "-".join(str(part).strip() for part in room_parts if str(part).strip()).strip()
        return folder_name or str(record.get("record_id") or "unnamed")

    def _matching_room_row(
        self,
        source_row: dict[str, Any],
        target_rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        source_label = self._normalize_room_label(self._room_label(source_row))
        if not source_label:
            return None
        for row in target_rows:
            if self._normalize_room_label(self._room_label(row)) == source_label:
                return row
        return None

    def _room_label(self, row: dict[str, Any]) -> str:
        community = str(row.get("小区") or row.get("社区") or row.get("楼盘") or "").strip()
        room_no = str(row.get("房号") or row.get("房间号") or row.get("门牌") or row.get("编号") or "").strip()
        return "".join(part for part in (community, room_no) if part)

    def _normalize_room_label(self, value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[\s_\-/（）()#号室幢栋单元]+", "", text)
        return text

    def _safe_path_part(self, value: str) -> str:
        cleaned = value.replace("\\", "_").replace("/", "_").strip()
        return cleaned or "unnamed"
