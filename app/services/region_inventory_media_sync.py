from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.feishu import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from app.services.region_inventory_constants import DRIVE_UPLOAD_SAFE_VIDEO_BYTES
from app.services.region_inventory_media import (
    brief_error,
    extract_docx_media_attachments,
    should_transcode_mov_upload_fallback,
    transcode_video_to_mp4,
)
from app.services.region_inventory_models import (
    ExistingMediaIndex,
    RegionInventoryRow,
    RegionSyncResult,
)
from app.services.region_inventory_utils import (
    drive_area_folder_name,
    folder_match_key,
    normalize_text,
    safe_name,
)


class RegionInventoryMediaSyncer:
    def __init__(
        self,
        *,
        client: Any,
        folder_cache: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.client = client
        self._folder_cache = folder_cache if folder_cache is not None else {}
        self.brief_error = brief_error
        self.extract_docx_media_attachments = extract_docx_media_attachments
        self.should_transcode_mov_upload_fallback = should_transcode_mov_upload_fallback
        self.transcode_video_to_mp4 = transcode_video_to_mp4

    async def sync_area_media(
        self,
        area_title: str,
        rows: list[RegionInventoryRow],
        result: RegionSyncResult,
    ) -> None:
        area_folder_token = await self.find_existing_area_folder(
            settings.feishu_region_sync_target_drive_folder_token,
            area_title,
        )
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            for row in rows:
                room_folder_token = await self.ensure_child_folder(area_folder_token, row.folder_name)
                existing_files = await self.client.list_folder_files(room_folder_token)
                existing = ExistingMediaIndex.from_drive_items(existing_files)
                for attachment in row.attachments:
                    await self.sync_attachment(
                        attachment,
                        room_folder_token,
                        existing,
                        work_dir,
                        row,
                        result,
                    )
                note_document_urls = {document.get("url", "") for document in row.note_documents}
                for document in row.note_documents:
                    await self.sync_note_document_media(
                        document,
                        room_folder_token,
                        existing,
                        work_dir,
                        row,
                        result,
                    )
                for link in row.note_links:
                    if link in note_document_urls:
                        continue
                    result.unsupported_note_links.append({"room": row.folder_name, "url": link})

    async def find_existing_area_folder(self, parent_token: str, area_title: str) -> str:
        folder_name = drive_area_folder_name(area_title)
        safe_folder_name = safe_name(folder_name)
        cache_key = (parent_token, f"area:{safe_folder_name}")
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        expected_norm = normalize_text(safe_folder_name)
        for item in await self.client.list_folder_files(parent_token):
            name = str(item.get("name") or item.get("title") or "")
            item_type = str(item.get("type") or "").lower()
            token = str(item.get("token") or item.get("file_token") or "")
            if item_type == "folder" and normalize_text(name) == expected_norm and token:
                self._folder_cache[cache_key] = token
                return token
        raise RuntimeError(f"目标云盘根目录缺少原有区域文件夹：{safe_folder_name}")

    async def ensure_child_folder(self, parent_token: str, folder_name: str) -> str:
        safe_folder_name = safe_name(folder_name)
        cache_key = (parent_token, folder_match_key(safe_folder_name))
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        expected_key = folder_match_key(safe_folder_name)
        for item in await self.client.list_folder_files(parent_token):
            name = str(item.get("name") or item.get("title") or "")
            item_type = str(item.get("type") or "").lower()
            token = str(item.get("token") or item.get("file_token") or "")
            if item_type == "folder" and folder_match_key(name) == expected_key and token:
                self._folder_cache[cache_key] = token
                return token
        created = await self.client.create_drive_folder(
            parent_folder_token=parent_token,
            name=safe_folder_name,
        )
        token = str(created.get("token") or created.get("file_token") or created.get("folder_token") or "")
        if not token:
            raise RuntimeError(f"Feishu folder token is empty: {created}")
        self._folder_cache[cache_key] = token
        return token

    async def sync_attachment(
        self,
        attachment: dict[str, Any],
        room_folder_token: str,
        existing: ExistingMediaIndex,
        work_dir: Path,
        row: RegionInventoryRow,
        result: RegionSyncResult,
    ) -> None:
        name = str(attachment.get("name") or attachment.get("file_name") or "").strip()
        file_token = str(attachment.get("file_token") or attachment.get("token") or attachment.get("fileKey") or "")
        download_url = str(attachment.get("url") or attachment.get("tmp_url") or attachment.get("download_url") or "")
        if not name or not file_token:
            result.media_failed.append({"room": row.folder_name, "file": name or "unnamed", "error": "missing token"})
            return
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
            result.media_skipped += 1
            return
        target_name = safe_name(name)
        converted_mp4_name = safe_name(f"{Path(target_name).stem}.mp4")
        if suffix == ".mov" and existing.has_name(converted_mp4_name):
            result.media_skipped += 1
            return
        try:
            local_path = work_dir / target_name
            await self.client.download_attachment(
                file_token=file_token,
                download_url=download_url,
                target_path=local_path,
            )
            local_size = local_path.stat().st_size
            if existing.has(target_name, local_size):
                result.media_skipped += 1
                return
            if suffix in VIDEO_EXTENSIONS and local_size > DRIVE_UPLOAD_SAFE_VIDEO_BYTES:
                await self.compress_and_upload_video(
                    local_path=local_path,
                    target_name=target_name,
                    suffix=suffix,
                    room_folder_token=room_folder_token,
                    existing=existing,
                    work_dir=work_dir,
                    result=result,
                )
                return
            await self.upload_media_file(
                room_folder_token=room_folder_token,
                file_path=local_path,
                file_name=target_name,
                existing=existing,
                result=result,
            )
        except Exception as exc:
            if self.should_transcode_mov_upload_fallback(suffix, exc):
                try:
                    await self.compress_and_upload_video(
                        local_path=local_path,
                        target_name=target_name,
                        suffix=suffix,
                        room_folder_token=room_folder_token,
                        existing=existing,
                        work_dir=work_dir,
                        result=result,
                    )
                    return
                except Exception as fallback_exc:
                    result.media_failed.append(
                        {
                            "room": row.folder_name,
                            "file": target_name,
                            "error": f"{self.brief_error(exc)}；转 mp4 后仍失败：{self.brief_error(fallback_exc)}",
                        }
                    )
                    return
            result.media_failed.append({"room": row.folder_name, "file": target_name, "error": str(exc)})

    async def compress_and_upload_video(
        self,
        *,
        local_path: Path,
        target_name: str,
        suffix: str,
        room_folder_token: str,
        existing: ExistingMediaIndex,
        work_dir: Path,
        result: RegionSyncResult,
    ) -> None:
        stem = Path(target_name).stem
        upload_name = safe_name(f"{stem}.mp4")
        if suffix.casefold() == ".mp4":
            upload_name = target_name
        mp4_path = work_dir / f"{stem}.compressed.mp4"
        mp4_path = self.transcode_video_to_mp4(local_path, mp4_path)
        mp4_size = mp4_path.stat().st_size
        if existing.has_name(upload_name) or existing.has(upload_name, mp4_size):
            result.media_skipped += 1
            return
        await self.upload_media_file(
            room_folder_token=room_folder_token,
            file_path=mp4_path,
            file_name=upload_name,
            existing=existing,
            result=result,
        )
        result.media_transcoded += 1

    async def upload_media_file(
        self,
        *,
        room_folder_token: str,
        file_path: Path,
        file_name: str,
        existing: ExistingMediaIndex,
        result: RegionSyncResult,
    ) -> None:
        await self.client.upload_drive_file(
            parent_folder_token=room_folder_token,
            file_path=file_path,
            file_name=file_name,
        )
        existing.add(file_name, file_path.stat().st_size)
        result.media_uploaded += 1

    async def sync_note_document_media(
        self,
        document: dict[str, str],
        room_folder_token: str,
        existing: ExistingMediaIndex,
        work_dir: Path,
        row: RegionInventoryRow,
        result: RegionSyncResult,
    ) -> None:
        document_id = document.get("token") or ""
        title = document.get("title") or row.folder_name
        url = document.get("url") or ""
        try:
            blocks = await self.client.list_docx_blocks(document_id=document_id)
        except Exception as exc:
            result.unsupported_note_links.append(
                {
                    "room": row.folder_name,
                    "url": url,
                    "reason": self.brief_error(exc),
                }
            )
            return
        attachments = self.extract_docx_media_attachments(blocks, title)
        if not attachments:
            result.unsupported_note_links.append(
                {
                    "room": row.folder_name,
                    "url": url,
                    "reason": "文档里没有识别到可同步的图片或视频",
                }
            )
            return
        for attachment in attachments:
            await self.sync_attachment(
                attachment,
                room_folder_token,
                existing,
                work_dir,
                row,
                result,
            )
